"""Password hashing, recoverable shared credentials and revocable sessions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Response
from .db import connect
from .settings import Settings

COOKIE_NAME = "teleport_session"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    # OWASP's N=2^15,r=8,p=3 profile balances VPS memory use and guessing cost.
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3, maxmem=64 * 1024 * 1024)
    return "scrypt$32768$8$3$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt" or int(n) > 131072 or int(r) > 16 or int(p) > 10:
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=256 * 1024 * 1024)
        return hmac.compare_digest(digest, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError, OverflowError):
        return False


def encrypt_password(password: str, settings: Settings) -> str | None:
    return Fernet(settings.secret_key).encrypt(password.encode()).decode() if password else None


def decrypt_password(ciphertext: str | None, settings: Settings) -> str:
    if not ciphertext:
        return ""
    try:
        return Fernet(settings.secret_key).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(503, "Chiave di cifratura non valida: ripristina la chiave originale.") from exc


@dataclass
class Session:
    token_hash: str
    csrf_token: str
    admin: bool
    unlocked: list[str]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def find_session(request: Request, settings: Settings) -> Session | None:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        return None
    with connect(settings) as connection:
        row = connection.execute("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (_hash_token(token), time.time())).fetchone()
    if not row:
        return None
    return Session(row["token_hash"], row["csrf_token"], bool(row["admin"]), json.loads(row["unlocked"]))


def new_session(response: Response, settings: Settings, admin: bool = False, unlocked: list[str] | None = None) -> Session:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    session = Session(_hash_token(token), csrf, admin, unlocked or [])
    with connect(settings) as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
        connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (session.token_hash, csrf, int(admin), json.dumps(session.unlocked), time.time() + settings.session_hours * 3600))
    response.set_cookie(COOKIE_NAME, token, max_age=settings.session_hours * 3600, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
    return session


def revoke_session(session: Session | None, settings: Settings) -> None:
    if session:
        with connect(settings) as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (session.token_hash,))


def require_session(request: Request, settings: Settings) -> Session:
    session = find_session(request, settings)
    if session is None:
        raise HTTPException(401, "Sessione scaduta. Ricarica la pagina e accedi di nuovo.")
    return session


def require_admin(request: Request, settings: Settings) -> Session:
    session = require_session(request, settings)
    if not session.admin:
        raise HTTPException(403, "Accedi come amministratore.")
    return session


def require_csrf(request: Request, settings: Settings) -> Session:
    session = require_session(request, settings)
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Richiesta da un'origine non autorizzata.")
    if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session.csrf_token.encode()):
        raise HTTPException(403, "Sessione non valida. Ricarica la pagina e riprova.")
    return session


def check_rate_limit(key: str, settings: Settings) -> float:
    now = time.time()
    with connect(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM login_attempts WHERE at<?", (now - 900,))
        count = connection.execute("SELECT COUNT(*) FROM login_attempts WHERE key=?", (key,)).fetchone()[0]
        if count >= 5:
            raise HTTPException(429, "Troppi tentativi. Riprova tra 15 minuti.")
        connection.execute("INSERT INTO login_attempts(key,at) VALUES (?,?)", (key, now))
    return now


def clear_rate_limit(key: str, settings: Settings) -> None:
    with connect(settings) as connection:
        connection.execute("DELETE FROM login_attempts WHERE key=?", (key,))

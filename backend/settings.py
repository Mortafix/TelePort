"""Configuration. Secret values never belong in API responses or logs."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("TELEPORT_DATA_DIR", "data")))
    database_path: Path | None = None
    timezone: str = "Europe/Rome"
    demo: bool = False
    secret_key: bytes = b""
    admin_password_hash: str = ""
    cookie_secure: bool = False
    session_hours: int = 12
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    telegram_phone: str = ""
    telegram_session_path: Path | None = None
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    run_worker: bool = True
    analysis_limit: int = 100
    moment_gap_minutes: int = 30

    def __post_init__(self) -> None:
        if not 5 <= self.moment_gap_minutes <= 240:
            raise ValueError('TELEPORT_MOMENT_GAP_MINUTES deve essere tra 5 e 240.')
        self.data_dir = Path(self.data_dir).resolve()
        self.database_path = Path(self.database_path or self.data_dir / "teleport.sqlite3").resolve()
        self.telegram_session_path = Path(self.telegram_session_path or self.data_dir / "telegram").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.secret_key:
            key_path = self.data_dir / "encryption.key"
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                self.secret_key = key_path.read_bytes().strip()
            else:
                self.secret_key = Fernet.generate_key()
                with os.fdopen(fd, "wb") as stream:
                    stream.write(self.secret_key)
        if isinstance(self.secret_key, str):
            self.secret_key = self.secret_key.encode()
        Fernet(self.secret_key)  # Fail clearly instead of silently replacing an invalid key.
        from zoneinfo import ZoneInfo
        ZoneInfo(self.timezone)

    @property
    def telegram_session(self) -> Path:
        return self.telegram_session_path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    from dotenv import load_dotenv
    load_dotenv()
    from .auth import hash_password
    password_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
    demo = _bool("TELEPORT_DEMO")
    password = os.getenv("ADMIN_PASSWORD", "")
    if demo and not password and not password_hash:
        password = "teleport-demo"
    if password and not password_hash:
        password_hash = hash_password(password)
    return Settings(
        data_dir=Path(os.getenv("TELEPORT_DATA_DIR", "data")),
        database_path=Path(os.environ["TELEPORT_DATABASE_PATH"]) if os.getenv("TELEPORT_DATABASE_PATH") else None,
        timezone=os.getenv("TELEPORT_TIMEZONE", "Europe/Rome"),
        demo=demo,
        secret_key=os.getenv("TELEPORT_SECRET_KEY", "").encode(),
        admin_password_hash=password_hash,
        cookie_secure=_bool("TELEPORT_COOKIE_SECURE"),
        session_hours=int(os.getenv("TELEPORT_SESSION_HOURS", "12")),
        telegram_api_id=os.getenv("TELEGRAM_API_ID", os.getenv("API_ID", "")),
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH", os.getenv("API_HASH", "")),
        telegram_phone=os.getenv("TELEGRAM_PHONE", os.getenv("PHONE_NUMBER", "")),
        telegram_session_path=Path(os.environ["TELEGRAM_SESSION_PATH"]) if os.getenv("TELEGRAM_SESSION_PATH") else None,
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        run_worker=_bool("TELEPORT_RUN_WORKER", True),
        analysis_limit=int(os.getenv("TELEPORT_ANALYSIS_LIMIT", "100")),
        moment_gap_minutes=int(os.getenv("TELEPORT_MOMENT_GAP_MINUTES", "30")),
    )

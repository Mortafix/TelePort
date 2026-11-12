"""FastAPI application, protected chat access and the compiled React application."""
from __future__ import annotations

import asyncio
import hmac
import json
import re
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import auth
from .db import connect, initialize
from .settings import Settings, get_settings
from .reports import cached_report, prepare_report, cache_report


class PasswordInput(BaseModel):
    password: str = Field(max_length=2048)


class JobInput(BaseModel):
    chat_id: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class PhoneInput(BaseModel):
    phone: str = Field(min_length=5, max_length=32)


class VerificationInput(BaseModel):
    code: str = Field(default="", max_length=20)
    password: str | None = Field(default=None, max_length=2048)


class ImportInput(BaseModel):
    ids: list[str | int] = Field(max_length=100)


def _date_bounds(start: str | None, end: str | None, settings: Settings) -> tuple[str | None, str | None]:
    zone = ZoneInfo(settings.timezone)
    try:
        start_day = date.fromisoformat(start) if start else None
        end_day = date.fromisoformat(end) if end else None
        if start_day and end_day and start_day > end_day:
            raise ValueError("date order")
        lo = datetime.combine(start_day, time.min, zone).astimezone(timezone.utc).isoformat() if start_day else None
        hi = datetime.combine(end_day + timedelta(days=1), time.min, zone).astimezone(timezone.utc).isoformat() if end_day else None
        return lo, hi
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "Intervallo non valido: usa date nel formato AAAA-MM-GG.") from exc


def _where_dates(column: str, lo: str | None, hi: str | None, params: list) -> str:
    result = ""
    if lo:
        result += f" AND {column}>=?"
        params.append(lo)
    if hi:
        result += f" AND {column}<?"
        params.append(hi)
    return result


def _public_moment(row) -> dict:
    moment = dict(row)
    for key, fallback in [("message_ids", []), ("analysis", None)]:
        value = moment.get(key)
        if isinstance(value, str):
            try:
                moment[key] = json.loads(value)
            except json.JSONDecodeError:
                moment[key] = fallback
    moment["message_count"] = len(moment["message_ids"]) if "message_ids" in moment else int(moment.get("message_count") or 0)
    moment["duration_minutes"] = round((datetime.fromisoformat(moment["end"]) - datetime.fromisoformat(moment["start"])).total_seconds() / 60, 2)
    moment["status"] = "analyzed" if moment.get("analysis") else "pending"
    return moment


def _seed_demo(settings: Settings) -> None:
    """Only explicit demo mode creates fictional records; never reads local chats."""
    from .analytics import rebuild_moments
    now = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0)
    chats = [("demo-giulia", "Giulia", "giulia_demo", 2), ("demo-marco", "Marco", "marco_demo", 5), ("demo-amici", "Gruppo amici", "amici_demo", 3)]
    texts = [
        "Ci vediamo domani per un caffè?", "Volentieri, dopo le cinque sono libero.",
        "Perfetto, allora ci aggiorniamo domani.", "Grazie! Mi fa davvero piacere.",
        "Ho finito il progetto, finalmente!", "Bella notizia, complimenti!",
        "Per sabato pensavo a una passeggiata al parco.", "Va bene, speriamo ci sia il sole.",
        "Oggi è stata una giornata piuttosto complicata.", "Mi dispiace, se vuoi ne parliamo più tardi.",
        "Ho cambiato programma, arrivo verso le otto.", "Grazie per avermi avvisato, ci organizziamo.",
        "Ti mando una foto del percorso.", "Che bel posto! Organizziamo un giro 😊",
    ]
    with connect(settings) as connection:
        for chat_id, title, username, step in chats:
            if connection.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
                continue
            connection.execute("INSERT INTO chats(id,title,username,last_sync) VALUES (?,?,?,?)", (chat_id, title, username, now.isoformat()))
            message_id = 0
            for day_offset in range(62, -1, -1):
                if day_offset % step == 1:
                    continue
                for block, hour in enumerate([0, 5, 13]):
                    for index in range(5 + ((day_offset + block) % 9)):
                        message_id += 1
                        sender_id = "me" if index % 2 == 0 else chat_id
                        sender_name = "Tu" if index % 2 == 0 else title
                        when = now - timedelta(days=day_offset) + timedelta(hours=hour, minutes=index * 2)
                        message_type = "photo" if message_id % 31 == 0 else ("audio" if message_id % 47 == 0 else "text")
                        text = "" if message_type != "text" else texts[(day_offset + index + block) % len(texts)]
                        connection.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date,reply_to_id) VALUES (?,?,?,?,?,?,?,?)", (chat_id, message_id, sender_id, sender_name, message_type, text, when.isoformat(), message_id - 1 if index else None))
            connection.execute("UPDATE chats SET version=1,last_message_id=? WHERE id=?", (message_id, chat_id))
    for chat_id, *_ in chats:
        rebuild_moments(chat_id, settings)
    # Explicit demo samples are labelled; they never trigger API requests or imply measured accuracy.
    with connect(settings) as connection:
        for index, row in enumerate(connection.execute("SELECT id FROM moments WHERE chat_id LIKE 'demo-%' ORDER BY start")):
            label = ["positive", "neutral", "mixed", "positive", "uncertain", "negative"][index % 6]
            analysis = {"label": label, "summary": "Organizzazione di appuntamenti e aggiornamenti sulla giornata.", "context": "Organizzazione di appuntamenti e aggiornamenti sulla giornata.", "emotions": ["joy"] if label == "positive" else [], "participants": [], "evidence_message_ids": [], "prompt_version": "demo", "demo": True}
            connection.execute("UPDATE moments SET analysis=?,model='demo',analyzed_at=? WHERE id=?", (json.dumps(analysis), now.isoformat(), row["id"]))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize(settings)
        if settings.demo:
            _seed_demo(settings)
        from .sync import TelegramService
        service = TelegramService(settings)
        app.state.telegram = service
        task = None
        if settings.run_worker:
            await service.start()
            from .jobs import worker_loop
            task = asyncio.create_task(worker_loop(service, settings=settings))
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await service.close()

    app = FastAPI(title="TelePort", version="2.0.0", lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        # Default validation responses echo input, potentially including passwords or 2FA codes.
        return JSONResponse({"detail": "Dati non validi. Controlla i campi e riprova."}, status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        elif request.url.path in {"/docs", "/redoc", "/openapi.json"} or response.status_code >= 400:
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    def chat_access(chat_id: str, request: Request, admin_only: bool = False):
        session = auth.require_admin(request, settings) if admin_only else auth.find_session(request, settings)
        if not admin_only and not (session and (session.admin or chat_id in session.unlocked)):
            raise HTTPException(403, "Accedi alla conversazione con la sua password.")
        with connect(settings) as connection:
            chat = connection.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if chat is None or chat["archived"]:
            raise HTTPException(404, "Conversazione non trovata.")
        if not admin_only and not (session and (session.admin or chat_id in session.unlocked)):
            raise HTTPException(403, "Sblocca questa conversazione con la sua password.")
        return chat

    def admin_mutation(request: Request):
        auth.require_csrf(request, settings)
        return auth.require_admin(request, settings)

    def ip_key(request: Request, kind: str) -> str:
        return kind + ":" + (request.client.host if request.client else "unknown")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "2.0.0"}

    def session_payload(session):
        return {"admin": session.admin, "unlocked_chat_ids": session.unlocked, "csrf_token": session.csrf_token, "configured": bool(settings.admin_password_hash), "demo": settings.demo, "timezone": settings.timezone}

    @app.get("/api/session")
    def session_info(request: Request, response: Response):
        session = auth.find_session(request, settings) or auth.new_session(response, settings)
        return session_payload(session)

    @app.post("/api/admin/login")
    def admin_login(payload: PasswordInput, request: Request, response: Response):
        old_session = auth.require_csrf(request, settings)
        if not settings.admin_password_hash:
            raise HTTPException(503, "Configura ADMIN_PASSWORD_HASH sul server per abilitare l'accesso admin.")
        key = ip_key(request, "admin")
        auth.check_rate_limit(key, settings)
        if not auth.verify_password(payload.password, settings.admin_password_hash):
            raise HTTPException(401, "Password non corretta.")
        auth.clear_rate_limit(key, settings)
        auth.revoke_session(old_session, settings)
        session = auth.new_session(response, settings, admin=True)
        return session_payload(session)

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        session = auth.require_csrf(request, settings)
        auth.revoke_session(session, settings)
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return session_payload(auth.new_session(response, settings))

    @app.get("/api/chats")
    def chats(request: Request):
        session = auth.find_session(request, settings)
        if not session or (not session.admin and not session.unlocked):
            return {"items": []}
        scope=""; params=[]
        if not session.admin:
            scope=" AND c.id IN ("+",".join("?" for _ in session.unlocked)+")"
            params=session.unlocked
        with connect(settings) as connection:
            rows = connection.execute("SELECT c.*,MIN(m.date) AS first_message,MAX(m.date) AS last_message,COUNT(m.id) AS message_count,GROUP_CONCAT(DISTINCT substr(m.date,1,4)) AS years FROM chats c LEFT JOIN messages m ON m.chat_id=c.id AND m.deleted=0 WHERE c.archived=0"+scope+" GROUP BY c.id ORDER BY c.title COLLATE NOCASE",params).fetchall()
        return {"items": [{"id": row["id"], "title": row["title"], "username": row["username"], "locked": bool(row["password_encrypted"]), "accessible": not row["password_encrypted"] or bool(session and (session.admin or row["id"] in session.unlocked)), "message_count": row["message_count"], "first_message": row["first_message"], "last_message": row["last_message"], "last_sync": row["last_sync"], "gap_minutes": row["gap_minutes"], "appearance": json.loads(row["appearance"]), "years": sorted((row["years"] or "").split(",")) if row["years"] else []} for row in rows]}

    def unlock_password(payload, request, chat_id=None):
        session=auth.require_csrf(request,settings)
        key=ip_key(request,"chat-unlock")
        attempt=auth.check_rate_limit(key,settings)
        with connect(settings) as db:
            rows=db.execute("SELECT id,password_encrypted FROM chats WHERE archived=0 AND password_encrypted IS NOT NULL").fetchall()
        matches=[]
        for row in rows:
            expected=auth.decrypt_password(row['password_encrypted'],settings)
            equal=hmac.compare_digest(payload.password.encode(),expected.encode())
            if expected and equal and (chat_id is None or row['id']==chat_id): matches.append(row['id'])
        if not matches:
            raise HTTPException(401,"Password non valida o accesso non disponibile.")
        with connect(settings) as db:
            db.execute("DELETE FROM login_attempts WHERE key=? AND at=?",(key,attempt))
            db.execute("UPDATE sessions SET unlocked=? WHERE token_hash=?",(json.dumps(sorted(set(session.unlocked+matches))),session.token_hash))
        return {'unlocked':True,'chat_ids':matches,'chat_id':matches[0]}

    @app.post('/api/unlock')
    def discover(payload:PasswordInput,request:Request):
        return unlock_password(payload,request)

    @app.post('/api/chats/{chat_id}/unlock')
    def unlock(chat_id:str,payload:PasswordInput,request:Request):
        return unlock_password(payload,request,chat_id)

    def filtered_messages(chat_id, start=None, end=None):
        params = [chat_id]
        lo, hi = _date_bounds(start, end, settings)
        query = "SELECT * FROM messages WHERE chat_id=? AND deleted=0"
        query += _where_dates("date", lo, hi, params)
        with connect(settings) as connection:
            return [dict(row) for row in connection.execute(query + " ORDER BY date,id", params)]

    def filtered_moments(chat_id, start=None, end=None):
        params = [chat_id]
        lo, hi = _date_bounds(start, end, settings)
        query = "SELECT * FROM moments WHERE chat_id=?"
        # Include a moment overlapping the selected local date interval.
        if lo:
            query += " AND end>=?"
            params.append(lo)
        if hi:
            query += " AND start<?"
            params.append(hi)
        with connect(settings) as connection:
            return [_public_moment(row) for row in connection.execute(query + " ORDER BY start,id", params)]

    @app.get("/api/chats/{chat_id}/report")
    def report(chat_id: str, request: Request, start: str | None = None, end: str | None = None):
        chat = chat_access(chat_id, request)
        _date_bounds(start, end, settings)
        cached = cached_report(chat_id, chat["version"], settings, start, end)
        if cached is not None:
            return decorate_report(cached, chat, settings)
        from .analytics import build_report
        result = build_report(filtered_messages(chat_id, start, end), filtered_moments(chat_id, start, end), timezone=settings.timezone)
        prepare_report(result, chat, settings)
        cache_report(result, chat, settings, start, end)
        return decorate_report(result, chat, settings)

    @app.get("/api/chats/{chat_id}/moments")
    def moments(chat_id: str, request: Request, start: str | None = None, end: str | None = None, tone: str | None = None, q: str = "", limit: int = 100, offset: int = 0):
        chat_access(chat_id, request)
        if not 1 <= limit <= 500 or offset < 0:
            raise HTTPException(422, "Parametri di paginazione non validi.")
        if tone not in {None, "all", "positive", "neutral", "negative", "mixed", "uncertain", "pending"}:
            raise HTTPException(422, "Filtro del tono non valido.")
        lo, hi = _date_bounds(start, end, settings)
        params: list = [chat_id]
        query = "FROM moments WHERE chat_id=?"
        if lo:
            query += " AND end>=?"
            params.append(lo)
        if hi:
            query += " AND start<?"
            params.append(hi)
        if q:
            query += " AND (instr(lower(COALESCE(json_extract(analysis,'$.context'),'')),lower(?))>0 OR instr(lower(COALESCE(json_extract(analysis,'$.context_parts'),'')),lower(?))>0 OR EXISTS (SELECT 1 FROM messages AS hit WHERE hit.chat_id=moments.chat_id AND hit.deleted=0 AND hit.date>=moments.start AND hit.date<=moments.end AND instr(lower(hit.text),lower(?))>0))"
            params.extend([q[:200]]*3)
        if tone == "pending":
            query += " AND analysis IS NULL"
        elif tone and tone != "all":
            query += " AND COALESCE(json_extract(analysis,'$.label'),'uncertain')=? AND analysis IS NOT NULL"
            params.append(tone)
        with connect(settings) as connection:
            total = connection.execute("SELECT COUNT(*) " + query, params).fetchone()[0]
            # Count membership in SQLite, avoiding transfer/decoding of the whole archive's IDs.
            rows = connection.execute("SELECT id,chat_id,start,end,analysis,model,analyzed_at,json_array_length(message_ids) AS message_count " + query + " ORDER BY start DESC,id DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        return {"items": [_public_moment(row) for row in rows], "total": total}

    @app.get("/api/chats/{chat_id}/messages")
    def messages(chat_id: str, request: Request, day: str | None = None, moment_id: str | None = None, q: str = "", sender: str | None = None, limit: int = 100, offset: int = 0):
        chat_access(chat_id, request)
        if not 1 <= limit <= 500 or offset < 0 or len(q) > 200:
            raise HTTPException(422, "Parametri di ricerca non validi.")
        params: list = [chat_id]
        query = "FROM messages WHERE chat_id=? AND deleted=0"
        lo, hi = _date_bounds(day, day, settings)
        query += _where_dates("date", lo, hi, params)
        if sender:
            query += " AND sender_id=?"
            params.append(sender)
        if q:
            query += " AND instr(lower(text),lower(?))>0"
            params.append(q)
        with connect(settings) as connection:
            if moment_id:
                moment = connection.execute("SELECT start,end FROM moments WHERE id=? AND chat_id=?", (moment_id, chat_id)).fetchone()
                if moment is None:
                    raise HTTPException(404, "Momento non trovato.")
                query += " AND date>=? AND date<=? AND id IN (SELECT value FROM moments AS selected,json_each(selected.message_ids) WHERE selected.id=? AND selected.chat_id=?)"
                params.extend([moment["start"], moment["end"], moment_id, chat_id])
            total = connection.execute("SELECT COUNT(*) " + query, params).fetchone()[0]
            items = [dict(row) for row in connection.execute("SELECT * " + query + " ORDER BY date,id LIMIT ? OFFSET ?", params + [limit, offset])]
            # Resolve reply previews in one query, scoped to the authorized chat (including earlier pages).
            reply_ids=list({m['reply_to_id'] for m in items if m.get('reply_to_id')})
            targets={}
            if reply_ids:
                targets={r['id']:dict(r) for r in connection.execute("SELECT id,sender_id,sender_name,type,text FROM messages WHERE chat_id=? AND deleted=0 AND id IN ("+','.join('?' for _ in reply_ids)+")",[chat_id]+reply_ids)}
            for m in items:
                if m.get('reply_to_id'):
                    target=targets.get(m['reply_to_id'])
                    m['reply_to']=({**target,'text':target['text'][:500]} if target else None)
        return {"items": items, "total": total}

    @app.get("/api/chats/{chat_id}/search")
    def search(chat_id: str, request: Request, q: str, start: str | None = None, end: str | None = None):
        chat_access(chat_id, request)
        if not q.strip() or len(q) > 200:
            raise HTTPException(422, "Inserisci una parola o una frase di massimo 200 caratteri.")
        from .analytics import TOKEN
        query_tokens = TOKEN.findall(q.strip().casefold())
        if not query_tokens:
            raise HTTPException(422, "Inserisci una parola o una frase da cercare.")
        width = len(query_tokens)
        by_sender: dict[str, dict] = {}
        total = matched = 0
        for message in filtered_messages(chat_id, start, end):
            occurrences = 0
            for sentence in re.split(r"[.!?;\n]+", message["text"].casefold()):
                tokens = TOKEN.findall(sentence)
                occurrences += sum(tokens[index:index + width] == query_tokens for index in range(len(tokens) - width + 1))
            if occurrences:
                total += occurrences
                matched += 1
                sid = message["sender_id"]
                row = by_sender.setdefault(sid, {"id": sid, "name": message["sender_name"], "count": 0, "messages": 0})
                row["count"] += occurrences
                row["messages"] += 1
        return {"query": q, "total": total, "messages": matched, "by_sender": list(by_sender.values())}

    @app.get("/api/admin/status")
    async def admin_status(request: Request):
        auth.require_admin(request, settings)
        from .jobs import list_jobs
        telegram = await request.app.state.telegram.status()
        return {"telegram": telegram, "openai": {"configured": bool(settings.openai_api_key), "model": settings.openai_model, "analysis_limit": settings.analysis_limit}, "jobs": list_jobs(settings=settings), "demo": settings.demo}

    @app.get("/api/admin/overview")
    def admin_overview(request: Request):
        auth.require_admin(request, settings)
        from .sentiment import PROMPT_VERSION
        with connect(settings) as db:
            # Separate aggregates avoid multiplying message counts by moment counts.
            db.execute("BEGIN")
            result = dict(db.execute("""SELECT COUNT(*) AS conversations,
                SUM(CASE WHEN password_encrypted IS NOT NULL AND password_encrypted<>'' THEN 1 ELSE 0 END) AS protected,
                MAX(last_sync) AS last_sync FROM chats WHERE archived=0""").fetchone())
            result.update(dict(db.execute("""SELECT COUNT(*) AS messages, MIN(m.date) AS first_message
                FROM messages m JOIN chats c ON c.id=m.chat_id WHERE c.archived=0 AND m.deleted=0""").fetchone()))
            result.update(dict(db.execute("""SELECT COUNT(*) AS moments,
                SUM(CASE WHEN m.analysis IS NOT NULL THEN 1 ELSE 0 END) AS analyzed,
                SUM(CASE WHEN m.analysis IS NULL OR m.model IS NULL OR m.model<>?
                  OR COALESCE(json_extract(m.analysis,'$.prompt_version'),'')<>? THEN 1 ELSE 0 END) AS pending
                FROM moments m JOIN chats c ON c.id=m.chat_id WHERE c.archived=0""",
                (settings.openai_model, PROMPT_VERSION)).fetchone()))
            result['active_jobs'] = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','retry')").fetchone()[0]
        for key in ('protected','analyzed','pending'):
            result[key] = result[key] or 0
        return result

    @app.get("/api/admin/chats/{chat_id}/password")
    def reveal_password(chat_id: str, request: Request):
        chat = chat_access(chat_id, request, admin_only=True)
        return {"password": auth.decrypt_password(chat["password_encrypted"], settings)}

    @app.post("/api/admin/chats/{chat_id}/password")
    def change_password(chat_id: str, payload: PasswordInput, request: Request):
        admin_mutation(request)
        chat_access(chat_id, request, admin_only=True)
        with connect(settings) as connection:
            connection.execute("UPDATE chats SET password_encrypted=? WHERE id=?", (auth.encrypt_password(payload.password, settings), chat_id))
            # Rotating a password revokes every previous guest unlock, not the admin role.
            for row in connection.execute("SELECT token_hash,unlocked FROM sessions"):
                unlocked = [cid for cid in json.loads(row["unlocked"]) if cid != chat_id]
                connection.execute("UPDATE sessions SET unlocked=? WHERE token_hash=?", (json.dumps(unlocked), row["token_hash"]))
        return {"updated": True, "locked": bool(payload.password)}

    @app.post("/api/admin/sync")
    def sync(payload: JobInput, request: Request):
        admin_mutation(request)
        if settings.demo:
            raise HTTPException(409, "La demo non è collegata a Telegram. Usa la modalità reale per aggiornare le chat.")
        if payload.chat_id:
            chat_access(payload.chat_id, request, admin_only=True)
        from .jobs import queue_job
        try:
            return queue_job("sync", chat_id=payload.chat_id, settings=settings)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/admin/analyze")
    def analyze(payload: JobInput, request: Request):
        admin_mutation(request)
        if not payload.chat_id:
            raise HTTPException(422, "Scegli una conversazione da analizzare.")
        chat_access(payload.chat_id, request, admin_only=True)
        if settings.demo:
            raise HTTPException(409, "Nella demo le stime sono illustrative: nessuna richiesta API viene inviata.")
        if not settings.openai_api_key:
            raise HTTPException(409, "Configura OPENAI_API_KEY sul server prima di avviare l'analisi.")
        from .jobs import queue_job
        try:
            return queue_job("analyze", chat_id=payload.chat_id, payload={"limit": min(payload.limit, settings.analysis_limit)}, settings=settings, reject_existing=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    async def telegram_call(request: Request, method: str, *args):
        if settings.demo:
            raise HTTPException(409, "La demo non invia richieste a Telegram.")
        try:
            return await getattr(request.app.state.telegram, method)(*args)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/admin/telegram/dialogs")
    async def telegram_dialogs(request: Request):
        auth.require_admin(request, settings)
        result = await telegram_call(request, "dialogs")
        return result if isinstance(result, dict) and "items" in result else {"items": result}

    @app.post("/api/admin/telegram/import")
    async def telegram_import(payload: ImportInput, request: Request):
        admin_mutation(request)
        return await telegram_call(request, "import_dialogs", payload.ids)

    @app.post("/api/admin/telegram/connect")
    async def telegram_connect(payload: PhoneInput, request: Request):
        admin_mutation(request)
        auth.check_rate_limit(ip_key(request, "telegram"), settings)
        result = await telegram_call(request, "connect", payload.phone)
        return {**result, "status": "connected" if result.get("connected") else "password_required" if result.get("needs_password") else "code_required"}

    @app.post("/api/admin/telegram/verify")
    async def telegram_verify(payload: VerificationInput, request: Request):
        admin_mutation(request)
        auth.check_rate_limit(ip_key(request, "telegram-verify"), settings)
        result = await telegram_call(request, "verify", payload.code, payload.password)
        return {**result, "status": "connected" if result.get("connected") else "password_required" if result.get("needs_password") else "code_required"}

    from .customization import install, decorate_report
    install(app, settings, chat_access, admin_mutation)

    frontend = Path(__file__).resolve().parent.parent / "frontend" / "dist"

    @app.get("/{path:path}", include_in_schema=False)
    def frontend_app(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Endpoint non trovato.")
        candidate = (frontend / path).resolve()
        if frontend.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        index = frontend / "index.html"
        if index.exists():
            return FileResponse(index, status_code=200 if path == "" else 404,
                                headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "Frontend non compilato. Avvia il frontend con Vite oppure esegui la build indicata nel README."}, status_code=503)

    return app


app = create_app()

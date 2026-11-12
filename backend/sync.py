"""Incremental Telegram ingestion. No network activity occurs on module import."""

from __future__ import annotations

import asyncio
import csv
import fcntl
import inspect
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect as database
from .settings import get_settings


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_date(value: Any) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class TelegramUnavailable(RuntimeError):
    """A deliberately safe error suitable for showing in the admin console."""


def message_type(message: Any) -> str:
    for attr, label in (("sticker", "sticker"), ("photo", "photo"), ("video", "video"),
                        ("voice", "audio"), ("audio", "audio"), ("document", "document"),
                        ("contact", "contact"), ("geo", "location")):
        if getattr(message, attr, None):
            return label
    if getattr(message, "action", None):
        return "service"
    return "text" if getattr(message, "message", None) or getattr(message, "text", None) else "unknown"


def message_record(chat_id: str, message: Any) -> dict:
    sender = getattr(message, "sender", None)
    name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
    sender_id = str(getattr(message, "sender_id", None) or "service")
    return {
        "chat_id": str(chat_id), "id": int(message.id), "sender_id": sender_id,
        "sender_name": name or getattr(sender, "title", None) or getattr(sender, "username", None) or sender_id,
        "type": message_type(message), "text": getattr(message, "message", None) or getattr(message, "text", None) or "",
        "date": iso_date(message.date),
        "edit_date": iso_date(message.edit_date) if getattr(message, "edit_date", None) else None,
        "reply_to_id": getattr(message, "reply_to_msg_id", None), "deleted": 0,
    }


MESSAGE_FIELDS = ("chat_id", "id", "sender_id", "sender_name", "type", "text", "date", "edit_date", "reply_to_id", "deleted")


def save_messages(chat_id: str, records: list[dict], cursor: int | None = None, settings=None, *, overwrite=True) -> int:
    """Commit one batch and its cursor together; replaying a batch is a no-op."""
    with database(settings) as conn:
        changes = conn.total_changes
        for record in records:
            conn.execute(
                "INSERT INTO messages (" + ",".join(MESSAGE_FIELDS) + ") VALUES (" + ",".join("?" for _ in MESSAGE_FIELDS) + ") " +
                ("ON CONFLICT(chat_id,id) DO UPDATE SET " + ",".join(f"{f}=excluded.{f}" for f in MESSAGE_FIELDS[2:]) +
                 " WHERE " + " OR ".join(f"messages.{f} IS NOT excluded.{f}" for f in MESSAGE_FIELDS[2:])
                 if overwrite else "ON CONFLICT(chat_id,id) DO NOTHING"),
                [record.get(f) for f in MESSAGE_FIELDS],
            )
        changed = conn.total_changes - changes
        if changed:
            conn.execute("UPDATE chats SET version=version+1 WHERE id=?", (str(chat_id),))
        if cursor is not None:
            conn.execute("UPDATE chats SET last_message_id=MAX(last_message_id,?) WHERE id=?", (cursor, str(chat_id)))
    return changed


def mark_deleted(chat_id: str, ids: list[int], settings=None) -> int:
    if not ids:
        return 0
    with database(settings) as conn:
        changes = conn.total_changes
        for message_id in ids:
            conn.execute("UPDATE messages SET deleted=1 WHERE chat_id=? AND id=? AND deleted=0", (str(chat_id), message_id))
        changed = conn.total_changes - changes
        if changed:
            conn.execute("UPDATE chats SET version=version+1 WHERE id=?", (str(chat_id),))
    return changed


async def report_progress(callback, **values):
    if callback:
        result = callback(values)
        if inspect.isawaitable(result):
            await result


class TelegramService:
    """One owner per process and filesystem lock; authentication is never interactive."""

    def __init__(self, settings=None, client_factory=None):
        self.settings = settings or get_settings()
        self.client_factory = client_factory
        self.client = None
        self.pending = None
        self._guard = asyncio.Lock()
        self._owner_file = None
        self._authorized = False
        self._account_id = None
        self._rebuild_tasks: dict[str, asyncio.Task] = {}

    async def start(self):
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        owner = open(self.settings.data_dir / "telegram-owner.lock", "a+")
        try:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner.close()
            raise TelegramUnavailable("Telegram è già gestito da un altro processo. Avvia un solo worker.") from None
        self._owner_file = owner
        account_file = self.settings.data_dir / "telegram-account-id"
        if account_file.exists():
            try:
                self._account_id = int(account_file.read_text().strip())
            except ValueError:
                raise TelegramUnavailable("Il riferimento dell'account Telegram non è valido.") from None

    def _remember_account(self, account_id):
        self._account_id = account_id
        path = self.settings.data_dir / "telegram-account-id"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(str(account_id))

    async def close(self):
        for task in self._rebuild_tasks.values():
            task.cancel()
        if self._rebuild_tasks:
            await asyncio.gather(*self._rebuild_tasks.values(), return_exceptions=True)
        await self._clear_pending()
        if self.client:
            await self.client.disconnect()
            self.client = None
        if self._owner_file:
            fcntl.flock(self._owner_file.fileno(), fcntl.LOCK_UN)
            self._owner_file.close()
            self._owner_file = None

    def _factory(self, path):
        if not self.settings.telegram_api_id or not self.settings.telegram_api_hash:
            raise TelegramUnavailable("Configura API_ID e API_HASH di Telegram sul server.")
        if self.settings.demo:
            raise TelegramUnavailable("Telegram non viene contattato in modalità dimostrativa.")
        if self.client_factory:
            return self.client_factory(str(path), int(self.settings.telegram_api_id), self.settings.telegram_api_hash)
        from telethon import TelegramClient
        return TelegramClient(str(path), int(self.settings.telegram_api_id), self.settings.telegram_api_hash,
                              flood_sleep_threshold=60, request_retries=3, connection_retries=3)

    async def _get_client(self):
        if not self._owner_file:
            raise TelegramUnavailable("Il worker Telegram non è attivo.")
        new_client = self.client is None
        if new_client:
            self.settings.telegram_session_path.parent.mkdir(parents=True, exist_ok=True)
            self.client = self._factory(self.settings.telegram_session_path)
            await self.client.connect()
            session_file = Path(str(self.settings.telegram_session_path).removesuffix(".session") + ".session")
            if session_file.exists():
                session_file.chmod(0o600)
            self._install_events(self.client)
        if not await self.client.is_user_authorized():
            self._authorized = False
            raise TelegramUnavailable("Ricollega l'account Telegram dal pannello admin.")
        if new_client or self._account_id is None:
            me = await self.client.get_me()
            if self._account_id is not None and me.id != self._account_id:
                await self.client.disconnect()
                self.client = None
                self._authorized = False
                raise TelegramUnavailable("La sessione appartiene a un altro account Telegram.")
            self._remember_account(me.id)
        self._authorized = True
        return self.client

    async def status(self):
        session = Path(str(self.settings.telegram_session_path).removesuffix(".session") + ".session")
        return {"configured": bool(self.settings.telegram_api_id and self.settings.telegram_api_hash),
                "connected": self._authorized, "session_present": session.exists(),
                "state": "connected" if self._authorized else "not_verified" if session.exists() else "disconnected",
                "pending": bool(self.pending), "account_id": self._account_id}

    async def dialogs(self):
        async with self._guard:
            client = await self._get_client()
            with database(self.settings) as conn:
                imported = {row[0] for row in conn.execute('SELECT telegram_id FROM chats WHERE archived=0 AND telegram_id IS NOT NULL')}
            items = []
            async for dialog in client.iter_dialogs(limit=200):
                if not dialog.is_user and not dialog.is_group and not dialog.is_channel:
                    continue
                entity = dialog.entity
                last_message = getattr(dialog, 'date', None)
                kind = ('bot' if getattr(entity, 'bot', False) else 'direct') if dialog.is_user else 'group' if dialog.is_group else 'channel'
                items.append({"id": str(dialog.id), "title": dialog.name or str(dialog.id),
                              "username": getattr(entity, "username", None), 'kind': kind,
                              'is_group': bool(dialog.is_group), 'is_channel': bool(dialog.is_channel),
                              'last_message_at': last_message.isoformat() if last_message else None,
                              'unread_count': max(0, int(getattr(dialog, 'unread_count', 0) or 0)),
                              'participants_count': getattr(entity, 'participants_count', None),
                              'imported': str(dialog.id) in imported})
            return items

    async def import_dialogs(self, ids):
        # Only import selected IDs that belong to the authenticated account's dialogs.
        wanted = {str(value) for value in ids}
        available = {item["id"]: item for item in await self.dialogs()}
        if not wanted.issubset(available):
            raise TelegramUnavailable("Una conversazione selezionata non è disponibile nell'account collegato.")
        from .jobs import queue_job
        imported = []
        unlinked_legacy = []
        for telegram_id in wanted:
            item = available[telegram_id]
            legacy_matched = False
            with database(self.settings) as conn:
                existing = conn.execute("SELECT id FROM chats WHERE telegram_id=?", (telegram_id,)).fetchone()
                candidate = None
                if not existing and item["username"]:
                    candidate = conn.execute("SELECT id FROM chats WHERE telegram_id IS NULL AND lower(username)=lower(?)",
                                             (item["username"],)).fetchone()
            if candidate:
                async with self._guard:
                    client = await self._get_client()
                    entity = await client.get_entity(int(telegram_id))
                    if await self._legacy_identity_matches(client, candidate["id"], entity):
                        existing = candidate
                        legacy_matched = True
                    else:
                        # A username can be reassigned. Preserve the CSV chat and create a distinct Telegram chat.
                        unlinked_legacy.append(candidate["id"])
            with database(self.settings) as conn:
                chat_id = existing["id"] if existing else telegram_id
                if existing:
                    conn.execute("UPDATE chats SET telegram_id=?,title=?,username=? WHERE id=?",
                                 (telegram_id, item["title"], item["username"], chat_id))
                    if legacy_matched:
                        conn.execute("UPDATE chats SET last_message_id=0 WHERE id=?", (chat_id,))
                else:
                    conn.execute("INSERT INTO chats(id,title,username,telegram_id) VALUES(?,?,?,?)",
                                 (chat_id, item["title"], item["username"], telegram_id))
            queue_job("sync", chat_id, settings=self.settings)
            imported.append(chat_id)
        return {"imported": imported, "unlinked_legacy": unlinked_legacy}

    async def connect(self, phone):
        async with self._guard:
            normalized = re.sub(r"[\s()-]", "", phone)
            if not re.fullmatch(r"\+[1-9][0-9]{6,14}", normalized):
                raise TelegramUnavailable("Inserisci il numero completo di prefisso internazionale, ad esempio +39…")
            # Discover the previous account before staging replacement; never revoke its session.
            try:
                await self._get_client()
            except TelegramUnavailable:
                pass
            await self._clear_pending()
            path = self.settings.data_dir / ("telegram-pending-" + uuid.uuid4().hex)
            pending_client = self._factory(path)
            try:
                await pending_client.connect()
                sent = await pending_client.send_code_request(normalized)
            except Exception as error:
                await pending_client.disconnect()
                self._remove_session_files(path)
                raise TelegramUnavailable(self._safe_error(error)) from None
            self.pending = {"client": pending_client, "path": path, "phone": normalized,
                            "hash": sent.phone_code_hash, "expires": time.monotonic() + 600, "needs_password": False}
            for file in path.parent.glob(path.name + ".session*"):
                file.chmod(0o600)
            return {"code_sent": True, "needs_password": False}

    async def verify(self, code, password=None):
        async with self._guard:
            if not self.pending or self.pending["expires"] < time.monotonic():
                await self._clear_pending()
                raise TelegramUnavailable("Il codice è scaduto. Richiedi un nuovo collegamento.")
            pending = self.pending
            try:
                if pending["needs_password"]:
                    if not password:
                        return {"needs_password": True}
                    await pending["client"].sign_in(password=password)
                else:
                    await pending["client"].sign_in(phone=pending["phone"], code=code, phone_code_hash=pending["hash"])
            except Exception as error:
                if type(error).__name__ == "SessionPasswordNeededError":
                    pending["needs_password"] = True
                    return {"needs_password": True}
                raise TelegramUnavailable(self._safe_error(error)) from None
            me = await pending["client"].get_me()
            if self._account_id is not None and me.id != self._account_id:
                await self._clear_pending()
                raise TelegramUnavailable("Questo progetto è associato a un altro account Telegram.")
            await pending["client"].disconnect()
            if self.client:
                await self.client.disconnect()
                self.client = None
            old_path = Path(str(pending["path"]) + ".session")
            target = Path(str(self.settings.telegram_session_path).removesuffix(".session") + ".session")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_path, target)
            target.chmod(0o600)
            self._remove_session_files(pending["path"])
            self.pending = None
            self._remember_account(me.id)
            self._authorized = True
            return {"connected": True, "needs_password": False}

    @staticmethod
    def _safe_error(error):
        messages = {
            "PhoneCodeInvalidError": "Codice non valido. Controllalo e riprova.",
            "PhoneCodeExpiredError": "Codice scaduto. Richiedine uno nuovo.",
            "PasswordHashInvalidError": "Password 2FA non valida.",
            "PhoneNumberInvalidError": "Numero di telefono non valido.",
            "FloodWaitError": "Telegram richiede una pausa. Riprova più tardi.",
            "ApiIdInvalidError": "Controlla API_ID e API_HASH sul server.",
        }
        return messages.get(type(error).__name__, "Telegram non è disponibile. Controlla il collegamento e riprova.")

    @staticmethod
    def _remove_session_files(path):
        for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    async def _clear_pending(self):
        if self.pending:
            await self.pending["client"].disconnect()
            self._remove_session_files(self.pending["path"])
            self.pending = None

    async def _resolve_chat(self, client, chat):
        reference = int(chat["telegram_id"]) if chat["telegram_id"] else chat["username"]
        if not reference:
            raise TelegramUnavailable("Associa questa chat importata a una conversazione Telegram.")
        if not chat["telegram_id"] and chat["id"].startswith("legacy:group:") and str(reference).lstrip("-").isdigit():
            # Old exports stored bare positive group IDs, which Telethon otherwise interprets as users.
            entity = None
            from telethon.utils import get_peer_id
            async for dialog in client.iter_dialogs():
                if (dialog.is_group or dialog.is_channel) and (str(getattr(dialog.entity, "id", "")) == str(reference)
                                                              or str(get_peer_id(dialog.entity)) == str(reference)):
                    entity = dialog.entity
                    break
            if entity is None:
                raise TelegramUnavailable("Il gruppo importato non è disponibile nell'account Telegram.")
        else:
            entity = await client.get_entity(reference)
        if not chat["telegram_id"]:
            if not await self._legacy_identity_matches(client, chat["id"], entity):
                raise TelegramUnavailable("L'identità della chat storica non è verificabile. Importa la conversazione Telegram "
                                          "come chat separata; l'archivio originale resta consultabile.")
            from telethon.utils import get_peer_id
            telegram_id = str(get_peer_id(entity))
            with database(self.settings) as conn:
                other = conn.execute("SELECT id FROM chats WHERE telegram_id=? AND id<>?", (telegram_id, chat["id"])).fetchone()
                if other:
                    raise TelegramUnavailable("Questa conversazione Telegram è già associata a un'altra chat importata.")
                conn.execute("UPDATE chats SET telegram_id=? WHERE id=?", (telegram_id, chat["id"]))
        return entity

    async def _legacy_identity_matches(self, client, chat_id, entity):
        with database(self.settings) as conn:
            samples = [dict(row) for row in conn.execute(
                "SELECT id,text,date FROM messages WHERE chat_id=? AND deleted=0 AND length(text)>=3 "
                "ORDER BY id DESC LIMIT 5", (chat_id,))]
        # One generic short message is not sufficient evidence for merging two private archives.
        if len(samples) < 2:
            return False
        remote = await client.get_messages(entity, ids=[row["id"] for row in samples])
        if len(remote) != len(samples):
            return False
        def normalize(text):
            return re.sub(r"\s+", " ", (text or "").replace("«", "").replace("»", "")).strip()
        matches = 0
        for saved, message in zip(samples, remote):
            if message is None or not getattr(message, "date", None) or int(message.id) != saved["id"]:
                continue
            text = getattr(message, "message", None) or getattr(message, "text", None) or ""
            if iso_date(message.date) == iso_date(saved["date"]) and normalize(text) == normalize(saved["text"]):
                matches += 1
        return matches >= 2

    async def sync_chat(self, chat_id, progress=None):
        async with self._guard:
            with database(self.settings) as conn:
                chat = conn.execute("SELECT * FROM chats WHERE id=?", (str(chat_id),)).fetchone()
            if chat is None:
                raise TelegramUnavailable("La conversazione non esiste più.")
            client = await self._get_client()
            entity = await self._resolve_chat(client, chat)
            # First binding of a CSV chat refetches accessible history once to restore stable sender IDs.
            saved, changed, cursor = 0, 0, int(chat["last_message_id"] or 0) if chat["telegram_id"] else 0
            await report_progress(progress, stage="import", saved=0, chat_id=str(chat_id))
            batch = []
            retries = 0
            while True:
                try:
                    async for message in client.iter_messages(entity, min_id=cursor, reverse=True):
                        if not getattr(message, "id", None) or not getattr(message, "date", None):
                            continue
                        batch.append(message_record(str(chat_id), message))
                        if len(batch) >= 250:
                            next_cursor = max(item["id"] for item in batch)
                            changed += save_messages(str(chat_id), batch, next_cursor, self.settings)
                            saved += len(batch)
                            cursor, batch = next_cursor, []
                            await report_progress(progress, stage="import", saved=saved, chat_id=str(chat_id))
                            await asyncio.sleep(0)
                    if batch:
                        cursor = max(item["id"] for item in batch)
                        changed += save_messages(str(chat_id), batch, cursor, self.settings)
                        saved += len(batch)
                        batch = []
                    break
                except Exception as error:
                    # Uncommitted messages are replayed after the last committed cursor.
                    batch = []
                    if type(error).__name__ != "FloodWaitError" or retries >= 3:
                        raise TelegramUnavailable(self._safe_error(error)) from None
                    wait = int(getattr(error, "seconds", 60))
                    if wait > 300:
                        raise TelegramUnavailable("Telegram richiede una pausa lunga. L'aggiornamento potrà essere ripreso.") from None
                    retries += 1
                    await report_progress(progress, stage="waiting", wait_seconds=wait, saved=saved, chat_id=str(chat_id))
                    await asyncio.sleep(max(1, wait))
            changed += await self._reconcile_recent(client, entity, str(chat_id))
            with database(self.settings) as conn:
                conn.execute("UPDATE chats SET last_sync=? WHERE id=?", (utcnow(), str(chat_id)))
            # Rebuilding even after an idempotent replay repairs a crash between save and rebuild.
            from .analytics import rebuild_moments
            await asyncio.to_thread(rebuild_moments, str(chat_id), settings=self.settings)
            await report_progress(progress, stage="complete", saved=saved, changed=changed, chat_id=str(chat_id))
            return {"saved": saved, "changed": changed, "last_message_id": cursor}

    async def _reconcile_recent(self, client, entity, chat_id):
        with database(self.settings) as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM messages WHERE chat_id=? AND deleted=0 ORDER BY id DESC LIMIT 200", (chat_id,))]
        if not ids:
            return 0
        messages = await client.get_messages(entity, ids=ids)
        # Missing pages are never interpreted as deletions. Explicit ID lookup must preserve cardinality.
        if len(messages) != len(ids) or not any(m is not None and getattr(m, "date", None) for m in messages):
            return 0
        records, deleted = [], []
        for expected_id, message in zip(ids, messages):
            if message is None:
                deleted.append(expected_id)
            elif getattr(message, "date", None) and int(message.id) == expected_id:
                records.append(message_record(chat_id, message))
        return save_messages(chat_id, records, settings=self.settings) + mark_deleted(chat_id, deleted, self.settings)

    def _install_events(self, client):
        if not hasattr(client, "add_event_handler"):
            return
        from telethon import events
        client.add_event_handler(self._on_message, events.NewMessage())
        client.add_event_handler(self._on_message, events.MessageEdited())
        client.add_event_handler(self._on_deleted, events.MessageDeleted())

    async def _on_message(self, event):
        with database(self.settings) as conn:
            chat = conn.execute("SELECT id FROM chats WHERE telegram_id=?", (str(event.chat_id),)).fetchone()
        if chat and getattr(event.message, "date", None):
            # Event delivery can have gaps: only history sync advances the durable history cursor.
            if save_messages(chat["id"], [message_record(chat["id"], event.message)], settings=self.settings):
                self._schedule_rebuild(chat["id"])

    async def _on_deleted(self, event):
        if event.chat_id is None:
            # Telegram does not identify every private-chat deletion; do not guess a chat.
            return
        with database(self.settings) as conn:
            chat = conn.execute("SELECT id FROM chats WHERE telegram_id=?", (str(event.chat_id),)).fetchone()
        if chat and mark_deleted(chat["id"], list(event.deleted_ids), self.settings):
            self._schedule_rebuild(chat["id"])

    def _schedule_rebuild(self, chat_id):
        previous = self._rebuild_tasks.get(chat_id)
        if previous and not previous.done():
            previous.cancel()
        async def rebuild():
            await asyncio.sleep(2)
            from .analytics import rebuild_moments
            await asyncio.to_thread(rebuild_moments, chat_id, settings=self.settings)
        self._rebuild_tasks[chat_id] = asyncio.create_task(rebuild())


def import_legacy(directory: Path, settings=None) -> dict:
    """Import CSV without running Telegram or accepting old pickle caches."""
    from cryptography.fernet import Fernet
    from .analytics import rebuild_moments
    settings = settings or get_settings()
    cipher = Fernet(settings.secret_key)
    result = {"chats": 0, "messages": 0, "skipped_files": 0, "invalid_rows": 0}
    for file in sorted(Path(directory).glob("*.csv")):
        match = re.fullmatch(r"(.+)_\[(g-)?([^\]]+)\]", file.stem)
        if not match:
            result["skipped_files"] += 1
            continue
        title, group, username = match.groups()
        # Numeric group IDs from legacy exports were unmarked; resolve via Telegram before binding.
        chat_id = "legacy:" + ("group:" if group else "user:") + username.lower()
        password = os.getenv("PASSWORD_" + username.upper())
        with database(settings) as conn:
            existing = conn.execute("SELECT id FROM chats WHERE lower(username)=lower(?)", (username,)).fetchall()
            if len(existing) > 1:
                result["skipped_files"] += 1
                continue
            if existing:
                chat_id = existing[0]["id"]
            conn.execute("INSERT OR IGNORE INTO chats(id,title,username,password_encrypted) VALUES(?,?,?,?)",
                         (chat_id, title.replace("_", " "), username,
                          cipher.encrypt(password.encode()).decode() if password else None))
            if password:
                conn.execute("UPDATE chats SET password_encrypted=COALESCE(password_encrypted,?) WHERE id=?",
                             (cipher.encrypt(password.encode()).decode(), chat_id))
        batch = []
        with file.open(newline="", encoding="utf-8-sig") as source:
            for row in csv.reader(source):
                try:
                    if len(row) != 8:
                        raise ValueError("Invalid legacy row")
                    message_id, author, kind, text, date, *_ = row
                    record = {"chat_id": chat_id, "id": int(message_id), "sender_id": "legacy:" + author,
                              "sender_name": author, "type": kind or "unknown", "text": text,
                              "date": iso_date(date), "edit_date": None, "reply_to_id": None, "deleted": 0}
                    batch.append(record)
                except (ValueError, TypeError):
                    result["invalid_rows"] += 1
                    continue
                if len(batch) >= 500:
                    result["messages"] += save_messages(chat_id, batch, max(r["id"] for r in batch), settings, overwrite=False)
                    batch = []
        if batch:
            result["messages"] += save_messages(chat_id, batch, max(r["id"] for r in batch), settings, overwrite=False)
        rebuild_moments(chat_id, settings=settings)
        result["chats"] += 1
    return result

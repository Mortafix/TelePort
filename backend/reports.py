"""Shared report formatting and cache warming outside interactive requests."""
from __future__ import annotations

import hashlib
import json

from .db import connect
from .settings import get_settings


def report_cache_key(settings, start=None, end=None) -> str:
    return hashlib.sha256(json.dumps([start, end, settings.timezone, settings.openai_model, "report-v4"]).encode()).hexdigest()


def cached_report(chat_id, version, settings, start=None, end=None):
    with connect(settings) as connection:
        cached = connection.execute("SELECT report FROM report_cache WHERE chat_id=? AND cache_key=? AND version=?", (chat_id, report_cache_key(settings, start, end), version)).fetchone()
    return json.loads(cached["report"]) if cached else None


def prepare_report(result, chat, settings):
    """All statistics remain complete; only the initial block list is bounded."""
    all_moments = result.get("moments", [])
    result["moments_total"] = len(all_moments)
    result["moments"] = [{key: value for key, value in moment.items() if key not in {"message_ids", "fingerprint"}} for moment in reversed(all_moments[-100:])]
    result["chat"] = {"id": chat["id"], "title": chat["title"], "gap_minutes": chat["gap_minutes"], "last_sync": chat["last_sync"]}
    result["demo"] = settings.demo
    result["timezone"] = settings.timezone
    return result


def cache_report(result, chat, settings, start=None, end=None) -> bool:
    """Do not store a snapshot superseded by an import or analysis in progress."""
    with connect(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT version FROM chats WHERE id=?", (chat["id"],)).fetchone()
        if not current or current["version"] != chat["version"]:
            return False
        connection.execute("INSERT OR REPLACE INTO report_cache VALUES (?,?,?,?)", (chat["id"], report_cache_key(settings, start, end), chat["version"], json.dumps(result, ensure_ascii=False)))
        connection.execute("DELETE FROM report_cache WHERE chat_id=? AND rowid NOT IN (SELECT rowid FROM report_cache WHERE chat_id=? ORDER BY rowid DESC LIMIT 12)", (chat["id"], chat["id"]))
    return True


def warm_report(chat_id: str, settings=None) -> dict | None:
    """Build the full-range report after a job; safe to call via asyncio.to_thread.

    Reads one consistent SQLite snapshot. A version guard prevents a concurrent
    import from poisoning the cache. No Telegram/OpenAI requests or private logs.
    """
    settings = settings or get_settings()
    with connect(settings) as connection:
        connection.execute("BEGIN")
        chat = connection.execute("SELECT * FROM chats WHERE id=?", (str(chat_id),)).fetchone()
        if chat is None:
            return None
        cached = connection.execute("SELECT report FROM report_cache WHERE chat_id=? AND cache_key=? AND version=?", (chat["id"], report_cache_key(settings), chat["version"])).fetchone()
        if cached:
            return json.loads(cached["report"])
        messages = [dict(row) for row in connection.execute("SELECT * FROM messages WHERE chat_id=? AND deleted=0 ORDER BY date,id", (chat["id"],))]
        moments = [dict(row) for row in connection.execute("SELECT * FROM moments WHERE chat_id=? ORDER BY start,id", (chat["id"],))]
        chat = dict(chat)
    from .analytics import build_report
    result = prepare_report(build_report(messages, moments, timezone=settings.timezone), chat, settings)
    cache_report(result, chat, settings)
    return result

"""Small persistent queue for one VPS. Jobs are leased and replay-safe."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from .db import connect
from .settings import get_settings
from .sync import TelegramUnavailable, utcnow

ACTIVE = ("queued", "running", "retry")


def public_job(row):
    if row is None:
        return None
    job = dict(row)
    for name in ("payload", "progress"):
        try:
            job[name] = json.loads(job.get(name) or "{}")
        except (ValueError, TypeError):
            job[name] = {}
    return job


def queue_job(kind, chat_id=None, payload=None, settings=None):
    settings = settings or get_settings()
    if kind not in {"sync", "analyze"}:
        raise ValueError("Tipo di attività non valido.")
    payload = dict(payload or {})
    chat_id = str(chat_id) if chat_id is not None else None
    with connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = [public_job(row) for row in conn.execute(
            "SELECT * FROM jobs WHERE kind=? AND status IN ('queued','running','retry') ORDER BY created_at", (kind,))]
        if kind == "sync":
            wanted = [chat_id] if chat_id else [row["id"] for row in conn.execute("SELECT id FROM chats ORDER BY id")]
            covered = set()
            for job in active:
                covered.update([job["chat_id"]] if job["chat_id"] else job["payload"].get("chat_ids", []))
            remaining = [target for target in wanted if target not in covered]
            if not remaining:
                if active:
                    return next((job for job in active if chat_id and
                                 (job["chat_id"] == chat_id or chat_id in job["payload"].get("chat_ids", []))), active[0])
                raise ValueError("Non ci sono conversazioni da aggiornare.")
            payload["chat_ids"] = remaining
        else:
            if not chat_id:
                raise ValueError("Seleziona una conversazione da analizzare.")
            for job in active:
                if job["chat_id"] == chat_id:
                    return job
        if chat_id and not conn.execute("SELECT id FROM chats WHERE id=? AND archived=0", (chat_id,)).fetchone():
            raise ValueError("Conversazione non trovata.")
        job_id, now = uuid.uuid4().hex, utcnow()
        conn.execute(
            "INSERT INTO jobs(id,kind,chat_id,status,progress,payload,created_at,updated_at,attempts,run_after) "
            "VALUES(?,?,?,'queued','{}',?,?,?,0,0)",
            (job_id, kind, chat_id, json.dumps(payload), now, now),
        )
        return public_job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def list_jobs(settings=None, limit=50):
    with connect(settings) as conn:
        return [public_job(row) for row in conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]


def claim_job(settings=None):
    now = time.time()
    with connect(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # A crashed third attempt must become visibly failed rather than stay 'running' forever.
        conn.execute("UPDATE jobs SET status='failed',error=?,lease_until=NULL,updated_at=? "
                     "WHERE status='running' AND lease_until<? AND attempts>=3",
                     ("Attività interrotta dopo più tentativi. Puoi avviarla di nuovo.", utcnow(), now))
        row = conn.execute(
            "SELECT * FROM jobs WHERE attempts<3 AND ((status IN ('queued','retry') AND run_after<=?) "
            "OR (status='running' AND lease_until<?)) ORDER BY created_at LIMIT 1", (now, now),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE jobs SET status='running',attempts=attempts+1,lease_until=?,updated_at=?,error=NULL WHERE id=?",
                     (now + 90, utcnow(), row["id"]))
        return public_job(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())


def update_progress(job_id, progress, settings=None):
    with connect(settings) as conn:
        conn.execute("UPDATE jobs SET progress=?,updated_at=?,lease_until=? WHERE id=? AND status='running'",
                     (json.dumps(progress, ensure_ascii=False), utcnow(), time.time() + 90, job_id))


async def _heartbeat(job_id, settings):
    while True:
        await asyncio.sleep(15)
        with connect(settings) as conn:
            conn.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE id=? AND status='running'",
                         (time.time() + 90, utcnow(), job_id))


async def execute_job(job, service, settings=None):
    settings = settings or get_settings()
    state = dict(job.get("progress") or {})
    def progress(values):
        state.update(values)
        update_progress(job["id"], state, settings)
    if job["kind"] == "sync":
        ids = [job["chat_id"]] if job["chat_id"] else job["payload"].get("chat_ids", [])
        for index, chat_id in enumerate(ids):
            def chat_progress(values):
                progress({**values, "chat_index": index + 1, "chat_total": len(ids)})
            await service.sync_chat(chat_id, progress=chat_progress)
            from .reports import warm_report
            chat_progress({"stage": "statistics"})
            await asyncio.to_thread(warm_report, chat_id, settings)
        progress({"stage": "complete", "chat_total": len(ids), "chat_index": len(ids)})
    elif job["kind"] == "analyze":
        from .sentiment import analyze_chat
        result = await analyze_chat(job["chat_id"], limit=min(max(int(job["payload"].get("limit", 100)), 1), 1000),
                                    progress=progress, settings=settings, budget=job.get("progress"))
        from .reports import warm_report
        progress({**result, "stage": "statistics"})
        await asyncio.to_thread(warm_report, job["chat_id"], settings)
        progress({"stage": "request_limit" if result.get("request_limit_reached") else "complete", **result})


async def worker_loop(service, settings=None):
    """Run in the API lifespan. Never starts Telegram or OpenAI without a queued job."""
    settings = settings or get_settings()
    if settings.demo:
        return
    while True:
        job = claim_job(settings)
        if job is None:
            await asyncio.sleep(1)
            continue
        heartbeat = asyncio.create_task(_heartbeat(job["id"], settings))
        try:
            await execute_job(job, service, settings)
            with connect(settings) as conn:
                conn.execute("UPDATE jobs SET status='complete',lease_until=NULL,error=NULL,updated_at=? WHERE id=?",
                             (utcnow(), job["id"]))
        except asyncio.CancelledError:
            with connect(settings) as conn:
                conn.execute("UPDATE jobs SET status='queued',attempts=MAX(0,attempts-1),lease_until=NULL,updated_at=? WHERE id=?",
                             (utcnow(), job["id"]))
            raise
        except Exception as error:
            # Never persist raw provider exceptions: they may contain phone numbers, text or credentials.
            safe = str(error) if isinstance(error, TelegramUnavailable) else (
                "L'analisi non è riuscita. Controlla configurazione e disponibilità del servizio." if job["kind"] == "analyze"
                else "L'aggiornamento non è riuscito. Controlla il collegamento Telegram.")
            with connect(settings) as conn:
                conn.execute("UPDATE jobs SET status=?,lease_until=NULL,error=?,run_after=?,updated_at=? WHERE id=?",
                             ("failed" if job["attempts"] >= 3 else "retry", safe,
                              time.time() + min(300, 5 * 4 ** (job["attempts"] - 1)), utcnow(), job["id"]))
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

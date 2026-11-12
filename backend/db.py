"""SQLite store. Stable Telegram IDs, UTC timestamps, WAL and resumable jobs."""
from __future__ import annotations

import sqlite3
from .settings import Settings, get_settings


class ClosingConnection(sqlite3.Connection):
    """A transaction context also releases the connection and WAL readers."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(settings: Settings | None = None) -> sqlite3.Connection:
    settings = settings or get_settings()
    connection = sqlite3.connect(settings.database_path, timeout=30, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def initialize(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    with connect(settings) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, username TEXT, telegram_id TEXT,
          password_encrypted TEXT, version INTEGER NOT NULL DEFAULT 0,
          last_message_id INTEGER NOT NULL DEFAULT 0, last_sync TEXT,
          gap_minutes INTEGER NOT NULL DEFAULT 30
        );
        CREATE TABLE IF NOT EXISTS messages (
          chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
          id INTEGER NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
          type TEXT NOT NULL DEFAULT 'text', text TEXT NOT NULL DEFAULT '', date TEXT NOT NULL,
          edit_date TEXT, reply_to_id INTEGER, deleted INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(chat_id,id)
        );
        CREATE INDEX IF NOT EXISTS messages_chronology ON messages(chat_id,deleted,date,id);
        CREATE TABLE IF NOT EXISTS moments (
          id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
          start TEXT NOT NULL, end TEXT NOT NULL, message_ids TEXT NOT NULL,
          fingerprint TEXT NOT NULL, analysis TEXT, model TEXT, analyzed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS moments_chronology ON moments(chat_id,start);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, chat_id TEXT,
          status TEXT NOT NULL DEFAULT 'queued', progress TEXT NOT NULL DEFAULT '{}',
          error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL DEFAULT '{}',
          run_after REAL NOT NULL DEFAULT 0, lease_until REAL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0,
          unlocked TEXT NOT NULL DEFAULT '[]', expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
          key TEXT NOT NULL, at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS login_attempts_key ON login_attempts(key,at);
        CREATE TABLE IF NOT EXISTS report_cache (
          chat_id TEXT NOT NULL, cache_key TEXT NOT NULL, version INTEGER NOT NULL,
          report TEXT NOT NULL, PRIMARY KEY(chat_id,cache_key)
        );
        """)
        columns={r[1] for r in connection.execute("PRAGMA table_info(chats)")}
        if 'appearance' not in columns:
            connection.execute("ALTER TABLE chats ADD COLUMN appearance TEXT NOT NULL DEFAULT '{}'")
        if 'archived' not in columns:
            connection.execute("ALTER TABLE chats ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        connection.execute("CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        # The segmentation threshold belongs to server configuration, including new imports.
        connection.execute('UPDATE chats SET gap_minutes=?,version=version+1 WHERE gap_minutes<>?',
            (settings.moment_gap_minutes, settings.moment_gap_minutes))
        connection.execute('DROP TRIGGER IF EXISTS chats_configured_gap')
        connection.execute(f'''CREATE TRIGGER chats_configured_gap AFTER INSERT ON chats
            BEGIN UPDATE chats SET gap_minutes={int(settings.moment_gap_minutes)} WHERE id=NEW.id; END''')
    try:
        settings.database_path.chmod(0o600)
    except OSError:
        pass

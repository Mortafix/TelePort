"""Explicit migration and setup commands; importing the application never imports chats."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
from pathlib import Path

from .db import connect, initialize
from .settings import get_settings


def main():
    parser = argparse.ArgumentParser(description="TelePort — configurazione e migrazione")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Prepara il database senza leggere conversazioni o contattare servizi")
    legacy = commands.add_parser("import-legacy", help="Importa i CSV precedenti, senza modificarli")
    legacy.add_argument("directory", nargs="?", default="conversations", type=Path)
    session = commands.add_parser("copy-session", help="Copia la vecchia sessione Telegram, senza modificarla")
    session.add_argument("source", nargs="?", default="chatty.session", type=Path)
    commands.add_parser("hash-password", help="Genera ADMIN_PASSWORD_HASH con input nascosto")
    commands.add_parser("warm-reports", help="Precalcola le statistiche delle chat già importate, senza chiamare servizi")
    args = parser.parse_args()
    if args.command == "hash-password":
        from .auth import hash_password
        password = getpass.getpass("Nuova password admin: ")
        if len(password) < 12:
            parser.error("Usa una password di almeno 12 caratteri.")
        if password != getpass.getpass("Ripeti password: "):
            parser.error("Le password non coincidono.")
        print(hash_password(password))
        return
    settings = get_settings()
    initialize(settings)
    if args.command == "init":
        print("Database pronto. Nessuna conversazione importata e nessun servizio contattato.")
    elif args.command == "import-legacy":
        from .sync import import_legacy
        if not args.directory.is_dir():
            parser.error("La cartella delle conversazioni non esiste.")
        print(json.dumps(import_legacy(args.directory, settings), ensure_ascii=False))
    elif args.command == "warm-reports":
        from .reports import warm_report
        with connect(settings) as conn:
            ids = [row["id"] for row in conn.execute("SELECT id FROM chats ORDER BY id")]
        completed = 0
        failed = 0
        for chat_id in ids:
            try:
                warm_report(chat_id, settings)
                completed += 1
            except Exception:
                # Provider-free maintenance; do not print private names or exception contents.
                failed += 1
        print(json.dumps({"chats": completed, "failed": failed}))
        if failed:
            raise SystemExit(1)
    elif args.command == "copy-session":
        source = args.source.resolve()
        target = Path(str(settings.telegram_session_path).removesuffix(".session") + ".session").resolve()
        if not source.is_file():
            parser.error("La sessione di origine non esiste.")
        if source == target or target.exists():
            parser.error("La sessione di destinazione esiste già. Nessun file sovrascritto.")
        target.parent.mkdir(parents=True, exist_ok=True)
        # SQLite backup includes committed WAL contents and avoids copying a half-written database.
        old = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        new = sqlite3.connect(target)
        try:
            old.backup(new)
        finally:
            old.close()
            new.close()
        os.chmod(target, 0o600)
        print("Sessione copiata. L'originale è rimasto invariato; la connessione non è stata avviata.")


if __name__ == "__main__":
    main()

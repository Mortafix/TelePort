import csv
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.db import connect, initialize
from backend.jobs import claim_job, execute_job, list_jobs, queue_job
from backend.settings import Settings
from backend.sync import TelegramService, TelegramUnavailable, import_legacy, mark_deleted, message_record, save_messages


def message(identifier, text="ciao", sender_id=1):
    return SimpleNamespace(id=identifier, message=text, text=text, sender_id=sender_id,
                           sender=SimpleNamespace(first_name="Persona", last_name="", username=None),
                           date=datetime(2026, 10, 1, 12, identifier % 60, tzinfo=timezone.utc),
                           edit_date=None, reply_to_msg_id=None)


class FakeClient:
    def __init__(self, messages=None):
        self.messages = messages or []
        self.requests = []
        self.getter = None

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=123)

    async def get_entity(self, value):
        return value

    async def iter_messages(self, entity, min_id, reverse):
        self.requests.append((min_id, reverse))
        for item in self.messages:
            if item.id > min_id:
                yield item

    async def get_messages(self, entity, ids):
        if self.getter:
            return self.getter(ids)
        known = {m.id: m for m in self.messages}
        return [known.get(value) for value in ids]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temporary.name), telegram_api_id="1", telegram_api_hash="test")
        initialize(self.settings)
        with connect(self.settings) as conn:
            conn.execute("INSERT INTO chats(id,title,telegram_id) VALUES('chat','Chat','1234')")

    def tearDown(self):
        self.temporary.cleanup()

    def test_idempotent_upserts_bump_version_only_on_changes(self):
        record = message_record("chat", message(1))
        self.assertEqual(save_messages("chat", [record], 1, self.settings), 1)
        self.assertEqual(save_messages("chat", [record], 1, self.settings), 0)
        record["text"] = "modificato"
        self.assertEqual(save_messages("chat", [record], 1, self.settings), 1)
        with connect(self.settings) as conn:
            row = conn.execute("SELECT version,last_message_id FROM chats WHERE id='chat'").fetchone()
        self.assertEqual(tuple(row), (2, 1))

    def test_cursor_and_message_batch_are_one_transaction(self):
        good = message_record("chat", message(1))
        bad = {**message_record("chat", message(2)), "sender_id": None}
        with self.assertRaises(sqlite3.IntegrityError):
            save_messages("chat", [good, bad], 2, self.settings)
        with connect(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT last_message_id FROM chats").fetchone()[0], 0)

    def test_delete_is_idempotent_and_preserves_history_cursor(self):
        save_messages("chat", [message_record("chat", message(9))], 9, self.settings)
        self.assertEqual(mark_deleted("chat", [9], self.settings), 1)
        self.assertEqual(mark_deleted("chat", [9], self.settings), 0)
        with connect(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT last_message_id FROM chats").fetchone()[0], 9)

    def test_sync_queue_coalesces_overlapping_scopes(self):
        with connect(self.settings) as conn:
            conn.execute("INSERT INTO chats(id,title) VALUES('second','Seconda')")
        single = queue_job("sync", "chat", settings=self.settings)
        same = queue_job("sync", "chat", settings=self.settings)
        self.assertEqual(single["id"], same["id"])
        all_chats = queue_job("sync", settings=self.settings)
        self.assertEqual(all_chats["payload"]["chat_ids"], ["second"])
        covered = queue_job("sync", "second", settings=self.settings)
        self.assertIn(covered["id"], {single["id"], all_chats["id"]})
        self.assertEqual(len(list_jobs(self.settings)), 2)

    def test_crashed_job_is_reclaimed_and_attempt_bounded(self):
        job = queue_job("sync", "chat", settings=self.settings)
        first = claim_job(self.settings)
        self.assertEqual(first["id"], job["id"])
        self.assertIsNone(claim_job(self.settings))
        with connect(self.settings) as conn:
            conn.execute("UPDATE jobs SET lease_until=?", (time.time() - 1,))
        self.assertEqual(claim_job(self.settings)["attempts"], 2)
        with connect(self.settings) as conn:
            conn.execute("UPDATE jobs SET lease_until=?,attempts=3", (time.time() - 1,))
        self.assertIsNone(claim_job(self.settings))
        self.assertEqual(list_jobs(self.settings)[0]["status"], "failed")

    def test_csv_import_is_explicit_repeatable_and_does_not_modify_original(self):
        folder = Path(self.temporary.name) / "legacy"
        folder.mkdir()
        source = folder / "Nome_con_spazi_[some_user].csv"
        with source.open("w", newline="") as stream:
            csv.writer(stream).writerows([
                [2, "Alice", "text", "Secondo", "2026-10-01 12:01:00+00:00", "joy", "positive", "0.8"],
                [1, "Alice", "text", "Primo", "2026-10-01 12:00:00+00:00", "joy", "positive", "0.8"],
                ["broken"],
            ])
        original = source.read_bytes()
        result = import_legacy(folder, self.settings)
        self.assertEqual(result["messages"], 2)
        self.assertEqual(result["invalid_rows"], 1)
        self.assertEqual(import_legacy(folder, self.settings)["messages"], 0)
        self.assertEqual(source.read_bytes(), original)
        with connect(self.settings) as conn:
            chat = conn.execute("SELECT * FROM chats WHERE username='some_user'").fetchone()
            self.assertEqual(chat["last_message_id"], 2)
            self.assertEqual(chat["title"], "Nome con spazi")
            ids = [r[0] for r in conn.execute("SELECT id FROM messages WHERE chat_id=? ORDER BY date,id", (chat["id"],))]
            self.assertEqual(ids, [1, 2])
            conn.execute("UPDATE messages SET text='Telegram aggiornato' WHERE chat_id=? AND id=1", (chat["id"],))
        import_legacy(folder, self.settings)
        with connect(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT text FROM messages WHERE chat_id=? AND id=1", (chat["id"],)).fetchone()[0],
                             "Telegram aggiornato")


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temporary.name), telegram_api_id="1", telegram_api_hash="test")
        initialize(self.settings)
        with connect(self.settings) as conn:
            conn.execute("INSERT INTO chats(id,title,telegram_id) VALUES('chat','Chat','1234')")
        self.client = FakeClient()
        self.service = TelegramService(self.settings, client_factory=lambda *args: self.client)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.temporary.cleanup()

    async def test_import_list_uses_cached_metadata_without_fetching_each_chat(self):
        calls=[]
        async def dialogs(limit):
            calls.append(limit)
            yield SimpleNamespace(id=1234,name='Known',is_user=True,is_group=False,is_channel=False,
                entity=SimpleNamespace(username='known'),date=datetime(2026,11,12,tzinfo=timezone.utc),unread_count=3)
            yield SimpleNamespace(id=-55,name='Group',is_user=False,is_group=True,is_channel=True,
                entity=SimpleNamespace(participants_count=28),date=None,unread_count=0)
            yield SimpleNamespace(id=77,name='Bot',is_user=True,is_group=False,is_channel=False,
                entity=SimpleNamespace(bot=True),date=None,unread_count=0)
        self.client.iter_dialogs=dialogs
        result=await self.service.dialogs()
        self.assertEqual(calls,[200])
        self.assertEqual(result[0]['username'],'known')
        self.assertEqual(result[0]['last_message_at'],'2026-11-12T00:00:00+00:00')
        self.assertEqual(result[0]['unread_count'],3)
        self.assertTrue(result[0]['imported'])
        self.assertEqual(result[1]['kind'],'group')
        self.assertEqual(result[1]['participants_count'],28)
        self.assertIsNone(result[1]['last_message_at'])
        self.assertFalse(result[1]['imported'])
        self.assertEqual(result[2]['kind'],'bot')
        self.assertNotIn('text',result[0])

    async def test_incremental_sync_handles_empty_history_and_resume(self):
        result = await self.service.sync_chat("chat")
        self.assertEqual(result["saved"], 0)
        self.client.messages = [message(4), message(8)]
        result = await self.service.sync_chat("chat")
        self.assertEqual(result["last_message_id"], 8)
        self.assertEqual(result["saved"], 2)
        self.client.messages.append(message(12))
        result = await self.service.sync_chat("chat")
        self.assertEqual(result["saved"], 1)
        self.assertEqual(self.client.requests, [(0, True), (0, True), (8, True)])
        with connect(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)

    async def test_short_or_empty_reconciliation_page_does_not_delete(self):
        self.client.messages = [message(1), message(2)]
        await self.service.sync_chat("chat")
        self.client.getter = lambda ids: []
        await self.service.sync_chat("chat")
        self.client.getter = lambda ids: [None for _ in ids]
        await self.service.sync_chat("chat")
        with connect(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE deleted=0").fetchone()[0], 2)

    async def test_direct_lookup_updates_edits_and_confirms_specific_deletion(self):
        self.client.messages = [message(1), message(2), message(3)]
        await self.service.sync_chat("chat")
        self.client.messages = [message(1, "testo corretto"), message(3)]
        await self.service.sync_chat("chat")
        with connect(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT text FROM messages WHERE id=1").fetchone()[0], "testo corretto")
            self.assertEqual(conn.execute("SELECT deleted FROM messages WHERE id=2").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT last_message_id FROM chats").fetchone()[0], 3)

    async def test_live_events_never_advance_history_cursor(self):
        with connect(self.settings) as conn:
            conn.execute("UPDATE chats SET last_message_id=5")
        await self.service._on_message(SimpleNamespace(chat_id=1234, message=message(99)))
        with connect(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT last_message_id FROM chats").fetchone()[0], 5)

    async def test_reassigned_username_never_merges_or_rebinds_legacy_archive(self):
        with connect(self.settings) as conn:
            conn.execute("INSERT INTO chats(id,title,username) VALUES('legacy:user:name','Archivio','name')")
        records = [message_record('legacy:user:name', message(1, 'Testo storico uno')),
                   message_record('legacy:user:name', message(2, 'Testo storico due'))]
        save_messages('legacy:user:name', records, 2, self.settings)
        self.client.messages = [message(1, 'Altro proprietario uno'), message(2, 'Altro proprietario due')]
        with self.assertRaises(TelegramUnavailable):
            await self.service.sync_chat('legacy:user:name')
        async def dialogs():
            return [{'id': '999', 'title': 'Nuovo proprietario', 'username': 'name'}]
        self.service.dialogs = dialogs
        result = await self.service.import_dialogs(['999'])
        self.assertEqual(result['imported'], ['999'])
        self.assertEqual(result['unlinked_legacy'], ['legacy:user:name'])
        with connect(self.settings) as conn:
            original = conn.execute("SELECT telegram_id,last_message_id FROM chats WHERE id='legacy:user:name'").fetchone()
            self.assertIsNone(original['telegram_id'])
            self.assertEqual(original['last_message_id'], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id='999'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT text FROM messages WHERE chat_id='legacy:user:name' AND id=1").fetchone()[0],
                             'Testo storico uno')

    async def test_verified_legacy_import_requires_overlaps_and_refetches_identity_history(self):
        chat_id = 'legacy:user:name'
        with connect(self.settings) as conn:
            conn.execute("INSERT INTO chats(id,title,username) VALUES(?,?,?)", (chat_id, 'Archivio', 'name'))
        self.client.messages = [message(1, 'Primo testo verificabile'), message(2, 'Secondo testo verificabile')]
        records = [message_record(chat_id, item) for item in self.client.messages]
        for record in records:
            record['sender_id'] = 'legacy:Persona'
        save_messages(chat_id, records, 2, self.settings)
        async def dialogs():
            return [{'id': '999', 'title': 'Conversazione verificata', 'username': 'name'}]
        self.service.dialogs = dialogs
        result = await self.service.import_dialogs(['999'])
        self.assertEqual(result['imported'], [chat_id])
        self.assertEqual(result['unlinked_legacy'], [])
        with connect(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT last_message_id FROM chats WHERE id=?", (chat_id,)).fetchone()[0], 0)
        await self.service.sync_chat(chat_id)
        self.assertEqual(self.client.requests[0], (0, True))
        with connect(self.settings) as conn:
            self.assertEqual({row[0] for row in conn.execute("SELECT sender_id FROM messages WHERE chat_id=?", (chat_id,))}, {'1'})

    async def test_single_session_owner(self):
        other = TelegramService(self.settings, client_factory=lambda *args: self.client)
        with self.assertRaises(TelegramUnavailable):
            await other.start()
        await other.close()

    async def test_startup_and_status_do_not_connect(self):
        self.assertIsNone(self.service.client)
        status = await self.service.status()
        self.assertFalse(status["connected"])
        self.assertIsNone(self.service.client)

    async def test_sync_job_warms_statistics_in_separate_stage(self):
        queue_job('sync', 'chat', settings=self.settings)
        job = claim_job(self.settings)
        seen = []
        async def sync_chat(chat_id, progress):
            progress({'stage': 'complete', 'saved': 4, 'changed': 4})
        def warm(chat_id, settings):
            current = list_jobs(settings)[0]['progress']
            seen.append((chat_id, current['stage'], current['saved']))
        with patch.dict('sys.modules', {'backend.reports': SimpleNamespace(warm_report=warm)}):
            await execute_job(job, SimpleNamespace(sync_chat=sync_chat), settings=self.settings)
        self.assertEqual(seen, [('chat', 'statistics', 4)])
        self.assertEqual(list_jobs(self.settings)[0]['progress']['stage'], 'complete')

    async def test_analysis_statistics_stage_preserves_request_budget(self):
        import json
        original = {'stage': 'analysis', 'requests': 5, 'completed': 3, 'input_tokens': 200}
        queue_job('analyze', 'chat', settings=self.settings)
        with connect(self.settings) as conn:
            conn.execute('UPDATE jobs SET progress=?', (json.dumps(original),))
        job = claim_job(self.settings)
        seen = []
        async def analyze(chat_id, **kwargs):
            self.assertEqual(kwargs['budget'], original)
            kwargs['progress']({'stage': 'analysis', 'requests': 7, 'completed': 4, 'input_tokens': 400})
            return {'requests': 7, 'completed': 4, 'remaining': 1, 'input_tokens': 400,
                    'request_limit_reached': True}
        def warm(chat_id, settings):
            current = list_jobs(settings)[0]['progress']
            seen.append((current['stage'], current['requests'], current['completed']))
        with patch('backend.sentiment.analyze_chat', new=analyze), \
                patch.dict('sys.modules', {'backend.reports': SimpleNamespace(warm_report=warm)}):
            await execute_job(job, None, settings=self.settings)
        self.assertEqual(seen, [('statistics', 7, 4)])
        final = list_jobs(self.settings)[0]['progress']
        self.assertEqual(final['stage'], 'request_limit')
        self.assertEqual(final['requests'], 7)

    async def test_flood_wait_replays_only_uncommitted_batch(self):
        class FloodWaitError(Exception):
            seconds = 0
        class InterruptedClient(FakeClient):
            async def iter_messages(inner, entity, min_id, reverse):
                inner.requests.append((min_id, reverse))
                yield message(1)
                if len(inner.requests) == 1:
                    raise FloodWaitError()
                yield message(2)
        self.client = InterruptedClient([message(1), message(2)])
        self.service.client_factory = lambda *args: self.client
        result = await self.service.sync_chat("chat")
        self.assertEqual(result["saved"], 2)
        self.assertEqual(self.client.requests, [(0, True), (0, True)])
        with connect(self.settings) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)

    async def test_wrong_account_reconnect_preserves_previous_session(self):
        session = Path(str(self.settings.telegram_session_path) + ".session")
        session.write_bytes(b"original-session")
        class PendingClient(FakeClient):
            def __init__(inner, path):
                super().__init__()
                Path(path + ".session").write_bytes(b"replacement-session")
            async def send_code_request(inner, phone):
                return SimpleNamespace(phone_code_hash="never-persist")
            async def sign_in(inner, **kwargs):
                pass
            async def get_me(inner):
                return SimpleNamespace(id=999)
        self.service.client_factory = lambda path, *args: PendingClient(path) if "pending-" in path else self.client
        await self.service.connect("+393001234567")
        with self.assertRaises(TelegramUnavailable):
            await self.service.verify("12345")
        self.assertEqual(session.read_bytes(), b"original-session")
        self.assertIsNone(self.service.pending)
        self.assertFalse(list(self.settings.data_dir.glob("telegram-pending-*")))

    async def test_successful_two_factor_reconnect_stages_then_replaces(self):
        session = Path(str(self.settings.telegram_session_path) + ".session")
        session.write_bytes(b"original-session")
        class SessionPasswordNeededError(Exception):
            pass
        class PendingClient(FakeClient):
            def __init__(inner, path):
                super().__init__()
                Path(path + ".session").write_bytes(b"replacement-session")
            async def send_code_request(inner, phone):
                return SimpleNamespace(phone_code_hash="never-persist")
            async def sign_in(inner, **kwargs):
                if "password" not in kwargs:
                    raise SessionPasswordNeededError()
        self.service.client_factory = lambda path, *args: PendingClient(path) if "pending-" in path else self.client
        result = await self.service.connect("+393001234567")
        self.assertTrue(result["code_sent"])
        result = await self.service.verify("12345")
        self.assertTrue(result["needs_password"])
        self.assertEqual(session.read_bytes(), b"original-session")
        result = await self.service.verify("", password="ephemeral-2fa")
        self.assertTrue(result["connected"])
        self.assertEqual(session.read_bytes(), b"replacement-session")
        self.assertEqual(session.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.service.pending)
        for path in self.settings.data_dir.iterdir():
            if path.is_file() and path.suffix not in {".sqlite3", ".db"}:
                self.assertNotIn(b"ephemeral-2fa", path.read_bytes())


if __name__ == "__main__":
    unittest.main()

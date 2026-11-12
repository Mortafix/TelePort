"""Security and snapshot regressions exercised through real HTTP endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.auth import COOKIE_NAME, encrypt_password, hash_password, verify_password
from backend.db import connect, initialize
from backend.main import _date_bounds, create_app
from backend.settings import Settings


@pytest.fixture
def settings(tmp_path):
    value = Settings(data_dir=tmp_path, admin_password_hash=hash_password("admin-test-password"), run_worker=False)
    initialize(value)
    with connect(value) as connection:
        connection.execute("INSERT INTO chats(id,title,password_encrypted) VALUES (?,?,?)", ("a", "Chat A", encrypt_password("alpha", value)))
        connection.execute("INSERT INTO chats(id,title,password_encrypted) VALUES (?,?,?)", ("b", "Chat B", encrypt_password("beta", value)))
        connection.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) VALUES (?,?,?,?,?,?,?)", ("a", 1, "1", "Alice", "text", "ciao ciao", "2026-10-24T10:00:00+00:00"))
    return value


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as value:
        yield value


def csrf(client):
    return {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}


def test_api_responses_are_not_indexable_or_cacheable(client):
    for path in ["/api/health", "/api/session", "/api/chats/a/messages", "/api/missing"]:
        response = client.get(path)
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/openapi.json").headers["X-Robots-Tag"] == "noindex, nofollow"


def test_built_public_home_and_publication_assets(client):
    from pathlib import Path
    if not (Path(__file__).resolve().parents[1] / "frontend/dist/index.html").is_file():
        pytest.skip("Build frontend before checking the production entry point")
    home = client.get("/")
    assert home.status_code == 200
    assert 'href="https://teleport.moris.dev/"' in home.text
    assert "Le conversazioni diventano ricordi." in home.text
    assert "Chat A" not in home.text and "Alice" not in home.text
    assert "X-Robots-Tag" not in home.headers
    for asset in ["/favicon.ico", "/favicon-32.png", "/apple-touch-icon.png", "/brand/dialogo.webp", "/brand/social-preview.png", "/robots.txt", "/sitemap.xml", "/site.webmanifest"]:
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert "text/html" not in response.headers["content-type"], asset
    assert client.get("/sitemap.xml").text.count("<loc>") == 1
    missing = client.get("/pagina-inesistente")
    assert missing.status_code == 404
    assert missing.headers["X-Robots-Tag"] == "noindex, nofollow"


def login(client):
    response = client.post("/api/admin/login", json={"password": "admin-test-password"}, headers=csrf(client))
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def test_archive_overview_is_admin_only_and_excludes_removed_content(client, settings):
    assert client.get('/api/admin/overview').status_code == 401
    client.post('/api/unlock', json={'password':'alpha'}, headers=csrf(client))
    assert client.get('/api/admin/overview').status_code == 403
    login(client)
    with connect(settings) as db:
        db.execute("UPDATE chats SET archived=1 WHERE id='b'")
        db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,text,date,deleted) VALUES('a',2,'1','Alice','removed','2026-10-24T11:00:00+00:00',1)")
        db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,text,date) VALUES('b',1,'2','Bob','archived','2026-10-24T11:00:00+00:00')")
    # Build moments before reading aggregate counts; the endpoint must not multiply rows.
    from backend.analytics import rebuild_moments
    rebuild_moments('a',settings)
    data=client.get('/api/admin/overview').json()
    assert (data['conversations'],data['messages'],data['protected']) == (1,1,1)
    assert (data['moments'],data['analyzed'],data['pending']) == (1,0,1)
    assert 'text' not in data and 'password' not in data


def test_moment_gap_is_configured_on_server_for_existing_and_new_chats(settings):
    settings.moment_gap_minutes=45
    initialize(settings)
    with connect(settings) as db:
        assert db.execute("SELECT gap_minutes FROM chats WHERE id='a'").fetchone()[0] == 45
        assert db.execute("SELECT version FROM chats WHERE id='a'").fetchone()[0] == 1
        db.execute("INSERT INTO chats(id,title) VALUES('new','New')")
        assert db.execute("SELECT gap_minutes FROM chats WHERE id='new'").fetchone()[0] == 45
    initialize(settings)
    with connect(settings) as db:
        assert db.execute("SELECT version FROM chats WHERE id='a'").fetchone()[0] == 1


def test_hashes_and_ciphertext(settings):
    digest = hash_password("example")
    assert digest != "example"
    assert verify_password("example", digest)
    assert not verify_password("wrong", digest)
    assert not verify_password("example", "broken")
    with connect(settings) as connection:
        stored = connection.execute("SELECT password_encrypted FROM chats WHERE id='a'").fetchone()[0]
    assert "alpha" not in stored
    assert (settings.data_dir / "encryption.key").stat().st_mode & 0o777 == 0o600


def test_every_private_content_route_checks_chat_scope(client):
    for path in ["report", "messages", "moments", "search?q=ciao"]:
        assert client.get("/api/chats/a/" + path).status_code == 403
    assert client.get("/api/admin/chats/a/password").status_code == 401
    response = client.post("/api/chats/a/unlock", json={"password": "alpha"}, headers=csrf(client))
    assert response.status_code == 200
    assert client.get("/api/chats/a/messages").json()["total"] == 1
    assert client.get("/api/chats/b/messages").status_code == 403
    assert client.get("/api/admin/chats/a/password").status_code == 403


def test_login_requires_csrf_and_rejects_foreign_origins(client):
    assert client.post("/api/admin/login", json={"password": "admin-test-password"}).status_code == 401
    headers = csrf(client)
    assert client.post("/api/admin/login", json={"password": "admin-test-password"}).status_code == 403
    assert client.post("/api/admin/login", json={"password": "admin-test-password"}, headers={**headers, "Origin": "https://other.example"}).status_code == 403
    assert client.get("/api/session").headers["cache-control"] == "no-store"


def test_admin_bypasses_chat_password_and_rotates_session(client):
    before = client.get("/api/session").json()
    cookie_before = client.cookies[COOKIE_NAME]
    headers = login(client)
    assert client.cookies[COOKIE_NAME] != cookie_before
    assert headers["X-CSRF-Token"] != before["csrf_token"]
    assert client.get("/api/chats/b/messages").status_code == 200
    assert client.get("/api/admin/chats/a/password").json() == {"password": "alpha"}
    old_cookie = client.cookies[COOKIE_NAME]
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/chats/a/messages").status_code == 403
    client.cookies.set(COOKIE_NAME, old_cookie, domain="testserver.local", path="/")
    assert not client.get("/api/session").json()["admin"]


def test_shared_password_rotation_revokes_guest_unlock(settings, client):
    headers = login(client)
    with TestClient(create_app(settings)) as guest:
        assert guest.post("/api/chats/a/unlock", json={"password": "alpha"}, headers=csrf(guest)).status_code == 200
        assert guest.get("/api/chats/a/messages").status_code == 200
        response = client.post("/api/admin/chats/a/password", json={"password": "new-secret"}, headers=headers)
        assert response.status_code == 200
        assert guest.get("/api/chats/a/messages").status_code == 403
        assert client.get("/api/admin/chats/a/password").json()["password"] == "new-secret"


def test_unicode_shared_password_works(settings, client):
    with connect(settings) as connection:
        connection.execute("UPDATE chats SET password_encrypted=? WHERE id='a'", (encrypt_password("caffè-☕", settings),))
    response = client.post("/api/chats/a/unlock", json={"password": "caffè-☕"}, headers=csrf(client))
    assert response.status_code == 200


def test_failed_logins_are_throttled(client):
    headers = csrf(client)
    for _ in range(5):
        assert client.post("/api/admin/login", json={"password": "wrong"}, headers=headers).status_code == 401
    assert client.post("/api/admin/login", json={"password": "wrong"}, headers=headers).status_code == 429


def test_guest_logout_also_revokes_chat_unlock(client):
    headers = csrf(client)
    assert client.post("/api/chats/a/unlock", json={"password": "alpha"}, headers=headers).status_code == 200
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/chats/a/messages").status_code == 403


def test_keyword_occurrences_are_not_substrings_and_include_repeats(client):
    login(client)
    assert client.get("/api/chats/a/search?q=ciao").json()["total"] == 2
    assert client.get("/api/chats/a/search?q=cia").json()["total"] == 0


def test_keyword_occurrences_preserve_apostrophes_and_sentence_boundaries(client, settings):
    login(client)
    with connect(settings) as connection:
        connection.execute("UPDATE messages SET text=? WHERE chat_id='a'", ("L'acqua è fresca. Acqua buona! bella. giornata",))
    assert client.get("/api/chats/a/search", params={"q": "acqua"}).json()["total"] == 1
    assert client.get("/api/chats/a/search", params={"q": "l'acqua"}).json()["total"] == 1
    assert client.get("/api/chats/a/search", params={"q": "bella giornata"}).json()["total"] == 0


def test_keyword_counts_follow_selected_report_dates(client, settings):
    login(client)
    with connect(settings) as connection:
        connection.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) VALUES (?,?,?,?,?,?,?)", ("a", 2, "1", "Alice", "text", "ciao", "2026-10-25T10:00:00+00:00"))
    assert client.get("/api/chats/a/search?q=ciao").json()["total"] == 3
    response = client.get("/api/chats/a/search?q=ciao&start=2026-10-25&end=2026-10-25")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["by_sender"][0]["count"] == 1
    assert client.get("/api/chats/a/search?q=ciao&start=invalid").status_code == 422


def test_local_day_handles_dst(settings):
    start, end = _date_bounds("2026-10-25", "2026-10-25", settings)
    assert (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() == 25 * 3600
    start, end = _date_bounds("2026-03-29", "2026-03-29", settings)
    assert (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() == 23 * 3600


def test_cache_refreshes_after_data_version_changes(client, settings):
    login(client)
    first = client.get("/api/chats/a/report")
    assert first.status_code == 200
    with connect(settings) as connection:
        cached_before = connection.execute("SELECT report FROM report_cache WHERE chat_id='a'").fetchone()[0]
        connection.execute("UPDATE messages SET text='nuovo messaggio decisamente diverso' WHERE chat_id='a'")
        connection.execute("UPDATE chats SET version=version+1 WHERE id='a'")
    assert client.get("/api/chats/a/report").status_code == 200
    with connect(settings) as connection:
        cached_after = connection.execute("SELECT report FROM report_cache WHERE chat_id='a'").fetchone()[0]
    assert json.loads(cached_before) != json.loads(cached_after)


def test_invalid_ranges_and_cross_chat_moments_are_rejected(client, settings):
    login(client)
    assert client.get("/api/chats/a/report?start=2026-11-01&end=2026-10-01").status_code == 422
    with connect(settings) as connection:
        connection.execute("INSERT INTO moments(id,chat_id,start,end,message_ids,fingerprint) VALUES ('b-moment','b','2026-01-01','2026-01-01','[1]','x')")
    assert client.get("/api/chats/a/messages?moment_id=b-moment").status_code == 404


def _many_moments(settings):
    from backend.analytics import rebuild_moments
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with connect(settings) as connection:
        for index in range(225):
            connection.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) VALUES (?,?,?,?,?,?,?)", ("a", index + 2, "1", "Alice", "text", "messaggio sintetico", (start + timedelta(hours=index)).isoformat()))
        connection.execute("UPDATE chats SET version=version+1 WHERE id='a'")
    rebuild_moments("a", settings)
    with connect(settings) as connection:
        rows = connection.execute("SELECT id FROM moments WHERE chat_id='a' ORDER BY start").fetchall()
        for index, row in enumerate(rows):
            if index % 3 == 0:
                connection.execute("UPDATE moments SET analysis=? WHERE id=?", (json.dumps({"label": "positive", "emotions": [], "participants": []}), row["id"]))
        connection.execute("UPDATE chats SET version=version+1 WHERE id='a'")
    return len(rows)


def test_report_keeps_complete_statistics_but_bounds_initial_moments(client, settings):
    count = _many_moments(settings)
    login(client)
    response = client.get("/api/chats/a/report")
    assert response.status_code == 200
    report = response.json()
    assert report["summary"]["total_messages"] == 226
    assert report["summary"]["moment_count"] == count
    assert report["moments_total"] == count
    assert len(report["moments"]) == 100
    assert all("message_ids" not in moment and "fingerprint" not in moment for moment in report["moments"])
    assert report["moments"][0]["start"] > report["moments"][-1]["start"]
    assert client.get("/api/chats/a/report").json() == report  # Same compact contract on cache hits.


def test_moments_pages_cover_entire_archive_with_tone_and_date_filters(client, settings):
    count = _many_moments(settings)
    login(client)
    items = []
    for offset in range(0, count, 100):
        response = client.get("/api/chats/a/moments", params={"limit": 100, "offset": offset})
        assert response.status_code == 200
        page = response.json()
        assert page["total"] == count
        assert all("message_ids" not in moment and "fingerprint" not in moment for moment in page["items"])
        items.extend(page["items"])
    assert len(items) == len({item["id"] for item in items}) == count
    assert items[0]["start"] > items[-1]["start"]
    positive = client.get("/api/chats/a/moments?tone=positive&limit=500").json()
    pending = client.get("/api/chats/a/moments?tone=pending&limit=500").json()
    assert positive["total"] + pending["total"] == count
    assert all(item["analysis"]["label"] == "positive" for item in positive["items"])
    assert all(item["analysis"] is None for item in pending["items"])
    assert client.get("/api/chats/a/moments?tone=uncertain").json()["total"] == 0
    day = client.get("/api/chats/a/moments?start=2026-10-24&end=2026-10-24").json()
    assert day["total"] == 1
    for query in ["tone=invalid", "limit=0", "limit=501", "offset=-1"]:
        assert client.get("/api/chats/a/moments?" + query).status_code == 422


def test_messages_in_a_very_large_moment_are_still_paginated(client, settings):
    login(client)
    with connect(settings) as connection:
        connection.execute("INSERT INTO moments(id,chat_id,start,end,message_ids,fingerprint) VALUES (?,?,?,?,?,?)", ("large", "a", "2026-10-24T00:00:00+00:00", "2026-10-25T00:00:00+00:00", json.dumps(list(range(1, 40001))), "large"))
    response = client.get("/api/chats/a/messages?moment_id=large&limit=10")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [message["id"] for message in response.json()["items"]] == [1]


def test_background_warming_reuses_the_exact_public_report_cache(client, settings, monkeypatch):
    from backend.reports import warm_report, report_cache_key
    import backend.analytics as analytics
    count = _many_moments(settings)
    warmed = warm_report("a", settings)
    assert warmed["moments_total"] == count
    assert len(warmed["moments"]) == 100
    assert "message_ids" not in warmed["moments"][0]
    assert warmed["chat"]["id"] == "a"
    with connect(settings) as connection:
        row = connection.execute("SELECT * FROM report_cache WHERE chat_id='a'").fetchone()
    assert row["cache_key"] == report_cache_key(settings)
    def fail_if_rebuilt(*args, **kwargs):
        raise AssertionError("A warmed report must not rebuild statistics on first read")
    monkeypatch.setattr(analytics, "build_report", fail_if_rebuilt)
    assert warm_report("a", settings) == warmed
    login(client)
    response = client.get("/api/chats/a/report")
    assert response.status_code == 200
    from backend.customization import decorate_report
    with connect(settings) as db:
        chat = db.execute("SELECT * FROM chats WHERE id='a'").fetchone()
    assert response.json() == decorate_report(warmed, chat, settings)
    assert warm_report("missing-chat", settings) is None


def test_warmer_never_caches_a_report_if_data_changed_during_calculation(settings, monkeypatch):
    from backend.reports import warm_report
    import backend.analytics as analytics
    original = analytics.build_report
    def concurrent_update(*args, **kwargs):
        with connect(settings) as connection:
            connection.execute("UPDATE chats SET version=version+1 WHERE id='a'")
        return original(*args, **kwargs)
    monkeypatch.setattr(analytics, "build_report", concurrent_update)
    assert warm_report("a", settings)["summary"]["total_messages"] == 1
    with connect(settings) as connection:
        assert connection.execute("SELECT COUNT(*) FROM report_cache WHERE chat_id='a'").fetchone()[0] == 0


def test_profile_appearance_archive_and_estimate_are_protected(client,settings):
    assert client.get('/api/admin/chats/a/estimate').status_code in (401,403)
    csrf=login(client)
    headers={'x-csrf-token':csrf} if isinstance(csrf,str) else {'x-csrf-token':client.get('/api/session').json()['csrf_token']}
    assert client.post('/api/admin/profile',json={'name':'Moris','image':''},headers=headers).status_code==200
    appearance=client.get('/api/admin/chats/a/appearance').json()
    owner=appearance['participants'][0]['id']
    response=client.post('/api/admin/chats/a/appearance',json={'title':'Nuovo titolo','kind':'direct','image':'','self_sender_id':owner,'colors':{owner:'#b76d8b'}},headers=headers)
    assert response.status_code==200
    report=client.get('/api/chats/a/report').json()
    assert report['participants'][0]['name']=='Moris'
    assert report['participants'][0]['color']=='#b76d8b'
    assert client.get('/api/admin/chats/a/estimate').json()['approximate'] is True
    assert client.post('/api/admin/profile',json={'name':'Moris','image':'data:image/svg+xml;base64,YQ=='},headers=headers).status_code==422
    assert client.post('/api/admin/chats/a/remove',json={},headers=headers).status_code==200
    assert client.get('/api/chats/a/report').status_code==404
    assert 'a' not in [c['id'] for c in client.get('/api/chats').json()['items']]
    assert client.post('/api/admin/chats/a/restore',json={},headers=headers).status_code==200
    assert client.get('/api/chats/a/report').status_code==200


def test_anonymous_and_guests_cannot_enumerate_conversations(client,settings):
    with connect(settings) as db:
        db.execute("INSERT INTO chats(id,title,username) VALUES('public','Nome riservato','username_privato')")
    assert client.get('/api/chats').json()=={'items':[]}
    assert client.get('/api/chats/public/messages').status_code==403
    for path in ['report','messages','moments','search?q=ciao']:
        assert client.get('/api/chats/public/'+path).status_code==403
    unlocked=client.post('/api/unlock',json={'password':'alpha'},headers=csrf(client))
    assert unlocked.status_code==200
    assert [c['id'] for c in client.get('/api/chats').json()['items']]==['a']
    assert 'Chat B' not in client.get('/api/chats').text
    headers=login(client)
    assert {c['id'] for c in client.get('/api/chats').json()['items']}=={'a','b','public'}
    client.post('/api/logout',headers=headers)
    assert client.get('/api/chats').json()=={'items':[]}


def test_password_discovery_throttles_across_sessions_and_chat_ids(client,settings):
    from fastapi.testclient import TestClient
    for i in range(5):
        with TestClient(create_app(settings)) as guest:
            endpoint='/api/unlock' if i%2==0 else '/api/chats/unknown/unlock'
            response=guest.post(endpoint,json={'password':'wrong'},headers=csrf(guest))
            assert response.status_code==401
            assert response.json()['detail']=='Password non valida o accesso non disponibile.'
    assert client.post('/api/unlock',json={'password':'alpha'},headers=csrf(client)).status_code==429


def test_successful_discovery_does_not_erase_failed_attempts(client):
    headers=csrf(client)
    for _ in range(4):
        assert client.post('/api/unlock',json={'password':'wrong'},headers=headers).status_code==401
    assert client.post('/api/unlock',json={'password':'alpha'},headers=headers).status_code==200
    assert client.post('/api/unlock',json={'password':'wrong'},headers=headers).status_code==401
    assert client.post('/api/unlock',json={'password':'beta'},headers=headers).status_code==429


def test_context_search_is_scoped_to_authorized_chat(client,settings):
    login(client)
    with connect(settings) as db:
        db.execute("INSERT INTO moments(id,chat_id,start,end,message_ids,fingerprint,analysis) VALUES('ctx-a','a','2026-10-24T10:00:00+00:00','2026-10-24T10:00:00+00:00','[1]','x',?)",(json.dumps({'label':'neutral','context':'Organizzazione di un viaggio in montagna','context_parts':['Prenotazione del rifugio']}),))
        db.execute("INSERT INTO moments(id,chat_id,start,end,message_ids,fingerprint,analysis) VALUES('ctx-b','b','2026-10-24T10:00:00+00:00','2026-10-24T10:00:00+00:00','[]','x',?)",(json.dumps({'context':'Tema riservato esclusivo'}),))
    assert client.get('/api/chats/a/moments?q=montagna').json()['total']==1
    assert client.get('/api/chats/a/moments?q=rifugio').json()['total']==1
    assert client.get('/api/chats/a/moments?q=esclusivo').json()['total']==0


def test_reply_preview_never_crosses_chat_or_exposes_deleted_text(client,settings):
    login(client)
    with connect(settings) as db:
        db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) VALUES('b',1,'2','B','text','Segreto altra chat','2026-10-24T09:00:00+00:00')")
        db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date,reply_to_id) VALUES('a',2,'1','Alice','text','Risposta','2026-10-24T10:01:00+00:00',1)")
    reply=client.get('/api/chats/a/messages?offset=1&limit=1').json()['items'][0]['reply_to']
    assert reply['text']=='ciao ciao'
    with connect(settings) as db: db.execute("UPDATE messages SET deleted=1 WHERE chat_id='a' AND id=1")
    assert client.get('/api/chats/a/messages').json()['items'][0]['reply_to'] is None


def test_legacy_owner_selection_and_colors_are_remapped(client,settings):
    login(client)
    with connect(settings) as db:
        db.execute("UPDATE chats SET appearance=? WHERE id='a'",(json.dumps({'self_sender_id':'legacy:Alice','colors':{'legacy:Alice':'#b76d8b','legacy:Missing':'#3159d9'}}),))
    data=client.get('/api/admin/chats/a/appearance').json()
    assert data['self_sender_id']=='1'
    assert data['colors']=={'1':'#b76d8b'}
    assert client.post('/api/admin/chats/a/appearance',json={k:data.get(k) for k in ['title','kind','self_sender_id','colors']}|{'image':''},headers=csrf(client)).status_code==200
    assert client.get('/api/chats/a/report').json()['participants'][0]['is_self'] is True

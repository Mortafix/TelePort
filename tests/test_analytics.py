from datetime import datetime, timezone
import json

from backend.analytics import build_report, segment_messages, rebuild_moments
from backend.db import connect, initialize
from backend.settings import Settings


def message(id, text="ciao mondo", date=None, sender="a"):
    return {"id":id,"chat_id":"test","sender_id":sender,"sender_name":sender,
        "text":text,"type":"text","date":date or f"2026-10-24T10:{id:02}:00+00:00"}


def test_order_calendar_denominator_and_real_phrase_boundaries():
    data = [message(3,"davvero bene", "2026-10-26T10:00:00+00:00"),
        message(1,"molto felice", "2026-10-24T10:00:00+00:00"),
        message(2,"molto bene", "2026-10-24T10:01:00+00:00", "b")]
    report = build_report(data, segment_messages(data))
    assert report["summary"]["first_message"] == data[1]["date"]
    assert report["summary"]["calendar_days"] == 3
    assert report["summary"]["mean_per_calendar_day"] == 1
    assert report["summary"]["mean_per_active_day"] == 1.5
    phrases = {p["value"] for p in report["phrases"]["bigrams"]}
    assert "molto bene" in phrases
    assert "felice molto" not in phrases
    assert report["activity"]["reply_times"][0]["median_seconds"] == 60


def test_sessions_cross_midnight_and_split_only_after_inactivity():
    messages = [message(1,date="2026-10-24T21:55:00+00:00"),
        message(2,date="2026-10-24T22:05:00+00:00"),
        message(3,date="2026-10-24T22:40:00+00:00")]
    moments = segment_messages(messages)
    assert [m["message_ids"] for m in moments] == [[1,2],[3]]
    assert build_report(messages,moments)["summary"]["calendar_days"] == 2


def test_empty_media_only_and_unicode_emojis():
    assert build_report([],[])["summary"]["total_messages"] == 0
    report = build_report([message(1,"👩🏽‍💻 👩🏽‍💻 ❤️")], [])
    assert report["emojis"]["top"][0] == {"value":"👩🏽‍💻","count":2}
    assert "❤️" in report["emojis"]["singletons"]
    assert build_report([message(1,"")],[])["words"]["top"] == []


def test_identity_separate_even_when_display_names_match():
    messages=[message(1,sender="1"),message(2,sender="2")]
    for m in messages:
        m["sender_name"]="Andrea"
    assert len(build_report(messages,[])["participants"]) == 2


def test_fingerprints_retain_unchanged_analysis_and_invalidate_edits(tmp_path):
    settings=Settings(data_dir=tmp_path)
    initialize(settings)
    with connect(settings) as db:
        db.execute("INSERT INTO chats(id,title) VALUES('test','Test')")
        for m in [message(1),message(2)]:
            db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) VALUES(?,?,?,?,?,?,?)", tuple(m[k] for k in ['chat_id','id','sender_id','sender_name','type','text','date']))
    block=rebuild_moments('test',settings)[0]
    with connect(settings) as db:
        db.execute("UPDATE moments SET analysis=? WHERE id=?",(json.dumps({"label":"neutral"}),block['id']))
    assert rebuild_moments('test',settings)[0]['analysis']['label']=='neutral'
    with connect(settings) as db:
        db.execute("UPDATE messages SET text='modificato' WHERE id=2")
    assert rebuild_moments('test',settings)[0]['analysis'] is None


def test_streaks_and_uncertainty_not_neutral():
    data=[message(1,date='2026-10-24T10:00:00+00:00'),message(2,date='2026-10-25T10:00:00+00:00')]
    blocks=segment_messages(data)
    blocks[0]['analysis']={"label":"uncertain","emotions":[],"participants":[]}
    report=build_report(data,blocks)
    assert report['activity']['longest_streak']==2
    assert report['sentiment']['counts']['neutral']==0
    assert report['sentiment']['counts']['uncertain']==1
    assert report['sentiment']['pending_moments']==1

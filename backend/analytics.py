"""Deterministic statistics; no network, downloads or ML on the read path."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from hashlib import sha256
import json
import heapq
import re
from statistics import mean, median, stdev
from zoneinfo import ZoneInfo

import emoji

STOPWORDS = set("a ad al allo ai agli all alla alle anche ancora avere aveva avevo abbiamo avete che chi ci cio ciò col come con contro cui da dal dallo dai dagli dalla dalle degli dei del della delle dello di dove e ed è era erano essere fa fai fanno fra fu gli ha hai hanno ho i il in io la le lei li lo loro lui ma me mi mia mie miei mio ne nei nel nella nelle nello non noi o ogni per perché piu più poi quale quali quando quanto quella quelle quelli quello questa queste questi questo qui se sei senza si sia siamo siete sono su sul sulla sulle te ti tra tu tua tue tuoi tuo un una uno vi voi già molto tanto tutto tutti tutte ed sì no c'è l' d' dell' dell all' dall' nell' sull'".split())
LABELS = ("positive", "neutral", "negative", "mixed", "uncertain")
TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


def instant(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=dt_timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt_timezone.utc)


def ordered(messages):
    return sorted((dict(m) for m in messages if not dict(m).get("deleted")), key=lambda m: (instant(m["date"]), int(m["id"])))


def segment_messages(messages: list[dict], gap_minutes: int = 30) -> list[dict]:
    if not 1 <= gap_minutes <= 1440:
        raise ValueError("La pausa deve essere compresa tra 1 e 1440 minuti")
    groups = []
    for message in ordered(messages):
        if not groups or instant(message["date"]) - instant(groups[-1][-1]["date"]) > timedelta(minutes=gap_minutes):
            groups.append([])
        groups[-1].append(message)
    result = []
    for group in groups:
        chat_id = str(group[0].get("chat_id", ""))
        content = [{k: m.get(k) for k in ("id", "sender_id", "text", "type", "date", "edit_date", "reply_to_id")} for m in group]
        result.append({
            "id": sha256(f"{chat_id}:{group[0]['id']}".encode()).hexdigest()[:24],
            "chat_id": chat_id, "start": instant(group[0]["date"]).isoformat(), "end": instant(group[-1]["date"]).isoformat(),
            "message_ids": [m["id"] for m in group], "message_count": len(group),
            "duration_minutes": round((instant(group[-1]["date"]) - instant(group[0]["date"])).total_seconds() / 60, 2),
            "fingerprint": sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "analysis": None, "model": None, "analyzed_at": None,
        })
    return result


def moment_dict(row) -> dict:
    m = dict(row)
    if isinstance(m["message_ids"], str):
        m["message_ids"] = json.loads(m["message_ids"])
    if isinstance(m.get("analysis"), str):
        m["analysis"] = json.loads(m["analysis"])
    m["message_count"] = len(m["message_ids"])
    m["duration_minutes"] = round((instant(m["end"]) - instant(m["start"])).total_seconds() / 60, 2)
    return m


def rebuild_moments(chat_id: str, settings=None) -> list[dict]:
    from .db import connect
    with connect(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        chat = db.execute("SELECT gap_minutes FROM chats WHERE id=?", (chat_id,)).fetchone()
        if chat is None:
            return []
        messages = [dict(r) for r in db.execute("SELECT * FROM messages WHERE chat_id=? AND deleted=0 ORDER BY date,id", (chat_id,))]
        previous = {r["id"]: dict(r) for r in db.execute("SELECT * FROM moments WHERE chat_id=?", (chat_id,))}
        moments = segment_messages(messages, chat["gap_minutes"])
        changes_before = db.total_changes
        for m in moments:
            old = previous.pop(m["id"], None)
            if old and old["fingerprint"] == m["fingerprint"]:
                continue
            db.execute("""INSERT INTO moments(id,chat_id,start,end,message_ids,fingerprint)
                VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                start=excluded.start,end=excluded.end,message_ids=excluded.message_ids,
                fingerprint=excluded.fingerprint,analysis=NULL,model=NULL,analyzed_at=NULL""",
                (m["id"], chat_id, m["start"], m["end"], json.dumps(m["message_ids"]), m["fingerprint"]))
        db.executemany("DELETE FROM moments WHERE id=?", [(id_,) for id_ in previous])
        if db.total_changes != changes_before:
            db.execute("UPDATE chats SET version=version+1 WHERE id=?", (chat_id,))
        return [moment_dict(r) for r in db.execute("SELECT * FROM moments WHERE chat_id=? ORDER BY start", (chat_id,))]


def ranked(counter, limit=100):
    return [{"value": value, "count": count} for value, count in heapq.nsmallest(limit, counter.items(), key=lambda item: (-item[1], item[0]))]


def lexical(messages):
    words, bigrams, trigrams, emojis = Counter(), Counter(), Counter(), Counter()
    for message in messages:
        text = message.get("text") or ""
        emojis.update(e["emoji"] for e in emoji.emoji_list(text))
        words.update(t for t in TOKEN.findall(text.lower()) if t not in STOPWORDS)
        # Genuine contiguous phrases, never across messages or removed stopwords.
        for sentence in re.split(r"[.!?;\n]+", text.lower()):
            tokens = TOKEN.findall(sentence)
            for n, counter in ((2, bigrams), (3, trigrams)):
                counter.update(" ".join(tokens[i:i+n]) for i in range(len(tokens)-n+1) if any(t not in STOPWORDS for t in tokens[i:i+n]))
    return words, bigrams, trigrams, emojis


def build_report(messages: list[dict], moments: list[dict], timezone="Europe/Rome") -> dict:
    tz = ZoneInfo(timezone)
    data = ordered(messages)
    moments = [moment_dict(m) for m in moments]
    groups = defaultdict(list)
    years, months, days = (defaultdict(Counter) for _ in range(3))
    heat = Counter()
    by_id = {m["id"]: m for m in data}
    for m in data:
        sender = str(m["sender_id"])
        groups[sender].append(m)
        date = instant(m["date"]).astimezone(tz)
        years[date.strftime("%Y")][sender] += 1
        months[date.strftime("%Y-%m")][sender] += 1
        days[date.strftime("%Y-%m-%d")][sender] += 1
        heat[(date.weekday(), date.hour)] += 1
    def series(values):
        return [{"date": date, "total": sum(counts.values()), "by_sender": dict(counts)} for date, counts in sorted(values.items())]
    first = instant(data[0]["date"]).astimezone(tz) if data else None
    last = instant(data[-1]["date"]).astimezone(tz) if data else None
    calendar_days = (last.date() - first.date()).days + 1 if data else 0
    lengths = [len(m.get("text") or "") for m in data]
    streak, longest_streak, previous_day = 0, 0, None
    for day in sorted(days):
        current = datetime.fromisoformat(day).date()
        streak = streak + 1 if previous_day and current - previous_day == timedelta(days=1) else 1
        longest_streak = max(streak, longest_streak)
        previous_day = current
    initiators, replies = Counter(), defaultdict(list)
    for block in moments:
        subset = [by_id[id_] for id_ in block["message_ids"] if id_ in by_id]
        if subset:
            initiators[str(subset[0]["sender_id"])] += 1
        for prev, cur in zip(subset, subset[1:]):
            if prev["sender_id"] != cur["sender_id"]:
                delay = (instant(cur["date"]) - instant(prev["date"])).total_seconds()
                if delay >= 0:
                    replies[str(cur["sender_id"])].append(delay)
    per_lexical = {sender: lexical(msgs) for sender, msgs in groups.items()}
    words, bigrams, trigrams, emojis = (Counter() for _ in range(4))
    for counters in per_lexical.values():
        for total, counter in zip((words, bigrams, trigrams, emojis), counters):
            total.update(counter)
    def longest(msgs):
        return heapq.nsmallest(10, (m for m in msgs if m.get("text")), key=lambda m: (-len(m["text"]), instant(m["date"]), m["id"]))
    sentiments, per_sentiments, emotions, per_emotions = Counter(), defaultdict(Counter), Counter(), defaultdict(Counter)
    tone_days = defaultdict(Counter)
    for m in moments:
        analysis = m.get("analysis")
        if not analysis:
            continue
        label = analysis.get("label", "uncertain")
        sentiments[label] += 1
        tone_days[instant(m["start"]).astimezone(tz).strftime("%Y-%m-%d")][label] += 1
        emotions.update(analysis.get("emotions", []))
        for p in analysis.get("participants", []):
            sender = str(p.get("speaker", ""))
            per_sentiments[sender][p.get("label", "uncertain")] += 1
            per_emotions[sender].update(p.get("emotions", []))
    return {
        "summary": {"total_messages": len(data), "total_chars": sum(lengths), "mean_length": mean(lengths) if lengths else 0,
            "std_length": stdev(lengths) if len(lengths) > 1 else 0, "calendar_days": calendar_days, "active_days": len(days),
            "mean_per_calendar_day": len(data)/calendar_days if calendar_days else 0, "mean_per_active_day": len(data)/len(days) if days else 0,
            "first_message": data[0]["date"] if data else None, "last_message": data[-1]["date"] if data else None,
            "moment_count": len(moments), "median_moment_minutes": median([m["duration_minutes"] for m in moments]) if moments else 0,
            "longest_streak": longest_streak},
        "participants": [{"id": sender, "name": msgs[-1]["sender_name"], "count": len(msgs),
            "characters": sum(len(m.get("text") or "") for m in msgs), "mean_length": mean(len(m.get("text") or "") for m in msgs),
            "std_length": stdev([len(m.get("text") or "") for m in msgs]) if len(msgs)>1 else 0,
            "started_moments": initiators[sender], "median_reply_seconds": median(replies[sender]) if replies[sender] else None}
            for sender, msgs in groups.items()],
        "frequency": {"years": series(years), "months": series(months), "days": series(days),
            "top_days": sorted(series(days), key=lambda v: (-v["total"], v["date"]))[:3],
            "top_month": max(series(months), key=lambda v: v["total"], default=None), "top_year": max(series(years), key=lambda v: v["total"], default=None)},
        "types": {"counts": dict(Counter(m["type"] for m in data)), "by_sender": {s: dict(Counter(m["type"] for m in msgs)) for s, msgs in groups.items()},
            "dominant": Counter(m["type"] for m in data).most_common(1)[0][0] if data else None},
        "lengths": {"longest": longest(data), "by_sender": {s: longest(msgs) for s, msgs in groups.items()}},
        "words": {"top": ranked(words), "by_sender": {s: ranked(v[0]) for s, v in per_lexical.items()}, "over_50": sum(c > 50 for c in words.values())},
        "phrases": {"bigrams": ranked(bigrams), "trigrams": ranked(trigrams),
            "by_sender": {s: {"bigrams": ranked(v[1]), "trigrams": ranked(v[2])} for s, v in per_lexical.items()},
            "over_50": sum(c > 50 for c in bigrams.values()) + sum(c > 50 for c in trigrams.values())},
        "emojis": {"top": ranked(emojis), "by_sender": {s: ranked(v[3]) for s, v in per_lexical.items()},
            "over_50": sum(c > 50 for c in emojis.values()), "singletons": sorted(e for e, n in emojis.items() if n == 1)},
        "sentiment": {"counts": {label: sentiments[label] for label in LABELS}, "by_sender": {s: dict(v) for s,v in per_sentiments.items()},
            "emotions": dict(emotions), "emotions_by_sender": {s: dict(v) for s,v in per_emotions.items()},
            "analyzed_moments": sum(sentiments.values()), "pending_moments": len(moments)-sum(sentiments.values()),
            "timeline": [{"date": day, **{label: counts[label] for label in LABELS}} for day, counts in sorted(tone_days.items())]},
        "activity": {"heatmap": [{"weekday": day, "hour": hour, "count": heat[(day,hour)]} for day in range(7) for hour in range(24)],
            "initiators": dict(initiators), "reply_times": [{"sender_id": s, "name": groups[s][-1]["sender_name"],
                "median_seconds": median(v), "samples": len(v)} for s,v in replies.items() if v], "longest_streak": longest_streak},
        "moments": moments,
    }

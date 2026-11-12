"""Authorized plain-text exports, streamed without loading message bodies into memory."""
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from .customization import chat_people
from .db import connect


def install(app, settings, chat_access):
    @app.get('/api/chats/{chat_id}/download')
    def download(chat_id: str, request: Request, moment_id: str | None = None):
        chat = chat_access(chat_id, request)
        people, _ = chat_people(chat, settings)
        names = {person['id']: person['name'] for person in people}
        zone = ZoneInfo(settings.timezone)
        moment=None
        if moment_id:
            with connect(settings) as db:
                moment=db.execute('SELECT start,end FROM moments WHERE chat_id=? AND id=?',(chat_id,moment_id)).fetchone()
            if moment is None:raise HTTPException(404,'Momento non trovato.')
        types = {'photo':'Foto', 'video':'Video', 'audio':'Audio', 'voice':'Messaggio vocale',
                 'sticker':'Sticker', 'document':'Documento', 'contact':'Contatto',
                 'location':'Posizione', 'service':'Messaggio di servizio'}

        def content():
            yield f"{chat['title']}\nFuso orario: {settings.timezone}\n\n"
            if moment:
                start=datetime.fromisoformat(moment['start']).astimezone(zone).strftime('%d/%m/%Y %H:%M:%S')
                end=datetime.fromisoformat(moment['end']).astimezone(zone).strftime('%d/%m/%Y %H:%M:%S')
                yield f"Momento: {start} — {end}\n\n"
            with connect(settings, check_same_thread=False) as db:
                db.execute('BEGIN')
                rows = db.execute('''SELECT m.*,r.text AS reply_text,r.type AS reply_type,
                    r.sender_id AS reply_sender FROM messages m LEFT JOIN messages r
                    ON r.chat_id=m.chat_id AND r.id=m.reply_to_id AND r.deleted=0
                    WHERE m.chat_id=? AND m.deleted=0''' + (" AND m.id IN (SELECT value FROM moments selected,json_each(selected.message_ids) WHERE selected.id=? AND selected.chat_id=?)" if moment_id else '') + ' ORDER BY m.date,m.id', (chat_id,moment_id,chat_id) if moment_id else (chat_id,))
                found = False
                parts=[];size=0
                for message in rows:
                    found = True
                    when = datetime.fromisoformat(message['date']).astimezone(zone).strftime('%d/%m/%Y %H:%M:%S')
                    author = names.get(message['sender_id'], 'Partecipante non disponibile')
                    entry=[f"[{when}] {author}\n"]
                    if message['reply_sender'] is not None:
                        original = message['reply_text'] or f"[{types.get(message['reply_type'],'Allegato')}]"
                        preview = ' '.join(original.split())[:300]
                        entry.append(f"> {names.get(message['reply_sender'],'Partecipante non disponibile')}: {preview}\n")
                    if message['type'] != 'text':
                        entry.append(f"[{types.get(message['type'],'Allegato')}]\n")
                    if message['text']:
                        entry.append(message['text']+'\n')
                    parts.extend(entry+['\n']);size+=sum(map(len,entry))+1
                    if size>=65536:
                        yield ''.join(parts)
                        parts=[];size=0
                if parts:yield ''.join(parts)
                if not found:
                    yield 'Nessun messaggio.\n'

        suffix=' - '+datetime.fromisoformat(moment['start']).astimezone(zone).strftime('%Y-%m-%d %H-%M') if moment else ''
        filename = quote((chat['title'] or 'conversazione')[:120] + suffix + '.txt', safe='')
        return StreamingResponse(content(), media_type='text/plain; charset=utf-8', headers={
            'Content-Disposition': f"attachment; filename=conversation.txt; filename*=UTF-8''{filename}",
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        })

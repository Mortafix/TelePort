"""Profile and conversation appearance. Images are bounded raster data, never executable SVG."""
import base64
import json
import re
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from .db import connect


def profile(settings):
    with connect(settings) as db:
        row=db.execute('SELECT data FROM profile WHERE id=1').fetchone()
    return json.loads(row[0]) if row else {'name':'Moris','image':''}


def validate_image(value):
    if not value:
        return ''
    match=re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)', value)
    if not match or len(value)>2800000:
        raise HTTPException(422,'Usa un’immagine PNG, JPEG o WebP di massimo 2 MB.')
    try:
        raw=base64.b64decode(match[2],validate=True)
    except ValueError:
        raise HTTPException(422,'Immagine non valida.')
    valid=(raw.startswith(b'\x89PNG\r\n\x1a\n') if match[1]=='png' else raw.startswith(b'\xff\xd8\xff') if match[1]=='jpeg' else raw[:4]==b'RIFF' and raw[8:12]==b'WEBP')
    if not valid or len(raw)>2000000:
        raise HTTPException(422,'Immagine non valida o troppo grande.')
    return value


def resolve_appearance(chat,people,settings):
    data=json.loads(chat['appearance'])
    ids={str(p['id']) for p in people}
    by_name={}
    for p in people: by_name.setdefault(p['name'],[]).append(str(p['id']))
    def resolve(value):
        value = str(value or '')
        if value in ids: return value
        if value and value.startswith('legacy:'):
            matches=by_name.get(value.removeprefix('legacy:'),[])
            if len(matches)==1:return matches[0]
        return ''
    data['self_sender_id']=resolve(data.get('self_sender_id',''))
    if not data['self_sender_id']:
        try: account=(settings.data_dir/'telegram-account-id').read_text().strip()
        except OSError: account='me'
        if account in ids: data['self_sender_id']=account
    colors={}
    for old,color in data.get('colors',{}).items():
        current=resolve(old)
        if current:colors[current]=color
    for index,p in enumerate(sorted(people,key=lambda p:str(p['id']))):
        colors.setdefault(str(p['id']),['#3159d9','#58a695','#ad7bc4','#c38b45'][index%4])
    data['colors']=colors
    data.setdefault('kind','group' if str(chat['telegram_id'] or '').startswith('-') or len(people)>2 else 'direct')
    return data


def decorate_report(result, chat, settings):
    result=dict(result)
    appearance=resolve_appearance(chat,result.get('participants',[]),settings)
    owner=profile(settings)
    people=[]
    for index,p in enumerate(result.get('participants',[])):
        p=dict(p); own=p['id']==appearance.get('self_sender_id')
        p.update(original_name=p['name'],is_self=own,color=appearance.get('colors',{}).get(p['id'],['#3159d9','#58a695','#ad7bc4','#c38b45'][index%4]),image=owner.get('image','') if own else appearance.get('image','') if appearance.get('kind','direct')=='direct' else '')
        if own: p['name']=owner['name']
        people.append(p)
    result['participants']=people
    result['appearance']=appearance
    result['profile']=owner
    result['chat']=dict(result.get('chat',{}))
    with connect(settings) as db:
        result['chat']['first_message']=db.execute('SELECT MIN(date) FROM messages WHERE chat_id=? AND deleted=0',(chat['id'],)).fetchone()[0]
    return result


class ProfileInput(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    image:str=Field(default='',max_length=2800000)


class AppearanceInput(BaseModel):
    title:str=Field(min_length=1,max_length=160)
    kind:str='direct'
    image:str=Field(default='',max_length=2800000)
    self_sender_id:str=''
    colors:dict[str,str]=Field(default_factory=dict)


def install(app,settings,chat_access,admin_mutation):
    @app.get('/api/profile')
    def get_profile(request:Request):
        from .auth import require_admin
        require_admin(request,settings)
        return profile(settings)

    @app.post('/api/admin/profile')
    def set_profile(payload:ProfileInput,request:Request):
        admin_mutation(request)
        data={'name':payload.name.strip(),'image':validate_image(payload.image)}
        if not data['name']: raise HTTPException(422,'Inserisci un nome.')
        with connect(settings) as db:
            db.execute('INSERT OR REPLACE INTO profile VALUES(1,?)',(json.dumps(data),))
        return data

    @app.get('/api/admin/chats/{chat_id}/appearance')
    def appearance(chat_id:str,request:Request):
        chat=chat_access(chat_id,request,admin_only=True)
        with connect(settings) as db:
            people=[dict(r) for r in db.execute('SELECT sender_id AS id,MAX(sender_name) AS name FROM messages WHERE chat_id=? AND deleted=0 GROUP BY sender_id',(chat_id,))]
        data=resolve_appearance(chat,people,settings)
        return {'title':chat['title'],'participants':people,**data}

    @app.post('/api/admin/chats/{chat_id}/appearance')
    def save_appearance(chat_id:str,payload:AppearanceInput,request:Request):
        admin_mutation(request);chat_access(chat_id,request,admin_only=True)
        if payload.kind not in {'direct','group'} or any(not re.fullmatch('#[0-9a-fA-F]{6}',v) for v in payload.colors.values()):
            raise HTTPException(422,'Colori o tipo conversazione non validi.')
        data=payload.model_dump();data['image']=validate_image(data['image']);title=data.pop('title').strip()
        if not title: raise HTTPException(422,'Inserisci un nome per la chat.')
        with connect(settings) as db:
            ids={r[0] for r in db.execute('SELECT DISTINCT sender_id FROM messages WHERE chat_id=?',(chat_id,))}
            if data['self_sender_id'] and data['self_sender_id'] not in ids: raise HTTPException(422,'Partecipante non valido.')
            if not set(data['colors']).issubset(ids): raise HTTPException(422,'Partecipante non valido.')
            db.execute('UPDATE chats SET title=?,appearance=? WHERE id=?',(title,json.dumps(data),chat_id))
        return {'saved':True}

    @app.post('/api/admin/chats/{chat_id}/remove')
    def remove(chat_id:str,request:Request):
        admin_mutation(request);chat_access(chat_id,request,admin_only=True)
        with connect(settings) as db:
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','retry') AND (chat_id=? OR chat_id IS NULL)",(chat_id,)).fetchone():
                raise HTTPException(409,'Attendi la fine delle attività prima di rimuovere la chat.')
            db.execute('UPDATE chats SET archived=1 WHERE id=?',(chat_id,))
        return {'removed':True,'recoverable':True}

    @app.get('/api/admin/removed')
    def removed(request:Request):
        from .auth import require_admin
        require_admin(request,settings)
        with connect(settings) as db: return {'items':[dict(r) for r in db.execute('SELECT id,title FROM chats WHERE archived=1')]}

    @app.post('/api/admin/chats/{chat_id}/restore')
    def restore(chat_id:str,request:Request):
        admin_mutation(request)
        with connect(settings) as db: db.execute('UPDATE chats SET archived=0 WHERE id=?',(chat_id,))
        return {'restored':True}

    @app.get('/api/admin/chats/{chat_id}/estimate')
    def estimate(chat_id:str,request:Request):
        chat_access(chat_id,request,admin_only=True)
        from .sentiment import chunks_for,INSTRUCTIONS,PROMPT_VERSION
        from .analytics import moment_dict
        with connect(settings) as db:
            moments=[moment_dict(m) for m in db.execute('SELECT * FROM moments WHERE chat_id=?',(chat_id,))]
            messages={m['id']:dict(m) for m in db.execute('SELECT * FROM messages WHERE chat_id=? AND deleted=0',(chat_id,))}
        chunks=0;tokens=0;pending=0
        for m in moments:
            if m['model']==settings.openai_model and m['analysis'] and m['analysis'].get('prompt_version')==PROMPT_VERSION: continue
            pending+=1
            for chunk in chunks_for([messages[i] for i in m['message_ids'] if i in messages])[0]:
                chunks+=1
                tokens+=(len((INSTRUCTIONS+json.dumps(chunk,ensure_ascii=False)).encode())+2)//3+80
        return {'estimated_input_tokens':tokens,'estimated_output_tokens':chunks*500,'max_output_tokens':chunks*3000,'requests':chunks,'pending_moments':pending,'model':settings.openai_model,'approximate':True,'note':'Stima indicativa da byte UTF-8, include contesto ripetuto e istruzioni. Output ipotizzato: 500 token a richiesta; non è un preventivo garantito.'}

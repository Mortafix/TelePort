import asyncio
from types import SimpleNamespace

import pytest

from backend.sentiment import chunks_for, combine, validate_tone, analyze_chat
from backend.settings import Settings
from backend.db import initialize, connect


def test_chunks_never_truncate_and_hide_real_speaker_names():
    text='x'*30001
    chunks,aliases=chunks_for([{'id':1,'sender_id':'private-telegram-id','date':'2026-10-24T10:00:00+00:00','text':text}])
    assert ''.join(m['text'] for chunk in chunks for m in chunk if not m['context_only'])==text
    assert aliases=={'S1':'private-telegram-id'}
    assert all(m['speaker']=='S1' for chunk in chunks for m in chunk)


def test_opposed_tones_become_mixed_not_neutral():
    results=[{'label':label,'summary':'Sintesi','emotions':[],'participants':[{'speaker':'S1','label':label,'emotions':[]}],'evidence_message_ids':[1]} for label in ['positive','negative']]
    result=combine(results,{'S1':'a'})
    assert result['label']=='mixed'
    assert result['participants'][0]['label']=='mixed'


def test_reject_hallucinated_speakers_and_evidence():
    chunk=[{'id':1,'speaker':'S1','context_only':False}]
    with pytest.raises(ValueError):
        validate_tone({'label':'neutral','summary':'','emotions':[],'participants':[],'evidence_message_ids':[2]},chunk)


def test_request_budget_survives_provider_failure_and_job_retry(tmp_path, monkeypatch):
    monkeypatch.setenv('TELEPORT_ANALYSIS_REQUEST_LIMIT','1')
    settings=Settings(data_dir=tmp_path,openai_api_key='test-not-a-key')
    initialize(settings)
    with connect(settings) as db:
        db.execute("INSERT INTO chats(id,title) VALUES('test','Test')")
        db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) VALUES('test',1,'a','A','text','Ciao','2026-10-24T10:00:00+00:00')")
    state={}
    class Failing:
        async def parse(self, **kwargs):
            raise RuntimeError('Provider offline')
    client=SimpleNamespace(responses=Failing())
    with pytest.raises(RuntimeError):
        asyncio.run(analyze_chat('test',settings=settings,client=client,progress=state.update))
    assert state['requests']==1
    result=asyncio.run(analyze_chat('test',settings=settings,client=client,budget=state))
    assert result['request_limit_reached'] and result['requests']==1


def test_openai_structured_request_cached_and_reanalyzes_changes(tmp_path):
    settings=Settings(data_dir=tmp_path,openai_model='gpt-6-luna',openai_api_key='test-not-a-key')
    initialize(settings)
    with connect(settings) as db:
        db.execute("INSERT INTO chats(id,title) VALUES('test','Test')")
        db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) VALUES('test',1,'a','Private Name','text','Ottima notizia!','2026-10-24T10:00:00+00:00')")
    calls=[]
    class FakeResponses:
        async def parse(self,**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_parsed={'label':'positive','summary':'Tono positivo','emotions':['joy'],'participants':[{'speaker':'S1','label':'positive','emotions':['joy']}],'evidence_message_ids':[1]}, usage=SimpleNamespace(input_tokens=100,output_tokens=40))
    client=SimpleNamespace(responses=FakeResponses())
    first=asyncio.run(analyze_chat('test',settings=settings,client=client))
    second=asyncio.run(analyze_chat('test',settings=settings,client=client))
    assert first['completed']==1 and second['requests']==0 and len(calls)==1
    assert calls[0]['store'] is False
    assert 'Private Name' not in str(calls[0]['input'])
    with connect(settings) as db:
        db.execute("UPDATE messages SET text='Altra ottima notizia!' WHERE id=1")
    asyncio.run(analyze_chat('test',settings=settings,client=client))
    assert len(calls)==2


def test_edit_during_provider_request_does_not_install_stale_analysis(tmp_path):
    settings = Settings(data_dir=tmp_path, openai_model='gpt-6-luna', openai_api_key='test-not-a-key')
    initialize(settings)
    with connect(settings) as db:
        db.execute("INSERT INTO chats(id,title) VALUES('test','Test')")
        db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) "
                   "VALUES('test',1,'a','Persona','text','Ottima notizia!','2026-10-24T10:00:00+00:00')")
    class ConcurrentEditResponses:
        async def parse(self, **kwargs):
            # Ingestion has committed an edit, but its debounced moment rebuild has not run yet.
            with connect(settings) as db:
                db.execute("UPDATE messages SET text='Non sono più contento.' WHERE chat_id='test' AND id=1")
                db.execute("UPDATE chats SET version=version+1 WHERE id='test'")
            return SimpleNamespace(output_parsed={
                'label': 'positive', 'summary': 'Tono positivo', 'emotions': ['joy'],
                'participants': [{'speaker': 'S1', 'label': 'positive', 'emotions': ['joy']}],
                'evidence_message_ids': [1]}, usage=SimpleNamespace(input_tokens=100, output_tokens=40))
    result = asyncio.run(analyze_chat('test', settings=settings,
                                     client=SimpleNamespace(responses=ConcurrentEditResponses())))
    assert result['completed'] == 0
    with connect(settings) as db:
        assert db.execute("SELECT analysis FROM moments WHERE chat_id='test'").fetchone()['analysis'] is None


def test_provider_failure_resumes_from_paid_chunk_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('TELEPORT_ANALYSIS_REQUEST_LIMIT', '100')
    settings = Settings(data_dir=tmp_path, openai_model='gpt-6-luna', openai_api_key='test-not-a-key')
    initialize(settings)
    text = ''.join(f'Passaggio dimostrativo numero {index}. ' for index in range(1000))
    source_message = {'id': 1, 'sender_id': 'a', 'text': text, 'date': '2026-10-24T10:00:00+00:00'}
    chunks, _ = chunks_for([source_message])
    assert len(chunks) >= 3
    with connect(settings) as db:
        db.execute("INSERT INTO chats(id,title) VALUES('test','Test')")
        db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) "
                   "VALUES('test',1,'a','Persona','text',?,'2026-10-24T10:00:00+00:00')", (text,))
    class Responses:
        def __init__(self, fail_second=False):
            self.calls = 0
            self.fail_second = fail_second
        async def parse(self, **kwargs):
            self.calls += 1
            if self.fail_second and self.calls == 2:
                raise RuntimeError('Simulated provider outage')
            return SimpleNamespace(output_parsed={
                'label': 'neutral', 'summary': 'Tono neutro', 'emotions': [],
                'participants': [{'speaker': 'S1', 'label': 'neutral', 'emotions': []}],
                'evidence_message_ids': [1]}, usage=SimpleNamespace(input_tokens=100, output_tokens=40))
    failed = Responses(fail_second=True)
    with pytest.raises(RuntimeError, match='Simulated provider outage'):
        asyncio.run(analyze_chat('test', settings=settings, client=SimpleNamespace(responses=failed)))
    with connect(settings) as db:
        assert db.execute('SELECT COUNT(*) FROM analysis_chunks').fetchone()[0] == 1
        assert db.execute("SELECT analysis FROM moments WHERE chat_id='test'").fetchone()['analysis'] is None
    resumed = Responses()
    result = asyncio.run(analyze_chat('test', settings=settings, client=SimpleNamespace(responses=resumed)))
    assert result['completed'] == 1
    assert resumed.calls == len(chunks) - 1
    assert result['requests'] == resumed.calls
    with connect(settings) as db:
        assert db.execute('SELECT COUNT(*) FROM analysis_chunks').fetchone()[0] == len(chunks)


def test_analysis_processes_latest_moments_first_and_persists_context(tmp_path):
    settings=Settings(data_dir=tmp_path,openai_api_key='test-not-a-key')
    initialize(settings)
    with connect(settings) as db:
        db.execute("INSERT INTO chats(id,title) VALUES('test','Test')")
        for i in range(1,4):
            db.execute("INSERT INTO messages(chat_id,id,sender_id,sender_name,type,text,date) VALUES('test',?,'a','A','text',?,?)",(i,f'Argomento {i}',f'2026-10-2{i}T10:00:00+00:00'))
    class Responses:
        async def parse(self,**kwargs):
            import json
            item=json.loads(kwargs['input'][0]['content'])[0]
            assert item['id']==3
            return SimpleNamespace(output_parsed={'label':'neutral','summary':'Organizzazione di un appuntamento','emotions':[],'participants':[{'speaker':'S1','label':'neutral','emotions':[]}],'evidence_message_ids':[3]},usage=None)
    assert asyncio.run(analyze_chat('test',limit=1,settings=settings,client=SimpleNamespace(responses=Responses())))['completed']==1
    with connect(settings) as db:
        import json
        rows=db.execute('SELECT analysis FROM moments ORDER BY start').fetchall()
        assert rows[0]['analysis'] is None and rows[1]['analysis'] is None
        assert json.loads(rows[2]['analysis'])['context']=='Organizzazione di un appuntamento'


def test_multichunk_context_keeps_topics_instead_of_generic_tone_description():
    results=[{'label':'neutral','summary':topic,'emotions':[],'participants':[],'evidence_message_ids':[]} for topic in ['Organizzazione di un viaggio.','Prenotazione dei biglietti.']]
    result=combine(results,{})
    assert 'viaggio' in result['context'] and 'biglietti' in result['context']

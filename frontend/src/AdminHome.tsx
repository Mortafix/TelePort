import {useEffect,useState} from 'react';
import {api,date,Json,num} from './api';
import {Avatar,Empty,ErrorNotice,Panel,Spinner} from './components';
import {ChevronRight,Settings2} from './icons';

export default function AdminHome({chats,onOpen,onAdmin}:{chats:Json[],onOpen:(id:string)=>void,onAdmin:()=>void}){
 const [data,setData]=useState<Json|null>(null),[error,setError]=useState('');
 useEffect(()=>{const controller=new AbortController();api('/admin/overview',undefined,controller.signal).then(setData).catch((e:Error)=>{if(e.name!=='AbortError')setError(e.message);});return()=>controller.abort();},[chats]);
 if(error)return <ErrorNotice message={error}/>;
 if(!data)return <Spinner/>;
 const recent=[...chats].sort((a,b)=>(b.last_message||'').localeCompare(a.last_message||'')).slice(0,6);
 return <div className="admin-home"><div className="page-heading"><h1>Archivio</h1><button className="button" onClick={onAdmin}><Settings2 size={16}/>Amministrazione</button></div>
  <div className="metrics-strip"><div><strong>{num(data.conversations)}</strong><span>Conversazioni</span></div><div><strong>{num(data.messages)}</strong><span>Messaggi</span></div><div><strong>{num(data.moments)}</strong><span>Momenti</span></div><div><strong>{num(data.analyzed)}</strong><span>Momenti analizzati</span></div></div>
  <div className="home-columns"><Panel title="Conversazioni recenti">{recent.length?<div className="home-conversations">{recent.map((chat,i)=><button key={chat.id} onClick={()=>onOpen(chat.id)}><Avatar name={chat.title} image={chat.appearance?.image} index={i}/><span><strong>{chat.title}</strong><small>{num(chat.message_count)} messaggi</small></span><time>{date(chat.last_message)}</time><ChevronRight size={15}/></button>)}</div>:<Empty title="Nessuna conversazione"><button className="text-button" onClick={onAdmin}>Importa da Telegram</button></Empty>}</Panel>
   <Panel title="Stato dell’archivio"><dl className="archive-status"><div><dt>Da analizzare o aggiornare</dt><dd>{num(data.pending)}</dd></div><div><dt>Chat con password</dt><dd>{num(data.protected)}</dd></div><div><dt>Attività in coda o in corso</dt><dd>{num(data.active_jobs)}</dd></div><div><dt>Primo messaggio</dt><dd>{date(data.first_message)}</dd></div><div><dt>Ultimo aggiornamento</dt><dd>{date(data.last_sync,{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'})}</dd></div></dl></Panel></div>
 </div>;
}

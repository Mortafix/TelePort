import {useEffect,useRef,useState} from 'react';
import {api,date,Json,num} from './api';
import {Empty,ErrorNotice,Spinner} from './components';
import {Brain,X} from './icons';

export default function AnalysisPlanner({chat,onClose,onQueued}:{chat:Json,onClose:()=>void,onQueued:()=>void}){
 const [plan,setPlan]=useState<Json|null>(null),[count,setCount]=useState(1),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const close=useRef<HTMLButtonElement>(null);
 useEffect(()=>{close.current?.focus();const c=new AbortController();api(`/admin/chats/${encodeURIComponent(chat.id)}/estimate`,undefined,c.signal).then(data=>{setPlan(data);setCount(Math.min(25,data.max_moments)||0);}).catch((e:Error)=>{if(e.name!=='AbortError')setError(e.message);});return()=>c.abort();},[chat.id]);
 const selection=plan?.options[count-1];
 return <section className="analysis-planner"><div className="section-heading"><div><h2>Analisi · {chat.title}</h2>{plan&&<p>{plan.model}</p>}</div><button type="button" ref={close} className="icon-button" aria-label="Torna alla gestione" disabled={busy} onClick={onClose}><X size={19}/></button></div>
 {error&&<ErrorNotice message={error} onDismiss={()=>setError('')}/>}
 {!plan&&!error?<Spinner text="Preparazione della stima…"/>:plan&&(!plan.max_moments?<Empty title="Nessun momento da analizzare">Tutti i momenti sono già aggiornati.</Empty>:<>
 <div className="analysis-quantity"><label htmlFor="analysis-count">Momenti da analizzare</label><output htmlFor="analysis-count">{num(count)} <span>di {num(plan.pending_moments)}</span></output></div>
 <input id="analysis-count" className="analysis-slider" type="range" min={1} max={plan.max_moments} step={1} value={count} disabled={busy||plan.max_moments===1} aria-valuetext={`${count} momenti`} onChange={e=>setCount(Number(e.target.value))} style={{'--progress':`${plan.max_moments>1?(count-1)/(plan.max_moments-1)*100:100}%`} as React.CSSProperties}/>
 <div className="analysis-slider-bounds"><span>1</span><span>{num(plan.max_moments)}</span></div>
 {selection&&<><div className="analysis-token-total"><strong>≈ {num(selection.estimated_total_tokens)}</strong><span>Token totali stimati</span><small>{num(selection.estimated_input_tokens)} input + {num(selection.estimated_output_tokens)} output</small></div>
 <div className="analysis-coverage"><span>Dai momenti più recenti fino al</span><strong>{date(selection.through,{day:'numeric',month:'long',year:'numeric',hour:'2-digit',minute:'2-digit'})}</strong></div></>}
 <p className="analysis-note">{plan.note}</p>
 {selection?.requests>plan.request_limit&&<p className="notice">Il limite di richieste configurato può richiedere più esecuzioni per completare questi momenti.</p>}
 {!plan.configured&&<p className="notice">Configura la chiave OpenAI per avviare l’analisi.</p>}
 <button className="button primary analysis-start" disabled={busy||!selection||!plan.configured} onClick={async()=>{setBusy(true);setError('');try{await api('/admin/analyze',{chat_id:chat.id,limit:count});onQueued();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}><Brain size={17}/>{busy?'Avvio…':`Avvia analisi di ${num(count)} ${count===1?'momento':'momenti'}`}</button>
 </>)}
 </section>;
}

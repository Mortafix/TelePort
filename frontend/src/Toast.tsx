import {useEffect} from 'react';
import {Check,X} from './icons';

export default function Toast({message,onClose}:{message:string,onClose:()=>void}){
 useEffect(()=>{const timer=setTimeout(onClose,6000);return()=>clearTimeout(timer);},[message]);
 return <div className="toast-success" role="status" aria-live="polite" aria-atomic="true"><Check size={18}/><span>{message}</span><button type="button" className="icon-button" aria-label="Chiudi notifica" onClick={onClose}><X size={15}/></button></div>;
}

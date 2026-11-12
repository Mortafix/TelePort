export type Json = Record<string, any>;
let csrf = '';
export function setCsrf(value: string) { csrf = value || ''; }
export async function api<T = any>(path: string, data?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin', signal,
    headers: {Accept: 'application/json', ...(data === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': csrf})},
    ...(data === undefined ? {} : {body: JSON.stringify(data)}),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : result.detail?.message || result.message || `La richiesta non è riuscita (${response.status}). Riprova.`);
  return result;
}
export const num = (value: number | undefined) => new Intl.NumberFormat('it-IT', {maximumFractionDigits:1}).format(value || 0);
export function date(value?: string, options: Intl.DateTimeFormatOptions = {day:'numeric',month:'short',year:'numeric'}) {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : new Intl.DateTimeFormat('it-IT', {timeZone:'Europe/Rome',...options}).format(d);
}
export const time = (value?: string) => date(value, {hour:'2-digit',minute:'2-digit'});
export const labels: Record<string, string> = {positive:'Positivo',negative:'Negativo',neutral:'Neutro',mixed:'Misto',uncertain:'Incerto',pending:'Da analizzare',joy:'Gioia',sadness:'Tristezza',anger:'Rabbia',fear:'Paura',surprise:'Sorpresa',affection:'Affetto',frustration:'Frustrazione',gratitude:'Gratitudine'};
export const tones: Record<string, string> = {positive:'#4fae80',neutral:'#c4ccd7',negative:'#cf806e',mixed:'#78afb5',uncertain:'#e2c48b',pending:'#e4e8f0'};
export const colors = ['#3159d9','#71a89e','#b18ad3','#d3a76a','#78b4d1','#d68e98'];
export function duration(seconds?: number) { if (seconds === undefined || seconds === null) return '—'; return seconds < 60 ? `${num(seconds)} s` : seconds < 3600 ? `${num(seconds/60)} min` : `${num(seconds/3600)} h`; }

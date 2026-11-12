import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {searchHighlighter} from './Highlight';
export default function Markdown({text,highlight=''}:{text:string,highlight?:string}){return <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[searchHighlighter(highlight)]} skipHtml components={{img:({alt})=><span className="markdown-image">[Immagine{alt?`: ${alt}`:''}]</span>,a:({href,children})=><a href={href} target="_blank" rel="noopener noreferrer">{children}</a>}}>{text}</ReactMarkdown>}

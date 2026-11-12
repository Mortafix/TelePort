function parts(text:string,query:string){
 const term=query.trim();
 if(!term)return [{text,match:false}];
 const expression=new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'giu');
 const result:{text:string,match:boolean}[]=[];let end=0;
 for(const match of text.matchAll(expression)){
  const index=match.index!;
  if(index>end)result.push({text:text.slice(end,index),match:false});
  result.push({text:match[0],match:true});end=index+match[0].length;
 }
 if(end<text.length)result.push({text:text.slice(end),match:false});
 return result;
}

export function Highlight({text,query=''}:{text:string,query?:string}){
 return <>{parts(text,query).map((part,i)=>part.match?<mark className="search-hit" key={i}>{part.text}</mark>:part.text)}</>;
}

// Highlight only visible text nodes after Markdown parsing; never touch URLs or markup.
export function searchHighlighter(query:string){
 return ()=>(tree:any)=>{
  if(!query.trim())return;
  function walk(node:any){
   if(!node.children||node.tagName==='mark')return;
   node.children=node.children.flatMap((child:any)=>{
    if(child.type!=='text'){walk(child);return [child];}
    return parts(child.value,query).map(part=>part.match?
     {type:'element',tagName:'mark',properties:{className:['search-hit']},children:[{type:'text',value:part.text}]}:
     {type:'text',value:part.text});
   });
  }
  walk(tree);
 };
}

/* Shared skill-map dialog component (SM). Inlined by diagrams/impeccable/build.py and diagrams/pstack/gen.py.
   Markup in text: **bold**  ==highlight (one per section)==  `code`  [[id]] (tappable chip, label from opts.label). */
var SM=(function(){
 function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
 function Dlg(dlg,opts){
  var body=el('div'),btn=el('button','x','×');btn.type='button';btn.setAttribute('aria-label','Close');
  dlg.textContent='';var bar=el('div','smbar'),bl=el('span','sml'),bt=el('span','smt');if(window.SMLOGO){var li=el('img','smlogo');li.src=window.SMLOGO;li.alt='';bl.appendChild(li)}bar.appendChild(bl);bar.appendChild(bt);dlg.appendChild(bar);bar.appendChild(btn);dlg.appendChild(body);dlg.addEventListener('scroll',function(){bar.classList.toggle('scrolled',dlg.scrollTop>24)});dlg.classList.add('sm');
  var self={body:body,dlg:dlg,opts:opts};
  function chip(id){var c=el('button','chip',opts.label?opts.label(id):id);c.type='button';c.dataset.go=id;return c}
  function md(p,s){String(s).split(/(\*\*.+?\*\*|==.+?==|`.+?`|\[\[[^\]]+\]\])/).forEach(function(x){if(!x)return;
   if(x.slice(0,2)==='**')p.appendChild(el('b',null,x.slice(2,-2)));else if(x.slice(0,2)==='==')p.appendChild(el('mark',null,x.slice(2,-2)));
   else if(x[0]==='`')p.appendChild(el('code',null,x.slice(1,-1)));else if(x.slice(0,2)==='[[')p.appendChild(chip(x.slice(2,-2)));else p.appendChild(document.createTextNode(x))})}
  var T=body;
  function sec(label,key){T.appendChild(el('h4','l-'+key,label))}
  function para(s){var p=el('p');md(p,s);T.appendChild(p)}
  function list(a,ordered){var u=el(ordered?'ol':'ul');a.forEach(function(x){var li=el('li');md(li,x);u.appendChild(li)});T.appendChild(u)}
  /* "When X -> use this" list: each item "situation → action" */
  function when(a){var u=el('ul','wl');(typeof a==='string'?[a]:a).forEach(function(x){var li=el('li'),i=x.indexOf('→');
   if(i<0){var c0=el('span','wc');md(c0,x);li.appendChild(c0)}else{var c=el('span','wc');md(c,x.slice(0,i).trim());li.appendChild(c);li.appendChild(el('span','ar','→'));var w=el('span','wu');md(w,x.slice(i+1).trim());li.appendChild(w)}
   u.appendChild(li)});T.appendChild(u)}
  function flow(f){var d=el('div','fl');f.forEach(function(tk,i){if(i)d.appendChild(el('span','ar','→'));
   if(Array.isArray(tk)){var g=el('span','cg');tk.forEach(function(x){g.appendChild(chip(x.slice(1)))});d.appendChild(g)}
   else if(tk&&tk.stack){var sk=el('span','stack');tk.stack.forEach(function(x){var p=el('span','pill');md(p,x);sk.appendChild(p)});d.appendChild(sk)}
   else if(tk[0]==='@'){d.appendChild(chip(tk.slice(1)))}
   else{var p=el('span','pill'+(i===0&&tk[0]==='/'?' cmd':''));md(p,tk);d.appendChild(p)}});T.appendChild(d)}
  /* rows: [label, content, kind]  kind 'code' = exact prompt, array content = numbered steps */
  function example(rows,note){var b=el('div','ex');rows.forEach(function(r){var row=el('div','exr');row.appendChild(el('b',null,r[0]));
   if(r[2]==='code')row.appendChild(el('code',null,r[1]));else if(Array.isArray(r[1])){var o=el('ol');r[1].forEach(function(s){var li=el('li');md(li,s);o.appendChild(li)});row.appendChild(o)}else{var s=el('span');md(s,r[1]);row.appendChild(s)}
   b.appendChild(row)});b.appendChild(el('p','exn',note||'Example'));T.appendChild(b)}
  function chips(ids){var h=el('div','hl');ids.forEach(function(n){h.appendChild(chip(n))});T.appendChild(h)}
  function note(s){var n=el('p','note2');md(n,s);T.appendChild(n)}
  function tags(a){var t=el('div','tags');a.forEach(function(x){var sp=el('span','tag'+(x[1]?' k':''));if(x[2]&&window.SMPLAT&&SMPLAT[x[2]])sp.innerHTML=SMPLAT[x[2]];sp.appendChild(document.createTextNode(x[0]));t.appendChild(sp)});body.appendChild(t)}
  /* model: {accent, title, tags:[[text,key?]], blocks:[{label,key,type,v,note}]}; type: p ul ol when flows example chips note node */
  var OPEN={when:1,is:1,grp:1,warn:1};
  function plain(x){return String(x).replace(/\[\[([^\]]+)\]\]/g,function(_,i){return opts.label?opts.label(i):i}).replace(/\*\*|==|`/g,'')}
  function cut(x,n){x=plain(x).replace(/\s+/g,' ').trim();return x.length>n?x.slice(0,n-1).trim()+'…':x}
  function flat(f){return f.map(function(t){return Array.isArray(t)?t.map(function(x){return plain(x.slice(1))}).join(' '):(t&&t.stack?t.stack[0]:(t[0]==='@'?plain(t.slice(1)):plain(t)))}).join(' → ')}
  function peek(b){if(b.peek)return b.peek;var v=b.v,t=b.type;
   if(t==='ol')return v.length+(v.length===1?' step':' steps');if(t==='ul')return v.length+(v.length===1?' item':' items');
   if(t==='p')return cut(v,70);if(t==='chips')return v.map(function(n){return opts.label?opts.label(n):n}).join(', ');
   if(t==='example'){var r=v.filter(function(x){return x[0]==='You type'||x[0]==='You say'})[0]||v[0];return cut(r[1],70)}
   if(t==='flows'){if(b.key==='calls'){var ids={};v.forEach(function(f){f.forEach(function(tk){if(Array.isArray(tk))tk.forEach(function(x){ids[x]=1});else if(typeof tk==='string'&&tk[0]==='@')ids[tk]=1})});var n=Object.keys(ids).length;return 'calls '+n}
    return v.length>1?cut(flat(v[0]),56)+' (+'+(v.length-1)+' more)':cut(flat(v[0]),70)}
   if(t==='when')return v.length+' situations';return ''}
  function block(b){var t=b.type;
   if(t==='p')para(b.v);else if(t==='ul')list(b.v);else if(t==='ol')list(b.v,1);else if(t==='when')when(b.v);else if(t==='flows')b.v.forEach(flow);
   else if(t==='example')example(b.v,b.note);else if(t==='chips')chips(b.v);else if(t==='note')note(b.v);else if(t==='node')b.v(T,self)}
  function preview(b){var n=b.v.length,keep=b.v.slice(0,3),rest=b.v.slice(3),t=b.type,c;
   block({type:t,v:keep,key:b.key});if(!rest.length)return;var ex=el('div','more');ex.hidden=true;T.appendChild(ex);var old=T;T=ex;block({type:t,v:rest,key:b.key});T=old;
   var tg=el('button','showall','Show all '+n);tg.type='button';tg.dataset.n=n;tg.setAttribute('aria-expanded','false');tg.onclick=function(){var o=ex.hidden;ex.hidden=!o;tg.setAttribute('aria-expanded',String(o));tg.textContent=o?'Show fewer':'Show all '+n;sync()};T.appendChild(tg)}
  function sync(){var ds=body.querySelectorAll('details'),mores=body.querySelectorAll('.more'),all=true;[].forEach.call(ds,function(d){if(!d.open)all=false});[].forEach.call(mores,function(m){if(m.hidden)all=false});xa.textContent=all?'Collapse all':'Expand all';xa.hidden=!(ds.length||mores.length);xa.dataset.all=all?'1':''}
  var xa=el('button','xa','Expand all');xa.type='button';xa.onclick=function(){var open=!xa.dataset.all;
   [].forEach.call(body.querySelectorAll('details'),function(d){d.open=open});
   [].forEach.call(body.querySelectorAll('.more'),function(m){m.hidden=!open;var tg=m.nextElementSibling;if(tg&&tg.classList.contains('showall')){tg.setAttribute('aria-expanded',String(open));tg.textContent=open?'Show fewer':'Show all '+tg.dataset.n}});sync()};
  bar.insertBefore(xa,btn);
  self.render=function(m){body.textContent='';bt.textContent=m.title||'';bar.classList.remove('scrolled');T=body;dlg.className='sm '+(m.accent||'');if(m.title)body.appendChild(el('h3',null,m.title));if(m.tags)tags(m.tags);
   m.blocks.forEach(function(b){
    if(!b.label){block(b);return}
    var isPrev=/^When X/.test(b.label)||(b.key==='picks'&&b.type==='flows'&&b.v.length>3);
    if(OPEN[b.key]&&!isPrev||isPrev){sec(b.label,b.key||'how');if(isPrev)preview(b);else block(b);return}
    var d=el('details','sec l-'+(b.key||'how')),sm=el('summary'),pk=peek(b);if(b.open)d.open=true;
    sm.appendChild(el('span','sl',b.label));if(pk)sm.appendChild(el('span','sp',pk));sm.appendChild(el('span','chv'));d.appendChild(sm);
    var inner=el('div','sb');d.appendChild(inner);body.appendChild(d);var old=T;T=inner;block(b);T=old;d.addEventListener('toggle',sync)});
   sync()};
  self.open=function(){if(!dlg.open){try{dlg.showModal()}catch(e){dlg.setAttribute('open','')}}dlg.scrollTop=0;btn.focus({preventScroll:true})};
  self.close=function(){if(dlg.open){try{dlg.close()}catch(e){dlg.removeAttribute('open')}}};
  dlg.addEventListener('click',function(e){var c=e.target.closest&&e.target.closest('[data-go]');if(c){opts.onGo&&opts.onGo(c.dataset.go);return}
   if(e.target.closest('.x')){self.close();return}
   var r=dlg.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)self.close()});
  dlg.addEventListener('close',function(){opts.onClose&&opts.onClose()});
  self.md=md;self.el=el;self.chip=chip;self.sec=sec;self.para=para;self.list=list;self.flow=flow;
  return self}
 /* "a • b • c" -> list; arrays pass through */
 function bullets(w){return Array.isArray(w)?w:String(w).split(' • ')}
 /* "**Situation:** text. (1) a. (2) b." -> {situation, steps[]} */
 function story(s){var m=String(s).match(/^\s*\*\*Situation:\*\*\s*([\s\S]*?)\s*(\(1\)[\s\S]*)?$/),sit=m?m[1]:'',steps=[];
  if(m&&m[2])m[2].split(/\(\d+\)\s*/).forEach(function(x){x=x.trim();if(x)steps.push(x)});return {situation:sit,steps:steps}}
 return {Dialog:Dlg,el:el,bullets:bullets,story:story}
})();

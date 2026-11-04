/* Shared page behaviour: theme (nav button), search + filter dimming, full screen, dialog open/close. Page JS sets SMPAGE = {model(id), label(id), afterFilter?(visible)} and window.SMDATA = {search:{id:text}, filt:{id:[values]}}. */
(function(){
 var root=document.documentElement,$=function(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))};
 try{var th=localStorage.getItem('sb-theme');if(th)root.setAttribute('data-theme',th)}catch(e){}
 document.getElementById('sbtt').onclick=function(){var d=root.getAttribute('data-theme')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'),n=d==='dark'?'light':'dark';root.setAttribute('data-theme',n);try{localStorage.setItem('sb-theme',n)}catch(e){}};
 var hero=document.getElementById('hero'),dlg=document.getElementById('pop'),P=SMPAGE,DATA=window.SMDATA||{search:{},filt:{}};
 var nodes=function(){return $('#hero svg [data-id]')},st={q:'',f:'all'},FKEY='sm-filter:'+location.pathname,cur=null,last=null;
 function apply(){var vis={};nodes().forEach(function(g){var id=g.dataset.id,t=DATA.search[id];if(t==null)return;
  var fv=DATA.filt[id],ok=(st.f==='all'||!fv||fv.indexOf(st.f)>=0)&&(!st.q||t.toLowerCase().indexOf(st.q)>=0);vis[id]=ok;g.classList.toggle('dim',!ok)});
  if(P.afterFilter)P.afterFilter(vis);
  var pill=document.getElementById('fpill');if(pill){if(st.f==='all'){pill.hidden=true}else{var ids={};nodes().forEach(function(g){var fv=DATA.filt[g.dataset.id];if(fv&&fv.indexOf(st.f)<0)ids[g.dataset.id]=1});
   var lbl=($('.seg button').filter(function(b){return b.dataset.f===st.f})[0]||{}).title||st.f;pill.textContent='';pill.appendChild(document.createTextNode('Showing '+lbl+' skills · '+Object.keys(ids).length+' greyed out · '));var a=document.createElement('button');a.type='button';a.textContent='Show all';a.onclick=function(){setF('all')};pill.appendChild(a);pill.hidden=false}}
  try{localStorage.setItem(FKEY,st.f)}catch(e){}}
 function setF(f){st.f=f;$('.seg button').forEach(function(x){x.setAttribute('aria-pressed',String(x.dataset.f===f))});apply()}
 try{var f0=localStorage.getItem(FKEY);if(f0&&$('.seg button').some(function(b){return b.dataset.f===f0}))setF(f0)}catch(e){}
 document.getElementById('q').addEventListener('input',function(e){st.q=e.target.value.trim().toLowerCase();apply()});
 $('.seg button').forEach(function(b){b.addEventListener('click',function(){st.f=b.dataset.f;$('.seg button').forEach(function(x){x.setAttribute('aria-pressed',String(x===b))});apply()})});
 var D=SM.Dialog(dlg,{label:P.label,onGo:function(id){open(id)},onClose:function(){nodes().forEach(function(x){x.classList.remove('sel')});cur=null;if(last)try{last.focus({preventScroll:true})}catch(e){}}});
var CMP=window.SMCOMPARE||null,GLOSS={},tip=null,tipFor=null;
 function cmpBlock(id){var gs=CMP.groups[id],PK=CMP.packs;return {label:'Compare with similar skills',key:'cmp',type:'node',open:true,peek:gs.length>1?gs.length+' comparisons':'vs '+gs[0].members.filter(function(x){return !(x.pack===CMP.pack&&x.skill===id)}).map(function(x){return PK[x.pack].name}).join(', '),v:function(body,self){
  gs.forEach(function(g){var el=self.el;body.appendChild(el('p','cmpt',g.topic));var bestBox=null,whyEl=null;if(g.best){var tl=el('div','cmptabs'),lb=el('span','cmplab','Which one\'s best?');bestBox=el('div','cmpcases');bestBox.setAttribute('role','tablist');tl.appendChild(lb);tl.appendChild(bestBox);body.appendChild(tl);whyEl=el('p','cmpwhy');body.appendChild(whyEl)}var w=el('div','cmpw'),t=el('table','cmp'),h=el('tr');h.appendChild(el('th','cmpc',''));
   g.members.forEach(function(m){var cur=m.pack===CMP.pack&&m.skill===id,th=el('th',cur?'cur':'');th.dataset.pack=m.pack;var im=el('img','cmpl');im.src=PK[m.pack].logo;im.alt='';im.width=18;im.height=18;th.appendChild(im);
    var href=cur?null:(m.pack!==CMP.pack&&PK[m.pack].path&&m.onMap?PK[m.pack].path+'#skill-'+m.skill:m.url),a=href?el('a',null,m.name):el('span',null,m.name);if(href){a.href=href;if(!/^\.\./.test(href)){a.target='_blank';a.rel='noopener'}}
    th.appendChild(a);th.appendChild(el('small',null,PK[m.pack].name+(cur?' (this)':'')));h.appendChild(th)});t.appendChild(h);
   g.rows.forEach(function(r){var tr=el('tr');tr.appendChild(el('th','cmpc',({'Use when':'When','Strictness':'Strict','You get':'Output','Unique':'Edge'})[r.aspect]||r.aspect));g.members.forEach(function(m){var td=el('td',m.pack===CMP.pack&&m.skill===id?'cur':'');td.dataset.pack=m.pack;if(r.aspect==='Strictness'&&m.strict){var dt=el('span','dots','●'.repeat(m.strict)+'○'.repeat(3-m.strict));dt.title='Strictness '+m.strict+' of 3';td.appendChild(dt);td.appendChild(document.createTextNode(' '))}self.md(td,r.values[m.pack]||'—');var dt_=(r.details||r.detail||{})[m.pack];if(dt_){td.classList.add('hasd');td.tabIndex=0;td.dataset.detail=dt_;td.setAttribute('role','button');td.setAttribute('aria-expanded','false')}tr.appendChild(td)});t.appendChild(tr)});
   var extra=[].slice.call(t.querySelectorAll('tr')).slice(5);
   var mb=null;if(extra.length>0){extra.forEach(function(r){r.hidden=true});mb=el('button','cmpmore','Show '+extra.length+' more rows');mb.type='button';mb.setAttribute('aria-expanded','false');mb.onclick=function(){var o=extra[0].hidden;extra.forEach(function(r){r.hidden=!o});mb.setAttribute('aria-expanded',String(o));mb.textContent=o?'Show fewer rows':'Show '+extra.length+' more rows'};}
   w.appendChild(t);body.appendChild(w);if(mb)body.appendChild(mb);var hint=el('p','cmphint','Tap or hover a cell with ⓘ for the detail. Dotted words have a definition.');body.appendChild(hint);
   if(bestBox){var why=whyEl,opts=[{label:'Overall',winner:g.best.overall,why:g.best.why}].concat((g.best.cases||[]).map(function(c){return {label:c.case,winner:c.winner,why:c.why}}));
    function pickCase(o,btn){[].forEach.call(bestBox.children,function(x){x.setAttribute('aria-pressed',String(x===btn))});
     [].forEach.call(t.querySelectorAll('[data-pack]'),function(c){var win=c.dataset.pack===o.winner;c.classList.toggle('win',win);c.classList.toggle('dimc',!win);var bd=c.querySelector('.bestb');if(bd)bd.remove();if(win&&c.tagName==='TH'){var x=el('span','bestb','🏆 Best');c.insertBefore(x,c.firstChild)}});
     why.textContent='';why.appendChild(document.createTextNode('🏆 '));var bb=el('b',null,(PK[o.winner]?PK[o.winner].name:o.winner)+': ');why.appendChild(bb);why.appendChild(document.createTextNode((o.why||'')+(o.label==='Overall'?'':' ('+o.label+')')))}
    opts.forEach(function(o,i){var bt=el('button','casechip',o.label);bt.type='button';bt.title=o.label;bt.setAttribute('aria-pressed','false');bt.setAttribute('role','tab');bt.onclick=function(){pickCase(o,bt)};bestBox.appendChild(bt)});
    pickCase(opts[0],bestBox.children[0])}
   if(g.sources&&g.sources.length){var sp=el('p','cmps');sp.appendChild(document.createTextNode('Sources: '));g.sources.forEach(function(s,i){if(i)sp.appendChild(document.createTextNode(' · '));var a=el('a',null,s.label);a.href=s.url;a.target='_blank';a.rel='noopener';sp.appendChild(a)});body.appendChild(sp)}})}}}
 function M(id){var m=P.model(id);GLOSS={};if(CMP&&CMP.groups[id])CMP.groups[id].forEach(function(g){Object.keys(g.glossary||{}).forEach(function(t){GLOSS[t]=g.glossary[t]})});if(!m||!CMP||!CMP.groups[id])return m;var i=-1;m.blocks.forEach(function(b,k){if(b.key==='is'&&i<0)i=k});m.blocks.splice(i<0?m.blocks.length:i+1,0,cmpBlock(id));return m}
 /* compare badge on nodes */
 if(CMP)nodes().forEach(function(g){if(!CMP.groups[g.dataset.id])return;var r=g.querySelector('rect');if(!r)return;var x=+r.getAttribute('x'),y=+r.getAttribute('y'),b=document.createElementNS('http://www.w3.org/2000/svg','g');b.setAttribute('class','cmpb');
  b.innerHTML='<title>Has a comparison with similar skills</title><circle cx="'+(x+3)+'" cy="'+(y+3)+'" r="8"/><text x="'+(x+3)+'" y="'+(y+7)+'" text-anchor="middle">≈</text>';g.appendChild(b)});

 function hideTip(){if(tip){tip.hidden=true;tipFor=null}[].forEach.call(dlg.querySelectorAll('[aria-expanded=true]'),function(x){x.setAttribute('aria-expanded','false')})}
 function showTip(el,text){if(!tip){tip=document.createElement('div');tip.className='smtip';tip.setAttribute('role','tooltip');dlg.appendChild(tip)}if(dlg.contains(tip)===false)dlg.appendChild(tip);
  tip.textContent=text;tip.hidden=false;tipFor=el;var r=el.getBoundingClientRect(),d=dlg.getBoundingClientRect(),tw=Math.min(300,d.width-24);tip.style.width=tw+'px';
  var x=Math.max(d.left+10,Math.min(r.left,d.right-tw-10)),lo=Math.max(d.top,0)+8,hi=Math.min(d.bottom,innerHeight)-8,th=tip.offsetHeight,y=r.bottom+6;if(y+th>hi)y=r.top-th-6;if(y<lo)y=Math.max(lo,hi-th);tip.style.left=x+'px';tip.style.top=y+'px'}
 function glossify(){var terms=Object.keys(GLOSS).sort(function(a,b){return b.length-a.length});if(!terms.length)return;
  var re=new RegExp('(^|[^\\w-])('+terms.map(function(t){return t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}).join('|')+')(?![\\w-])','gi');
  var roots=[].slice.call(dlg.querySelectorAll('table.cmp td, table.cmp th:not(.cmpc), ul.wl .wc, ul.wl .wu, h4.l-is + p, .cmpwhy'));
  roots.forEach(function(root){var w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,null),ns=[],n;while(n=w.nextNode()){if(n.parentNode.closest&&n.parentNode.closest('.gl,button,a,code'))continue;ns.push(n)}
   ns.forEach(function(n){var t=n.nodeValue;if(!re.test(t)){re.lastIndex=0;return}re.lastIndex=0;var f=document.createDocumentFragment(),last=0,m;
    while(m=re.exec(t)){var st=m.index+m[1].length;f.appendChild(document.createTextNode(t.slice(last,st)));var sp=document.createElement('span');sp.className='gl';sp.tabIndex=0;sp.textContent=m[2];var key=terms.filter(function(x){return x.toLowerCase()===m[2].toLowerCase()})[0];sp.dataset.tip=GLOSS[key];f.appendChild(sp);last=st+m[2].length}
    f.appendChild(document.createTextNode(t.slice(last)));n.parentNode.replaceChild(f,n)})})}
 var hov=matchMedia('(hover:hover)');
 dlg.addEventListener('mouseover',function(e){if(!hov.matches)return;var g=e.target.closest&&e.target.closest('.gl,td.hasd');if(g&&g!==tipFor&&!(tip&&tip.dataset.pin))showTip(g,g.dataset.tip||g.dataset.detail)});
 dlg.addEventListener('mouseout',function(e){if(!hov.matches||!tip||tip.dataset.pin)return;var g=e.target.closest&&e.target.closest('.gl,td.hasd');if(g&&!g.contains(e.relatedTarget))hideTip()});
 dlg.addEventListener('click',function(e){var g=e.target.closest&&e.target.closest('.gl,td.hasd');
  if(!g){if(!e.target.closest('.smtip')){if(tip)delete tip.dataset.pin;hideTip()}return}
  if(g.matches('td.hasd')&&!hov.matches){var nx=g.querySelector('.cdet');if(nx){nx.remove();g.setAttribute('aria-expanded','false')}else{var dv=document.createElement('div');dv.className='cdet';dv.textContent=g.dataset.detail;g.appendChild(dv);g.setAttribute('aria-expanded','true')}return}
  if(tip&&tipFor===g&&tip.dataset.pin){delete tip.dataset.pin;hideTip();return}
  showTip(g,g.dataset.tip||g.dataset.detail);tip.dataset.pin='1';g.setAttribute('aria-expanded','true')},true);
 dlg.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){var g=e.target.closest&&e.target.closest('.gl,td.hasd');if(g){e.preventDefault();g.click()}}});
 dlg.addEventListener('scroll',function(){hideTip()});
 function open(id){var g=nodes().filter(function(x){return x.dataset.id===id})[0];if(!g||!M(id))return;if(!dlg.open)last=g;
  D.render(M(id));glossify();cur=id;nodes().forEach(function(x){x.classList.toggle('sel',x===g)});D.open()}
 window.SMopen=open;
 var sc=document.getElementById('sc');
 sc.addEventListener('click',function(e){var n=e.target.closest('[data-id]');if(n&&!(window.SMcanvas&&window.SMcanvas.moved))open(n.dataset.id)});
 sc.addEventListener('keydown',function(e){var n=e.target.closest&&e.target.closest('[data-id]');if(n&&(e.key==='Enter'||e.key===' ')){e.preventDefault();open(n.dataset.id)}});
 var fsb=document.getElementById('fsb'),fst=document.getElementById('fst'),exp=false;
 function fsOn(){return document.fullscreenElement===hero}
 function sync(){var on=exp||fsOn();fst.textContent=on?'Exit':'Full screen';fsb.setAttribute('aria-label',on?'Exit full screen':'Full screen')}
 function expand(on){exp=on;hero.classList.toggle('exp',on);root.classList.toggle('lock',on);sync()}
 function exitAll(){if(fsOn()){try{document.exitFullscreen()}catch(e){}}if(exp)expand(false)}
 fsb.addEventListener('click',function(){if(exp||fsOn()){exitAll();return}var p;try{p=hero.requestFullscreen?hero.requestFullscreen():null}catch(x){p=null}
  if(p&&p.then){p.then(sync,function(){expand(true)})}else if(!p){expand(true)}});
 document.addEventListener('fullscreenchange',sync);
 document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!dlg.open&&exp)expand(false)});
 apply();
 function fromHash(){var h=location.hash.slice(1);if(!/^[A-Za-z0-9._-]+$/.test(h))return;var id=h.replace(/^skill-/,'');if(M(id)&&nodes().some(function(x){return x.dataset.id===id}))open(id)}
 window.addEventListener('hashchange',fromHash);setTimeout(fromHash,0);
})();

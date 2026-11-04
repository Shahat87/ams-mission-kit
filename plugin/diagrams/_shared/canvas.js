/* Shared canvas mode + collapsible groups. SVG contract: g[data-region=gid] (group box), [data-grp=gid] (members), [data-head] (title element, also a node),
   path/line [data-edge="fromGid toGid"], path [data-trunk=gid] (shared trunk, hidden once its group moves). Layout state is saved per page. */
(function(){
 var hero=document.getElementById('hero'),sc=document.getElementById('sc'),svg=sc.querySelector('svg');if(!svg)return;
 var NS='http://www.w3.org/2000/svg',KEY='sm-canvas:'+location.pathname.replace(/index\.html$/,''),THR=5;
 var st={on:false,off:{},col:{},cam:null};try{var s0=JSON.parse(localStorage.getItem(KEY)||'null');if(s0){st.on=!!s0.on;st.off=s0.off||{};st.col=s0.col||{};st.cam=s0.cam||null}}catch(e){}
 var FORCE=window.SMCANVAS==='always';if(FORCE)st.on=true;try{if(!localStorage.getItem(KEY)&&window.SMCOLLAPSED)window.SMCOLLAPSED.forEach(function(i){st.col[i]=true})}catch(e){}
 function save(){try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}}
 var $a=function(s,r){return Array.prototype.slice.call((r||svg).querySelectorAll(s))};
 var vb0=svg.getAttribute('viewBox').split(/[ ,]+/).map(Number);
 /* groups */
 var G={};
 $a('[data-region]').forEach(function(r){var id=r.dataset.region;G[id]={id:id,region:r,rect:r.querySelector('rect'),members:[],head:null,fit:r.dataset.fit?r.dataset.fit.split(' '):null}});
 $a('[data-grp]').forEach(function(n){var id=n.dataset.grp;if(!G[id])G[id]={id:id,region:null,rect:null,members:[],head:null};if(n.hasAttribute('data-head'))G[id].head=n;else G[id].members.push(n)});
 var TXT=function(s){return s.replace(/\s+/g,' ')};
 Object.keys(G).forEach(function(id){var g=G[id];g.auto={x:0,y:0};
  if(g.rect){g.x0=+g.rect.getAttribute('x');g.y0=+g.rect.getAttribute('y');g.w0=+g.rect.getAttribute('width');g.h0=+g.rect.getAttribute('height');g.h=g.h0}
  if(g.rect&&g.members.length&&!g.fit&&!window.SMNOCOLLAPSE){g.cx=g.x0+g.w0-18;g.cy=g.y0+18;
   var c=document.createElementNS(NS,'g');c.setAttribute('role','button');c.setAttribute('tabindex','0');c.dataset.chev=id;
   c.innerHTML='<circle r="11"/><path d=""/>';g.chev=c;
   var t=document.createElementNS(NS,'text');t.setAttribute('text-anchor','end');t.style.display='none';g.count=t;
   var cl=(g.region.getAttribute('class')||'').split(' ').filter(function(x){return /^(c-|g-)/.test(x)}).join(' ');c.setAttribute('class','gchev '+cl);t.setAttribute('class','gcount '+cl);
   g.region.parentNode.appendChild(t);g.region.parentNode.appendChild(c)}});
 Object.keys(G).forEach(function(id){var g=G[id];if(g.fit){var mb=0;g.fit.forEach(function(f){if(G[f])mb=Math.max(mb,G[f].y0+G[f].h0)});g.pad0=g.y0+g.h0-mb}});
 /* reserve a slot for the chevron: ellipsize labels that would run under it */
 Object.keys(G).forEach(function(id){var g=G[id];if(!g.chev)return;var lim=g.x0+g.w0-30;
  $a('text',g.region).concat(g.head?$a('text.rt',g.head):[]).forEach(function(t){if(t.classList.contains('gcount')||t.classList.contains('chev'))return;
   var x=+t.getAttribute('x')||0,full=t.textContent,w=t.getComputedTextLength?t.getComputedTextLength():0;
   if(x+w<=lim||!w)return;var n=full.length;while(n>3&&x+t.getComputedTextLength()>lim){n--;t.textContent=full.slice(0,n).trim()+'…'}
   var ti=document.createElementNS(NS,'title');ti.textContent=full;t.appendChild(ti)})});
 /* rows: groups whose boxes share a top edge */
 var rows=[],rowOf={};
 (function(){var gs=Object.keys(G).map(function(i){return G[i]}).filter(function(g){return g.rect&&!g.fit}).sort(function(a,b){return a.y0-b.y0});
  gs.forEach(function(g){var r=rows[rows.length-1];if(r&&Math.abs(r.y0-g.y0)<10)r.gs.push(g);else rows.push({y0:g.y0,gs:[g]});rowOf[g.id]=rows.length-1});
  rows.forEach(function(r){r.h0=Math.max.apply(null,r.gs.map(function(g){return g.h0}))})})();
 var edges=$a('[data-edge]').map(function(p){var a=p.dataset.edge.split(' ');return {el:p,a:a[0],b:a[1],bus:/\b(bus|fan)\b/.test(p.getAttribute('class')||''),d0:p.getAttribute('d')||null,line:p.tagName==='line'?{x1:p.getAttribute('x1'),y1:p.getAttribute('y1'),x2:p.getAttribute('x2'),y2:p.getAttribute('y2')}:null}});
 var trunks=$a('[data-trunk]');
 function setT(el,x,y){if(x||y)el.setAttribute('transform','translate('+x+' '+y+')');else el.removeAttribute('transform')}
 function uoff(id){var o=st.on?st.off[id]:null;return o||{x:0,y:0}}
 function eff(g){var u=uoff(g.id);return {x:u.x+g.auto.x,y:u.y+g.auto.y}}
 function noff(el){var o=st.on?st.off['n:'+el.dataset.id]:null;return o||{x:0,y:0}}
 function colH(g){if(!g.head)return 54;var hb=g.head.getBBox();return hb.y+hb.height+26-g.y0}
 function layout(){
  var G_=Object.keys(G).map(function(i){return G[i]});
  G_.forEach(function(g){g.isCol=!!st.col[g.id]&&!!g.chev;if(g.rect&&!g.fit)g.h=g.isCol?colH(g):g.h0});
  var shift=0;rows.forEach(function(r){var cur=Math.max.apply(null,r.gs.map(function(g){return g.h}));r.shift=shift;r.h=cur;shift+=cur-r.h0});
  G_.forEach(function(g){if(g.rect&&!g.fit)g.auto={x:0,y:rows[rowOf[g.id]].shift}});
  G_.forEach(function(g){if(g.fit){var f0=G[g.fit[0]],mb=0;g.auto={x:0,y:f0?f0.auto.y:0};g.fit.forEach(function(f){if(G[f])mb=Math.max(mb,G[f].y0+G[f].auto.y+G[f].h)});g.h=mb+g.pad0-(g.y0+g.auto.y)}});
  G_.forEach(function(g){var o=eff(g);
   [g.region,g.head].forEach(function(el){if(el)setT(el,o.x,o.y)});
   if(g.rect)g.rect.setAttribute('height',g.h);
   if(g.chev){g.chev.setAttribute('transform','translate('+(g.cx+o.x)+' '+(g.cy+o.y)+')');g.chev.querySelector('path').setAttribute('d',g.isCol?'M-3 -4 L3 0 L-3 4':'M-4 -3 L0 3 L4 -3');g.chev.setAttribute('aria-label',(g.isCol?'Expand ':'Collapse ')+g.id)}
   if(g.count){g.count.style.display=g.isCol?'':'none';var cn=g.members.filter(function(m){return m.dataset.id&&!m.classList.contains('ch')}).length||g.members.length;g.count.textContent=cn+(cn===1?' item':' items');g.count.setAttribute('x',g.x0+g.w0-12+o.x);g.count.setAttribute('y',g.y0+g.h-8+o.y)}
   g.members.forEach(function(n){var m=noff(n);setT(n,o.x+m.x,o.y+m.y);n.style.display=g.isCol?'none':''})});
  var dirtyG={};G_.forEach(function(g){var o=eff(g);if(o.x||o.y||g.isCol||(g.fit&&g.h!==g.h0))dirtyG[g.id]=1});
  var rt=rows.map(function(r){return Math.min.apply(null,r.gs.map(function(g){return g.y0+eff(g).y}))}),rb=rows.map(function(r){return Math.max.apply(null,r.gs.map(function(g){return g.y0+eff(g).y+g.h}))});
  edges.forEach(function(e){var A=G[e.a],B=G[e.b];if(!A||!B)return;
   if(!dirtyG[e.a]&&!dirtyG[e.b]){if(e.d0)e.el.setAttribute('d',e.d0);else if(e.line){for(var k in e.line)e.el.setAttribute(k,e.line[k])}return}
   var a=gb(A),b=gb(B),d,ra=rowOf[e.a],rb_=rowOf[e.b],y1=a.t+(A.head?hh(A):a.h/2),y2=b.t+(B.head?hh(B):b.h/2);
   if(e.bus){var tx=a.r+24,bx=b.l+b.w/2,by=(rb_!=null?rt[rb_]:b.t)-14;d='M'+a.r+' '+y1+' H'+tx+' V'+by+' H'+bx+' V'+b.t}
   else if(ra!=null&&rb_!=null&&ra!==rb_){var x1=a.l+a.w/2,x2=b.l+b.w/2,ym=rb[ra]+14;d='M'+x1+' '+a.b+' V'+ym+' H'+x2+' V'+b.t}
   else if(b.l>=a.r-4){var xm=(a.r+b.l)/2;d='M'+a.r+' '+y1+' H'+xm+' V'+y2+' H'+b.l}
   else{var xm2=(a.l+b.r)/2;d='M'+a.l+' '+y1+' H'+xm2+' V'+y2+' H'+b.r}
   if(e.line){var ln=document.createElementNS(NS,'path');for(var i=0;i<e.el.attributes.length;i++){var at=e.el.attributes[i];if(!/^(x1|y1|x2|y2)$/.test(at.name))ln.setAttribute(at.name,at.value)}e.el.parentNode.replaceChild(ln,e.el);e.el=ln;e.line=null}
   e.el.setAttribute('d',d)});
  var anyR=edges.some(function(e){return e.bus&&(dirtyG[e.a]||dirtyG[e.b])});
  trunks.forEach(function(t){t.style.display=anyR?'none':''});
  if(!st.on)shrink();
 }
 function hh(g){if(!g.rect)return 25;var hb=g.head.getBBox();return hb.y+hb.height/2-g.y0}
 function gb(g){var o=eff(g);if(g.rect){var x=g.x0+o.x,y=g.y0+o.y;return {l:x,t:y,w:g.w0,h:g.h,r:x+g.w0,b:y+g.h}}
  var bb=g.head.getBBox();return {l:bb.x+o.x,t:bb.y+o.y,w:bb.width,h:bb.height,r:bb.x+bb.width+o.x,b:bb.y+bb.height+o.y}}
 /* normal mode: shrink the drawing to its content so page height follows collapsed groups */
 function shrink(){var bb=svg.getBBox(),h=Math.min(vb0[3],Math.max(120,bb.y+bb.height+14-vb0[1]));svg.setAttribute('viewBox',vb0[0]+' '+vb0[1]+' '+vb0[2]+' '+h)}
 /* camera */
 var cam={cx:0,cy:0,k:1};
 function size(){return [sc.clientWidth||800,sc.clientHeight||500]}
 function contentBox(){var bb=svg.getBBox();return {x:bb.x,y:bb.y,w:bb.width,h:bb.height}}
 function apply(){var s=size(),W=s[0],H=s[1];svg.setAttribute('viewBox',(cam.cx-W/(2*cam.k))+' '+(cam.cy-H/(2*cam.k))+' '+(W/cam.k)+' '+(H/cam.k))}
 function fit(){var s=size(),b=contentBox(),k=Math.min((s[0]-24)/b.w,(s[1]-24)/b.h),cx=b.x+b.w/2,cy=b.y+b.h/2;if(s[0]<700&&k<.6){k=.6;cx=b.x+s[0]/(2*k)-12/k;cy=b.y+s[1]/(2*k)-12/k}cam={k:k,cx:cx,cy:cy};apply();persist()}
 function zoomAt(f,sx,sy){var s=size();if(sx==null){sx=s[0]/2;sy=s[1]/2}var wx=cam.cx+(sx-s[0]/2)/cam.k,wy=cam.cy+(sy-s[1]/2)/cam.k,k=Math.min(4,Math.max(.15,cam.k*f));cam={k:k,cx:wx-(sx-s[0]/2)/k,cy:wy-(sy-s[1]/2)/k};apply();persist()}
 function persist(){st.cam=cam;save()}
 /* gestures */
 var ptrs={},g0=null,pinch0=0,pinchCam=null;window.SMcanvas={moved:false};
 function toWorld(cx,cy){var r=sc.getBoundingClientRect(),s=size();return [cam.cx+(cx-r.left-s[0]/2)/cam.k,cam.cy+(cy-r.top-s[1]/2)/cam.k]}
 sc.addEventListener('pointerdown',function(e){if(!st.on)return;if(e.pointerType==='mouse'&&e.button!==0)return;if(e.target.closest('[data-chev]'))return;
  ptrs[e.pointerId]=[e.clientX,e.clientY];SMcanvas.moved=false;
  var n=e.target.closest('[data-id]'),r=e.target.closest('[data-region]'),what=null;
  if(n){what=n.hasAttribute('data-head')?{g:n.dataset.grp}:(n.dataset.grp?{n:n.dataset.id,gid:n.dataset.grp}:null)}else if(r&&G[r.dataset.region]){what={g:r.dataset.region}}
  if(Object.keys(ptrs).length>1){g0=null;pinch0=pd();pinchCam={k:cam.k,cx:cam.cx,cy:cam.cy};return}
  g0={x:e.clientX,y:e.clientY,what:what,cam:{k:cam.k,cx:cam.cx,cy:cam.cy},o:what?(what.g?JSON.parse(JSON.stringify(uoff(what.g))):JSON.parse(JSON.stringify(st.off['n:'+what.n]||{x:0,y:0}))):null,drag:false}});
 function pd(){var p=Object.keys(ptrs).map(function(k){return ptrs[k]});return p.length>1?Math.hypot(p[0][0]-p[1][0],p[0][1]-p[1][1]):0}
 window.addEventListener('pointermove',function(e){if(!ptrs[e.pointerId])return;ptrs[e.pointerId]=[e.clientX,e.clientY];
  if(Object.keys(ptrs).length>1&&pinch0){var d=pd(),p=Object.keys(ptrs).map(function(k){return ptrs[k]}),r=sc.getBoundingClientRect();cam={k:pinchCam.k,cx:pinchCam.cx,cy:pinchCam.cy};zoomAt(d/pinch0,(p[0][0]+p[1][0])/2-r.left,(p[0][1]+p[1][1])/2-r.top);SMcanvas.moved=true;return}
  if(!g0)return;var dx=e.clientX-g0.x,dy=e.clientY-g0.y;if(!g0.drag&&Math.hypot(dx,dy)<THR)return;
  if(!g0.drag){g0.drag=true;SMcanvas.moved=true;hero.classList.add('grabbing')}
  if(g0.what){var k=cam.k,nx=g0.o.x+dx/k,ny=g0.o.y+dy/k;if(g0.what.g)st.off[g0.what.g]={x:nx,y:ny};else st.off['n:'+g0.what.n]={x:nx,y:ny};layout()}
  else{cam={k:g0.cam.k,cx:g0.cam.cx-dx/g0.cam.k,cy:g0.cam.cy-dy/g0.cam.k};apply()}});
 function up(e){if(!ptrs[e.pointerId])return;delete ptrs[e.pointerId];if(Object.keys(ptrs).length<2)pinch0=0;
  if(!Object.keys(ptrs).length){hero.classList.remove('grabbing');if(g0&&g0.drag){if(g0.what)save();else persist()}g0=null}}
 window.addEventListener('pointerup',up);window.addEventListener('pointercancel',up);
 sc.addEventListener('wheel',function(e){if(!st.on)return;e.preventDefault();var r=sc.getBoundingClientRect(),dy=e.deltaMode===1?e.deltaY*16:e.deltaY,dx=e.deltaMode===1?e.deltaX*16:e.deltaX,pinch=e.ctrlKey||e.metaKey;
  if(!pinch&&e.deltaMode===0&&(Math.abs(dx)>0||(Math.abs(dy)<40&&!Number.isInteger(dy)))){cam.cx+=dx/cam.k;cam.cy+=dy/cam.k;apply();persist();return}
  zoomAt(Math.exp(-dy*(pinch?.012:.0022)),e.clientX-r.left,e.clientY-r.top)},{passive:false});
 /* collapse */
 function toggle(id){st.col[id]=!st.col[id];if(!st.col[id])delete st.col[id];layout();sync();save()}
 sc.addEventListener('click',function(e){var c=e.target.closest('[data-chev]');if(c){e.stopPropagation();toggle(c.dataset.chev)}},true);
 sc.addEventListener('keydown',function(e){var c=e.target.closest&&e.target.closest('[data-chev]');if(c&&(e.key==='Enter'||e.key===' ')){e.preventDefault();toggle(c.dataset.chev)}});
 var ids=Object.keys(G).filter(function(i){return G[i].members.length&&G[i].chev});
 var bcol=document.getElementById('cv-col');
 function allCol(){return ids.every(function(i){return st.col[i]})}
 function sync(){var a=allCol();bcol.textContent=a?'Expand all':'Collapse all';bcol.setAttribute('aria-label',a?'Expand all groups':'Collapse all groups')}
 bcol.onclick=function(){var a=allCol();ids.forEach(function(i){if(a)delete st.col[i];else st.col[i]=true});layout();sync();save();if(st.on)fit()};
 /* mode */
 var bon=document.getElementById('cv-on'),cvc=document.getElementById('cvc');
 function mode(on,fitNow){st.on=on;hero.classList.toggle('cv',on);bon.setAttribute('aria-pressed',String(on));cvc.hidden=!on;
  if(on){if(st.cam&&!fitNow){cam={k:st.cam.k,cx:st.cam.cx,cy:st.cam.cy};apply()}else{fit()}}else{svg.setAttribute('viewBox',vb0.join(' '))}
  layout();save()}
 bon.onclick=function(){mode(!st.on,!st.cam)};if(FORCE)bon.hidden=true;if(window.SMNOCOLLAPSE)bcol.hidden=true;
 document.getElementById('cv-in').onclick=function(){zoomAt(1.25)};document.getElementById('cv-out').onclick=function(){zoomAt(0.8)};
 document.getElementById('cv-fit').onclick=fit;
 document.getElementById('cv-reset').onclick=function(){st.off={};st.col={};if(window.SMCOLLAPSED)window.SMCOLLAPSED.forEach(function(i){st.col[i]=true});st.cam=null;layout();sync();fit()};
 window.addEventListener('resize',function(){if(st.on)apply()});
 document.addEventListener('fullscreenchange',function(){if(st.on)setTimeout(fit,50)});
 hero.addEventListener('transitionend',function(){});
 var wasExp=false;new MutationObserver(function(){var x=hero.classList.contains('exp');if(x!==wasExp){wasExp=x;if(st.on)setTimeout(fit,30)}}).observe(hero,{attributes:true,attributeFilter:['class']});
 mode(st.on,false);sync();
})();

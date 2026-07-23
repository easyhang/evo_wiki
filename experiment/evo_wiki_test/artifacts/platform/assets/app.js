
async function initSearch(){
  const input = document.getElementById('search');
  const box = document.getElementById('search-results');
  if(!input || !box) return;
  let data=[];
  let selected=-1;
  const typeNames={concept:'概念',entity:'实体',source:'原文',index:'索引',page:'页面'};
  const indexPath = input.dataset.searchIndex || 'search-index.json';
  const base = indexPath.replace(/search-index\.json$/, '');
  try { data = await (await fetch(indexPath)).json(); } catch(e) { return; }
  function render(){
    const q = input.value.trim().toLowerCase();
    box.innerHTML = '';
    selected=-1;
    if(!q){ input.setAttribute('aria-expanded','false'); return; }
    const found=data.map(p => {
      const title=String(p.title||'').toLowerCase();
      const type=String(typeNames[p.type]||p.type||'').toLowerCase();
      const text=String(p.text||'').toLowerCase();
      let score=title===q?300:title.startsWith(q)?200:title.includes(q)?120:0;
      if(type.includes(q)) score+=40;
      if(text.includes(q)) score+=10;
      return {p,score};
    }).filter(x => x.score>0).sort((a,b) => b.score-a.score || String(a.p.title).localeCompare(String(b.p.title),'zh-CN')).slice(0,8);
    found.forEach((entry,index) => {
      const p=entry.p;
      const a = document.createElement('a');
      a.href = base + p.path;
      a.id='search-option-'+index;
      a.setAttribute('role','option');
      a.setAttribute('aria-selected','false');
      a.textContent = `${p.title} · ${typeNames[p.type]||p.type}`;
      box.appendChild(a);
    });
    input.setAttribute('aria-expanded',found.length?'true':'false');
    if(!found.length) box.textContent='没有匹配页面';
  }
  function move(delta){
    const options=Array.from(box.querySelectorAll('[role="option"]'));
    if(!options.length) return;
    selected=(selected+delta+options.length)%options.length;
    options.forEach((option,index) => {
      const active=index===selected;
      option.classList.toggle('active',active);
      option.setAttribute('aria-selected',active?'true':'false');
    });
    input.setAttribute('aria-activedescendant',options[selected].id);
  }
  input.addEventListener('input', render);
  input.addEventListener('keydown', event => {
    if(event.key==='ArrowDown'){ event.preventDefault(); move(1); }
    else if(event.key==='ArrowUp'){ event.preventDefault(); move(-1); }
    else if(event.key==='Enter' && selected>=0){
      const option=box.querySelectorAll('[role="option"]')[selected];
      if(option){ event.preventDefault(); location.href=option.href; }
    } else if(event.key==='Escape'){
      box.innerHTML=''; selected=-1; input.setAttribute('aria-expanded','false');
      input.removeAttribute('aria-activedescendant');
    }
  });
}
function initSidebar(){
  const sidebar=document.getElementById('wiki-sidebar');
  const toggle=document.getElementById('sidebar-toggle');
  const overlay=document.getElementById('sidebar-overlay');
  if(!sidebar || !toggle || !overlay) return;
  let previousFocus=null;
  function setOpen(open){
    sidebar.classList.toggle('open',open);
    document.body.classList.toggle('sidebar-open',open);
    overlay.hidden=!open;
    toggle.setAttribute('aria-expanded',open?'true':'false');
    if(open){ previousFocus=document.activeElement; const target=sidebar.querySelector('a,input,button,summary'); if(target) target.focus(); }
    else if(previousFocus && typeof previousFocus.focus==='function') previousFocus.focus();
  }
  toggle.addEventListener('click',() => setOpen(!sidebar.classList.contains('open')));
  overlay.addEventListener('click',() => setOpen(false));
  sidebar.addEventListener('click',event => { if(event.target.closest('a') && matchMedia('(max-width: 980px)').matches) setOpen(false); });
  document.addEventListener('keydown',event => {
    if(event.key==='Escape' && sidebar.classList.contains('open')){ event.preventDefault(); setOpen(false); }
    if(event.key==='Tab' && sidebar.classList.contains('open')){
      const focusable=Array.from(sidebar.querySelectorAll('a,input,button,summary,[tabindex]:not([tabindex="-1"])')).filter(node => !node.hidden);
      if(!focusable.length) return;
      const first=focusable[0],last=focusable[focusable.length-1];
      if(event.shiftKey && document.activeElement===first){ event.preventDefault(); last.focus(); }
      else if(!event.shiftKey && document.activeElement===last){ event.preventDefault(); first.focus(); }
    }
  });
}
function initRelatedPanel(){
  document.querySelectorAll('.related-panel').forEach(panel => {
    const items = Array.from(panel.querySelectorAll('details'));
    const expand = panel.querySelector('[data-related-expand]');
    const collapse = panel.querySelector('[data-related-collapse]');
    if(expand) expand.addEventListener('click', () => items.forEach(d => d.open = true));
    if(collapse) collapse.addEventListener('click', () => items.forEach(d => d.open = false));
  });
}
function initEnhancements(){
  initSidebar();
  const activeNav=document.querySelector('.nav-link.active');
  if(activeNav){
    const group=activeNav.closest('details.nav-group');
    if(group) group.open=true;
  }
  initRelatedPanel();
  if(window.mermaid) mermaid.initialize({ startOnLoad: true, theme: 'default' });
  if(window.renderMathInElement) renderMathInElement(document.body, { delimiters: [
    {left: '$$', right: '$$', display: true},
    {left: '$', right: '$', display: false}
  ]});
}
initSearch();
window.addEventListener('load', initEnhancements);

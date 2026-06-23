
async function initSearch(){
  const input = document.getElementById('search');
  const box = document.getElementById('search-results');
  if(!input || !box) return;
  let data=[];
  const indexPath = input.dataset.searchIndex || 'search-index.json';
  const base = indexPath.replace(/search-index\.json$/, '');
  try { data = await (await fetch(indexPath)).json(); } catch(e) { return; }
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    box.innerHTML = '';
    if(!q) return;
    data.filter(p => (p.title + ' ' + p.type + ' ' + p.text).toLowerCase().includes(q)).slice(0,8).forEach(p => {
      const a = document.createElement('a');
      a.href = base + p.path;
      a.textContent = `${p.title} · ${p.type}`;
      box.appendChild(a);
    });
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
  initRelatedPanel();
  if(window.mermaid) mermaid.initialize({ startOnLoad: true, theme: 'default' });
  if(window.renderMathInElement) renderMathInElement(document.body, { delimiters: [
    {left: '$$', right: '$$', display: true},
    {left: '$', right: '$', display: false}
  ]});
}
initSearch();
window.addEventListener('load', initEnhancements);

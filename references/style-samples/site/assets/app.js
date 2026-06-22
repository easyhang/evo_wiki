
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
function initEnhancements(){
  if(window.mermaid) mermaid.initialize({ startOnLoad: true, theme: 'default' });
  if(window.renderMathInElement) renderMathInElement(document.body, { delimiters: [
    {left: '$$', right: '$$', display: true},
    {left: '$', right: '$', display: false}
  ]});
}
initSearch();
window.addEventListener('load', initEnhancements);

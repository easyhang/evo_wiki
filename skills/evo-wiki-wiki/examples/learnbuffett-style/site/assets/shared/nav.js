
/* Cross-app topbar: [Wiki | 问答 | 图谱]. Renders into #evo-topbar, highlights
   the current surface by URL. Links point at / (Wiki) and /app (问答/图谱). The
   brand name is read from <body data-site-title> so it matches the sidebar logo;
   both wiki pages and the SPA load this single file. */
(function () {
  var mount = document.getElementById('evo-topbar');
  if (!mount) return;
  var siteTitle = (document.body && document.body.dataset && document.body.dataset.siteTitle) || 'Evo Wiki';
  var onApp = location.pathname.indexOf('/app') === 0;
  var hash = location.hash || '';
  var onGraph = onApp && (hash.indexOf('#graph') === 0 || hash.indexOf('#entity/') === 0);
  var tabs = [
    { key: 'wiki', label: 'Wiki', href: '/', active: !onApp },
    { key: 'qa', label: '问答', href: '/app', active: onApp && !onGraph },
    { key: 'graph', label: '图谱', href: '/app#graph', active: onGraph }
  ];
  var links = tabs.map(function (t) {
    var cls = 'evo-topbar-link' + (t.active ? ' active' : '');
    return '<a class="' + cls + '" href="' + t.href + '">' + t.label + '</a>';
  }).join('');
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]; }); }
  mount.innerHTML = '<div class="evo-topbar-inner"><a class="evo-topbar-brand" href="/">' + esc(siteTitle) + '</a><nav class="evo-topbar-nav">' + links + '</nav></div>';
})();

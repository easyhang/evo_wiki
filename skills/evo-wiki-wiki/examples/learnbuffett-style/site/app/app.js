
/* Read-only LightRAG-inspired SPA: query parameters, label search, subgraph view. */
(function () {
  var QA = document.getElementById('view-qa');
  var GRAPH = document.getElementById('view-graph');
  var ENTITY = document.getElementById('view-entity');
  var chatHistory = [];
  var selectedGraphNode = null;

  function el(tag, cls, html) { var n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; }
  function escapeHtml(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]; }); }
  async function json(url, opts) { var r = await fetch(url, opts); if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }
  function asArray(v) { return Array.isArray(v) ? v : []; }
  function nodeLabel(n) { return n.label || n.id || (n.properties && (n.properties.entity_id || n.properties.name)) || 'unknown'; }
  function nodeId(n) { return n.id || n.label || nodeLabel(n); }
  function edgeSource(e) { return e.source || e.from || e.src_id || (e.properties && e.properties.src_id); }
  function edgeTarget(e) { return e.target || e.to || e.tgt_id || (e.properties && e.properties.tgt_id); }

  function pageShell(title, sub, main, aside) {
    return '<div class="spa-shell"><div class="spa-article"><h1 class="spa-h1">' + escapeHtml(title) + '</h1><p class="spa-sub">' + escapeHtml(sub) + '</p>' + main + '</div><aside class="spa-aside">' + aside + '</aside></div>';
  }

  function settingsHtml() {
    return '<div class="spa-card"><h3>LightRAG 参数</h3>' +
      '<div class="spa-field"><label>Query Mode</label><select id="qa-mode" class="spa-select"><option value="mix">mix</option><option value="hybrid">hybrid</option><option value="local">local</option><option value="global">global</option><option value="naive">naive</option><option value="bypass">bypass</option></select></div>' +
      '<div class="spa-grid-2"><div class="spa-field"><label>Top K</label><input id="qa-top-k" class="spa-input" type="number" min="1" value="40"></div><div class="spa-field"><label>Chunk Top K</label><input id="qa-chunk-top-k" class="spa-input" type="number" min="1" value="20"></div></div>' +
      '<div class="spa-grid-2"><div class="spa-field"><label>Entity Tokens</label><input id="qa-entity-tokens" class="spa-input" type="number" min="1" value="6000"></div><div class="spa-field"><label>Relation Tokens</label><input id="qa-relation-tokens" class="spa-input" type="number" min="1" value="8000"></div></div>' +
      '<div class="spa-field"><label>Total Tokens</label><input id="qa-total-tokens" class="spa-input" type="number" min="1" value="30000"></div>' +
      '<div class="spa-row wrap"><label class="spa-muted"><input id="qa-refs" type="checkbox" checked> References</label><label class="spa-muted"><input id="qa-context" type="checkbox"> Context only</label><label class="spa-muted"><input id="qa-prompt" type="checkbox"> Prompt only</label></div>' +
      '<p class="spa-muted">参考 LightRAG WebUI：支持 mode、top_k、chunk_top_k、token budget、context/prompt-only 与 references。</p></div>';
  }

  function renderRefs(refs) {
    refs = asArray(refs);
    if (!refs.length) return '';
    return '<div class="spa-refs"><h4>参考来源 (' + refs.length + ')</h4>' + refs.map(function (ref, i) {
      var file = ref.file_path || ref.source || ref.path || ref.file || ('来源 ' + (i + 1));
      var content = asArray(ref.content).join('\n').slice(0, 260);
      return '<a class="spa-ref" href="#"><span>' + escapeHtml(file) + '</span>' + (content ? '<small>' + escapeHtml(content) + '</small>' : '') + '</a>';
    }).join('') + '</div>';
  }

  function appendMessage(box, role, content, refs, isError) {
    var msg = el('div', 'spa-message ' + (role === 'user' ? 'user' : 'assistant'));
    msg.innerHTML = '<div class="spa-role">' + escapeHtml(role) + '</div><div class="spa-answer ' + (isError ? 'spa-error' : '') + '">' + escapeHtml(content) + '</div>' + renderRefs(refs);
    box.appendChild(msg); box.scrollIntoView({ block: 'end' });
  }

  function queryPayload(q) {
    function num(id) { var v = parseInt(document.getElementById(id).value, 10); return isNaN(v) ? undefined : v; }
    return {
      query: q,
      mode: document.getElementById('qa-mode').value,
      top_k: num('qa-top-k'),
      chunk_top_k: num('qa-chunk-top-k'),
      max_entity_tokens: num('qa-entity-tokens'),
      max_relation_tokens: num('qa-relation-tokens'),
      max_total_tokens: num('qa-total-tokens'),
      include_references: document.getElementById('qa-refs').checked,
      only_need_context: document.getElementById('qa-context').checked || undefined,
      only_need_prompt: document.getElementById('qa-prompt').checked || undefined,
      conversation_history: chatHistory.slice(-8)
    };
  }

  function renderQA() {
    chatHistory = [];
    QA.innerHTML = pageShell('问答', 'LightRAG 只读问答。支持 LightRAG WebUI 的核心检索参数，并保留对话上下文。',
      '<div class="spa-card"><textarea id="qa-input" class="spa-textarea" placeholder="输入问题。也可以用 /hybrid、/mix、/local、/global、/naive、/bypass 前缀临时切换模式…"></textarea><div class="spa-row wrap"><button id="qa-send" class="spa-btn">提问</button><button id="qa-clear" class="spa-btn secondary">清空</button></div></div><div id="qa-chat" class="spa-chat"></div>',
      settingsHtml());
    var input = document.getElementById('qa-input'), send = document.getElementById('qa-send'), chat = document.getElementById('qa-chat');
    async function ask() {
      var raw = input.value.trim(); if (!raw) return;
      var m = raw.match(/^[/](naive|local|global|hybrid|mix|bypass)\s+([\s\S]+)/); var q = raw;
      if (m) { document.getElementById('qa-mode').value = m[1]; q = m[2]; }
      appendMessage(chat, 'user', raw); chatHistory.push({ role:'user', content:q }); input.value = ''; send.disabled = true;
      var loading = el('div', 'spa-loading', '检索中…'); chat.appendChild(loading);
      try {
        var data = await json('/api/query', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(queryPayload(q)) });
        loading.remove(); var answer = data.response || '(空回答)'; appendMessage(chat, 'assistant', answer, data.references || data.ref_results || []); chatHistory.push({ role:'assistant', content:answer });
      } catch (e) { loading.remove(); appendMessage(chat, 'assistant', '提问失败：' + e.message + '（确认 LightRAG lane 已构建且服务在运行）', [], true); }
      finally { send.disabled = false; }
    }
    send.addEventListener('click', ask); input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) ask(); });
    document.getElementById('qa-clear').addEventListener('click', function () { chatHistory = []; chat.innerHTML = ''; });
  }

  function graphAsideHtml() {
    return '<div class="spa-card"><h3>图谱控制</h3><div class="spa-field"><label>Label 搜索</label><input id="graph-label" class="spa-input" placeholder="输入实体 label；空值加载热门 label"></div><div class="spa-grid-2"><div class="spa-field"><label>Max Depth</label><input id="graph-depth" class="spa-input" type="number" min="1" value="3"></div><div class="spa-field"><label>Max Nodes</label><input id="graph-nodes" class="spa-input" type="number" min="1" value="1000"></div></div><div class="spa-row wrap"><button id="graph-load" class="spa-btn">取子图</button><button id="graph-popular" class="spa-btn secondary">热门</button></div><div id="graph-labels" class="spa-chiprow"></div></div><div class="spa-card"><h3>节点详情</h3><div id="graph-detail" class="spa-muted">点击图中节点查看属性，并进入实体枢纽页。</div></div>';
  }

  async function loadPopular(container, input) {
    container.innerHTML = '<span class="spa-loading">加载热门 label…</span>';
    try {
      var labels = await json('/api/graph/label/popular?limit=24');
      container.innerHTML = asArray(labels).map(function (l) { return '<button class="spa-chip" data-label="' + escapeHtml(l) + '">' + escapeHtml(l) + '</button>'; }).join('') || '<span class="spa-muted">暂无热门 label</span>';
      container.querySelectorAll('[data-label]').forEach(function (b) { b.addEventListener('click', function () { input.value = b.getAttribute('data-label'); document.getElementById('graph-load').click(); }); });
    } catch (e) { container.innerHTML = '<span class="spa-error">热门 label 加载失败：' + escapeHtml(e.message) + '</span>'; }
  }

  function renderGraph() {
    GRAPH.innerHTML = pageShell('图谱', '按 LightRAG label 搜索、加载子图、查看节点属性。浏览器只取一个子图，不拉全量知识图谱。',
      '<div class="spa-card"><h3>知识图谱</h3><div id="graph-canvas" class="spa-graph-canvas"><p class="spa-loading">选择一个 label 后加载子图。</p></div><div id="graph-stats" class="spa-statbar"></div></div>', graphAsideHtml());
    var input = document.getElementById('graph-label'), labels = document.getElementById('graph-labels'), canvas = document.getElementById('graph-canvas');
    async function searchLabels(q) {
      if (!q.trim()) return loadPopular(labels, input);
      try {
        var found = await json('/api/graph/label/search?q=' + encodeURIComponent(q) + '&limit=12');
        labels.innerHTML = asArray(found).map(function (l) { return '<button class="spa-chip" data-label="' + escapeHtml(l) + '">' + escapeHtml(l) + '</button>'; }).join('');
        labels.querySelectorAll('[data-label]').forEach(function (b) { b.addEventListener('click', function () { input.value = b.getAttribute('data-label'); }); });
      } catch (_) {}
    }
    async function load(label) {
      label = (label || input.value || '').trim(); if (!label) return loadPopular(labels, input);
      canvas.innerHTML = '<p class="spa-loading">取子图中…</p>';
      try {
        var depth = parseInt(document.getElementById('graph-depth').value, 10) || 3;
        var maxNodes = parseInt(document.getElementById('graph-nodes').value, 10) || 1000;
        var data = await json('/api/graphs?label=' + encodeURIComponent(label) + '&max_depth=' + depth + '&max_nodes=' + maxNodes);
        renderSubgraph(canvas, data, label, true);
      } catch (e) { canvas.innerHTML = '<p class="spa-error">取子图失败：' + escapeHtml(e.message) + '</p>'; }
    }
    input.addEventListener('input', function () { clearTimeout(input._t); input._t = setTimeout(function () { searchLabels(input.value); }, 220); });
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') load(); });
    document.getElementById('graph-load').addEventListener('click', function () { load(); });
    document.getElementById('graph-popular').addEventListener('click', function () { loadPopular(labels, input); });
    loadPopular(labels, input);
  }

  function renderDetail(node) {
    var box = document.getElementById('graph-detail'); if (!box) return;
    var label = nodeLabel(node); var props = node.properties || {};
    var rows = Object.keys(props).slice(0, 12).map(function (k) { return '<dt>' + escapeHtml(k) + '</dt><dd>' + escapeHtml(typeof props[k] === 'object' ? JSON.stringify(props[k]) : props[k]) + '</dd>'; }).join('');
    box.innerHTML = '<strong>' + escapeHtml(label) + '</strong><dl class="spa-props">' + rows + '</dl><a class="spa-entity-link" href="#entity/' + encodeURIComponent(label) + '">打开实体枢纽页 →</a>';
  }

  function renderSubgraph(container, data, rootLabel, clickable) {
    container.innerHTML = ''; selectedGraphNode = null;
    var nodes = asArray(data.nodes), edges = asArray(data.edges || data.relationships);
    document.getElementById('graph-stats') && (document.getElementById('graph-stats').innerHTML = '<span class="spa-stat">Nodes ' + nodes.length + '</span><span class="spa-stat">Edges ' + edges.length + '</span><span class="spa-stat">Root ' + escapeHtml(rootLabel) + '</span>');
    if (!nodes.length) { container.appendChild(el('p', 'spa-loading', '该实体暂无图谱邻域。')); return; }
    var w = 920, h = 520, cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 80, pos = {}, byId = {};
    nodes.forEach(function (n) { byId[nodeId(n)] = n; byId[nodeLabel(n)] = n; });
    nodes.forEach(function (n, i) { var id = nodeId(n); if (i === 0 || nodeLabel(n) === rootLabel) { pos[id] = {x:cx,y:cy}; return; } var a = (i - 1) / Math.max(1, nodes.length - 1) * 2 * Math.PI; pos[id] = {x:cx + R * Math.cos(a), y:cy + R * Math.sin(a)}; });
    var svg = '<svg viewBox="0 0 ' + w + ' ' + h + '" xmlns="http://www.w3.org/2000/svg">';
    edges.forEach(function (e, i) { var s = pos[edgeSource(e)], t = pos[edgeTarget(e)]; if (!s || !t) return; var mx=(s.x+t.x)/2, my=(s.y+t.y)/2; svg += '<line class="spa-graph-edge" x1="' + s.x + '" y1="' + s.y + '" x2="' + t.x + '" y2="' + t.y + '"/>'; if (i < 40 && (e.type || (e.properties && e.properties.keywords))) svg += '<text class="spa-graph-edge-label" x="' + mx + '" y="' + my + '">' + escapeHtml(String(e.type || e.properties.keywords).slice(0, 22)) + '</text>'; });
    nodes.forEach(function (n) { var id = nodeId(n), label = nodeLabel(n), p = pos[id]; if (!p) return; var root = label === rootLabel; svg += '<circle class="spa-graph-node' + (root ? ' selected' : '') + '" cx="' + p.x + '" cy="' + p.y + '" r="' + (root ? 12 : 9) + '" data-id="' + escapeHtml(id) + '"/>'; svg += '<text class="spa-graph-label" x="' + p.x + '" y="' + (p.y + 24) + '">' + escapeHtml(label.slice(0, 20)) + '</text>'; });
    svg += '</svg>'; container.innerHTML = svg;
    container.querySelectorAll('.spa-graph-node').forEach(function (c) { c.addEventListener('click', function () { var n = byId[c.getAttribute('data-id')]; selectedGraphNode = n; renderDetail(n); if (clickable && n) location.hash = '#entity/' + encodeURIComponent(nodeLabel(n)); }); });
  }

  function renderEntity(slug) {
    ENTITY.innerHTML = pageShell(slug, '实体枢纽页：图谱邻域 + 预置问答 + Wiki 原文链接。',
      '<div class="spa-card"><h3>图谱邻域</h3><div id="entity-graph" class="spa-graph-canvas"><p class="spa-loading">加载中…</p></div><div id="graph-stats" class="spa-statbar"></div></div><div class="spa-card"><h3>预置问答</h3><div class="spa-row"><input id="entity-q" class="spa-input" value="关于 ' + escapeHtml(slug) + '，请详细介绍。"><button id="entity-ask" class="spa-btn">提问</button></div><div id="entity-answer"></div></div><div class="spa-card"><h3>Wiki 原文</h3><a class="spa-entity-link" href="/entities/' + encodeURIComponent(slug) + '.html">在 Wiki 中查看该实体 →</a></div>',
      '<div class="spa-card"><h3>节点详情</h3><div id="graph-detail" class="spa-muted">点击图中节点查看属性。</div></div>');
    (async function () { try { renderSubgraph(document.getElementById('entity-graph'), await json('/api/graphs?label=' + encodeURIComponent(slug) + '&max_depth=3&max_nodes=1000'), slug, false); } catch (e) { document.getElementById('entity-graph').innerHTML = '<p class="spa-error">图谱加载失败：' + escapeHtml(e.message) + '</p>'; } })();
    document.getElementById('entity-ask').addEventListener('click', async function () { var out = document.getElementById('entity-answer'), q = document.getElementById('entity-q').value; out.innerHTML = '<p class="spa-loading">检索中…</p>'; try { var data = await json('/api/query', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ query:q, mode:'mix', include_references:true, top_k:40, chunk_top_k:20 }) }); out.innerHTML = '<div class="spa-answer">' + escapeHtml(data.response || '(空回答)') + '</div>' + renderRefs(data.references || []); } catch (e) { out.innerHTML = '<p class="spa-error">提问失败：' + escapeHtml(e.message) + '</p>'; } });
  }

  function route() { QA.classList.remove('active'); GRAPH.classList.remove('active'); ENTITY.classList.remove('active'); var hash = location.hash || ''; if (hash.indexOf('#entity/') === 0) { renderEntity(decodeURIComponent(hash.slice('#entity/'.length))); ENTITY.classList.add('active'); } else if (hash === '#graph') { renderGraph(); GRAPH.classList.add('active'); } else { renderQA(); QA.classList.add('active'); } }
  window.addEventListener('hashchange', route); window.addEventListener('load', route);
})();

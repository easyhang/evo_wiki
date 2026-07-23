
/* Read-only LightRAG-inspired SPA: query parameters, label search, subgraph view. */
(function () {
  var QA = document.getElementById('view-qa');
  var GRAPH = document.getElementById('view-graph');
  var ENTITY = document.getElementById('view-entity');
  var chatHistory = [];
  var selectedGraphNode = null;
  var registry = { entities:[], sources:{} };
  var currentGraphEdges = [];
  var currentGraphById = {};
  var HISTORY_TURNS = 3;
  var GRAPH_MAX_DEPTH = 2;
  var GRAPH_MAX_NODES = 50;
  var GRAPH_POPULAR_LIMIT = 12;
  var BODY_DATA = (document.body && document.body.dataset) || {};
  var FEATURES = {
    qa: BODY_DATA.navQa !== 'false',
    graph: BODY_DATA.navGraph !== 'false',
    entity: BODY_DATA.navEntity !== 'false'
  };

  function el(tag, cls, html) { var n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; }
  function escapeHtml(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]; }); }
  async function json(url, opts) { var r = await fetch(url, opts); if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }
  function asArray(v) { return Array.isArray(v) ? v : []; }
  function refContent(v) { if (Array.isArray(v)) return v.filter(function (part) { return typeof part === 'string' && part; }); return typeof v === 'string' && v ? [v] : []; }
  async function loadRegistry() {
    try {
      var value = await json('/wiki-registry.json');
      if (value && value.schema_version === 1) registry = value;
    } catch (_) {}
  }
  function basename(value) {
    return String(value || '').replace(/\\/g, '/').split('/').pop().toLowerCase();
  }
  function entityFor(value) {
    var key = decodeURIComponent(String(value || '')).toLowerCase();
    return asArray(registry.entities).find(function (entity) {
      var pathSlug = String(entity.wiki_path || '').split('/').pop().replace(/[.]html$/, '');
      return [entity.title, entity.graph_label, pathSlug].concat(asArray(entity.aliases)).some(function (candidate) {
        return String(candidate || '').toLowerCase() === key;
      });
    }) || null;
  }
  function sourceFor(value) {
    var sources = registry.sources || {};
    return sources[basename(value)] || null;
  }
  function safeWikiHref(path) {
    return typeof path === 'string' && /^[a-z0-9_./%-]+$/i.test(path) && path.indexOf('..') < 0 ? '/' + path.replace(/^[/]+/, '') : null;
  }
  function stripModelReferences(text) {
    return String(text || '').replace(/\n#{1,6}\s*(?:references?|参考文献|引用来源)\s*\n[\s\S]*$/i, '').trim();
  }
  function safeInline(text) {
    var value = escapeHtml(text);
    value = value.replace(/`([^`]+)`/g, '<code>$1</code>');
    value = value.replace(/[*][*]([^*]+)[*][*]/g, '<strong>$1</strong>');
    value = value.replace(/[*]([^*]+)[*]/g, '<em>$1</em>');
    value = value.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" rel="noopener noreferrer">$1</a>');
    return value;
  }
  function safeMarkdown(text) {
    var lines = stripModelReferences(text).split(/\r?\n/), out = [], list = null, code = false, codeLines = [];
    function closeList() { if (list) { out.push('</' + list + '>'); list = null; } }
    lines.forEach(function (raw) {
      var line = raw.trim();
      if (/^```/.test(line)) {
        closeList();
        if (code) { out.push('<pre><code>' + escapeHtml(codeLines.join('\n')) + '</code></pre>'); codeLines = []; }
        code = !code; return;
      }
      if (code) { codeLines.push(raw); return; }
      if (!line) { closeList(); return; }
      var heading = line.match(/^(#{1,3})\s+(.+)/);
      if (heading) { closeList(); var level = heading[1].length + 1; out.push('<h' + level + '>' + safeInline(heading[2]) + '</h' + level + '>'); return; }
      var bullet = line.match(/^[-*]\s+(.+)/);
      var ordered = line.match(/^\d+[.]\s+(.+)/);
      if (bullet || ordered) {
        var wanted = ordered ? 'ol' : 'ul';
        if (list !== wanted) { closeList(); list = wanted; out.push('<' + list + '>'); }
        out.push('<li>' + safeInline((bullet || ordered)[1]) + '</li>'); return;
      }
      closeList();
      if (/^>\s+/.test(line)) out.push('<blockquote>' + safeInline(line.replace(/^>\s+/, '')) + '</blockquote>');
      else out.push('<p>' + safeInline(line) + '</p>');
    });
    closeList();
    if (codeLines.length) out.push('<pre><code>' + escapeHtml(codeLines.join('\n')) + '</code></pre>');
    return out.join('');
  }
  function evidenceTokens(text) {
    var out = [], lower = String(text || '').toLowerCase(), ascii = lower.match(/[a-z0-9_]{2,}/g) || [];
    ascii.forEach(function (token) { if (out.indexOf(token) < 0) out.push(token); });
    var cjk = lower.match(/[一-鿿]/g) || [], generic = ['请给','给出','当前','语料','没有','涉及','说明','依据','并说','的确','机构','成立','年份'], genericChars = '请给出当前语料没有涉及的并说明依据份';
    for (var i = 0; i + 1 < cjk.length; i++) { var pair = cjk[i] + cjk[i + 1]; if (generic.indexOf(pair) < 0 && pair.split('').every(function (ch) { return genericChars.indexOf(ch) < 0; }) && out.indexOf(pair) < 0) out.push(pair); }
    return out;
  }
  function evidenceGate(query, data) {
    var refs = asArray(data && (data.references || data.ref_results));
    if (!refs.length) return { refs: [], warning: null };
    var tokens = evidenceTokens(query); if (tokens.length < 2) return { refs: refs, warning: null };
    var content = refs.map(function (ref) { return refContent(ref && ref.content).join(' '); }).join(' ').toLowerCase();
    if (!content) return { refs: refs, warning: '返回了引用，但没有 chunk 内容，无法完成证据校验。' };
    var matched = tokens.some(function (token) { return content.indexOf(token) >= 0; });
    if (!matched) return { refs: [], warning: '回答未找到与问题直接匹配的证据，已隐藏不相关引用。' };
    return { refs: refs, warning: null };
  }
  function nodeLabel(n) { return n.label || n.id || (n.properties && (n.properties.entity_id || n.properties.name)) || 'unknown'; }
  function nodeId(n) { return n.id || n.label || nodeLabel(n); }
  function edgeSource(e) { return e.source || e.from || e.src_id || (e.properties && e.properties.src_id); }
  function edgeTarget(e) { return e.target || e.to || e.tgt_id || (e.properties && e.properties.tgt_id); }

  function pageShell(title, sub, main, aside) {
    return '<div class="spa-shell"><div class="spa-article"><h1 class="spa-h1">' + escapeHtml(title) + '</h1><p class="spa-sub">' + escapeHtml(sub) + '</p>' + main + '</div><aside class="spa-aside">' + aside + '</aside></div>';
  }

  function settingsHtml() {
    return '<div class="spa-card"><h3>受控检索参数</h3>' +
      '<div class="spa-field"><label for="qa-mode">Query Mode</label><select id="qa-mode" class="spa-select"><option value="mix">mix</option><option value="hybrid">hybrid</option><option value="local">local</option><option value="global">global</option><option value="naive">naive</option></select></div>' +
      '<div class="spa-field"><label for="qa-top-k">Top K</label><input id="qa-top-k" class="spa-input" type="number" min="1" max="100" value="20"></div>' +
      '<p class="spa-muted">最多携带最近 ' + HISTORY_TURNS + ' 轮已验证问答。Shadow、拒绝和错误不会进入上下文。</p></div>';
  }

  function renderRefs(refs) {
    refs = asArray(refs);
    if (!refs.length) return '';
    return '<div class="spa-refs"><h4>参考来源 (' + refs.length + ')</h4>' + refs.map(function (ref, i) {
      var file = ref.source_label || ref.file_path || ref.source || ref.path || ref.file || ('来源 ' + (i + 1));
      var content = refContent(ref.content).join('\n').slice(0, 260);
      var mapped = sourceFor(file), href = mapped && safeWikiHref(mapped.wiki_path);
      var inner = '<span>' + escapeHtml(mapped && mapped.title ? mapped.title : file) + '</span>' + (content ? '<small>' + escapeHtml(content) + '</small>' : '');
      return href ? '<a class="spa-ref" href="' + escapeHtml(href) + '">' + inner + '</a>' : '<div class="spa-ref no-link">' + inner + '</div>';
    }).join('') + '</div>';
  }

  function resultHtml(data) {
    var verdict = data && data.evidence && data.evidence.verdict;
    var answer = data && data.answer;
    if (verdict === 'passed' && data.status === 'answered') {
      return '<div class="spa-status verified">已验证</div><div class="spa-answer">' + safeMarkdown(answer) + '</div>' + renderRefs(data.citations);
    }
    if (verdict === 'shadow_failed' && data.status === 'answered') {
      var codes = asArray(data.evidence.codes).join(', ');
      var audit = data.audit_id ? '审核编号：' + data.audit_id : '已进入受控审核';
      return '<div class="spa-status warning">未验证 · Shadow</div><p class="spa-audit">' + escapeHtml(audit + (codes ? ' · ' + codes : '')) + '</p><details class="spa-shadow"><summary>查看未验证回答（默认折叠）</summary><div class="spa-answer">' + safeMarkdown(answer) + '</div></details>' + renderRefs(data.citations);
    }
    var label = data.status === 'maintenance' ? '系统处于受控维护窗口，请稍后再试。' : data.status === 'needs_audit' ? '该问题需要人工审核，当前不展示回答正文。' : data.status === 'failed' ? '查询失败，当前不展示回答正文。' : '当前证据不足，系统已拒绝回答。';
    return '<div class="spa-status blocked">' + escapeHtml(data.status || 'refused') + '</div><div class="spa-error">' + escapeHtml(label) + '</div>';
  }

  function appendMessage(box, role, content, data, isError) {
    var msg = el('div', 'spa-message ' + (role === 'user' ? 'user' : 'assistant'));
    msg.innerHTML = '<div class="spa-role">' + escapeHtml(role) + '</div>' + (data ? resultHtml(data) : '<div class="spa-answer ' + (isError ? 'spa-error' : '') + '">' + escapeHtml(content) + '</div>');
    box.appendChild(msg); box.scrollIntoView({ block: 'end' });
  }

  function queryPayload(q) {
    function num(id) { var v = parseInt(document.getElementById(id).value, 10); return isNaN(v) ? undefined : v; }
    var payload = {
      schema_version: 1,
      query: q,
      mode: document.getElementById('qa-mode').value,
      top_k: num('qa-top-k')
    };
    if (HISTORY_TURNS > 0 && chatHistory.length) {
      payload.conversation_history = chatHistory.slice(-HISTORY_TURNS * 2);
    }
    return payload;
  }

  function renderQA() {
    chatHistory = [];
    QA.innerHTML = pageShell('问答', 'LightRAG 只读问答。支持 LightRAG WebUI 的核心检索参数，并保留对话上下文。',
      '<div class="spa-card"><label for="qa-input" class="spa-muted">问题</label><textarea id="qa-input" class="spa-textarea" placeholder="输入问题。也可以用 /hybrid、/mix、/local、/global、/naive 前缀临时切换模式…"></textarea><div class="spa-row wrap"><button id="qa-send" class="spa-btn">提问</button><button id="qa-clear" class="spa-btn secondary">清空</button></div></div><div id="qa-chat" class="spa-chat" aria-live="polite"></div>',
      settingsHtml());
    document.getElementById('qa-mode').value = 'mix';
    var input = document.getElementById('qa-input'), send = document.getElementById('qa-send'), chat = document.getElementById('qa-chat');
    async function ask() {
      var raw = input.value.trim(); if (!raw) return;
      var m = raw.match(/^[/](naive|local|global|hybrid|mix)\s+([\s\S]+)/); var q = raw;
      if (m) { document.getElementById('qa-mode').value = m[1]; q = m[2]; }
      appendMessage(chat, 'user', raw); input.value = ''; send.disabled = true;
      var loading = el('div', 'spa-loading', '检索中…'); chat.appendChild(loading);
      try {
        var data = await json('/api/query', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(queryPayload(q)) });
        loading.remove(); appendMessage(chat, 'assistant', '', data, data.status !== 'answered'); if (data.status === 'answered' && data.evidence && data.evidence.verdict === 'passed') { chatHistory.push({ role:'user', content:q }, { role:'assistant', content:data.answer }); chatHistory = HISTORY_TURNS > 0 ? chatHistory.slice(-HISTORY_TURNS * 2) : []; }
      } catch (e) { loading.remove(); appendMessage(chat, 'assistant', '提问失败：' + e.message + '（确认 LightRAG lane 已构建且服务在运行）', null, true); }
      finally { send.disabled = false; }
    }
    send.addEventListener('click', ask); input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) ask(); });
    document.getElementById('qa-clear').addEventListener('click', function () { chatHistory = []; chat.innerHTML = ''; });
  }

  function graphAsideHtml() {
    return '<div class="spa-card"><h3>图谱控制</h3><div class="spa-field"><label for="graph-label">Label 搜索</label><input id="graph-label" class="spa-input" placeholder="输入实体 label；空值加载热门 label"></div><div class="spa-grid-2"><div class="spa-field"><label for="graph-depth">Max Depth</label><input id="graph-depth" class="spa-input" type="number" min="1" max="3" value="' + GRAPH_MAX_DEPTH + '"></div><div class="spa-field"><label for="graph-nodes">Max Nodes</label><input id="graph-nodes" class="spa-input" type="number" min="10" max="200" value="' + GRAPH_MAX_NODES + '"></div></div><div class="spa-row wrap"><button id="graph-load" class="spa-btn">取子图</button><button id="graph-popular" class="spa-btn secondary">热门</button></div><div id="graph-labels" class="spa-chiprow" aria-live="polite"></div></div><div class="spa-card"><h3>节点详情</h3><div id="graph-detail" class="spa-muted" aria-live="polite">点击图中节点查看属性和可用操作。</div></div>';
  }

  async function loadPopular(container, input) {
    container.innerHTML = '<span class="spa-loading">加载热门 label…</span>';
    try {
      var labels = await json('/api/graph/label/popular?limit=' + GRAPH_POPULAR_LIMIT);
      container.innerHTML = asArray(labels).map(function (l) { return '<button class="spa-chip" data-label="' + escapeHtml(l) + '">' + escapeHtml(l) + '</button>'; }).join('') || '<span class="spa-muted">暂无热门 label</span>';
      container.querySelectorAll('[data-label]').forEach(function (b) { b.addEventListener('click', function () { input.value = b.getAttribute('data-label'); document.getElementById('graph-load').click(); }); });
    } catch (e) { container.innerHTML = '<span class="spa-error">热门 label 加载失败：' + escapeHtml(e.message) + '</span>'; }
  }

  function renderGraph() {
    GRAPH.innerHTML = pageShell('图谱', '按 LightRAG label 搜索、加载子图、查看节点属性。浏览器只取一个子图，不拉全量知识图谱。',
      '<div class="spa-card"><h3>知识图谱</h3><div class="spa-graph-tools" aria-label="图谱缩放"><button id="graph-zoom-out" type="button" aria-label="缩小">−</button><button id="graph-reset" type="button" aria-label="复位">复位</button><button id="graph-zoom-in" type="button" aria-label="放大">＋</button></div><div id="graph-canvas" class="spa-graph-canvas" tabindex="0"><p class="spa-loading">选择一个 label 后加载子图。</p></div><div id="graph-stats" class="spa-statbar" aria-live="polite"></div></div>', graphAsideHtml());
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
        var depth = Math.max(1, Math.min(3, parseInt(document.getElementById('graph-depth').value, 10) || GRAPH_MAX_DEPTH));
        var maxNodes = Math.max(10, Math.min(200, parseInt(document.getElementById('graph-nodes').value, 10) || GRAPH_MAX_NODES));
        document.getElementById('graph-depth').value = depth;
        document.getElementById('graph-nodes').value = maxNodes;
        var data = await json('/api/graphs?label=' + encodeURIComponent(label) + '&max_depth=' + depth + '&max_nodes=' + maxNodes);
        renderSubgraph(canvas, data, label);
      } catch (e) { canvas.innerHTML = '<p class="spa-error">取子图失败：' + escapeHtml(e.message) + '</p>'; }
    }
    input.addEventListener('input', function () { clearTimeout(input._t); input._t = setTimeout(function () { searchLabels(input.value); }, 220); });
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') load(); });
    document.getElementById('graph-load').addEventListener('click', function () { load(); });
    document.getElementById('graph-popular').addEventListener('click', function () { loadPopular(labels, input); });
    bindZoomControls(canvas, 'graph');
    loadPopular(labels, input);
  }

  function renderDetail(node) {
    var box = document.getElementById('graph-detail'); if (!box) return;
    var label = nodeLabel(node); var props = node.properties || {};
    var rows = Object.keys(props).slice(0, 12).map(function (k) { return '<dt>' + escapeHtml(k) + '</dt><dd>' + escapeHtml(typeof props[k] === 'object' ? JSON.stringify(props[k]) : props[k]) + '</dd>'; }).join('');
    var id = nodeId(node);
    var relations = currentGraphEdges.filter(function (edge) {
      return String(edgeSource(edge)) === String(id) || String(edgeTarget(edge)) === String(id) || String(edgeSource(edge)) === label || String(edgeTarget(edge)) === label;
    }).slice(0, 12).map(function (edge) {
      var other = String(edgeSource(edge)) === String(id) || String(edgeSource(edge)) === label ? edgeTarget(edge) : edgeSource(edge);
      var relation = edge.type || (edge.properties && (edge.properties.keywords || edge.properties.description)) || '关联';
      return '<li>' + escapeHtml(String(relation).slice(0, 80)) + ' → ' + escapeHtml(other) + '</li>';
    }).join('');
    var entity = entityFor(label), wikiHref = entity && safeWikiHref(entity.wiki_path);
    box.innerHTML = '<strong>' + escapeHtml(label) + '</strong><dl class="spa-props">' + rows + '</dl>' +
      (relations ? '<h4>关系</h4><ul class="spa-props">' + relations + '</ul>' : '') +
      '<div class="spa-row wrap"><button type="button" class="spa-btn secondary" data-center-node="' + escapeHtml(label) + '">以此节点为中心</button>' +
      '<a class="spa-entity-link" href="#entity/' + encodeURIComponent(label) + '">打开实体枢纽</a>' +
      (wikiHref ? '<a class="spa-entity-link" href="' + escapeHtml(wikiHref) + '">查看 Wiki</a>' : '') + '</div>';
    var center = box.querySelector('[data-center-node]');
    if (center) center.addEventListener('click', function () {
      var target = center.getAttribute('data-center-node');
      if (location.hash !== '#graph') location.hash = '#graph';
      setTimeout(function () {
        var input = document.getElementById('graph-label');
        var load = document.getElementById('graph-load');
        if (input && load) { input.value = target; load.click(); }
      }, 0);
    });
  }

  function bindZoomControls(container, prefix) {
    var scale = 1;
    function apply() {
      var svg = container.querySelector('svg');
      if (svg) svg.style.transform = 'scale(' + scale.toFixed(2) + ')';
    }
    function change(delta) { scale = Math.max(.6, Math.min(1.8, scale + delta)); apply(); }
    var plus = document.getElementById(prefix + '-zoom-in'), minus = document.getElementById(prefix + '-zoom-out'), reset = document.getElementById(prefix + '-reset');
    if (plus) plus.addEventListener('click', function () { change(.15); });
    if (minus) minus.addEventListener('click', function () { change(-.15); });
    if (reset) reset.addEventListener('click', function () { scale = 1; apply(); container.scrollTo(0, 0); });
    container.addEventListener('keydown', function (event) {
      if (event.key === '+' || event.key === '=') { event.preventDefault(); change(.15); }
      else if (event.key === '-') { event.preventDefault(); change(-.15); }
      else if (event.key === '0') { event.preventDefault(); scale = 1; apply(); }
    });
  }

  function renderSubgraph(container, data, rootLabel) {
    container.innerHTML = ''; selectedGraphNode = null;
    var rawNodes = asArray(data.nodes);
    var nodes = rawNodes.slice().sort(function (a, b) {
      var ar = nodeLabel(a) === rootLabel ? 0 : 1, br = nodeLabel(b) === rootLabel ? 0 : 1;
      return ar - br || nodeLabel(a).localeCompare(nodeLabel(b), 'zh-CN');
    }).slice(0, 200);
    var allowed = {};
    nodes.forEach(function (node) { allowed[String(nodeId(node))] = true; allowed[nodeLabel(node)] = true; });
    var edges = asArray(data.edges || data.relationships).filter(function (edge) {
      return allowed[String(edgeSource(edge))] && allowed[String(edgeTarget(edge))];
    });
    currentGraphEdges = edges;
    document.getElementById('graph-stats') && (document.getElementById('graph-stats').innerHTML = '<span class="spa-stat">Nodes ' + nodes.length + '</span><span class="spa-stat">Edges ' + edges.length + '</span><span class="spa-stat">Root ' + escapeHtml(rootLabel) + '</span>');
    if (!nodes.length) { container.appendChild(el('p', 'spa-loading', '该实体暂无图谱邻域。')); return; }
    var w = 900, h = 620, cx = w / 2, cy = h / 2, pos = {}, byId = {}, adjacency = {};
    nodes.forEach(function (n) { byId[nodeId(n)] = n; byId[nodeLabel(n)] = n; });
    currentGraphById = byId;
    nodes.forEach(function (n) { adjacency[String(nodeId(n))] = []; });
    edges.forEach(function (edge) {
      var sourceNode = byId[edgeSource(edge)], targetNode = byId[edgeTarget(edge)];
      if (!sourceNode || !targetNode) return;
      var source = String(nodeId(sourceNode)), target = String(nodeId(targetNode));
      adjacency[source].push(target); adjacency[target].push(source);
    });
    Object.keys(adjacency).forEach(function (key) { adjacency[key].sort(function (a, b) { return nodeLabel(byId[a]).localeCompare(nodeLabel(byId[b]), 'zh-CN'); }); });
    var root = nodes.find(function (node) { return nodeLabel(node) === rootLabel || String(nodeId(node)) === String(rootLabel); }) || nodes[0];
    var rootId = String(nodeId(root)), levels = {}; levels[rootId] = 0;
    var queue = [rootId];
    while (queue.length) {
      var current = queue.shift();
      adjacency[current].forEach(function (next) {
        if (levels[next] == null) { levels[next] = levels[current] + 1; queue.push(next); }
      });
    }
    var maxLevel = Math.max.apply(null, Object.keys(levels).map(function (key) { return levels[key]; }));
    nodes.forEach(function (node) { var id = String(nodeId(node)); if (levels[id] == null) levels[id] = maxLevel + 1; });
    var rings = {};
    nodes.forEach(function (node) { var level = levels[String(nodeId(node))]; (rings[level] || (rings[level] = [])).push(node); });
    Object.keys(rings).forEach(function (key) {
      var level = parseInt(key, 10), ring = rings[key].sort(function (a, b) { return nodeLabel(a).localeCompare(nodeLabel(b), 'zh-CN'); });
      if (level === 0) { pos[String(nodeId(ring[0]))] = {x:cx,y:cy,show:true}; return; }
      var radius = Math.min(265, 105 * level), labelStep = Math.max(1, Math.ceil(ring.length / 10));
      ring.forEach(function (node, index) {
        var angle = -Math.PI / 2 + index * 2 * Math.PI / ring.length;
        pos[String(nodeId(node))] = {x:cx + radius * Math.cos(angle), y:cy + radius * Math.sin(angle), show:index % labelStep === 0};
      });
    });
    var svg = '<svg viewBox="0 0 ' + w + ' ' + h + '" xmlns="http://www.w3.org/2000/svg">';
    edges.forEach(function (edge) { var sourceNode = byId[edgeSource(edge)], targetNode = byId[edgeTarget(edge)]; if (!sourceNode || !targetNode) return; var s = pos[String(nodeId(sourceNode))], t = pos[String(nodeId(targetNode))]; if (!s || !t) return; svg += '<line class="spa-graph-edge" x1="' + s.x + '" y1="' + s.y + '" x2="' + t.x + '" y2="' + t.y + '"/>'; });
    nodes.forEach(function (n) { var id = String(nodeId(n)), label = nodeLabel(n), p = pos[id]; if (!p) return; var isRoot = id === rootId; svg += '<g class="spa-graph-node-group" tabindex="0" role="button" aria-label="选择节点 ' + escapeHtml(label) + '" data-id="' + escapeHtml(id) + '"><title>' + escapeHtml(label) + '</title><circle class="spa-graph-node' + (isRoot ? ' selected' : '') + '" cx="' + p.x + '" cy="' + p.y + '" r="' + (isRoot ? 12 : 9) + '"/>'; if (p.show) svg += '<text class="spa-graph-label" x="' + p.x + '" y="' + (p.y + 24) + '">' + escapeHtml(label.slice(0, 12)) + '</text>'; svg += '</g>'; });
    svg += '</svg>'; container.innerHTML = svg;
    function select(group) {
      container.querySelectorAll('.spa-graph-node').forEach(function (circle) { circle.classList.remove('selected'); });
      var circle = group.querySelector('.spa-graph-node'); if (circle) circle.classList.add('selected');
      var n = byId[group.getAttribute('data-id')]; selectedGraphNode = n; if (n) renderDetail(n);
    }
    container.querySelectorAll('.spa-graph-node-group').forEach(function (group) {
      group.addEventListener('click', function () { select(group); });
      group.addEventListener('keydown', function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(group); } });
    });
  }

  function renderEntity(slug) {
    var entity = entityFor(slug);
    var graphLabel = entity ? entity.graph_label : slug;
    var title = entity ? entity.title : slug;
    var wikiHref = entity && safeWikiHref(entity.wiki_path);
    ENTITY.innerHTML = pageShell(title, '实体枢纽页：图谱邻域 + 预置问答 + Wiki 原文链接。',
      '<div class="spa-card"><h3>图谱邻域</h3><div class="spa-graph-tools" aria-label="图谱缩放"><button id="entity-zoom-out" type="button" aria-label="缩小">−</button><button id="entity-reset" type="button" aria-label="复位">复位</button><button id="entity-zoom-in" type="button" aria-label="放大">＋</button></div><div id="entity-graph" class="spa-graph-canvas" tabindex="0"><p class="spa-loading">加载中…</p></div><div id="graph-stats" class="spa-statbar" aria-live="polite"></div></div><div class="spa-card"><h3>预置问答</h3><div class="spa-row"><label for="entity-q" class="spa-muted">问题</label><input id="entity-q" class="spa-input" value="关于 ' + escapeHtml(title) + '，请详细介绍。"><button id="entity-ask" class="spa-btn">提问</button></div><div id="entity-answer" aria-live="polite"></div></div><div class="spa-card"><h3>Wiki 原文</h3>' + (wikiHref ? '<a class="spa-entity-link" href="' + escapeHtml(wikiHref) + '">在 Wiki 中查看该实体 →</a>' : '<p class="spa-muted">该图谱节点暂无对应 Wiki 页面。</p>') + '</div>',
      '<div class="spa-card"><h3>节点详情</h3><div id="graph-detail" class="spa-muted">点击图中节点查看属性。</div></div>');
    var entityCanvas = document.getElementById('entity-graph');
    bindZoomControls(entityCanvas, 'entity');
    (async function () { try { renderSubgraph(entityCanvas, await json('/api/graphs?label=' + encodeURIComponent(graphLabel) + '&max_depth=' + GRAPH_MAX_DEPTH + '&max_nodes=' + GRAPH_MAX_NODES), graphLabel); } catch (e) { entityCanvas.innerHTML = '<p class="spa-error">图谱加载失败：' + escapeHtml(e.message) + '</p>'; } })();
    document.getElementById('entity-ask').addEventListener('click', async function () { var out = document.getElementById('entity-answer'), q = document.getElementById('entity-q').value; out.innerHTML = '<p class="spa-loading">检索中…</p>'; try { var data = await json('/api/query', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ schema_version:1, query:q, mode:'mix', top_k:20 }) }); out.innerHTML = resultHtml(data); } catch (e) { out.innerHTML = '<p class="spa-error">提问失败：' + escapeHtml(e.message) + '</p>'; } });
  }

  function route() {
    QA.classList.remove('active'); GRAPH.classList.remove('active'); ENTITY.classList.remove('active');
    var hash = location.hash || '';
    if (hash.indexOf('#entity/') === 0 && FEATURES.entity) {
      renderEntity(decodeURIComponent(hash.slice('#entity/'.length))); ENTITY.classList.add('active');
    } else if (hash === '#graph' && FEATURES.graph) {
      renderGraph(); GRAPH.classList.add('active');
    } else if (FEATURES.qa) {
      renderQA(); QA.classList.add('active');
    } else if (FEATURES.graph) {
      renderGraph(); GRAPH.classList.add('active');
    }
  }
  window.addEventListener('hashchange', route); window.addEventListener('load', async function () { await loadRegistry(); route(); });
})();

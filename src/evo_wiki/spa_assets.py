"""Read-only SPA shell for the Evo wiki Web platform.

The SPA is a fixed, project-agnostic reader surface for 问答 / 图谱 / 实体枢纽.
It mirrors the read-only parts of LightRAG WebUI (query parameters, label search,
subgraph browsing, node details) while sharing Evo Wiki's theme.css/nav.js so the
Wiki and app surfaces look like one system.
"""
from __future__ import annotations

import html

from .config import EvoConfig
from .paths import ProjectPaths


def write_spa_assets(paths: ProjectPaths, config: EvoConfig) -> None:
    """Write the fixed SPA shell into ``wiki dist/app/``."""
    presentation = config.validate(paths.root)
    app_dir = paths.wiki_dist / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    site_title = html.escape(presentation["title"])
    site_description = html.escape(presentation["description"])
    navigation = presentation["navigation"]
    logo_public_path = presentation["logo_public_path"]
    logo_url = f"../{logo_public_path}" if logo_public_path else ""
    (app_dir / "index.html").write_text(
        (
            SPA_INDEX_HTML.replace("{{site_title}}", site_title)
            .replace("{{site_description}}", site_description)
            .replace("{{logo_url}}", html.escape(logo_url))
            .replace("{{nav_wiki}}", str(navigation["wiki"]).lower())
            .replace("{{nav_qa}}", str(navigation["qa"]).lower())
            .replace("{{nav_graph}}", str(navigation["graph"]).lower())
            .replace(
                "{{nav_entity}}",
                str(navigation["entity_hub"]).lower(),
            )
        ),
        encoding="utf-8",
    )
    (app_dir / "app.css").write_text(SPA_CSS, encoding="utf-8")
    query_defaults = presentation["query_defaults"]
    graph_defaults = presentation["graph_defaults"]
    app_js = (
        SPA_JS.replace("{{query_mode}}", query_defaults["mode"])
        .replace("{{query_top_k}}", str(query_defaults["top_k"]))
        .replace(
            "{{query_history_turns}}",
            str(query_defaults["history_turns"]),
        )
        .replace("{{graph_max_depth}}", str(graph_defaults["max_depth"]))
        .replace("{{graph_max_nodes}}", str(graph_defaults["max_nodes"]))
        .replace(
            "{{graph_popular_limit}}",
            str(graph_defaults["popular_limit"]),
        )
        .replace("{{site_description}}", site_description)
    )
    (app_dir / "app.js").write_text(app_js, encoding="utf-8")


SPA_INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>问答 · 图谱 · {{site_title}}</title>
  <meta name="description" content="{{site_description}}">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600;700;900&family=Crimson+Pro:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="../assets/shared/theme.css">
  <link rel="stylesheet" href="./app.css">
  <script defer src="../assets/shared/nav.js"></script>
  <script defer src="./app.js"></script>
</head>
<body data-site-title="{{site_title}}" data-site-description="{{site_description}}"
  data-logo-url="{{logo_url}}" data-nav-wiki="{{nav_wiki}}"
  data-nav-qa="{{nav_qa}}" data-nav-graph="{{nav_graph}}"
  data-nav-entity="{{nav_entity}}">
  <div id="evo-topbar"></div>
  <main class="spa-main">
    <section id="view-qa" class="spa-view"></section>
    <section id="view-graph" class="spa-view"></section>
    <section id="view-entity" class="spa-view"></section>
  </main>
</body>
</html>
"""


SPA_CSS = """
/* SPA layout uses the same shared tokens and topbar (from shared/theme.css) as Wiki. */
* { box-sizing:border-box; }
html { font-size:15px; scroll-behavior:smooth; }
body { margin:0; font-family:var(--sans); color:var(--text); background:var(--bg); line-height:1.8; }

.spa-main { max-width:1160px; margin:0 auto; padding:calc(var(--topbar-h) + 32px) 24px 80px; }
.spa-view { display:none; }
.spa-view.active { display:block; }
.spa-shell { display:grid; grid-template-columns:minmax(0, 820px) 280px; gap:28px; align-items:start; }
.spa-article { min-width:0; }
.spa-aside { position:sticky; top:calc(var(--topbar-h) + 28px); }
.spa-h1 { font-family:var(--serif); font-size:30px; font-weight:900; line-height:1.3; color:var(--heading); margin:0 0 8px; letter-spacing:-.5px; }
.spa-sub { color:var(--text2); font-size:14px; margin:0 0 24px; max-width:720px; }
.spa-card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:18px 20px; margin-bottom:18px; }
.spa-card h3, .spa-card h4 { font-family:var(--serif); color:var(--heading2); margin:0 0 10px; line-height:1.35; }
.spa-card h3 { font-size:17px; }
.spa-card h4 { font-size:15px; }
.spa-muted { color:var(--text2); font-size:13px; }
.spa-row { display:flex; gap:10px; align-items:center; margin-bottom:12px; }
.spa-row.wrap { flex-wrap:wrap; }
.spa-input, .spa-textarea, .spa-select { width:100%; border:1px solid var(--border); background:var(--bg2); color:var(--text); border-radius:8px; padding:10px 12px; font-size:14px; font-family:var(--sans); }
.spa-textarea { min-height:84px; resize:vertical; line-height:1.65; }
.spa-input:focus, .spa-textarea:focus, .spa-select:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-glow); }
.spa-btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; border:0; background:var(--accent); color:#fff; border-radius:8px; padding:10px 16px; font-size:14px; font-weight:600; cursor:pointer; font-family:var(--sans); white-space:nowrap; }
.spa-btn.secondary { background:var(--bg2); color:var(--text); border:1px solid var(--border); }
.spa-btn:hover { filter:brightness(.94); }
.spa-btn:disabled { opacity:.55; cursor:not-allowed; }
.spa-field { margin-bottom:12px; }
.spa-field label { display:block; color:var(--text2); font-size:12px; font-weight:600; margin:0 0 5px; }
.spa-grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.spa-chiprow { display:flex; flex-wrap:wrap; gap:7px; margin-top:10px; }
.spa-chip { border:1px solid var(--border); background:var(--bg); color:var(--text2); border-radius:999px; padding:4px 10px; font-size:12px; cursor:pointer; }
.spa-chip:hover { color:var(--accent); border-color:var(--accent-border); background:var(--accent-glow); }
.spa-chat { display:flex; flex-direction:column; gap:12px; }
.spa-message { border:1px solid var(--border); border-radius:12px; padding:14px 16px; background:var(--card); }
.spa-message.user { background:var(--accent-glow); border-color:var(--accent-border); }
.spa-role { color:var(--text2); font-size:11px; text-transform:uppercase; letter-spacing:.08em; font-weight:700; margin-bottom:5px; }
.spa-answer { font-size:15px; color:var(--text); line-height:1.85; }
.spa-answer h1, .spa-answer h2, .spa-answer h3 { font-size:1.05em; margin:14px 0 7px; }
.spa-answer p, .spa-answer ul, .spa-answer ol, .spa-answer blockquote { margin:7px 0; }
.spa-answer pre { overflow:auto; background:var(--bg2); padding:10px; border-radius:7px; }
.spa-answer code { background:var(--bg2); padding:1px 4px; border-radius:4px; }
.spa-loading { color:var(--text2); font-size:14px; }
.spa-error { color:var(--danger); font-size:14px; }
.spa-status { display:inline-flex; align-items:center; gap:6px; margin-bottom:9px; border-radius:999px; padding:3px 9px; font-size:12px; font-weight:700; }
.spa-status.verified { color:#0f6b45; background:#e8f7ef; }
.spa-status.warning { color:#8a4b08; background:#fff3dd; }
.spa-status.blocked { color:var(--text2); background:var(--bg2); }
.spa-cite { color:var(--link); font-weight:700; text-decoration:none; }
.spa-cite-pending { color:#8a4b08; font-size:.85em; }
.spa-refs { margin-top:14px; border-top:1px solid var(--border); padding-top:12px; }
.spa-refs h4 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--text2); margin-bottom:8px; font-family:var(--sans); }
.spa-ref { display:block; padding:8px 0; color:var(--link); text-decoration:none; border-bottom:1px solid var(--border); font-size:13px; }
.spa-ref.no-link { color:var(--text2); }
.spa-ref small { display:block; color:var(--text2); margin-top:3px; line-height:1.5; }
.spa-evidence-graph { margin-top:14px; border:1px solid var(--border); border-radius:12px; padding:12px; background:var(--bg2); }
.spa-evidence-graph h4 { margin:0 0 4px; font-family:var(--sans); font-size:13px; color:var(--text); }
.spa-evidence-graph-meta { margin:0 0 9px; color:var(--text2); font-size:12px; }
.spa-mini-graph { min-height:220px; overflow:auto; border:1px solid var(--border); border-radius:9px; background:var(--bg); }
.spa-mini-graph svg { width:100%; min-width:520px; height:auto; display:block; }
.spa-mini-node { fill:var(--accent); stroke:#fff; stroke-width:2px; }
.spa-mini-node.root { fill:var(--accent-strong); }
.spa-mini-label { font-size:10px; font-family:var(--sans); fill:var(--text); text-anchor:middle; }
.spa-mini-edge { stroke:var(--text2); stroke-opacity:.32; stroke-width:1.1px; }
.spa-mini-graph-links { margin-top:8px; }
.spa-graph-canvas { background:var(--bg2); border:1px solid var(--border); border-radius:12px; min-height:430px; padding:12px; overflow:auto; position:relative; }
.spa-graph-canvas svg { width:100%; height:auto; display:block; transform-origin:center; transition:transform .15s ease; }
.spa-graph-tools { display:flex; justify-content:flex-end; gap:6px; margin-bottom:8px; }
.spa-graph-tools button { border:1px solid var(--border); border-radius:6px; background:var(--bg); color:var(--text); min-width:34px; height:32px; cursor:pointer; }
.spa-graph-tools button:focus-visible, .spa-graph-node-group:focus-visible { outline:3px solid var(--accent-glow); outline-offset:2px; }
.spa-graph-node { fill:var(--accent); stroke:#fff; stroke-width:2px; cursor:pointer; }
.spa-graph-node-group:hover .spa-graph-node, .spa-graph-node.selected { fill:var(--accent-strong); }
.spa-graph-label { font-size:11px; font-family:var(--sans); fill:var(--text); text-anchor:middle; pointer-events:none; }
.spa-graph-edge { stroke:var(--text2); stroke-opacity:.35; stroke-width:1.2px; }
.spa-graph-edge-label { display:none; }
.spa-statbar { display:flex; gap:10px; flex-wrap:wrap; margin:10px 0 0; }
.spa-stat { background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:5px 9px; color:var(--text2); font-size:12px; }
.spa-props { font-size:12px; color:var(--text2); }
.spa-props dt { font-weight:700; color:var(--text); margin-top:8px; }
.spa-props dd { margin:2px 0 0; word-break:break-word; }
.spa-entity-link { display:inline-flex; margin-top:8px; color:var(--accent); text-decoration:none; font-size:13px; font-weight:500; }
.spa-entity-link:hover { text-decoration:underline; }
@media (max-width: 980px) {
  .spa-shell { display:block; }
  .spa-aside { position:static; }
  .spa-main { padding:calc(var(--topbar-h) + 24px) 18px 56px; }
  #view-graph .spa-shell { display:flex; flex-direction:column; }
  #view-graph .spa-aside { display:contents; }
  #view-graph .spa-aside .spa-card:first-child { order:-2; }
  #view-graph .spa-article { order:-1; }
  #view-graph .spa-aside .spa-card:last-child { order:0; }
}
@media (max-width: 680px) {
  .spa-row { align-items:stretch; flex-direction:column; }
  .spa-grid-2 { grid-template-columns:1fr; }
  .spa-h1 { font-size:24px; }
  .spa-graph-canvas { min-height:320px; height:320px; padding:8px; }
  #view-graph .spa-grid-2 { grid-template-columns:1fr 1fr; }
  #view-graph .spa-row { flex-direction:row; }
}
"""


SPA_JS = """
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
  var HISTORY_TURNS = {{query_history_turns}};
  var GRAPH_MAX_DEPTH = {{graph_max_depth}};
  var GRAPH_MAX_NODES = {{graph_max_nodes}};
  var GRAPH_POPULAR_LIMIT = {{graph_popular_limit}};
  var EVIDENCE_GRAPH_MAX_DEPTH = 1;
  var EVIDENCE_GRAPH_MAX_NODES = 24;
  var EVIDENCE_GRAPH_MAX_ATTEMPTS = 3;
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
    return String(value || '').replace(/\\\\/g, '/').split('/').pop().toLowerCase();
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
    return String(text || '').replace(/\\n#{1,6}\\s*(?:references?|参考文献|引用来源)\\s*\\n[\\s\\S]*$/i, '').trim();
  }
  function safeInline(text, refs) {
    var value = escapeHtml(text);
    value = value.replace(/`([^`]+)`/g, '<code>$1</code>');
    value = value.replace(/[*][*]([^*]+)[*][*]/g, '<strong>$1</strong>');
    value = value.replace(/[*]([^*]+)[*]/g, '<em>$1</em>');
    value = value.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g, '<a href="$2" rel="noopener noreferrer">$1</a>');
    value = value.replace(/\\[(\\d{1,3})\\]/g, function (_, marker) {
      var ref = asArray(refs).find(function (item) { return String(item.marker) === marker; });
      return ref
        ? '<a class="spa-cite" href="#evidence-' + escapeHtml(ref.citation_id) + '">[' + marker + ']</a>'
        : '<span class="spa-cite-pending">[依据待核验]</span>';
    });
    return value;
  }
  function safeMarkdown(text, refs) {
    var lines = stripModelReferences(text).split(/\\r?\\n/), out = [], list = null, code = false, codeLines = [];
    function closeList() { if (list) { out.push('</' + list + '>'); list = null; } }
    lines.forEach(function (raw) {
      var line = raw.trim();
      if (/^```/.test(line)) {
        closeList();
        if (code) { out.push('<pre><code>' + escapeHtml(codeLines.join('\\n')) + '</code></pre>'); codeLines = []; }
        code = !code; return;
      }
      if (code) { codeLines.push(raw); return; }
      if (!line) { closeList(); return; }
      var heading = line.match(/^(#{1,3})\\s+(.+)/);
      if (heading) { closeList(); var level = heading[1].length + 1; out.push('<h' + level + '>' + safeInline(heading[2], refs) + '</h' + level + '>'); return; }
      var bullet = line.match(/^[-*]\\s+(.+)/);
      var ordered = line.match(/^\\d+[.]\\s+(.+)/);
      if (bullet || ordered) {
        var wanted = ordered ? 'ol' : 'ul';
        if (list !== wanted) { closeList(); list = wanted; out.push('<' + list + '>'); }
        out.push('<li>' + safeInline((bullet || ordered)[1], refs) + '</li>'); return;
      }
      closeList();
      if (/^>\\s+/.test(line)) out.push('<blockquote>' + safeInline(line.replace(/^>\\s+/, ''), refs) + '</blockquote>');
      else out.push('<p>' + safeInline(line, refs) + '</p>');
    });
    closeList();
    if (codeLines.length) out.push('<pre><code>' + escapeHtml(codeLines.join('\\n')) + '</code></pre>');
    return out.join('');
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
      '<div class="spa-field"><label for="qa-top-k">Top K</label><input id="qa-top-k" class="spa-input" type="number" min="1" max="100" value="{{query_top_k}}"></div>' +
      '<p class="spa-muted">最多携带最近 ' + HISTORY_TURNS + ' 轮已成功展示的问答；证据状态不会影响对话连续性。</p></div>';
  }

  function renderRefs(refs) {
    refs = asArray(refs);
    if (!refs.length) return '';
    return '<div class="spa-refs"><h4>可核验依据 (' + refs.length + ')</h4>' + refs.map(function (ref, i) {
      var file = ref.source_label || ref.file_path || ref.source || ref.path || ref.file || ('来源 ' + (i + 1));
      var content = refContent(ref.excerpts || ref.content).join('\\n').slice(0, 260);
      var mapped = sourceFor(file), href = mapped && safeWikiHref(mapped.wiki_path);
      var title = '<strong>回答断言 [' + escapeHtml(ref.marker || String(i + 1)) + '] → 文档来源：</strong> ' + escapeHtml(mapped && mapped.title ? mapped.title : file);
      var inner = '<span>' + title + '</span>' + (content ? '<small>依据片段：' + escapeHtml(content) + '</small>' : '');
      return href ? '<a id="evidence-' + escapeHtml(ref.citation_id) + '" class="spa-ref" href="' + escapeHtml(href) + '">' + inner + '</a>' : '<div id="evidence-' + escapeHtml(ref.citation_id) + '" class="spa-ref no-link">' + inner + '</div>';
    }).join('') + '</div>';
  }

  function evidenceSubgraphSlot(refs) {
    return asArray(refs).length
      ? '<div class="spa-evidence-subgraph-slot" aria-live="polite"></div>'
      : '';
  }

  function normalizedText(value) {
    value = String(value || '').trim().toLowerCase();
    return value.normalize ? value.normalize('NFKC') : value;
  }

  function relevanceTerms(value) {
    var normalized = normalizedText(value), seen = {}, terms = [];
    function add(term) {
      term = normalizedText(term);
      if (term.length < 2 || seen[term]) return;
      seen[term] = true;
      terms.push(term);
    }
    (normalized.match(/[a-z0-9_]{2,}/g) || []).forEach(add);
    (normalized.match(/[\\u3400-\\u9fff]+/g) || []).forEach(function (run) {
      if (run.length === 2) add(run);
      for (var index = 0; index + 1 < run.length; index += 1) {
        add(run.slice(index, index + 2));
      }
    });
    return terms;
  }

  function evidenceSeedScore(question, ref, label, mapped) {
    var file = ref.source_label || ref.file_path || ref.source ||
      ref.path || ref.file || '';
    var excerpts = refContent(ref.excerpts || ref.content).join('\\n');
    var questionText = normalizedText(question);
    var excerptText = normalizedText(excerpts);
    var context = normalizedText([
      label,
      mapped && mapped.title,
      file,
      excerpts
    ].join('\\n'));
    var score = 0;
    relevanceTerms(questionText).forEach(function (term) {
      if (context.indexOf(term) >= 0) score += 2;
      if (excerptText.indexOf(term) >= 0) score += 1;
    });
    var normalizedLabel = normalizedText(label);
    if (normalizedLabel && questionText.indexOf(normalizedLabel) >= 0) {
      score += 40;
    }
    if (normalizedLabel && excerptText.indexOf(normalizedLabel) >= 0) {
      score += 12;
    }
    return score;
  }

  function evidenceSubgraphSeeds(refs, question) {
    var candidates = [], byValue = {}, order = 0;
    function add(value, marker, ref, mapped) {
      value = String(value || '').trim();
      var key = normalizedText(value);
      if (!value) return;
      var candidate = {
        value: value,
        kind: '实体 label',
        marker: String(marker || ''),
        score: evidenceSeedScore(question, ref, value, mapped),
        order: order++
      };
      if (!byValue[key]) {
        byValue[key] = candidate;
        candidates.push(candidate);
      } else if (candidate.score > byValue[key].score) {
        byValue[key].score = candidate.score;
        byValue[key].marker = candidate.marker;
      }
    }
    asArray(refs).forEach(function (ref, index) {
      var file = ref.source_label || ref.file_path || ref.source ||
        ref.path || ref.file || '';
      var mapped = sourceFor(file);
      asArray(ref.graph_labels).forEach(function (label) {
        add(label, ref.marker || String(index + 1), ref, mapped);
      });
      asArray(mapped && mapped.graph_labels).forEach(function (label) {
        add(label, ref.marker || String(index + 1), ref, mapped);
      });
    });
    return candidates.sort(function (left, right) {
      return right.score - left.score || left.order - right.order ||
        normalizedText(left.value).localeCompare(normalizedText(right.value));
    });
  }

  function renderEvidenceMiniGraph(host, data, seed) {
    var nodes = asArray(data && data.nodes).slice(
      0,
      EVIDENCE_GRAPH_MAX_NODES
    );
    if (!nodes.length) return false;
    var byId = {}, allowed = {};
    nodes.forEach(function (node) {
      var id = String(nodeId(node)), label = nodeLabel(node);
      byId[id] = node;
      byId[label] = node;
      allowed[id] = true;
      allowed[label] = true;
    });
    var edges = asArray(data.edges || data.relationships).filter(
      function (edge) {
        return allowed[String(edgeSource(edge))] &&
          allowed[String(edgeTarget(edge))];
      }
    ).slice(0, EVIDENCE_GRAPH_MAX_NODES * 3);
    var normalizedSeed = normalizedText(seed.value);
    var root = nodes.find(function (node) {
      return normalizedText(nodeId(node)) === normalizedSeed ||
        normalizedText(nodeLabel(node)) === normalizedSeed;
    });
    if (!root) return false;
    var rootId = String(nodeId(root));
    var width = 720, height = 300;
    var centerX = width / 2, centerY = height / 2, positions = {};
    positions[rootId] = { x:centerX, y:centerY };
    var others = nodes.filter(function (node) {
      return String(nodeId(node)) !== rootId;
    });
    others.forEach(function (node, index) {
      var angle = -Math.PI / 2 +
        index * 2 * Math.PI / Math.max(1, others.length);
      var radius = others.length > 12 && index % 2 ? 128 : 108;
      positions[String(nodeId(node))] = {
        x: centerX + radius * Math.cos(angle),
        y: centerY + radius * Math.sin(angle)
      };
    });
    var svg = '<svg viewBox="0 0 ' + width + ' ' + height +
      '" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="引用关联知识子图">';
    edges.forEach(function (edge) {
      var sourceNode = byId[String(edgeSource(edge))];
      var targetNode = byId[String(edgeTarget(edge))];
      if (!sourceNode || !targetNode) return;
      var sourcePosition = positions[String(nodeId(sourceNode))];
      var targetPosition = positions[String(nodeId(targetNode))];
      if (!sourcePosition || !targetPosition) return;
      svg += '<line class="spa-mini-edge" x1="' + sourcePosition.x +
        '" y1="' + sourcePosition.y + '" x2="' + targetPosition.x +
        '" y2="' + targetPosition.y + '"><title>' +
        escapeHtml(edge.type || edge.label || '关联') + '</title></line>';
    });
    nodes.forEach(function (node) {
      var id = String(nodeId(node)), label = nodeLabel(node);
      var point = positions[id];
      if (!point) return;
      var isRoot = id === rootId;
      svg += '<g><title>' + escapeHtml(label) +
        '</title><circle class="spa-mini-node' +
        (isRoot ? ' root' : '') + '" cx="' + point.x + '" cy="' +
        point.y + '" r="' + (isRoot ? 11 : 8) +
        '"/><text class="spa-mini-label" x="' + point.x + '" y="' +
        (point.y + 20) + '">' + escapeHtml(label.slice(0, 14)) +
        '</text></g>';
    });
    svg += '</svg>';
    var links = nodes.slice(0, 12).map(function (node) {
      var label = nodeLabel(node), entity = entityFor(label);
      return entity
        ? '<a class="spa-chip" href="#entity/' +
          encodeURIComponent(entity.graph_label || label) + '">' +
          escapeHtml(label) + '</a>'
        : '<span class="spa-chip">' + escapeHtml(label) + '</span>';
    }).join('');
    host.innerHTML =
      '<section class="spa-evidence-graph"><h4>引用关联知识子图</h4>' +
      '<p class="spa-evidence-graph-meta">根据' +
      escapeHtml(seed.kind) + '“' + escapeHtml(seed.value) +
      '”拉取 · 对应引用 [' + escapeHtml(seed.marker) +
      '] · 1 跳 · ' + nodes.length + ' 个节点 / ' + edges.length +
      ' 条关系</p><div class="spa-mini-graph">' + svg + '</div>' +
      (links
        ? '<div class="spa-chiprow spa-mini-graph-links">' + links + '</div>'
        : '') +
      '</section>';
    return true;
  }

  async function hydrateEvidenceSubgraph(container, data, question) {
    var host = container &&
      container.querySelector('.spa-evidence-subgraph-slot');
    var refs = asArray(data && data.citations);
    if (!host || !refs.length) return;
    var seeds = evidenceSubgraphSeeds(refs, question).slice(
      0,
      EVIDENCE_GRAPH_MAX_ATTEMPTS
    );
    if (!seeds.length) {
      host.remove();
      return;
    }
    host.innerHTML =
      '<p class="spa-loading">正在加载引用关联子图…</p>';
    for (var index = 0; index < seeds.length; index += 1) {
      try {
        var seed = seeds[index];
        var graph = await json(
          '/api/graphs?label=' + encodeURIComponent(seed.value) +
          '&max_depth=' + EVIDENCE_GRAPH_MAX_DEPTH +
          '&max_nodes=' + EVIDENCE_GRAPH_MAX_NODES
        );
        if (renderEvidenceMiniGraph(host, graph, seed)) return;
      } catch (_) {}
    }
    host.remove();
  }

  function resultHtml(data) {
    var answer = data && data.answer;
    if (data && data.generation_status === 'succeeded' && answer) {
      var labels = {
        grounded: ['verified', '已引用知识库资料'],
        partially_grounded: ['warning', '部分依据待核验'],
        ungrounded: ['warning', '未检索到知识库依据，此回答由模型通用知识生成']
      };
      var state = labels[data.evidence_status] || ['warning', '依据状态未知'];
      return '<div class="spa-status ' + state[0] + '">' +
        escapeHtml(state[1]) + '</div><div class="spa-answer">' +
        safeMarkdown(answer, data.citations) + '</div>' +
        renderRefs(data.citations) +
        evidenceSubgraphSlot(data.citations);
    }
    var label = data && data.error_code === 'QUERY_MAINTENANCE_ACTIVE' ? '系统处于受控维护窗口，请稍后再试。' : '回答生成失败，请稍后重试。';
    return '<div class="spa-status blocked">生成失败</div><div class="spa-error">' + escapeHtml(label) + '</div>';
  }

  function appendMessage(box, role, content, data, isError, question) {
    var msg = el('div', 'spa-message ' + (role === 'user' ? 'user' : 'assistant'));
    msg.innerHTML = '<div class="spa-role">' + escapeHtml(role) + '</div>' + (data ? resultHtml(data) : '<div class="spa-answer ' + (isError ? 'spa-error' : '') + '">' + escapeHtml(content) + '</div>');
    box.appendChild(msg);
    if (data && data.generation_status === 'succeeded') {
      hydrateEvidenceSubgraph(msg, data, question);
    }
    box.scrollIntoView({ block: 'end' });
  }

  function queryPayload(q) {
    function num(id) { var v = parseInt(document.getElementById(id).value, 10); return isNaN(v) ? undefined : v; }
    var payload = {
      schema_version: 2,
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
    document.getElementById('qa-mode').value = '{{query_mode}}';
    var input = document.getElementById('qa-input'), send = document.getElementById('qa-send'), chat = document.getElementById('qa-chat');
    async function ask() {
      var raw = input.value.trim(); if (!raw) return;
      var m = raw.match(/^[/](naive|local|global|hybrid|mix)\\s+([\\s\\S]+)/); var q = raw;
      if (m) { document.getElementById('qa-mode').value = m[1]; q = m[2]; }
      appendMessage(chat, 'user', raw); input.value = ''; send.disabled = true;
      var loading = el('div', 'spa-loading', '检索中…'); chat.appendChild(loading);
      try {
        var data = await json('/api/query', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(queryPayload(q)) });
        loading.remove(); appendMessage(chat, 'assistant', '', data, data.generation_status !== 'succeeded', q); if (data.generation_status === 'succeeded' && data.answer) { chatHistory.push({ role:'user', content:q }, { role:'assistant', content:data.answer }); chatHistory = HISTORY_TURNS > 0 ? chatHistory.slice(-HISTORY_TURNS * 2) : []; }
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
    document.getElementById('entity-ask').addEventListener('click', async function () { var out = document.getElementById('entity-answer'), q = document.getElementById('entity-q').value; out.innerHTML = '<p class="spa-loading">检索中…</p>'; try { var data = await json('/api/query', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ schema_version:2, query:q, mode:'mix', top_k:20 }) }); out.innerHTML = resultHtml(data); if (data.generation_status === 'succeeded') hydrateEvidenceSubgraph(out, data, q); } catch (e) { out.innerHTML = '<p class="spa-error">提问失败：' + escapeHtml(e.message) + '</p>'; } });
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
"""

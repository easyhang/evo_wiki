"""SPA shell for the Evo wiki Web platform.

The SPA is a fixed, project-agnostic surface for 问答 / 图谱 / 实体枢纽 and
the local-only review center.
It mirrors the read-only parts of LightRAG WebUI (query parameters, label search,
subgraph browsing, node details) while sharing Evo Wiki's theme.css/nav.js so the
Wiki and app surfaces look like one system.
"""
from __future__ import annotations

import hashlib
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
    asset_version = hashlib.sha256(
        f"{SPA_CSS}\0{app_js}".encode("utf-8")
    ).hexdigest()[:12]
    (app_dir / "index.html").write_text(
        (
            SPA_INDEX_HTML.replace("{{site_title}}", site_title)
            .replace("{{site_description}}", site_description)
            .replace("{{logo_url}}", html.escape(logo_url))
            .replace("{{asset_version}}", asset_version)
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
  <link rel="stylesheet" href="./app.css?v={{asset_version}}">
  <script defer src="../assets/shared/nav.js"></script>
  <script defer src="./app.js?v={{asset_version}}"></script>
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
    <section id="view-audit" class="spa-view"></section>
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
.spa-btn.danger { background:var(--danger); }
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
.spa-answer { min-width:0; font-size:15px; color:var(--text); line-height:1.85; overflow-wrap:anywhere; }
.spa-answer h1, .spa-answer h2, .spa-answer h3, .spa-answer h4, .spa-answer h5, .spa-answer h6 { color:var(--heading); line-height:1.45; }
.spa-answer h1 { font-size:1.25em; margin:18px 0 8px; }
.spa-answer h2 { font-size:1.16em; margin:16px 0 8px; }
.spa-answer h3 { font-size:1.08em; margin:14px 0 7px; }
.spa-answer h4, .spa-answer h5, .spa-answer h6 { font-size:1em; margin:12px 0 6px; }
.spa-answer p, .spa-answer ul, .spa-answer ol, .spa-answer blockquote, .spa-answer table { margin:8px 0; }
.spa-answer ul, .spa-answer ol { padding-left:1.45em; }
.spa-answer li + li { margin-top:3px; }
.spa-answer blockquote { border-left:3px solid var(--accent-border); color:var(--text2); padding:2px 0 2px 12px; }
.spa-answer hr { border:0; border-top:1px solid var(--border); margin:16px 0; }
.spa-answer pre { max-width:100%; overflow:auto; background:var(--bg2); padding:10px; border-radius:7px; }
.spa-answer pre code { background:transparent; padding:0; }
.spa-answer code { background:var(--bg2); padding:1px 4px; border-radius:4px; }
.spa-answer del { color:var(--text2); }
.spa-answer table { display:block; width:100%; max-width:100%; overflow-x:auto; border-collapse:collapse; font-size:.94em; }
.spa-answer th, .spa-answer td { border:1px solid var(--border); padding:6px 8px; text-align:left; vertical-align:top; }
.spa-answer th { background:var(--bg2); color:var(--heading); font-weight:700; }
.spa-md-image { color:var(--text2); font-size:.9em; }
.spa-wiki-entity { color:var(--link); font-weight:650; text-decoration:underline; text-underline-offset:3px; }
.spa-loading { color:var(--text2); font-size:14px; }
.spa-error { color:var(--danger); font-size:14px; }
.spa-notice { border:1px solid var(--accent-border); background:var(--accent-glow); color:var(--text); border-radius:9px; padding:10px 12px; margin-bottom:12px; font-size:13px; }
.spa-notice.warning { border-color:#e4b95f; background:#fff7e8; color:#7a4700; }
.spa-notice.danger { border-color:#e2a7a7; background:#fff0f0; color:#8a2424; }
.spa-status { display:inline-flex; align-items:center; gap:6px; margin-bottom:9px; border-radius:999px; padding:3px 9px; font-size:12px; font-weight:700; }
.spa-status.verified { color:#0f6b45; background:#e8f7ef; }
.spa-status.warning { color:#8a4b08; background:#fff3dd; }
.spa-status.blocked { color:var(--text2); background:var(--bg2); }
.spa-cite { color:var(--link); font-weight:700; text-decoration:none; }
.spa-cite-pending { color:#8a4b08; font-size:.85em; }
.spa-refs { margin-top:14px; border-top:1px solid var(--border); padding-top:12px; }
.spa-refs h4 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--text2); margin-bottom:8px; font-family:var(--sans); }
.spa-refs-empty { color:var(--text2); font-size:13px; line-height:1.6; }
.spa-ref { display:block; padding:8px 0; color:var(--link); text-decoration:none; border-bottom:1px solid var(--border); font-size:13px; }
.spa-ref.no-link { color:var(--text2); }
.spa-ref small { display:block; color:var(--text2); margin-top:3px; line-height:1.5; }
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
.spa-audit-filters { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
.spa-audit-filter { border:1px solid var(--border); background:var(--bg2); color:var(--text2); border-radius:999px; padding:7px 13px; cursor:pointer; font:600 13px var(--sans); }
.spa-audit-filter.active { border-color:var(--accent); background:var(--accent-glow); color:var(--accent); }
.spa-audit-item { padding:0; overflow:hidden; }
.spa-audit-head { width:100%; display:flex; align-items:flex-start; justify-content:space-between; gap:16px; border:0; background:transparent; color:var(--text); padding:16px 18px; cursor:pointer; text-align:left; font:inherit; }
.spa-audit-head:hover { background:var(--bg2); }
.spa-audit-question { display:block; color:var(--heading); font-weight:700; line-height:1.5; }
.spa-audit-meta { display:flex; gap:7px; flex-wrap:wrap; margin-top:7px; color:var(--text2); font-size:12px; }
.spa-audit-detail { border-top:1px solid var(--border); padding:16px 18px 18px; }
.spa-audit-section { margin-top:16px; }
.spa-audit-section h4 { margin:0 0 7px; }
.spa-audit-history { margin:0; padding-left:20px; }
.spa-audit-history li { margin-bottom:8px; }
.spa-audit-events { font-size:12px; color:var(--text2); }
.spa-audit-empty { text-align:center; padding:32px 18px; color:var(--text2); }
.spa-audit-status { flex:0 0 auto; }
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
  var AUDIT = document.getElementById('view-audit');
  var chatHistory = [];
  var qaMessages = [];
  var qaDraft = '';
  var qaMode = '{{query_mode}}';
  var qaTopK = {{query_top_k}};
  var qaSessionLoaded = false;
  var selectedGraphNode = null;
  var registry = { entities:[], sources:{} };
  var currentGraphEdges = [];
  var currentGraphById = {};
  var HISTORY_TURNS = {{query_history_turns}};
  var GRAPH_MAX_DEPTH = {{graph_max_depth}};
  var GRAPH_MAX_NODES = {{graph_max_nodes}};
  var GRAPH_POPULAR_LIMIT = {{graph_popular_limit}};
  var QA_SESSION_KEY = 'evo-wiki:qa-session:v1';
  var QA_STORED_MESSAGE_LIMIT = 20;
  var BODY_DATA = (document.body && document.body.dataset) || {};
  var FEATURES = {
    qa: BODY_DATA.navQa !== 'false',
    graph: BODY_DATA.navGraph !== 'false',
    entity: BODY_DATA.navEntity !== 'false',
    audit: false
  };

  function el(tag, cls, html) { var n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; }
  function escapeHtml(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]; }); }
  async function json(url, opts) {
    var r = await fetch(url, opts), value = null;
    try { value = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(value && value.error_code ? value.error_code : 'HTTP ' + r.status);
    return value;
  }
  function asArray(v) { return Array.isArray(v) ? v : []; }
  function refContent(v) { if (Array.isArray(v)) return v.filter(function (part) { return typeof part === 'string' && part; }); return typeof v === 'string' && v ? [v] : []; }
  function supportedQueryMode(value) {
    return ['naive', 'local', 'global', 'hybrid', 'mix'].indexOf(value) >= 0;
  }
  function boundedTopK(value) {
    var parsed = parseInt(value, 10);
    return isNaN(parsed) ? {{query_top_k}} : Math.max(1, Math.min(100, parsed));
  }
  function qaSessionStorage() {
    try { return window.sessionStorage || null; } catch (_) { return null; }
  }
  function compactCitationForSession(ref) {
    ref = ref && typeof ref === 'object' ? ref : {};
    return {
      citation_id:String(ref.citation_id || '').slice(0, 160),
      marker:String(ref.marker || '').slice(0, 8),
      source_label:String(ref.source_label || '').slice(0, 600),
      file_path:String(ref.file_path || '').slice(0, 600),
      source:String(ref.source || '').slice(0, 600),
      path:String(ref.path || '').slice(0, 600),
      file:String(ref.file || '').slice(0, 600),
      excerpts:refContent(ref.excerpts || ref.content).slice(0, 3).map(function (part) { return part.slice(0, 1000); })
    };
  }
  function compactResultForSession(data) {
    if (!data || data.generation_status !== 'succeeded' || typeof data.answer !== 'string' || !data.answer) return null;
    var history = data.review_history && typeof data.review_history === 'object' ? data.review_history : {};
    return {
      generation_status:'succeeded',
      answer:data.answer.slice(0, 100000),
      evidence_status:String(data.evidence_status || '').slice(0, 40),
      citations:asArray(data.citations).slice(0, 20).map(compactCitationForSession),
      review_history:{
        previous_rejection_count:Math.max(0, Number(history.previous_rejection_count || 0)),
        exact_rejected_answer_repeat:Boolean(history.exact_rejected_answer_repeat)
      }
    };
  }
  function persistQaSession() {
    var storage = qaSessionStorage(); if (!storage) return;
    try {
      storage.setItem(QA_SESSION_KEY, JSON.stringify({
        schema_version:1,
        messages:qaMessages.slice(-QA_STORED_MESSAGE_LIMIT),
        conversation_history:HISTORY_TURNS > 0 ? chatHistory.slice(-HISTORY_TURNS * 2) : [],
        draft:qaDraft.slice(0, 20000),
        mode:supportedQueryMode(qaMode) ? qaMode : '{{query_mode}}',
        top_k:boundedTopK(qaTopK)
      }));
    } catch (_) {}
  }
  function restoreQaSession() {
    if (qaSessionLoaded) return;
    qaSessionLoaded = true;
    var storage = qaSessionStorage(); if (!storage) return;
    try {
      var parsed = JSON.parse(storage.getItem(QA_SESSION_KEY) || 'null');
      if (!parsed || parsed.schema_version !== 1) return;
      qaMessages = asArray(parsed.messages).slice(-QA_STORED_MESSAGE_LIMIT).reduce(function (items, message) {
        if (!message || typeof message !== 'object') return items;
        if (message.role === 'user' && typeof message.content === 'string') {
          items.push({ role:'user', content:message.content.slice(0, 20000) });
        } else if (message.role === 'assistant') {
          var result = compactResultForSession(message.data);
          if (result) items.push({ role:'assistant', data:result });
        }
        return items;
      }, []);
      chatHistory = asArray(parsed.conversation_history).filter(function (turn) {
        return turn && (turn.role === 'user' || turn.role === 'assistant') && typeof turn.content === 'string';
      }).map(function (turn) {
        return { role:turn.role, content:turn.content.slice(0, 100000) };
      });
      chatHistory = HISTORY_TURNS > 0 ? chatHistory.slice(-HISTORY_TURNS * 2) : [];
      qaDraft = typeof parsed.draft === 'string' ? parsed.draft.slice(0, 20000) : '';
      qaMode = supportedQueryMode(parsed.mode) ? parsed.mode : '{{query_mode}}';
      qaTopK = boundedTopK(parsed.top_k);
    } catch (_) {
      try { storage.removeItem(QA_SESSION_KEY); } catch (_) {}
    }
  }
  async function loadRegistry() {
    try {
      var value = await json('/wiki-registry.json');
      if (value && value.schema_version === 1) registry = value;
    } catch (_) {}
  }
  async function loadCapabilities() {
    try {
      var value = await json('/api/capabilities');
      FEATURES.audit = Boolean(value && value.audit_center);
    } catch (_) { FEATURES.audit = false; }
    if (!FEATURES.audit) return;
    var nav = document.querySelector('.evo-topbar-nav');
    if (nav && !nav.querySelector('[data-audit-link]')) {
      var link = document.createElement('a');
      link.className = 'evo-topbar-link';
      link.href = '/app#audit';
      link.textContent = '审核';
      link.setAttribute('data-audit-link', 'true');
      nav.appendChild(link);
    }
  }
  function syncAuditNavigation() {
    var auditActive = FEATURES.audit && location.hash === '#audit';
    var auditLink = document.querySelector('[data-audit-link]');
    var qaLink = document.querySelector('.evo-topbar-link[href="/app"]');
    var graphLink = document.querySelector('.evo-topbar-link[href="/app#graph"]');
    var graphActive = location.hash === '#graph' || location.hash.indexOf('#entity/') === 0;
    if (auditLink) auditLink.classList.toggle('active', auditActive);
    if (qaLink) qaLink.classList.toggle('active', !auditActive && !graphActive);
    if (graphLink) graphLink.classList.toggle('active', !auditActive && graphActive);
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
  function normalizedEntityTerm(value) {
    return String(value || '').normalize('NFKC').trim().toLowerCase();
  }
  function autoLinkTermAllowed(value) {
    var term = String(value || '').trim();
    if (term.length < 2 || term.length > 80) return false;
    if (/\\s{2,}/.test(term) || /[<>]/.test(term)) return false;
    if (/某/.test(term) && !/[（(][^）)]+[）)]/.test(term) && term.indexOf('案') < 0) return false;
    return true;
  }
  function entityLinkContext(refs) {
    var activeLabels = {};
    asArray(refs).forEach(function (ref) {
      var file = ref.source_label || ref.file_path || ref.source || ref.path || ref.file;
      var mapped = sourceFor(file);
      asArray(mapped && mapped.graph_labels).forEach(function (label) {
        activeLabels[normalizedEntityTerm(label)] = true;
      });
    });
    if (!Object.keys(activeLabels).length) return { candidates:[], linkedEntities:{} };

    var owners = {};
    asArray(registry.entities).forEach(function (entity) {
      var href = safeWikiHref(entity.wiki_path);
      if (!href) return;
      [entity.title, entity.graph_label].concat(asArray(entity.aliases)).forEach(function (term) {
        term = String(term || '').trim();
        if (!autoLinkTermAllowed(term)) return;
        var key = normalizedEntityTerm(term);
        if (!owners[key]) owners[key] = { term:term, entities:[] };
        if (!owners[key].entities.some(function (item) { return item.wiki_path === entity.wiki_path; })) {
          owners[key].entities.push(entity);
        }
      });
    });

    var candidates = [];
    Object.keys(owners).forEach(function (key) {
      var owner = owners[key];
      if (owner.entities.length !== 1) return;
      var entity = owner.entities[0];
      if (!activeLabels[normalizedEntityTerm(entity.graph_label)]) return;
      candidates.push({
        term:owner.term,
        normalized:key,
        entityKey:String(entity.wiki_path),
        href:safeWikiHref(entity.wiki_path)
      });
    });
    candidates.sort(function (a, b) {
      return b.term.length - a.term.length || a.normalized.localeCompare(b.normalized);
    });
    return { candidates:candidates, linkedEntities:{} };
  }
  function linkWikiEntities(text, context) {
    var value = String(text || ''), candidates = context && asArray(context.candidates);
    if (!candidates || !candidates.length) return escapeHtml(value);
    var out = '', offset = 0;
    while (offset < value.length) {
      var matched = null;
      for (var i = 0; i < candidates.length; i += 1) {
        var candidate = candidates[i];
        if (context.linkedEntities[candidate.entityKey]) continue;
        if (value.slice(offset, offset + candidate.term.length) === candidate.term) { matched = candidate; break; }
      }
      if (!matched) { out += escapeHtml(value.charAt(offset)); offset += 1; continue; }
      context.linkedEntities[matched.entityKey] = true;
      out += '<a class="spa-wiki-entity" href="' + escapeHtml(matched.href) + '">' + escapeHtml(value.slice(offset, offset + matched.term.length)) + '</a>';
      offset += matched.term.length;
    }
    return out;
  }
  function stripModelReferences(text) {
    return String(text || '').replace(/\\n#{1,6}\\s*(?:references?|参考文献|引用来源)\\s*\\n[\\s\\S]*$/i, '').trim();
  }
  function safeInline(text, refs, entityContext) {
    var protectedParts = [];
    function stash(part) {
      var marker = '@@EVO_INLINE_' + protectedParts.length + '@@';
      protectedParts.push(part); return marker;
    }
    var value = String(text || '');
    value = value.replace(/!\\[([^\\]]*)\\]\\(([^)\\s]+)(?:\\s+["'][^"']*["'])?\\)/g, function (_, label) {
      return stash('<span class="spa-md-image">[图像：' + escapeHtml(label || '未命名') + ']</span>');
    });
    value = value.replace(/(\\x60+)([\\s\\S]*?)\\1/g, function (_, _ticks, code) {
      return stash('<code>' + escapeHtml(code) + '</code>');
    });
    value = value.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)(?:\\s+["'][^"']*["'])?\\)/g, function (_, label, href) {
      return stash('<a href="' + escapeHtml(href) + '" rel="noopener noreferrer">' + escapeHtml(label) + '</a>');
    });
    value = value.replace(/\\[(\\d{1,3})\\]/g, function (_, marker) {
      var ref = asArray(refs).find(function (item) { return String(item.marker) === marker; });
      return stash(ref
        ? '<a class="spa-cite" href="#evidence-' + escapeHtml(ref.citation_id) + '">[' + marker + ']</a>'
        : '<span class="spa-cite-pending">[依据待核验]</span>');
    });
    value = value.replace(/<([A-Za-z][\\w:-]*)(?:\\s[^>]*)?>.*?<\\/\\1>/g, function (raw) { return stash(escapeHtml(raw)); });
    value = value.replace(/<[^>]*>/g, function (raw) { return stash(escapeHtml(raw)); });
    value = value.replace(/https?:\\/\\/[^\\s<]+/g, function (raw) { return stash(escapeHtml(raw)); });
    value = linkWikiEntities(value, entityContext);
    value = value.replace(/(?:\\*\\*\\*|___)([^*_]+)(?:\\*\\*\\*|___)/g, '<strong><em>$1</em></strong>');
    value = value.replace(/(?:\\*\\*|__)([^*_]+)(?:\\*\\*|__)/g, '<strong>$1</strong>');
    value = value.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    value = value.replace(/(^|[^\\w])\\*([^*\\n]+)\\*(?!\\w)/g, '$1<em>$2</em>');
    value = value.replace(/(^|[^\\w])_([^_\\n]+)_(?!\\w)/g, '$1<em>$2</em>');
    protectedParts.forEach(function (part, index) {
      value = value.split('@@EVO_INLINE_' + index + '@@').join(part);
    });
    return value;
  }
  function safeMarkdown(text, refs) {
    var lines = stripModelReferences(text).split(/\\r?\\n/);
    var out = [], list = null, code = false, codeFence = '', codeLines = [];
    var entityContext = entityLinkContext(refs);
    function closeList() {
      if (list) { out.push('</' + list + '>'); list = null; }
    }
    function tableCells(line) {
      var value = String(line || '').trim();
      if (value.charAt(0) === '|') value = value.slice(1);
      if (value.charAt(value.length - 1) === '|') value = value.slice(0, -1);
      return value.split('|').map(function (cell) { return cell.trim(); });
    }
    function isTableDivider(line) {
      var cells = tableCells(line);
      return cells.length > 1 && cells.every(function (cell) {
        return /^:?-{3,}:?$/.test(cell);
      });
    }
    function isFence(line) {
      return String(line || '').match(/^\\s*(\\x60{3,}|~{3,})/);
    }
    function isBlockStart(index) {
      var line = String(lines[index] || '').trim();
      return !line || isFence(line) || /^(#{1,6})\\s+/.test(line) ||
        /^(?:[-*_]\\s*){3,}$/.test(line) || /^>\\s?/.test(line) ||
        /^[-+*]\\s+/.test(line) || /^\\d+[.)]\\s+/.test(line) ||
        (index + 1 < lines.length && line.indexOf('|') >= 0 && isTableDivider(lines[index + 1]));
    }
    for (var index = 0; index < lines.length; index += 1) {
      var raw = lines[index], line = String(raw || '').trim(), fence = isFence(raw);
      if (fence) {
        closeList();
        if (code && fence[1].charAt(0) === codeFence) {
          out.push('<pre><code>' + escapeHtml(codeLines.join('\\n')) + '</code></pre>');
          codeLines = []; code = false; codeFence = '';
        } else if (!code) {
          code = true; codeFence = fence[1].charAt(0);
        } else {
          codeLines.push(raw);
        }
        continue;
      }
      if (code) { codeLines.push(raw); continue; }
      if (!line) { closeList(); continue; }
      var heading = line.match(/^(#{1,6})\\s+(.+?)\\s*#*\\s*$/);
      if (heading) {
        closeList();
        var level = heading[1].length;
        out.push('<h' + level + '>' + safeInline(heading[2], refs, entityContext) + '</h' + level + '>');
        continue;
      }
      if (/^(?:[-*_]\\s*){3,}$/.test(line)) {
        closeList(); out.push('<hr>'); continue;
      }
      if (index + 1 < lines.length && line.indexOf('|') >= 0 && isTableDivider(lines[index + 1])) {
        closeList();
        var headers = tableCells(line), rows = [], columnCount = headers.length;
        index += 2;
        while (index < lines.length && String(lines[index] || '').trim().indexOf('|') >= 0) {
          var cells = tableCells(lines[index]);
          if (cells.length !== columnCount) break;
          rows.push(cells); index += 1;
        }
        index -= 1;
        out.push('<table><thead><tr>' + headers.map(function (cell) {
          return '<th>' + safeInline(cell, refs, entityContext) + '</th>';
        }).join('') + '</tr></thead><tbody>' + rows.map(function (cells) {
          return '<tr>' + cells.map(function (cell) {
            return '<td>' + safeInline(cell, refs, entityContext) + '</td>';
          }).join('') + '</tr>';
        }).join('') + '</tbody></table>');
        continue;
      }
      if (/^>\\s?/.test(line)) {
        closeList();
        var quoteLines = [];
        while (index < lines.length && /^>\\s?/.test(String(lines[index] || '').trim())) {
          quoteLines.push(String(lines[index]).trim().replace(/^>\\s?/, ''));
          index += 1;
        }
        index -= 1;
        out.push('<blockquote>' + quoteLines.map(function (part) {
          return safeInline(part, refs, entityContext);
        }).join('<br>') + '</blockquote>');
        continue;
      }
      var bullet = line.match(/^[-+*]\\s+(.+)/);
      var ordered = line.match(/^\\d+[.)]\\s+(.+)/);
      if (bullet || ordered) {
        var wanted = ordered ? 'ol' : 'ul';
        if (list !== wanted) { closeList(); list = wanted; out.push('<' + list + '>'); }
        var item = (bullet || ordered)[1], task = item.match(/^\\[([ xX])\\]\\s+(.+)/);
        var taskPrefix = '';
        if (task) {
          taskPrefix = '<input type="checkbox" disabled' + (task[1].toLowerCase() === 'x' ? ' checked' : '') + '> ';
          item = task[2];
        }
        out.push('<li>' + taskPrefix + safeInline(item, refs, entityContext) + '</li>');
        continue;
      }
      closeList();
      var paragraph = [line];
      while (index + 1 < lines.length && !isBlockStart(index + 1)) {
        index += 1;
        paragraph.push(String(lines[index]).trim());
      }
      out.push('<p>' + paragraph.map(function (part) {
        return safeInline(part, refs, entityContext);
      }).join('<br>') + '</p>');
    }
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

  function noTrustedEvidenceHtml() {
    return '<div class="spa-refs spa-refs-empty"><h4>可核验依据</h4>' +
      '<p>暂无可信本地依据。该回答仍可阅读，但不能作为知识库结论使用；已进入人工审核。</p></div>';
  }

  function reviewHistoryHtml(data) {
    var history = data && data.review_history;
    var count = history && Number(history.previous_rejection_count || 0);
    if (!count) return '';
    var exact = Boolean(history.exact_rejected_answer_repeat);
    return '<div class="spa-notice ' + (exact ? 'danger' : 'warning') + '">' +
      '<strong>历史审核预警：</strong>该问题此前有 ' + count +
      ' 条回答被驳回。' + (exact
        ? ' 本次答案与一条已驳回答案完全相同，请勿直接采用。'
        : ' 本次答案仍需结合引用谨慎核验。') + '</div>';
  }

  function resultHtml(data) {
    var answer = data && data.answer;
    if (data && data.generation_status === 'succeeded' && answer) {
      var labels = {
        grounded: ['verified', '已引用知识库资料'],
        partially_grounded: ['warning', '部分依据待核验'],
        ungrounded: ['warning', '本地知识库未覆盖，已进入人工审核']
      };
      var state = labels[data.evidence_status] || ['warning', '依据状态未知'];
      var trustedRefs = data.evidence_status === 'ungrounded' ? [] : data.citations;
      return reviewHistoryHtml(data) + '<div class="spa-status ' + state[0] + '">' +
        escapeHtml(state[1]) + '</div><div class="spa-answer">' +
        safeMarkdown(answer, trustedRefs) + '</div>' +
        (data.evidence_status === 'ungrounded' ? noTrustedEvidenceHtml() : renderRefs(data.citations));
    }
    var label = data && data.error_code === 'QUERY_MAINTENANCE_ACTIVE' ? '系统处于受控维护窗口，请稍后再试。' : '回答生成失败，请稍后重试。';
    return '<div class="spa-status blocked">生成失败</div><div class="spa-error">' + escapeHtml(label) + '</div>';
  }

  function appendMessage(box, role, content, data, isError) {
    var msg = el('div', 'spa-message ' + (role === 'user' ? 'user' : 'assistant'));
    msg.innerHTML = '<div class="spa-role">' + escapeHtml(role) + '</div>' + (data ? resultHtml(data) : '<div class="spa-answer ' + (isError ? 'spa-error' : '') + '">' + escapeHtml(content) + '</div>');
    box.appendChild(msg);
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
    restoreQaSession();
    QA.innerHTML = pageShell('问答', 'LightRAG 只读问答。支持 LightRAG WebUI 的核心检索参数，并保留对话上下文。',
      '<div class="spa-card"><label for="qa-input" class="spa-muted">问题</label><textarea id="qa-input" class="spa-textarea" placeholder="输入问题。也可以用 /hybrid、/mix、/local、/global、/naive 前缀临时切换模式…"></textarea><div class="spa-row wrap"><button id="qa-send" class="spa-btn">提问</button><button id="qa-clear" class="spa-btn secondary">清空</button></div></div><div id="qa-chat" class="spa-chat" aria-live="polite"></div>',
      settingsHtml());
    var mode = document.getElementById('qa-mode'), topK = document.getElementById('qa-top-k');
    mode.value = qaMode; topK.value = qaTopK;
    var input = document.getElementById('qa-input'), send = document.getElementById('qa-send'), chat = document.getElementById('qa-chat');
    input.value = qaDraft;
    qaMessages.forEach(function (message) {
      appendMessage(chat, message.role, message.content || '', message.data || null, false);
    });
    async function ask() {
      var raw = input.value.trim(); if (!raw) return;
      var m = raw.match(/^[/](naive|local|global|hybrid|mix)\\s+([\\s\\S]+)/); var q = raw;
      if (m) { mode.value = m[1]; q = m[2]; }
      qaMode = supportedQueryMode(mode.value) ? mode.value : '{{query_mode}}';
      qaTopK = boundedTopK(topK.value);
      appendMessage(chat, 'user', raw); input.value = ''; qaDraft = ''; persistQaSession(); send.disabled = true;
      var loading = el('div', 'spa-loading', '检索中…'); chat.appendChild(loading);
      try {
        var data = await json('/api/query', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(queryPayload(q)) });
        loading.remove(); appendMessage(chat, 'assistant', '', data, data.generation_status !== 'succeeded');
        var storedResult = compactResultForSession(data);
        if (storedResult) {
          qaMessages.push({ role:'user', content:raw.slice(0, 20000) }, { role:'assistant', data:storedResult });
          qaMessages = qaMessages.slice(-QA_STORED_MESSAGE_LIMIT);
          chatHistory.push({ role:'user', content:q }, { role:'assistant', content:data.answer });
          chatHistory = HISTORY_TURNS > 0 ? chatHistory.slice(-HISTORY_TURNS * 2) : [];
          persistQaSession();
        }
      } catch (e) { loading.remove(); appendMessage(chat, 'assistant', '提问失败：' + e.message + '（确认 LightRAG lane 已构建且服务在运行）', null, true); }
      finally { send.disabled = false; }
    }
    send.addEventListener('click', ask); input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) ask(); });
    input.addEventListener('input', function () { qaDraft = input.value; persistQaSession(); });
    mode.addEventListener('change', function () { qaMode = supportedQueryMode(mode.value) ? mode.value : '{{query_mode}}'; persistQaSession(); });
    topK.addEventListener('input', function () { qaTopK = boundedTopK(topK.value); persistQaSession(); });
    document.getElementById('qa-clear').addEventListener('click', function () {
      chatHistory = []; qaMessages = []; chat.innerHTML = ''; persistQaSession();
    });
  }

  function auditStatusLabel(status) {
    return {
      OPEN:'待审核', IN_REVIEW:'审核中', RESOLVED:'已通过',
      REJECTED:'已驳回', WAIVED:'已豁免'
    }[status] || '状态未知';
  }

  function auditTriggerLabel(code) {
    return {
      QUERY_REFERENCES_IRRELEVANT:'引用与问题相关性不足',
      QUERY_REFERENCE_RELEVANCE_INSUFFICIENT:'引用与问题的有效关联不足',
      QUERY_REFERENCE_RELEVANCE_UNVERIFIED:'问题过短，引用相关性无法确认',
      QUERY_LOCAL_LAW_SUPPORT_MISSING:'本地知识库未覆盖所问法条',
      QUERY_CRITICAL_FACT_UNSUPPORTED:'关键事实缺少引用支撑',
      QUERY_REFERENCE_NOT_ACTIVE:'引用来源当前未激活',
      QUERY_REFERENCE_UNMAPPED:'引用来源无法映射',
      QUERY_REFERENCE_AMBIGUOUS:'引用来源存在歧义',
      QUERY_GENERAL_MODEL_FALLBACK:'使用了模型通用知识回答'
    }[code] || code || '待人工核验';
  }

  function auditStatusHtml(item) {
    var cls = item.status === 'RESOLVED' ? 'verified' :
      item.status === 'REJECTED' ? 'blocked' : 'warning';
    return '<span class="spa-status spa-audit-status ' + cls + '">' +
      escapeHtml(auditStatusLabel(item.status)) + '</span>';
  }

  function auditActionsHtml(item) {
    if (item.status !== 'OPEN' && item.status !== 'IN_REVIEW') return '';
    return '<div class="spa-row wrap spa-audit-actions">' +
      '<button type="button" class="spa-btn" data-audit-resolution="APPROVED">通过</button>' +
      '<button type="button" class="spa-btn danger" data-audit-resolution="REJECTED">驳回</button>' +
      '</div>';
  }

  function auditContentHtml(item) {
    var content = item.content;
    var actions = auditActionsHtml(item);
    if (!item.content_available || !content) {
      return '<div class="spa-notice warning"><strong>内容不可用。</strong> ' +
        '该历史记录没有可读取的受保护快照，系统不会补造问题、回答或引用。</div>' + actions;
    }
    var history = asArray(content.conversation_history);
    var citations = asArray(content.citations);
    var historyHtml = history.length ? '<div class="spa-audit-section"><h4>对话上下文</h4><ol class="spa-audit-history">' +
      history.map(function (turn) {
        return '<li><strong>' + escapeHtml(turn.role === 'assistant' ? 'AI' : '用户') + '：</strong>' +
          escapeHtml(turn.content || '') + '</li>';
      }).join('') + '</ol></div>' : '';
    return '<div class="spa-audit-section"><h4>问题</h4><div class="spa-answer">' +
      escapeHtml(content.question || '') + '</div></div>' + historyHtml +
      '<div class="spa-audit-section"><h4>待核验回答</h4><div class="spa-answer">' +
      safeMarkdown(content.answer || '', citations) + '</div>' + renderRefs(citations) + '</div>' +
      actions;
  }

  async function resolveAudit(item, resolution, detail) {
    var buttons = detail.querySelectorAll('[data-audit-resolution]');
    buttons.forEach(function (button) { button.disabled = true; });
    try {
      await json('/api/audits/' + encodeURIComponent(item.id) + '/resolve', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ resolution:resolution })
      });
      var flash = document.getElementById('audit-flash');
      if (flash) {
        flash.className = 'spa-notice';
        flash.textContent = resolution === 'APPROVED' ? '审核已通过。' : '回答已驳回，正文已保留用于后续对比。';
      }
      await loadAuditList(document.querySelector('.spa-audit-filter.active').getAttribute('data-status'));
    } catch (error) {
      detail.insertAdjacentHTML('afterbegin', '<div class="spa-notice danger">操作失败：' + escapeHtml(error.message) + '</div>');
      buttons.forEach(function (button) { button.disabled = false; });
    }
  }

  async function expandAudit(item, detail) {
    if (detail.getAttribute('data-loaded') === 'true') {
      detail.hidden = !detail.hidden;
      return;
    }
    detail.hidden = false;
    detail.innerHTML = '<p class="spa-loading">正在读取受保护审核快照…</p>';
    try {
      var data = await json('/api/audits/' + encodeURIComponent(item.id));
      var full = data.item;
      var events = asArray(full.events);
      detail.innerHTML = '<div class="spa-audit-meta"><span>审核 ID：' + escapeHtml(full.id) +
        '</span><span>触发原因：' + escapeHtml(auditTriggerLabel(full.trigger_code)) + '</span></div>' +
        auditContentHtml(full) +
        (events.length ? '<div class="spa-audit-section spa-audit-events"><h4>审核记录</h4>' +
          events.map(function (event) { return '<div>' + escapeHtml(event.created_at) + ' · ' +
            escapeHtml(event.action) + ' · ' + escapeHtml(event.actor) + '</div>'; }).join('') + '</div>' : '');
      detail.setAttribute('data-loaded', 'true');
      detail.querySelectorAll('[data-audit-resolution]').forEach(function (button) {
        button.addEventListener('click', function () {
          resolveAudit(full, button.getAttribute('data-audit-resolution'), detail);
        });
      });
    } catch (error) {
      detail.innerHTML = '<p class="spa-error">审核详情加载失败：' + escapeHtml(error.message) + '</p>';
    }
  }

  async function loadAuditList(status) {
    var list = document.getElementById('audit-list');
    if (!list) return;
    list.innerHTML = '<p class="spa-loading">正在加载审核记录…</p>';
    try {
      var data = await json('/api/audits?status=' + encodeURIComponent(status));
      var items = asArray(data.items);
      if (!items.length) {
        list.innerHTML = '<div class="spa-card spa-audit-empty">当前分类没有审核记录。</div>';
        return;
      }
      list.innerHTML = items.map(function (item) {
        var question = item.question_summary || '内容不可用';
        return '<article class="spa-card spa-audit-item"><button type="button" class="spa-audit-head" data-audit-id="' +
          escapeHtml(item.id) + '"><span><span class="spa-audit-question">' + escapeHtml(question) +
          '</span><span class="spa-audit-meta"><span>' + escapeHtml(auditTriggerLabel(item.trigger_code)) +
          '</span><span>' + escapeHtml(item.severity) + '</span><span>' + escapeHtml(item.created_at) +
          '</span></span></span>' + auditStatusHtml(item) + '</button>' +
          '<div class="spa-audit-detail" data-audit-detail="' + escapeHtml(item.id) + '" hidden></div></article>';
      }).join('');
      items.forEach(function (item) {
        var head = list.querySelector('[data-audit-id="' + item.id + '"]');
        var detail = list.querySelector('[data-audit-detail="' + item.id + '"]');
        if (head && detail) head.addEventListener('click', function () { expandAudit(item, detail); });
      });
    } catch (error) {
      list.innerHTML = '<div class="spa-card"><p class="spa-error">审核列表加载失败：' + escapeHtml(error.message) + '</p></div>';
    }
  }

  function renderAudit() {
    AUDIT.innerHTML = pageShell('审核中心', '本机单用户审核台。展开记录核对问题、回答与引用，单击即可通过或驳回。',
      '<div id="audit-flash" aria-live="polite"></div>' +
      '<div class="spa-audit-filters" role="group" aria-label="审核状态筛选">' +
      '<button type="button" class="spa-audit-filter active" data-status="OPEN">待审核</button>' +
      '<button type="button" class="spa-audit-filter" data-status="RESOLVED">已通过</button>' +
      '<button type="button" class="spa-audit-filter" data-status="REJECTED">已驳回</button></div>' +
      '<div id="audit-list" aria-live="polite"></div>',
      '<div class="spa-card"><h3>审核说明</h3><p class="spa-muted">通过后删除受保护正文；驳回后保留正文，并在相同查询再次回答时显示历史预警。操作立即生效。</p></div>');
    document.querySelectorAll('.spa-audit-filter').forEach(function (button) {
      button.addEventListener('click', function () {
        document.querySelectorAll('.spa-audit-filter').forEach(function (candidate) { candidate.classList.remove('active'); });
        button.classList.add('active');
        loadAuditList(button.getAttribute('data-status'));
      });
    });
    loadAuditList('OPEN');
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
    document.getElementById('entity-ask').addEventListener('click', async function () { var out = document.getElementById('entity-answer'), q = document.getElementById('entity-q').value; out.innerHTML = '<p class="spa-loading">检索中…</p>'; try { var data = await json('/api/query', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ schema_version:2, query:q, mode:'mix', top_k:20 }) }); out.innerHTML = resultHtml(data); } catch (e) { out.innerHTML = '<p class="spa-error">提问失败：' + escapeHtml(e.message) + '</p>'; } });
  }

  function route() {
    QA.classList.remove('active'); GRAPH.classList.remove('active'); ENTITY.classList.remove('active'); AUDIT.classList.remove('active');
    var hash = location.hash || '';
    if (hash === '#audit' && FEATURES.audit) {
      renderAudit(); AUDIT.classList.add('active');
    } else if (hash.indexOf('#entity/') === 0 && FEATURES.entity) {
      renderEntity(decodeURIComponent(hash.slice('#entity/'.length))); ENTITY.classList.add('active');
    } else if (hash === '#graph' && FEATURES.graph) {
      renderGraph(); GRAPH.classList.add('active');
    } else if (FEATURES.qa) {
      renderQA(); QA.classList.add('active');
    } else if (FEATURES.graph) {
      renderGraph(); GRAPH.classList.add('active');
    }
    syncAuditNavigation();
  }
  window.addEventListener('hashchange', route); window.addEventListener('load', async function () { await Promise.all([loadRegistry(), loadCapabilities()]); route(); });
})();
"""

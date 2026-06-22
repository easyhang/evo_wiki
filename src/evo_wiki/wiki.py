from __future__ import annotations

import html
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import EvoConfig
from .paths import ProjectPaths
from .utils import relpath, slugify, utc_now, write_json
from .wiki_health import extract_wikilinks, lint_wiki_artifacts, parse_yaml_frontmatter


@dataclass(frozen=True)
class WikiPage:
    source: Path
    output: Path
    title: str
    text: str
    links: list[str]
    page_type: str
    sources: list[str] = field(default_factory=list)
    word_count: int = 0


def ensure_wiki_stub(paths: ProjectPaths, config: EvoConfig) -> None:
    """Create llm-wiki-style starter wiki-src files for Claude Code to edit."""
    paths.wiki_src.mkdir(parents=True, exist_ok=True)
    for subdir in ["concepts", "entities", "summaries"]:
        (paths.wiki_src / subdir).mkdir(parents=True, exist_ok=True)
    for page in config.wiki.get("pages", []):
        rel = page.get("path", "index.md")
        target = paths.wiki_src / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        title = page.get("title") or Path(rel).stem.replace("-", " ").title()
        page_type = page.get("type", infer_page_type(target, paths.wiki_src))
        description = page.get("description", "Claude Code should replace this stub with sourced content.")
        sources = page.get("sources", [])
        source_lines = "\n".join(f"- `{src}`" for src in sources) or "- 待 Claude Code 绑定来源"
        target.write_text(
            "---\n"
            f'title: "{title}"\n'
            f"type: {page_type}\n"
            f"created: {utc_now()[:10]}\n"
            f"updated: {utc_now()[:10]}\n"
            "sources: []\n"
            "tags: []\n"
            "---\n\n"
            f"# {title}\n\n"
            f"> {description}\n\n"
            "<!-- evo:agent-content:start -->\n"
            "本页是 Evo wiki 生成的占位页。请让 Claude Code 基于 corpus 原始语料补全内容。\n"
            "<!-- evo:agent-content:end -->\n\n"
            "## Sources\n\n"
            f"{source_lines}\n",
            encoding="utf-8",
        )


def render_wiki(paths: ProjectPaths, config: EvoConfig) -> dict:
    ensure_wiki_stub(paths, config)
    if paths.wiki_dist.exists():
        shutil.rmtree(paths.wiki_dist)
    paths.wiki_dist.mkdir(parents=True, exist_ok=True)
    (paths.wiki_dist / "assets").mkdir(parents=True, exist_ok=True)

    markdown_files = sorted(paths.wiki_src.rglob("*.md"))
    link_map = build_link_map(paths, markdown_files)
    pages = [render_page(paths, config, md, link_map) for md in markdown_files]
    write_assets(paths)
    write_search_index(paths, pages)
    write_dependency_graph(paths, pages, link_map)
    health = lint_wiki_artifacts(paths.root, paths.wiki_src, paths.wiki_audit, paths.wiki_log)
    write_json(paths.wiki_reports / "wiki-health.json", health)

    report = {
        "status": "success" if health["status"] in {"clean", "issues_found"} else "failed",
        "generated_at": utc_now(),
        "page_count": len(pages),
        "html_output": relpath(paths.wiki_dist / "index.html", paths.root),
        "llm_wiki_model": {
            "layout": "index + concepts/entities/summaries + audit/log/queries",
            "final_output": "static_html",
            "renderer": "evo_wiki.wiki",
        },
        "pages": [
            {
                "source": relpath(page.source, paths.root),
                "output": relpath(page.output, paths.root),
                "title": page.title,
                "type": page.page_type,
                "word_count": page.word_count,
                "sources": page.sources,
                "links": page.links,
            }
            for page in pages
        ],
        "health": health,
        "warnings": collect_warnings(paths, pages, health),
    }
    write_json(paths.wiki_reports / "wiki-report.json", report)
    write_json(
        paths.wiki / "manifest.json",
        {
            "status": "success",
            "generated_at": report["generated_at"],
            "output": relpath(paths.wiki_dist / "index.html", paths.root),
            "page_count": len(pages),
            "health_status": health["status"],
            "health_issue_count": health["issue_count"],
        },
    )
    return report


def build_link_map(paths: ProjectPaths, markdown_files: list[Path]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for md in markdown_files:
        rel_html = md.relative_to(paths.wiki_src).with_suffix(".html").as_posix()
        raw = md.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(raw)
        title = str(frontmatter.get("title") or extract_title(body) or md.stem)
        aliases = {md.stem.lower(), slugify(md.stem), title.lower(), slugify(title)}
        if md.name == "index.md" and md.parent != paths.wiki_src:
            aliases.add(md.parent.name.lower())
            aliases.add(slugify(md.parent.name))
        for alias in aliases:
            mapping[alias] = rel_html
    return mapping


def render_page(paths: ProjectPaths, config: EvoConfig, md_path: Path, link_map: dict[str, str]) -> WikiPage:
    raw = md_path.read_text(encoding="utf-8")
    frontmatter, markdown = split_frontmatter(raw)
    title = str(frontmatter.get("title") or extract_title(markdown) or md_path.stem.replace("-", " ").title())
    page_type = str(frontmatter.get("type") or infer_page_type(md_path, paths.wiki_src))
    rel = md_path.relative_to(paths.wiki_src).with_suffix(".html")
    resolver = make_link_resolver(current=rel.as_posix(), link_map=link_map)
    body = markdown_to_html(markdown, resolver=resolver)
    out = paths.wiki_dist / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    nav = build_nav(paths, current=rel.as_posix())
    html_doc = page_template(config, title, nav, body, current=rel.as_posix(), page_type=page_type)
    out.write_text(html_doc, encoding="utf-8")
    text = strip_markdown(markdown)
    links = sorted(set(extract_wikilinks(markdown)))
    return WikiPage(
        source=md_path,
        output=out,
        title=title,
        text=text,
        links=links,
        page_type=page_type,
        sources=parse_sources(frontmatter, markdown),
        word_count=count_words(markdown),
    )


def split_frontmatter(markdown: str) -> tuple[dict[str, object], str]:
    if not markdown.startswith("---\n"):
        return {}, markdown
    lines = markdown.splitlines()
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            raw_fm = "\n".join(lines[: idx + 1])
            parsed = parse_yaml_frontmatter(raw_fm) or {}
            return parsed, "\n".join(lines[idx + 1 :]).lstrip("\n")
    return {}, markdown


def infer_page_type(path: Path, wiki_src: Path) -> str:
    try:
        rel = path.relative_to(wiki_src)
    except ValueError:
        return "page"
    if rel.parts and rel.parts[0] in {"concepts", "entities", "summaries"}:
        return {"concepts": "concept", "entities": "entity", "summaries": "summary"}[rel.parts[0]]
    if rel.name == "index.md":
        return "index"
    return "page"


def extract_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def strip_markdown(markdown: str) -> str:
    text = re.sub(r"```.*?```", " ", markdown, flags=re.S)
    text = re.sub(r"\$\$.*?\$\$", " ", text, flags=re.S)
    text = re.sub(r"[#>*_`\[\]()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def count_words(markdown: str) -> int:
    text = strip_markdown(markdown)
    english = re.findall(r"[A-Za-z0-9_]+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return len(english) + len(cjk)


def parse_sources(frontmatter: dict[str, object], markdown: str) -> list[str]:
    sources: list[str] = []
    raw = frontmatter.get("sources")
    if isinstance(raw, list):
        sources.extend(str(item).strip() for item in raw if str(item).strip())
    elif isinstance(raw, str) and raw and raw != "[]":
        sources.append(raw)
    in_sources = False
    for line in markdown.splitlines():
        if line.lower().strip() in {"## sources", "## references", "## 来源"}:
            in_sources = True
            continue
        if in_sources and line.startswith("## "):
            break
        if in_sources and line.strip().startswith("- "):
            sources.append(line.strip()[2:].strip().strip("`"))
    return sorted(set(sources))


def markdown_to_html(markdown: str, *, resolver: Callable[[str], str]) -> str:
    blocks: list[str] = []
    in_code = False
    code_lang = ""
    code_lines: list[str] = []
    para: list[str] = []
    ul: list[str] = []
    table: list[str] = []
    in_math = False
    math_lines: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            blocks.append("<p>" + inline(" ".join(para), resolver=resolver) + "</p>")
            para = []

    def flush_ul() -> None:
        nonlocal ul
        if ul:
            blocks.append("<ul>" + "".join(f"<li>{inline(item, resolver=resolver)}</li>" for item in ul) + "</ul>")
            ul = []

    def split_row(row: str) -> list[str]:
        return [cell.strip() for cell in row.strip().strip("|").split("|")]

    def flush_table() -> None:
        nonlocal table
        if not table:
            return
        rows = table
        table = []
        # 合法表格：表头行 + 仅由 |-:空格 组成的分隔行。否则降级为段落，避免吞内容。
        is_table = (
            len(rows) >= 2
            and set(rows[1].replace("|", "").replace("-", "").replace(":", "").strip()) <= {""}
            and "-" in rows[1]
        )
        if not is_table:
            for row in rows:
                blocks.append("<p>" + inline(row, resolver=resolver) + "</p>")
            return
        header = split_row(rows[0])
        thead = "<thead><tr>" + "".join(f"<th>{inline(c, resolver=resolver)}</th>" for c in header) + "</tr></thead>"
        body_rows = []
        for row in rows[2:]:
            cells = split_row(row)
            body_rows.append("<tr>" + "".join(f"<td>{inline(c, resolver=resolver)}</td>" for c in cells) + "</tr>")
        tbody = "<tbody>" + "".join(body_rows) + "</tbody>"
        blocks.append("<table>" + thead + tbody + "</table>")

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped == "$$":
            if in_math:
                blocks.append('<div class="math-block">$$\n' + html.escape("\n".join(math_lines)) + "\n$$</div>")
                math_lines = []
                in_math = False
            else:
                flush_para(); flush_ul(); flush_table(); in_math = True
            continue
        if in_math:
            math_lines.append(line)
            continue
        if stripped.startswith("```"):
            if in_code:
                code = html.escape("\n".join(code_lines))
                if code_lang == "mermaid":
                    blocks.append('<div class="mermaid">' + code + "</div>")
                else:
                    blocks.append(f'<pre><code class="language-{html.escape(code_lang)}">' + code + "</code></pre>")
                code_lines = []
                code_lang = ""
                in_code = False
            else:
                flush_para(); flush_ul(); flush_table(); in_code = True; code_lang = stripped[3:].strip().lower()
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            flush_para(); flush_ul(); flush_table(); continue
        if stripped.startswith("|"):
            flush_para(); flush_ul(); table.append(stripped)
        elif stripped.startswith("#"):
            flush_para(); flush_ul(); flush_table()
            level = min(len(stripped) - len(stripped.lstrip("#")), 6)
            text = stripped[level:].strip()
            anchor = slugify(text)
            blocks.append(f'<h{level} id="{anchor}">{inline(text, resolver=resolver)}</h{level}>')
        elif stripped.startswith("- "):
            flush_para(); flush_table(); ul.append(stripped[2:].strip())
        elif stripped.startswith("> "):
            flush_para(); flush_ul(); flush_table(); blocks.append("<blockquote>" + inline(stripped[2:].strip(), resolver=resolver) + "</blockquote>")
        else:
            flush_table(); para.append(stripped)
    flush_para(); flush_ul(); flush_table()
    return "\n".join(blocks)


def inline(text: str, *, resolver: Callable[[str], str]) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\$([^$]+)\$", r'<span class="math-inline">$\1$</span>', escaped)
    escaped = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: wikilink(m.group(1), m.group(2), resolver), escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def wikilink(target: str, label: str | None, resolver: Callable[[str], str]) -> str:
    href = resolver(target)
    css = "wikilink" if href != "#missing" else "wikilink missing"
    return f'<a class="{css}" href="{html.escape(href)}">{html.escape(label or target)}</a>'


def make_link_resolver(*, current: str, link_map: dict[str, str]) -> Callable[[str], str]:
    def resolve(target: str) -> str:
        key = target.lower()
        rel = link_map.get(key) or link_map.get(slugify(target))
        if not rel:
            return "#missing"
        return relpath_from(current, rel)

    return resolve


def build_nav(paths: ProjectPaths, *, current: str) -> str:
    items = []
    grouped = {"index": [], "concepts": [], "entities": [], "summaries": [], "other": []}
    for md in sorted(paths.wiki_src.rglob("*.md")):
        rel_md = md.relative_to(paths.wiki_src)
        rel = rel_md.with_suffix(".html").as_posix()
        raw = md.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(raw)
        title = str(frontmatter.get("title") or extract_title(body) or md.stem)
        active = " active" if rel == current else ""
        href = relpath_from(current, rel)
        group = rel_md.parts[0] if rel_md.parts and rel_md.parts[0] in grouped else "other"
        if rel_md.name == "index.md" and len(rel_md.parts) == 1:
            group = "index"
        extra = " nav-home" if group == "index" else ""
        grouped[group].append(
            f'<a class="nav-link{extra}{active}" href="{html.escape(href)}">{html.escape(title)}</a>'
        )
    labels = {"index": "入口", "concepts": "概念", "entities": "实体", "summaries": "摘要", "other": "其他"}
    for group in ["index", "concepts", "entities", "summaries", "other"]:
        if grouped[group]:
            if group == "index":
                items.append("".join(grouped[group]))
            else:
                items.append(
                    f'<div class="nav-group"><div class="nav-group-title">{labels[group]}</div>'
                    + "".join(grouped[group])
                    + "</div>"
                )
    return "\n".join(items)


def relpath_from(current: str, target: str) -> str:
    import os

    current_dir = Path(current).parent.as_posix()
    start = "." if current_dir == "." else current_dir
    return os.path.relpath(target, start=start).replace("\\", "/")


def asset_prefix(current: str) -> str:
    depth = len(Path(current).parent.parts) if Path(current).parent.as_posix() != "." else 0
    return "../" * depth


TYPE_LABELS = {
    "concept": "概念",
    "entity": "实体",
    "summary": "摘要",
    "index": "索引",
    "page": "页面",
}


def type_badge(page_type: str) -> str:
    if page_type == "index":
        return ""
    label = TYPE_LABELS.get(page_type, page_type)
    return (
        '<div class="meta">'
        f'<span class="type-badge type-{html.escape(page_type)}">{html.escape(label)}</span>'
        "</div>"
    )


def page_template(config: EvoConfig, title: str, nav: str, body: str, *, current: str, page_type: str) -> str:
    site_title = html.escape(config.wiki.get("title", "Evo Wiki"))
    prefix = asset_prefix(current)
    home_href = f"{prefix}index.html"
    meta = type_badge(page_type)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · {site_title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600;700;900&family=Crimson+Pro:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{prefix}assets/style.css">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.css">
  <script defer src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.js"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/contrib/auto-render.min.js"></script>
  <script defer src="{prefix}assets/app.js"></script>
</head>
<body data-page-type="{html.escape(page_type)}">
  <aside class="sidebar">
    <div class="sidebar-header">
      <a class="logo" href="{home_href}"><span class="logo-icon">📚</span>{site_title}</a>
    </div>
    <div class="sidebar-search">
      <input id="search" placeholder="搜索 Wiki..." autocomplete="off" data-search-index="{prefix}search-index.json">
      <div id="search-results"></div>
    </div>
    <nav class="sidebar-nav">{nav}</nav>
  </aside>
  <main class="main">
    <article class="article">{meta}{body}</article>
  </main>
</body>
</html>
"""


def write_assets(paths: ProjectPaths) -> None:
    (paths.wiki_dist / "assets" / "style.css").write_text(STYLE, encoding="utf-8")
    (paths.wiki_dist / "assets" / "app.js").write_text(APP_JS, encoding="utf-8")


def write_search_index(paths: ProjectPaths, pages: list[WikiPage]) -> None:
    index = [
        {
            "title": page.title,
            "type": page.page_type,
            "path": page.output.relative_to(paths.wiki_dist).as_posix(),
            "text": page.text[:5000],
        }
        for page in pages
    ]
    (paths.wiki_dist / "search-index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def write_dependency_graph(paths: ProjectPaths, pages: list[WikiPage], link_map: dict[str, str]) -> None:
    graph = {
        relpath(page.source, paths.root): {
            "links": page.links,
            "resolved_links": {link: link_map.get(link.lower()) or link_map.get(slugify(link)) for link in page.links},
            "output": relpath(page.output, paths.root),
            "type": page.page_type,
            "sources": page.sources,
        }
        for page in pages
    }
    write_json(paths.wiki_state / "wiki-dependency-graph.json", graph)


def collect_warnings(paths: ProjectPaths, pages: list[WikiPage], health: dict) -> list[dict]:
    warnings: list[dict] = []
    if not pages:
        warnings.append({"code": "no_pages", "message": "artifacts/wiki/wiki-src has no Markdown pages."})
    for page in pages:
        if "占位页" in page.text or "待 Claude Code" in page.text:
            warnings.append({"code": "stub_content", "page": relpath(page.source, paths.root), "message": "Page still looks like a stub; ask Claude Code to generate sourced content."})
        if not page.sources and page.page_type not in {"index"}:
            warnings.append({"code": "missing_sources", "page": relpath(page.source, paths.root), "message": "Page has no source references."})
    for issue in health.get("issues", []):
        if issue.get("severity") in {"error", "warn"}:
            warnings.append({"code": issue.get("code"), "page": issue.get("path"), "message": issue.get("message")})
    return warnings


STYLE = """
/* Evo wiki theme — inspired by learnbuffett.com (warm cream + navy + gold, serif headings). */
:root {
  color-scheme: light;
  --bg:#FAF7F2; --bg2:#F3EDE4; --text:#1B1B18; --text2:#6B6560;
  --gold:#B8860B; --gold-light:#D4A843; --gold-glow:rgba(184,134,11,.12);
  --navy:#1A2332; --navy-light:#2C3E50; --cream:#FFF8EE;
  --border:#E0D6C8; --card:#FFFFFF; --link:#8B5E0B;
  --serif:'Noto Serif SC','Crimson Pro',Georgia,'Times New Roman',serif;
  --sans:'DM Sans',-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
  --sidebar-w:260px;
}
* { box-sizing:border-box; margin:0; padding:0; }
html { font-size:15px; scroll-behavior:smooth; }
body { font-family:var(--sans); color:var(--text); background:var(--bg); display:flex; min-height:100vh; line-height:1.8; }

/* ===== SIDEBAR ===== */
.sidebar { width:var(--sidebar-w); background:var(--navy); position:fixed; top:0; left:0; bottom:0; overflow-y:auto; z-index:100; display:flex; flex-direction:column; }
.sidebar-header { padding:20px 16px 16px; border-bottom:1px solid rgba(255,255,255,.08); }
.logo { color:#fff; font-size:17px; font-weight:700; text-decoration:none; letter-spacing:.5px; font-family:var(--serif); display:inline-flex; align-items:center; gap:10px; }
.logo-icon { font-size:22px; line-height:1; }
.sidebar-search { padding:14px 12px 6px; }
.sidebar-search input { width:100%; border:1px solid rgba(255,255,255,.14); background:rgba(255,255,255,.06); color:#fff; border-radius:8px; padding:9px 12px; font-size:13px; font-family:var(--sans); }
.sidebar-search input::placeholder { color:rgba(255,255,255,.4); }
.sidebar-search input:focus { outline:none; border-color:var(--gold); }
#search-results { margin-top:6px; }
#search-results a { display:block; color:#cbd5e1; text-decoration:none; padding:7px 10px; border-radius:6px; font-size:12px; }
#search-results a:hover { background:rgba(255,255,255,.06); color:#fff; }
.sidebar-nav { flex:1; padding:8px 0 24px; overflow-y:auto; }
.sidebar-nav::-webkit-scrollbar { width:4px; }
.sidebar-nav::-webkit-scrollbar-thumb { background:rgba(255,255,255,.15); border-radius:2px; }
.nav-link { display:block; padding:6px 16px; color:#cbd5e1; text-decoration:none; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; border-left:3px solid transparent; transition:all .15s; }
.nav-link:hover { background:rgba(255,255,255,.06); color:#fff; }
.nav-link.active { color:#fff; background:rgba(184,134,11,.15); border-left-color:var(--gold); font-weight:600; }
.nav-link.nav-home { font-size:14px; padding:10px 16px; font-weight:500; }
.nav-group { margin-bottom:4px; }
.nav-group-title { padding:10px 16px 6px; color:#cbd5e1; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.5px; }

/* ===== MAIN / ARTICLE ===== */
.main { margin-left:max(var(--sidebar-w), calc((100vw - 1160px) / 2)); flex:1; position:relative; max-width:1160px; }
.article { max-width:820px; padding:48px 48px 80px; }
.meta { font-size:13px; color:var(--text2); margin-bottom:16px; display:flex; align-items:center; gap:8px; }
.type-badge { font-size:11px; padding:2px 9px; border-radius:4px; color:#fff; font-weight:600; letter-spacing:.5px; }
.type-concept { background:#7C5E2A; }
.type-entity  { background:#1A6B7C; }
.type-summary { background:#2A6B4F; }
.type-index   { background:#6B6560; }
.type-page    { background:var(--navy-light); }

.article h1 { font-family:var(--serif); font-size:30px; line-height:1.3; margin-bottom:24px; font-weight:900; color:var(--navy); letter-spacing:-.5px; }
.article h2 { font-family:var(--serif); font-size:21px; margin:36px 0 14px; padding-bottom:8px; border-bottom:2px solid var(--border); font-weight:700; color:var(--navy); }
.article h3 { font-family:var(--serif); font-size:17px; margin:24px 0 10px; font-weight:600; color:var(--navy-light); }
.article h4 { font-family:var(--serif); font-size:15px; margin:18px 0 8px; font-weight:600; color:var(--navy-light); }
.article p { margin:10px 0; }
.article ul, .article ol { padding-left:24px; margin:10px 0; }
.article li { margin:4px 0; }
.article a { color:var(--link); text-decoration:none; border-bottom:1px solid rgba(139,94,11,.3); }
.article a:hover { border-bottom-color:var(--gold); }

.article a.wikilink { color:var(--link); text-decoration:none; border-bottom:none; background:linear-gradient(to bottom,transparent 60%,var(--gold-glow) 60%); transition:background .2s; font-weight:500; }
.article a.wikilink:hover { background:linear-gradient(to bottom,transparent 40%,rgba(184,134,11,.2) 40%); }
.article a.wikilink.missing { color:#aaa; text-decoration:line-through; background:none; }

.article blockquote { background:var(--cream); border-left:4px solid var(--gold); padding:14px 20px; margin:16px 0; border-radius:0 8px 8px 0; font-style:italic; color:#5C4813; font-family:var(--serif); }
.article code { background:var(--bg2); color:#7C5E2A; border-radius:5px; padding:2px 6px; font-size:.9em; }
.article pre { background:var(--navy); color:#e6e1d6; padding:18px 20px; border-radius:12px; overflow:auto; margin:16px 0; border:1px solid rgba(0,0,0,.15); }
.article pre code { background:none; color:inherit; padding:0; }
.article table { border-collapse:collapse; width:100%; margin:16px 0; font-size:14px; }
.article th, .article td { border:1px solid var(--border); padding:8px 12px; text-align:left; }
.article th { background:var(--bg2); font-weight:600; color:var(--navy); }
.article hr { border:none; border-top:1px solid var(--border); margin:32px 0; }

.mermaid { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; margin:18px 0; }
.math-block { overflow:auto; padding:12px 0; }

@media (max-width: 980px) {
  body { display:block; }
  .sidebar { position:static; width:auto; bottom:auto; }
  .main { margin-left:0; max-width:100%; }
  .article { padding:28px 22px 64px; }
  .article h1 { font-size:24px; }
}
"""

APP_JS = """
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
"""

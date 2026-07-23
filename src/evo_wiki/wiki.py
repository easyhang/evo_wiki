from __future__ import annotations

import html
import json
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable

from .config import EvoConfig
from .corpus import scan_corpus
from .paths import ProjectPaths
from .spa_assets import write_spa_assets
from .state.contracts import StateError
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
    for subdir in ["concepts", "entities", "sources"]:
        (paths.wiki_src / subdir).mkdir(parents=True, exist_ok=True)
    for page in config.wiki.get("pages", []):
        rel = page.get("path", "index.md")
        target = paths.wiki_src / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        title = page.get("title") or Path(rel).stem.replace("-", " ").title()
        page_type = page.get("type", infer_page_type(target, paths.wiki_src))
        description = page.get("description", "Claude Code should replace this stub with corpus-grounded content.")
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
            "本页是 Evo wiki 生成的占位页。请让 Claude Code 基于 corpus 原始语料补全内容；页面中不需要单独标明来源。\n"
            "<!-- evo:agent-content:end -->\n",
            encoding="utf-8",
        )


def update_wiki_progress(paths: ProjectPaths, progress: dict, phase: str, status: str, **updates: object) -> None:
    """Write a resumable progress checkpoint for Wiki rendering.

    该文件不是自动续跑引擎，而是给 Claude Code/用户断点续处理用的检查点：
    可以看出已完成哪些页面、卡在哪个阶段、下一步应从哪里恢复。
    """
    now = utc_now()
    progress.setdefault("started_at", now)
    progress["updated_at"] = now
    progress["status"] = status
    progress["current_phase"] = phase
    progress.update(updates)
    phases = progress.setdefault("phases", [])
    if isinstance(phases, list):
        phases.append({"phase": phase, "status": status, "at": now, **updates})
    write_json(paths.wiki / "progress.json", progress)


def render_wiki(paths: ProjectPaths, config: EvoConfig) -> dict:
    presentation = config.validate(paths.root)
    corpus_files = scan_corpus(paths.root, paths.corpus)
    progress: dict = {
        "schema_version": 1,
        "lane": "wiki",
        "status": "running",
        "completed_pages": [],
        "failed_pages": [],
        "resume_hint": "Inspect completed_pages and current_phase; rerun evo-wiki render-wiki after fixing the failed phase.",
    }
    update_wiki_progress(paths, progress, "start", "running")
    try:
        ensure_wiki_stub(paths, config)
        update_wiki_progress(paths, progress, "ensure_wiki_stub", "running")
        if paths.wiki_dist.exists():
            shutil.rmtree(paths.wiki_dist)
        paths.wiki_dist.mkdir(parents=True, exist_ok=True)
        (paths.wiki_dist / "assets").mkdir(parents=True, exist_ok=True)
        update_wiki_progress(paths, progress, "prepare_dist", "running")

        markdown_files = sorted(paths.wiki_src.rglob("*.md"))
        progress["total_pages"] = len(markdown_files)
        progress["source_files"] = [relpath(path, paths.root) for path in markdown_files]
        update_wiki_progress(paths, progress, "scan_wiki_src", "running", total_pages=len(markdown_files))

        link_map = build_link_map(paths, markdown_files)
        page_index = build_page_index(paths, markdown_files)
        backlink_index = build_backlink_index(paths, markdown_files, page_index)
        registry = build_wiki_registry(paths, markdown_files)
        write_json(paths.wiki_dist / "wiki-registry.json", registry)
        update_wiki_progress(paths, progress, "build_link_map", "running", link_alias_count=len(link_map))

        pages: list[WikiPage] = []
        for index, md in enumerate(markdown_files, start=1):
            try:
                page = render_page(
                    paths,
                    config,
                    md,
                    link_map,
                    page_index,
                    backlink_index,
                    registry,
                )
            except Exception as exc:
                failed = {"source": relpath(md, paths.root), "error": str(exc)}
                progress.setdefault("failed_pages", []).append(failed)
                update_wiki_progress(paths, progress, "render_pages", "failed", failed_page=failed, rendered_pages=len(pages))
                raise
            pages.append(page)
            progress.setdefault("completed_pages", []).append(
                {"source": relpath(page.source, paths.root), "output": relpath(page.output, paths.root), "title": page.title, "type": page.page_type}
            )
            update_wiki_progress(paths, progress, "render_pages", "running", rendered_pages=index, total_pages=len(markdown_files))

        write_assets(paths, config)
        update_wiki_progress(paths, progress, "write_assets", "running")
        write_spa_assets(paths, config)
        update_wiki_progress(paths, progress, "write_spa_assets", "running")
        write_search_index(paths, pages)
        update_wiki_progress(paths, progress, "write_search_index", "running", search_entries=len(pages))
        write_dependency_graph(paths, pages, link_map)
        update_wiki_progress(paths, progress, "write_dependency_graph", "running")

        # 结束阶段必须 lint：链接、孤儿页、索引收录、audit/log 形状与 HTML 必需原文页结构在这里汇总。
        health = lint_wiki_artifacts(
            paths.root,
            paths.wiki_src,
            paths.wiki_audit,
            paths.wiki_log,
            content_contract_version=presentation[
                "content_contract_version"
            ],
            corpus_paths=[item.path for item in corpus_files],
        )
        write_json(paths.wiki_reports / "wiki-health.json", health)
        update_wiki_progress(
            paths,
            progress,
            "lint_wiki",
            "running",
            lint_status=health["status"],
            lint_issue_count=health["issue_count"],
        )

        report = {
            "status": "success" if health["status"] in {"clean", "issues_found"} else "failed",
            "content_contract_version": presentation[
                "content_contract_version"
            ],
            "generated_at": utc_now(),
            "page_count": len(pages),
            "html_output": relpath(paths.wiki_dist / "index.html", paths.root),
            "progress": relpath(paths.wiki / "progress.json", paths.root),
            "lint": {
                "status": health["status"],
                "issue_count": health["issue_count"],
                "report": relpath(paths.wiki_reports / "wiki-health.json", paths.root),
            },
            "llm_wiki_model": {
                "layout": "index + concepts/entities/sources + audit/log/queries",
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
            "contract": health["contract"],
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
                "progress": relpath(paths.wiki / "progress.json", paths.root),
                "lint_report": relpath(paths.wiki_reports / "wiki-health.json", paths.root),
                "health_status": health["status"],
                "health_issue_count": health["issue_count"],
            },
        )
        update_wiki_progress(
            paths,
            progress,
            "complete",
            "success",
            report=relpath(paths.wiki_reports / "wiki-report.json", paths.root),
            lint_status=health["status"],
            lint_issue_count=health["issue_count"],
        )
        return report
    except Exception as exc:
        update_wiki_progress(paths, progress, progress.get("current_phase", "unknown"), "failed", error=str(exc))
        raise


def build_link_map(paths: ProjectPaths, markdown_files: list[Path]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for md in markdown_files:
        rel_html = md.relative_to(paths.wiki_src).with_suffix(".html").as_posix()
        raw = md.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(raw)
        title = str(frontmatter.get("title") or extract_title(body) or md.stem)
        aliases = {
            md.stem.lower(),
            slugify(md.stem),
            title.lower(),
            slugify(title),
            *frontmatter_aliases(frontmatter),
        }
        if md.name == "index.md" and md.parent != paths.wiki_src:
            aliases.add(md.parent.name.lower())
            aliases.add(slugify(md.parent.name))
        for alias in aliases:
            mapping[alias] = rel_html
    return mapping


def build_page_index(paths: ProjectPaths, markdown_files: list[Path]) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for md in markdown_files:
        rel_html = md.relative_to(paths.wiki_src).with_suffix(".html").as_posix()
        raw = md.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(raw)
        title = str(frontmatter.get("title") or extract_title(body) or md.stem)
        page_type = str(frontmatter.get("type") or infer_page_type(md, paths.wiki_src))
        meta = {"title": title, "type": page_type, "path": rel_html, "summary": extract_page_summary(body)}
        aliases = {
            md.stem.lower(),
            slugify(md.stem),
            title.lower(),
            slugify(title),
            *frontmatter_aliases(frontmatter),
        }
        if md.name == "index.md" and md.parent != paths.wiki_src:
            aliases.add(md.parent.name.lower())
            aliases.add(slugify(md.parent.name))
        for alias in aliases:
            index[alias] = meta
    return index


def build_backlink_index(paths: ProjectPaths, markdown_files: list[Path], page_index: dict[str, dict[str, str]]) -> dict[str, dict[str, dict[str, dict]]]:
    backlink_index: dict[str, dict[str, dict[str, dict]]] = {}
    allowed_types = {"concept", "entity"}
    for md in markdown_files:
        rel_html = md.relative_to(paths.wiki_src).with_suffix(".html").as_posix()
        raw = md.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(raw)
        source_type = str(frontmatter.get("type") or infer_page_type(md, paths.wiki_src))
        if source_type not in allowed_types:
            continue
        title = str(frontmatter.get("title") or extract_title(body) or md.stem)
        source_meta = {"title": title, "type": source_type, "path": rel_html, "summary": extract_page_summary(body)}
        for link in extract_wikilinks(body):
            target_meta = page_index.get(link.lower()) or page_index.get(slugify(link))
            if not target_meta or target_meta.get("type") not in allowed_types:
                continue
            target_path = target_meta["path"]
            if target_path == rel_html:
                continue
            groups = backlink_index.setdefault(target_path, {"concept": {}, "entity": {}})
            groups[source_type][rel_html] = {"meta": source_meta, "excerpts": []}
    return backlink_index


def render_page(
    paths: ProjectPaths,
    config: EvoConfig,
    md_path: Path,
    link_map: dict[str, str],
    page_index: dict[str, dict[str, str]],
    backlink_index: dict[str, dict[str, dict[str, dict]]],
    registry: dict[str, object],
) -> WikiPage:
    raw = md_path.read_text(encoding="utf-8")
    frontmatter, markdown = split_frontmatter(raw)
    title = str(frontmatter.get("title") or extract_title(markdown) or md_path.stem.replace("-", " ").title())
    page_type = str(frontmatter.get("type") or infer_page_type(md_path, paths.wiki_src))
    if page_type == "source":
        markdown = polish_source_markdown(markdown)
    rel = md_path.relative_to(paths.wiki_src).with_suffix(".html")
    current = rel.as_posix()
    resolver = make_link_resolver(current=current, link_map=link_map)
    body = markdown_to_html(markdown, resolver=resolver)
    page_sources = parse_sources(frontmatter, markdown)
    if page_type in {"concept", "entity"}:
        body += source_basis_panel(page_sources, current, registry)
    links = sorted(set(extract_wikilinks(markdown)))
    out = paths.wiki_dist / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    nav = build_nav(paths, current=current)
    if page_type == "source":
        aside = source_related_panel(markdown, links, current, page_index)
    elif page_type == "entity":
        graph_label = str(frontmatter.get("graph_label") or title).strip()
        aside = graph_hub_panel(graph_label) + backlink_related_panel(current, backlink_index)
    elif page_type == "concept":
        aside = backlink_related_panel(current, backlink_index)
    else:
        aside = ""
    html_doc = page_template(config, title, nav, body, current=current, page_type=page_type, aside=aside)
    out.write_text(html_doc, encoding="utf-8")
    text = strip_markdown(markdown)
    return WikiPage(
        source=md_path,
        output=out,
        title=title,
        text=text,
        links=links,
        page_type=page_type,
        sources=page_sources,
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


def frontmatter_alias_values(frontmatter: dict[str, object]) -> list[str]:
    raw = frontmatter.get("aliases")
    values: list[str] = []
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw]
    elif isinstance(raw, str) and raw.strip() not in {"", "[]"}:
        text = raw.strip()
        if text.startswith("[") and text.endswith("]"):
            values = [
                item.strip().strip("\"'")
                for item in text[1:-1].split(",")
            ]
        else:
            values = [text]
    return sorted({value for value in values if value})


def frontmatter_aliases(frontmatter: dict[str, object]) -> set[str]:
    return {
        alias
        for value in frontmatter_alias_values(frontmatter)
        if value
        for alias in (value.lower(), slugify(value))
        if alias
    }


def public_source_basename(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.replace("\\", "/"))
    return PurePosixPath(normalized).name


def build_wiki_registry(
    paths: ProjectPaths,
    markdown_files: list[Path],
) -> dict[str, object]:
    """Build a public entity/source lookup without workspace paths."""
    entities: list[dict[str, object]] = []
    sources: dict[str, dict[str, object]] = {}
    graph_labels: dict[str, str] = {}
    source_graph_labels: dict[str, set[str]] = {}
    for md in markdown_files:
        raw = md.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(raw)
        title = str(
            frontmatter.get("title")
            or extract_title(body)
            or md.stem
        ).strip()
        page_type = str(
            frontmatter.get("type")
            or infer_page_type(md, paths.wiki_src)
        )
        wiki_path = (
            md.relative_to(paths.wiki_src)
            .with_suffix(".html")
            .as_posix()
        )
        if page_type == "entity":
            graph_label = str(
                frontmatter.get("graph_label") or title
            ).strip()
            if not graph_label:
                raise StateError(
                    "entity graph_label must not be empty",
                    error_code="WIKI_REGISTRY_MAPPING_INVALID",
                )
            normalized_label = unicodedata.normalize(
                "NFKC",
                graph_label,
            ).casefold()
            previous = graph_labels.get(normalized_label)
            if previous is not None and previous != wiki_path:
                raise StateError(
                    "duplicate entity graph_label prevents a unique mapping",
                    error_code="WIKI_REGISTRY_MAPPING_INVALID",
                )
            graph_labels[normalized_label] = wiki_path
            explicit_aliases = frontmatter_alias_values(frontmatter)
            for source in parse_sources(frontmatter, body):
                basename = public_source_basename(source)
                if basename:
                    source_graph_labels.setdefault(
                        basename.casefold(),
                        set(),
                    ).add(graph_label)
            entities.append(
                {
                    "title": title,
                    "graph_label": graph_label,
                    "aliases": explicit_aliases,
                    "wiki_path": wiki_path,
                }
            )
        if page_type == "source":
            for source in parse_sources(frontmatter, body):
                basename = public_source_basename(source)
                if not basename:
                    continue
                previous = sources.get(basename.casefold())
                if previous is not None and previous["wiki_path"] != wiki_path:
                    raise StateError(
                        "source basename maps to more than one Wiki source page",
                        error_code="WIKI_REGISTRY_MAPPING_INVALID",
                    )
                sources[basename.casefold()] = {
                    "basename": basename,
                    "title": title,
                    "wiki_path": wiki_path,
                }
    for key, source in sources.items():
        source["graph_labels"] = sorted(
            source_graph_labels.get(key, set()),
            key=lambda value: (
                unicodedata.normalize("NFKC", value).casefold(),
                value,
            ),
        )
    return {
        "schema_version": 1,
        "entities": sorted(
            entities,
            key=lambda item: (
                str(item["graph_label"]).casefold(),
                str(item["wiki_path"]),
            ),
        ),
        "sources": {
            key: sources[key]
            for key in sorted(sources)
        },
    }


def infer_page_type(path: Path, wiki_src: Path) -> str:
    try:
        rel = path.relative_to(wiki_src)
    except ValueError:
        return "page"
    if rel.parts and rel.parts[0] in {"concepts", "entities", "sources"}:
        return {"concepts": "concept", "entities": "entity", "sources": "source"}[rel.parts[0]]
    if rel.name == "index.md":
        return "index"
    return "page"


def extract_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def extract_page_summary(markdown: str, *, max_chars: int = 140) -> str:
    summary = extract_named_section(markdown, {"摘要", "summary"}) or first_content_paragraph(markdown)
    summary = clean_summary_text(strip_markdown(_wikilink_plain(summary)))
    return truncate_text(summary, max_chars)


def clean_summary_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"([\u4e00-\u9fff])\s+([\u4e00-\u9fff])", r"\1\2", text)
    text = re.sub(r"([\u4e00-\u9fff])\s+(?=[，。！？；：、])", r"\1", text)
    text = re.sub(r"([（《「『【])\s+", r"\1", text)
    text = re.sub(r"\s+([）》」』】])", r"\1", text)
    return text


def extract_named_section(markdown: str, names: set[str]) -> str | None:
    lines = markdown.splitlines()
    start: int | None = None
    for idx, line in enumerate(lines):
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match and match.group(1).strip().lower() in names:
            start = idx + 1
            break
    if start is None:
        return None
    collected: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        collected.append(line)
    text = "\n".join(collected).strip()
    return text or None


def first_content_paragraph(markdown: str) -> str:
    paragraph: list[str] = []
    in_code = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line or line.startswith("---"):
            if paragraph:
                break
            continue
        if line.startswith("#") or line.startswith("|") or set(line) <= {"-", ":", "|", " "}:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        paragraph.append(line)
    return " ".join(paragraph)


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


def polish_source_markdown(markdown: str) -> str:
    """Conservatively clean source-page formatting before rendering.

    只处理显示层面的空白小毛病：尾随空格、全角空格、连续空格、过多空行，以及中文与
    wikilink/标点之间的多余空格；不改写语义内容，也不回写 Markdown 源文件。
    """
    polished_lines: list[str] = []
    in_code = False
    blank_count = 0
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip().replace("　", " ")
        if line.strip().startswith("```"):
            in_code = not in_code
            polished_lines.append(line)
            blank_count = 0
            continue
        if in_code:
            polished_lines.append(line)
            continue
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        line = re.sub(r"([\u4e00-\u9fff])\s+([\u4e00-\u9fff])", r"\1\2", line)
        line = re.sub(r"([\u4e00-\u9fff])\s+(?=[，。！？；：、])", r"\1", line)
        line = re.sub(r"([（《「『【])\s+", r"\1", line)
        line = re.sub(r"\s+([）》」』】])", r"\1", line)
        line = re.sub(r"([\u4e00-\u9fff])\s+(\[\[)", r"\1\2", line)
        line = re.sub(r"(\]\])\s+([\u4e00-\u9fff])", r"\1\2", line)
        line = re.sub(r"(\]\])\s+(?=[，。！？；：、])", r"\1", line)
        if re.fullmatch(r"[一二三四五六七八九十百]+、.{1,60}", line):
            line = "### " + line
        elif re.fullmatch(
            r"[（(][一二三四五六七八九十百]+[）)].{1,60}",
            line,
        ):
            line = "#### " + line
        if not line:
            blank_count += 1
            if blank_count <= 1:
                polished_lines.append("")
            continue
        blank_count = 0
        polished_lines.append(line)
    return "\n".join(polished_lines).strip() + "\n"


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
    if href == "#self":
        return (
            '<span class="wikilink self">'
            f"{html.escape(label or target)}</span>"
        )
    css = "wikilink" if href != "#missing" else "wikilink missing"
    return f'<a class="{css}" href="{html.escape(href)}">{html.escape(label or target)}</a>'


def make_link_resolver(*, current: str, link_map: dict[str, str]) -> Callable[[str], str]:
    def resolve(target: str) -> str:
        key = target.lower()
        rel = link_map.get(key) or link_map.get(slugify(target))
        if not rel:
            return "#missing"
        if rel == current:
            return "#self"
        return relpath_from(current, rel)

    return resolve


_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_SENTENCE_ENDERS = "。！？!?；;\n"
_MAX_EXCERPTS_PER_LINK = 2
_RELATED_SUMMARY_PREVIEW_CHARS = 96


def _original_section(markdown: str) -> str:
    """Return the source page's original-content section, falling back to full text."""
    heading = re.search(r"^##\s*(原文内容|Original.*)\s*$", markdown, flags=re.M)
    if not heading:
        return markdown
    start = heading.end()
    nxt = re.search(r"^##\s", markdown[start:], flags=re.M)
    end = start + nxt.start() if nxt else len(markdown)
    return markdown[start:end]


def _sentence_around(text: str, start: int, end: int) -> str:
    left = start
    while left > 0 and text[left - 1] not in _SENTENCE_ENDERS:
        left -= 1
    right = end
    while right < len(text) and text[right] not in _SENTENCE_ENDERS:
        right += 1
    if right < len(text):
        right += 1
    return text[left:right].strip()


def _wikilink_plain(text: str) -> str:
    return _WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)


def _excerpt_html(snippet: str, label: str) -> str:
    plain = _wikilink_plain(snippet)
    escaped = html.escape(plain)
    esc_label = html.escape(label)
    if esc_label:
        escaped = re.sub(
            re.escape(esc_label),
            f'<mark class="related-hit">{esc_label}</mark>',
            escaped,
        )
    return escaped


def source_related_panel(markdown: str, links: list[str], current: str, page_index: dict[str, dict[str, str]]) -> str:
    original = _original_section(markdown)
    # 1. 按概念/实体分桶，记录每个被链接页面在原文中的出现上下文。
    groups: dict[str, dict[str, dict]] = {"concept": {}, "entity": {}}
    for match in _WIKILINK_RE.finditer(original):
        target = match.group(1).strip()
        label = (match.group(2) or target).strip()
        meta = page_index.get(target.lower()) or page_index.get(slugify(target))
        if not meta or meta.get("type") not in groups:
            continue
        bucket = groups[meta["type"]]
        entry = bucket.setdefault(meta["path"], {"meta": meta, "excerpts": []})
        sentence = _sentence_around(original, match.start(), match.end())
        if sentence:
            entry["excerpts"].append(_excerpt_html(sentence, label))

    # 2. 兜底：frontmatter/正文里声明了链接但原文未命中，也列出（无上下文摘录）。
    for link in links:
        meta = page_index.get(link.lower()) or page_index.get(slugify(link))
        if not meta or meta.get("type") not in groups:
            continue
        groups[meta["type"]].setdefault(meta["path"], {"meta": meta, "excerpts": []})

    return related_panel_html(groups, current, link_label="查看原文 →", show_mentions=True)


def backlink_related_panel(current: str, backlink_index: dict[str, dict[str, dict[str, dict]]]) -> str:
    groups = backlink_index.get(current)
    if not groups:
        return ""
    return related_panel_html(groups, current, link_label="查看页面 →", show_mentions=False)


def related_panel_html(groups: dict[str, dict[str, dict]], current: str, *, link_label: str, show_mentions: bool) -> str:
    if not groups.get("concept") and not groups.get("entity"):
        return ""

    labels = {"concept": "概念", "entity": "实体"}
    total = len(groups.get("concept", {})) + len(groups.get("entity", {}))
    sections = []
    for group in ["concept", "entity"]:
        entries = groups.get(group, {})
        if not entries:
            continue
        items = []
        for entry in sorted(entries.values(), key=lambda item: item["meta"]["title"]):
            meta = entry["meta"]
            href = relpath_from(current, meta["path"])
            excerpts = entry.get("excerpts", [])
            mentions = len(excerpts)
            count_label = f'<span class="related-mentions">{mentions} 处</span>' if show_mentions and mentions else ""
            summary = truncate_text(str(meta.get("summary") or "").strip(), _RELATED_SUMMARY_PREVIEW_CHARS)
            summary_preview = f'<span class="related-summary-preview">{html.escape(summary)}</span>' if summary else ""
            body_parts = []
            body_parts.extend(
                f'<p class="related-excerpt">{exc}</p>'
                for exc in excerpts[:_MAX_EXCERPTS_PER_LINK]
            )
            if show_mentions and mentions > _MAX_EXCERPTS_PER_LINK:
                body_parts.append(
                    f'<p class="related-more">…还有 {mentions - _MAX_EXCERPTS_PER_LINK} 处提及</p>'
                )
            body_parts.append(
                f'<a class="related-original-link" href="{html.escape(href)}">{html.escape(link_label)}</a>'
            )
            items.append(
                f'<details class="related-item related-{group}">'
                f'<summary class="related-item-head"><span class="related-item-main"><span class="related-item-title">{html.escape(meta["title"])}</span>{summary_preview}</span>{count_label}</summary>'
                f'<div class="related-item-body">{"".join(body_parts)}</div>'
                "</details>"
            )
        sections.append(
            f'<details class="related-section"><summary>{labels[group]} <span class="related-count">{len(entries)}</span></summary>'
            f'<div class="related-items">{"".join(items)}</div></details>'
        )

    return (
        '<aside class="page-aside"><div class="related-panel">'
        f'<div class="related-title"><span>链接到本页 <span class="related-total">{total}</span></span>'
        '<span class="related-actions"><button type="button" data-related-expand>展开</button><button type="button" data-related-collapse>折叠</button></span></div>'
        + "".join(sections)
        + "</div></aside>"
    )


def graph_hub_panel(slug: str) -> str:
    """Entity page aside: a link into the SPA entity hub (/app#entity/<slug>).

    Closes the loop wiki → graph: from an entity's wiki page the reader can
    jump to its graph neighborhood + preset Q&A in the SPA. Site-root-absolute
    href so it works at any page depth.
    """
    href = f"/app#entity/{html.escape(slug)}"
    return (
        '<aside class="page-aside"><div class="related-panel">'
        '<div class="related-title"><span>知识图谱</span></div>'
        f'<div class="related-items"><p class="related-excerpt">查看该实体在图谱中的邻域，并直接发起提问。</p>'
        f'<a class="related-original-link" href="{href}">查看图谱邻域 →</a></div>'
        "</div></aside>"
    )


def source_basis_panel(
    page_sources: list[str],
    current: str,
    registry: dict[str, object],
) -> str:
    if not page_sources:
        return ""
    source_map = registry.get("sources")
    if not isinstance(source_map, dict):
        source_map = {}
    items: list[str] = []
    for source in page_sources:
        basename = public_source_basename(source)
        mapped = source_map.get(basename.casefold())
        if isinstance(mapped, dict) and isinstance(
            mapped.get("wiki_path"),
            str,
        ):
            href = relpath_from(current, str(mapped["wiki_path"]))
            title = str(mapped.get("title") or basename)
            items.append(
                '<li><a class="provenance-link" '
                f'href="{html.escape(href)}">{html.escape(title)}</a>'
                f'<span>{html.escape(basename)}</span></li>'
            )
        else:
            items.append(
                f"<li><span>{html.escape(basename)}</span>"
                '<small>尚未建立来源页</small></li>'
            )
    return (
        '<section class="source-basis" aria-labelledby="source-basis-title">'
        '<h2 id="source-basis-title">来源依据</h2><ul>'
        + "".join(items)
        + "</ul></section>"
    )



def build_nav(paths: ProjectPaths, *, current: str) -> str:
    items = []
    grouped = {"index": [], "concepts": [], "entities": [], "sources": [], "other": []}
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
    labels = {"index": "入口", "concepts": "概念", "entities": "实体", "sources": "原文", "other": "其他"}
    for group in ["index", "concepts", "entities", "sources", "other"]:
        if grouped[group]:
            if group == "index":
                items.append("".join(grouped[group]))
            else:
                count = len(grouped[group])
                items.append(
                    f'<details class="nav-group"><summary class="nav-group-title"><span>{labels[group]}</span><span class="nav-count">{count}</span></summary>'
                    + '<div class="nav-group-links">'
                    + "".join(grouped[group])
                    + "</div></details>"
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
    "source": "原文",
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


def page_template(config: EvoConfig, title: str, nav: str, body: str, *, current: str, page_type: str, aside: str = "") -> str:
    site_title = html.escape(str(config.wiki.get("title", "Evo Wiki")))
    prefix = asset_prefix(current)
    home_href = f"{prefix}index.html"
    meta = type_badge(page_type)
    brand = config.wiki.get("brand") or {}
    navigation = config.wiki.get("navigation") or {}
    logo_path = brand.get("logo_path")
    logo_public = (
        "assets/shared/brand-logo" + Path(str(logo_path)).suffix.lower()
        if logo_path
        else None
    )
    logo_url = f"{prefix}{logo_public}" if logo_public else ""
    logo_markup = (
        f'<img class="brand-logo" src="{html.escape(logo_url)}" alt="">'
        if logo_url
        else '<span class="logo-icon">📚</span>'
    )
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
  <script defer src="{prefix}assets/shared/nav.js"></script>
  <script defer src="{prefix}assets/app.js"></script>
</head>
<body data-page-type="{html.escape(page_type)}" data-site-title="{site_title}"
  data-logo-url="{html.escape(logo_url)}"
  data-nav-wiki="{str(bool(navigation.get("wiki", True))).lower()}"
  data-nav-qa="{str(bool(navigation.get("qa", True))).lower()}"
  data-nav-graph="{str(bool(navigation.get("graph", True))).lower()}"
  data-nav-entity="{str(bool(navigation.get("entity_hub", True))).lower()}">
  <div id="evo-topbar"></div>
  <button id="sidebar-toggle" class="sidebar-toggle" type="button"
    aria-controls="wiki-sidebar" aria-expanded="false">目录</button>
  <div id="sidebar-overlay" class="sidebar-overlay" hidden></div>
  <aside id="wiki-sidebar" class="sidebar" aria-label="Wiki 目录">
    <div class="sidebar-header">
      <a class="logo" href="{home_href}">{logo_markup}{site_title}</a>
    </div>
    <div class="sidebar-search">
      <label for="search">搜索 Wiki</label>
      <input id="search" placeholder="标题、类型或正文…" autocomplete="off"
        role="combobox" aria-autocomplete="list" aria-controls="search-results"
        aria-expanded="false" data-search-index="{prefix}search-index.json">
      <div id="search-results" role="listbox" aria-live="polite"></div>
    </div>
    <nav class="sidebar-nav">{nav}</nav>
  </aside>
  <main id="wiki-main" class="main" tabindex="-1">
    <div class="content-shell">
      <article class="article">{meta}{body}</article>
      {aside}
    </div>
  </main>
</body>
</html>
"""


def write_assets(paths: ProjectPaths, config: EvoConfig) -> None:
    (paths.wiki_dist / "assets" / "style.css").write_text(STYLE, encoding="utf-8")
    (paths.wiki_dist / "assets" / "app.js").write_text(APP_JS, encoding="utf-8")
    write_shared_assets(paths, config)


def write_shared_assets(paths: ProjectPaths, config: EvoConfig) -> None:
    """Shared shell: design tokens + cross-app topbar. Single source for wiki + future SPA."""
    presentation = config.validate(paths.root)
    shared = paths.wiki_dist / "assets" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    primary_color = presentation["primary_color"]
    red, green, blue = (
        int(primary_color[1:3], 16),
        int(primary_color[3:5], 16),
        int(primary_color[5:7], 16),
    )
    theme = (
        SHARED_THEME.replace("{{primary_color}}", primary_color)
        .replace("{{primary_rgb}}", f"{red},{green},{blue}")
    )
    (shared / "theme.css").write_text(theme, encoding="utf-8")
    (shared / "nav.js").write_text(SHARED_NAV_JS, encoding="utf-8")
    logo_source = presentation["logo_source"]
    logo_public_path = presentation["logo_public_path"]
    if logo_source is not None and logo_public_path is not None:
        shutil.copy2(logo_source, paths.wiki_dist / logo_public_path)


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
            warnings.append({"code": "stub_content", "page": relpath(page.source, paths.root), "message": "Page still looks like a stub; ask Claude Code to generate complete content."})
    for issue in health.get("issues", []):
        if issue.get("severity") in {"error", "warn"}:
            warnings.append({"code": issue.get("code"), "page": issue.get("path"), "message": issue.get("message")})
    return warnings


STYLE = """
/* Evo wiki layout — white + blue, clean academic style.
   Design tokens live in shared/theme.css (single source for wiki + future SPA). */
@import url('shared/theme.css');
* { box-sizing:border-box; margin:0; padding:0; }
html { font-size:15px; scroll-behavior:smooth; }
body { font-family:var(--sans); color:var(--text); background:var(--bg); display:flex; min-height:100vh; line-height:1.8; }

/* Cross-app topbar styles are defined once in shared/theme.css (imported above). */

/* ===== SIDEBAR ===== */
.sidebar { width:var(--sidebar-w); background:var(--sidebar-bg); position:fixed; top:var(--topbar-h); left:0; bottom:0; overflow-y:auto; z-index:100; display:flex; flex-direction:column; border-right:1px solid var(--border); }
.sidebar-header { padding:20px 16px 16px; border-bottom:1px solid var(--border); }
.logo { color:var(--text); font-size:17px; font-weight:700; text-decoration:none; letter-spacing:.5px; font-family:var(--serif); display:inline-flex; align-items:center; gap:10px; }
.logo-icon { font-size:22px; line-height:1; }
.sidebar-search { padding:14px 12px 6px; }
.sidebar-search label { display:block; color:var(--text2); font-size:12px; font-weight:600; margin-bottom:5px; }
.sidebar-search input { width:100%; border:1px solid var(--border); background:var(--bg2); color:var(--text); border-radius:8px; padding:9px 12px; font-size:13px; font-family:var(--sans); }
.sidebar-search input::placeholder { color:#999; }
.sidebar-search input:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-glow); }
#search-results { margin-top:6px; }
#search-results a { display:block; color:var(--text2); text-decoration:none; padding:7px 10px; border-radius:6px; font-size:12px; }
#search-results a:hover, #search-results a.active { background:var(--accent-glow); color:var(--accent); }
.sidebar-toggle, .sidebar-overlay { display:none; }
.sidebar-nav { flex:1; padding:8px 0 24px; overflow-y:auto; text-align:left; }
.sidebar-nav::-webkit-scrollbar { width:4px; }
.sidebar-nav::-webkit-scrollbar-thumb { background:var(--border); border-radius:2px; }
.nav-link { display:block; padding:6px 16px; color:var(--text2); text-decoration:none; text-align:left; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; border-left:3px solid transparent; transition:all .15s; }
.nav-link:hover { background:var(--accent-glow); color:var(--accent); }
.nav-link.active { color:var(--accent); background:var(--accent-glow); border-left-color:var(--accent); font-weight:600; }
.nav-link.nav-home { font-size:14px; padding:10px 16px; font-weight:500; color:var(--text); }
.nav-group { margin-bottom:4px; }
.nav-group-title { list-style:none; cursor:pointer; display:flex; align-items:center; justify-content:flex-start; gap:8px; padding:10px 16px 6px; color:var(--text2); text-align:left; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.5px; }
.nav-group-title > span:first-of-type { margin-right:auto; }
.nav-group-title::-webkit-details-marker { display:none; }
.nav-group-title::before { content:'▾'; color:var(--accent-light); margin-right:6px; transition:transform .16s ease; }
.nav-group:not([open]) .nav-group-title::before { transform:rotate(-90deg); }
.nav-count { min-width:22px; padding:1px 7px; border-radius:999px; background:var(--bg2); color:var(--text2); font-size:11px; text-align:center; }
.nav-group-links { padding-bottom:4px; }

/* ===== MAIN / ARTICLE ===== */
.main { margin-left:max(var(--sidebar-w), calc((100vw - 1160px) / 2)); flex:1; position:relative; max-width:1160px; padding-top:var(--topbar-h); }
.content-shell { display:grid; grid-template-columns:minmax(0, 820px) 260px; align-items:start; gap:28px; }
.article { max-width:820px; padding:48px 48px 80px; }
.page-aside { padding:48px 24px 80px 0; }
.related-panel { position:sticky; top:28px; padding:4px 0; }
.related-title { display:flex; justify-content:space-between; align-items:center; gap:10px; color:var(--text2); font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.08em; padding-bottom:8px; border-bottom:1px solid var(--border); margin-bottom:6px; }
.related-actions { display:inline-flex; gap:10px; font-family:var(--sans); }
.related-actions button { border:none; background:none; color:var(--text2); padding:0; font-size:11px; cursor:pointer; opacity:.7; }
.related-actions button:hover { color:var(--accent); opacity:1; }
.related-total, .related-count { color:var(--text2); font-family:var(--sans); font-size:11px; font-weight:600; opacity:.65; }
.related-section { padding-top:8px; margin-top:8px; }
.related-section > summary { cursor:pointer; list-style:none; display:flex; align-items:center; gap:6px; color:var(--text2); font-size:11px; font-weight:600; letter-spacing:.04em; }
.related-section > summary::-webkit-details-marker { display:none; }
.related-section > summary::before { content:'▾'; color:var(--text2); font-size:9px; opacity:.6; transition:transform .16s ease; }
.related-section:not([open]) > summary::before { transform:rotate(-90deg); }
.related-section .related-count { margin-left:auto; }
.related-items { display:flex; flex-direction:column; margin-top:4px; }
.related-item { border:none; background:none; }
.related-item > summary { cursor:pointer; list-style:none; display:flex; align-items:flex-start; gap:8px; padding:7px 0 7px 14px; }
.related-item > summary::-webkit-details-marker { display:none; }
.related-item > summary::before { content:'▸'; color:var(--text2); opacity:.5; font-size:9px; line-height:1.8; transition:transform .16s ease; }
.related-item[open] > summary::before { color:var(--accent); opacity:1; transform:rotate(90deg); }
.related-item-main { display:flex; flex-direction:column; gap:3px; min-width:0; flex:1; }
.related-item-title { color:var(--link); font-size:13px; font-weight:500; line-height:1.45; }
.related-summary-preview { color:var(--text2); font-size:12px; line-height:1.55; font-weight:400; opacity:.9; display:block; }
.related-item:hover .related-item-title { color:var(--accent); }
.related-mentions { margin-left:auto; white-space:nowrap; font-size:11px; line-height:1.8; color:var(--text2); opacity:.55; }
.related-item-body { padding:0 0 8px 28px; }
.related-summary { margin:6px 0 7px; padding:7px 9px; border-left:2px solid var(--accent); border-radius:0 8px 8px 0; background:var(--accent-glow); font-size:12px; line-height:1.55; color:var(--text); }
.related-excerpt { margin:6px 0 0; font-size:12px; line-height:1.6; color:var(--text2); }
.related-hit { background:none; color:var(--text); border-bottom:1.5px solid var(--accent); padding:0; font-weight:600; }
.related-more { margin:5px 0 0; font-size:11px; color:var(--text2); opacity:.55; }
.related-original-link { display:inline-flex; margin-top:8px; color:var(--accent); text-decoration:none; font-size:12px; font-weight:500; opacity:.85; }
.related-original-link:hover { text-decoration:underline; opacity:1; }
.meta { font-size:13px; color:var(--text2); margin-bottom:16px; display:flex; align-items:center; gap:8px; }
.type-badge { font-size:11px; padding:2px 9px; border-radius:4px; color:#fff; font-weight:600; letter-spacing:.5px; }
.type-concept { background:#2563EB; }
.type-entity  { background:#0891B2; }
.type-source  { background:var(--accent-strong); }
.type-index   { background:#6B7280; }
.type-page    { background:#4B5563; }

.article h1 { font-family:var(--serif); font-size:30px; line-height:1.3; margin-bottom:24px; font-weight:900; color:var(--heading); letter-spacing:-.5px; }
.article h2 { font-family:var(--serif); font-size:21px; margin:36px 0 14px; padding-bottom:8px; border-bottom:2px solid var(--border); font-weight:700; color:var(--heading); }
.article h3 { font-family:var(--serif); font-size:17px; margin:24px 0 10px; font-weight:600; color:var(--heading2); }
.article h4 { font-family:var(--serif); font-size:15px; margin:18px 0 8px; font-weight:600; color:var(--heading2); }
.article p { margin:10px 0; }
.article ul, .article ol { padding-left:24px; margin:10px 0; }
.article li { margin:4px 0; }
.article a { color:var(--link); text-decoration:none; border-bottom:1px solid var(--accent-border); }
.article a:hover { border-bottom-color:var(--accent); }

.article a.wikilink { color:var(--link); text-decoration:none; border-bottom:none; background:linear-gradient(to bottom,transparent 60%,var(--accent-glow) 60%); transition:background .2s; font-weight:500; }
.article a.wikilink:hover { background:linear-gradient(to bottom,transparent 40%,rgba(37,99,235,.18) 40%); }
.article a.wikilink.missing { color:#aaa; text-decoration:line-through; background:none; }
.article .wikilink.self { color:var(--text); font-weight:600; background:var(--bg2); border-radius:4px; padding:1px 4px; }
.source-basis { margin:34px 0 8px; padding:16px 18px; border:1px solid var(--accent-border); border-radius:10px; background:var(--accent-glow); }
.source-basis h2 { margin:0 0 10px; padding:0; border:0; font-family:var(--sans); font-size:14px; }
.source-basis ul { margin:0; padding-left:20px; }
.source-basis li { margin:6px 0; }
.source-basis li span, .source-basis li small { display:block; color:var(--text2); font-size:12px; overflow-wrap:anywhere; }

.article blockquote { background:var(--cream); border-left:4px solid var(--accent); padding:14px 20px; margin:16px 0; border-radius:0 8px 8px 0; font-style:italic; color:#1E3A5F; font-family:var(--serif); }
.article code { background:var(--bg2); color:#2563EB; border-radius:5px; padding:2px 6px; font-size:.9em; }
.article pre { background:#1E293B; color:#e6e1d6; padding:18px 20px; border-radius:12px; overflow:auto; margin:16px 0; border:1px solid rgba(0,0,0,.15); }
.article pre code { background:none; color:inherit; padding:0; }
.article table { border-collapse:collapse; width:100%; margin:16px 0; font-size:14px; }
.article th, .article td { border:1px solid var(--border); padding:8px 12px; text-align:left; }
.article th { background:var(--bg2); font-weight:600; color:var(--heading); }
.article hr { border:none; border-top:1px solid var(--border); margin:32px 0; }

.mermaid { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; margin:18px 0; }
.math-block { overflow:auto; padding:12px 0; }

@media (max-width: 980px) {
  body { display:block; padding-top:var(--topbar-h); }
  .sidebar { position:fixed; width:min(86vw, 320px); top:0; bottom:0; z-index:310; transform:translateX(-105%); transition:transform .2s ease; box-shadow:0 18px 50px rgba(15,23,42,.22); }
  .sidebar.open { transform:translateX(0); }
  .sidebar-toggle { display:inline-flex; position:fixed; top:7px; left:12px; z-index:260; align-items:center; justify-content:center; height:34px; padding:0 11px; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--text); font:600 13px var(--sans); cursor:pointer; }
  .sidebar-overlay { display:block; position:fixed; inset:0; z-index:300; background:rgba(15,23,42,.35); }
  .sidebar-overlay[hidden] { display:none; }
  body.sidebar-open { overflow:hidden; }
  .main { margin-left:0; max-width:100%; padding-top:0; }
  .content-shell { display:block; }
  .article { padding:28px 22px 36px; }
  .page-aside { padding:0 22px 48px; }
  .related-panel { position:static; }
  .article h1 { font-size:24px; }
}
"""

APP_JS = """
async function initSearch(){
  const input = document.getElementById('search');
  const box = document.getElementById('search-results');
  if(!input || !box) return;
  let data=[];
  let selected=-1;
  const typeNames={concept:'概念',entity:'实体',source:'原文',index:'索引',page:'页面'};
  const indexPath = input.dataset.searchIndex || 'search-index.json';
  const base = indexPath.replace(/search-index\\.json$/, '');
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
"""


# ===== Shared shell: design tokens + cross-app topbar =====
# Single source of truth for theme + navigation. render-wiki (wiki pages) and
# the future SPA both reference assets/shared/{theme.css,nav.js}, so the two
# surfaces never drift. See design_update/2026-07-14.html §3 (共享壳).

SHARED_THEME = """
/* Evo wiki shared design tokens + shared components — single source for wiki + SPA. */
:root {
  color-scheme: light;
  --bg:#FFFFFF; --bg2:#F5F6F8; --text:#111111; --text2:#555555;
  --heading:#111111; --heading2:#1E293B;
  --accent:{{primary_color}}; --accent-light:{{primary_color}}; --accent-strong:{{primary_color}};
  --accent-glow:rgba({{primary_rgb}},.10); --accent-border:rgba({{primary_rgb}},.25);
  --danger:#C92A3A;
  --sidebar-bg:#FFFFFF; --cream:#F0F4FF;
  --border:#E5E7EB; --card:#FFFFFF; --link:#2563EB;
  --serif:'Noto Serif SC','Crimson Pro',Georgia,'Times New Roman',serif;
  --sans:'DM Sans',-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
  --sidebar-w:260px;
  --topbar-h:48px;
}

/* Cross-app topbar (shared shell): identical on Wiki + SPA, defined once here so
   the two surfaces can never drift. Both style.css and app.css inherit it. */
#evo-topbar { position:fixed; top:0; left:0; right:0; height:var(--topbar-h); z-index:200; background:var(--bg); border-bottom:1px solid var(--border); }
.evo-topbar-inner { max-width:1160px; margin:0 auto; height:100%; display:flex; align-items:center; justify-content:space-between; padding:0 20px; }
.evo-topbar-brand { color:var(--text); text-decoration:none; font-family:var(--serif); font-size:15px; font-weight:700; }
.brand-logo { width:24px; height:24px; object-fit:contain; display:inline-block; }
.evo-topbar-brand .brand-logo { width:22px; height:22px; margin-right:8px; vertical-align:middle; }
.evo-topbar-nav { display:flex; gap:4px; }
.evo-topbar-link { color:var(--text2); text-decoration:none; font-size:13px; font-weight:500; padding:7px 14px; border-radius:8px; transition:all .15s; }
.evo-topbar-link:hover { background:var(--accent-glow); color:var(--accent); }
.evo-topbar-link.active { color:var(--accent); background:var(--accent-glow); font-weight:600; }
@media (max-width: 980px) {
  .evo-topbar-inner { justify-content:flex-end; padding:0 8px 0 72px; }
  .evo-topbar-brand { display:none; }
  .evo-topbar-nav { gap:2px; }
  .evo-topbar-link { padding:7px 11px; }
}
"""


SHARED_NAV_JS = """
/* Cross-app topbar: [Wiki | 问答 | 图谱]. Renders into #evo-topbar, highlights
   the current surface by URL. Links point at / (Wiki) and /app (问答/图谱). The
   brand name is read from <body data-site-title> so it matches the sidebar logo;
   both wiki pages and the SPA load this single file. */
(function () {
  var mount = document.getElementById('evo-topbar');
  if (!mount) return;
  var siteTitle = (document.body && document.body.dataset && document.body.dataset.siteTitle) || 'Evo Wiki';
  var data = (document.body && document.body.dataset) || {};
  function enabled(name, fallback) {
    var value = data[name];
    return value == null ? fallback : value === 'true';
  }
  var onApp = location.pathname.indexOf('/app') === 0;
  var hash = location.hash || '';
  var onGraph = onApp && (hash.indexOf('#graph') === 0 || hash.indexOf('#entity/') === 0);
  var tabs = [];
  if (enabled('navWiki', true)) tabs.push({ key: 'wiki', label: 'Wiki', href: '/', active: !onApp });
  if (enabled('navQa', true)) tabs.push({ key: 'qa', label: '问答', href: '/app', active: onApp && !onGraph });
  if (enabled('navGraph', true)) tabs.push({ key: 'graph', label: '图谱', href: '/app#graph', active: onGraph });
  var links = tabs.map(function (t) {
    var cls = 'evo-topbar-link' + (t.active ? ' active' : '');
    return '<a class="' + cls + '" href="' + t.href + '">' + t.label + '</a>';
  }).join('');
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]; }); }
  var logo = data.logoUrl ? '<img class="brand-logo" src="' + esc(data.logoUrl) + '" alt="">' : '';
  mount.innerHTML = '<div class="evo-topbar-inner"><a class="evo-topbar-brand" href="/">' + logo + esc(siteTitle) + '</a><nav class="evo-topbar-nav">' + links + '</nav></div>';
})();
"""

"""Unit tests for the design-review fixes (H1, H2, M1-M4, L1-L4)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from evo_wiki.config import EvoConfig, deep_merge
from evo_wiki.corpus import (
    CorpusFile,
    diff_against_previous,
    persist_corpus_state,
    scan_corpus,
)
from evo_wiki.docker_export import export_docker
from evo_wiki.cli import lane_state_path, merge_change_sets
from evo_wiki.lightrag_lane import build_lightrag
from evo_wiki.paths import ProjectPaths
from evo_wiki.utils import write_json
from evo_wiki.wiki import markdown_to_html, parse_sources, render_wiki
from evo_wiki.wiki_health import lint_wiki_artifacts, parse_yaml_frontmatter


def make_file(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --- corpus diff (supports H1/H2) -------------------------------------------

def test_corpus_diff_add_modify_delete(tmp_path: Path):
    corpus = tmp_path / "corpus"
    make_file(tmp_path, "corpus/raw/a.md", "alpha")
    make_file(tmp_path, "corpus/raw/b.md", "beta")
    state = tmp_path / "state.json"
    persist_corpus_state(scan_corpus(tmp_path, corpus), state)

    # modify a, delete b, add c
    make_file(tmp_path, "corpus/raw/a.md", "alpha-2")
    (corpus / "raw" / "b.md").unlink()
    make_file(tmp_path, "corpus/raw/c.md", "gamma")

    change = diff_against_previous(scan_corpus(tmp_path, corpus), state)
    assert change["added"] == ["corpus/raw/c.md"]
    assert change["modified"] == ["corpus/raw/a.md"]
    assert change["deleted"] == ["corpus/raw/b.md"]


# --- H1: deletion forces requires_rebuild -----------------------------------

def test_lightrag_dry_run_deletion_requires_rebuild(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    # ledger records a doc that is no longer present in the current input
    write_json(
        paths.lightrag_state / "lightrag-import-ledger.json",
        {"documents": {"old-id": {"source_path": "corpus/raw/gone.md", "sha256": "sha256:old"}}},
    )
    write_json(paths.lightrag_input / "manifest.json", {"status": "prepared", "document_count": 1})
    doc = {"id": "kept-id", "source_path": "corpus/raw/kept.md", "sha256": "sha256:new", "text": "kept text"}
    (paths.lightrag_input / "documents.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")

    report = build_lightrag(paths, dry_run=True)
    assert report["status"] == "dry_run"
    assert report["requires_rebuild"] is True
    assert report["deleted_pending_rebuild"] == ["corpus/raw/gone.md"]


def test_lightrag_dry_run_no_deletion_no_rebuild(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    write_json(paths.lightrag_input / "manifest.json", {"status": "prepared", "document_count": 1})
    doc = {"id": "kept-id", "source_path": "corpus/raw/kept.md", "sha256": "sha256:new", "text": "kept text"}
    (paths.lightrag_input / "documents.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")

    report = build_lightrag(paths, dry_run=True)
    assert report["requires_rebuild"] is False
    assert report["deleted_pending_rebuild"] == []


def test_lightrag_build_submits_to_existing_service(tmp_path: Path):
    requests: list[tuple[str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            requests.append((self.path, payload))
            if self.path == "/documents/text":
                body = {"status": "success", "message": "accepted", "track_id": "insert-1"}
            elif self.path == "/query":
                body = {"response": "smoke answer", "references": []}
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002 - stdlib handler API
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        paths = ProjectPaths.from_root(tmp_path)
        paths.ensure_base_dirs()
        write_json(paths.lightrag_input / "manifest.json", {"status": "prepared", "document_count": 1})
        doc = {"id": "doc-id", "source_path": "corpus/raw/doc.md", "sha256": "sha256:doc", "text": "hello service"}
        (paths.lightrag_input / "documents.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")

        base_url = f"http://127.0.0.1:{server.server_port}"
        report = build_lightrag(paths, smoke_query="hello?", config={"base_url": base_url})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert report["status"] == "success"
    assert report["service"]["base_url"] == base_url
    assert report["service_track_ids"] == [{"source_path": "corpus/raw/doc.md", "status": "success", "track_id": "insert-1"}]
    assert requests == [
        ("/documents/text", {"text": "hello service", "file_source": "corpus/raw/doc.md"}),
        ("/query", {"query": "hello?", "mode": "hybrid", "include_references": True}),
    ]
    smoke = json.loads((paths.lightrag_queries / "smoke-test.json").read_text(encoding="utf-8"))
    assert smoke["answer"] == "smoke answer"


# --- H2: per-lane corpus-state independence ---------------------------------

def test_per_lane_corpus_state_is_independent(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    files = [CorpusFile(path="corpus/raw/a.md", sha256="sha256:a", size=1, suffix=".md", text_like=True)]
    # only the wiki lane has been run/persisted
    persist_corpus_state(files, lane_state_path(paths, "wiki"))

    wiki_change = diff_against_previous(files, lane_state_path(paths, "wiki"))
    lightrag_change = diff_against_previous(files, lane_state_path(paths, "lightrag"))

    assert wiki_change == {"added": [], "modified": [], "deleted": []}
    # lightrag still sees the file as new because its baseline is untouched
    assert lightrag_change["added"] == ["corpus/raw/a.md"]


def test_merge_change_sets_unions():
    merged = merge_change_sets([
        {"added": ["a"], "modified": [], "deleted": []},
        {"added": ["b"], "modified": ["c"], "deleted": ["d"]},
    ])
    assert merged == {"added": ["a", "b"], "modified": ["c"], "deleted": ["d"]}


# --- M1: markdown tables ----------------------------------------------------

def test_markdown_table_render():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    html = markdown_to_html(md, resolver=lambda t: "#")
    assert "<table>" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html and "<td>2</td>" in html


def test_markdown_pipe_without_separator_is_not_table():
    md = "| just text with a pipe\n"
    html = markdown_to_html(md, resolver=lambda t: "#")
    assert "<table>" not in html
    assert "<p>" in html


# --- M2: frontmatter block lists -------------------------------------------

def test_frontmatter_parses_block_list():
    text = "---\ntitle: Home\nsources:\n  - corpus/raw/a.md\n  - corpus/raw/b.md\n---\n"
    fields = parse_yaml_frontmatter(text)
    assert fields["title"] == "Home"
    assert fields["sources"] == ["corpus/raw/a.md", "corpus/raw/b.md"]


def test_parse_sources_handles_list_frontmatter():
    frontmatter = {"sources": ["corpus/raw/a.md", "corpus/raw/b.md"]}
    assert parse_sources(frontmatter, "") == ["corpus/raw/a.md", "corpus/raw/b.md"]


# --- M4: deep merge ---------------------------------------------------------

def test_deep_merge_preserves_unspecified_nested_keys():
    base = {"lightrag": {"mode": "service", "base_url": "http://127.0.0.1:9621", "input_file": "in"}}
    override = {"lightrag": {"base_url": "http://localhost:9621"}}
    merged = deep_merge(base, override)
    assert merged["lightrag"] == {"mode": "service", "base_url": "http://localhost:9621", "input_file": "in"}
    # base must not be mutated
    assert base["lightrag"]["base_url"] == "http://127.0.0.1:9621"


def test_config_load_deep_merges_user_overrides(tmp_path: Path):
    write_json(tmp_path / "project.json", {"lightrag": {"base_url": "http://localhost:9621"}})
    config = EvoConfig.load(tmp_path)
    assert config.project["lightrag"]["base_url"] == "http://localhost:9621"
    # other default nested keys preserved
    assert config.project["lightrag"]["mode"] == "service"
    assert config.project["lightrag"]["input_file"] == "artifacts/lightrag/input/documents.jsonl"


# --- L3: .dockerignore ------------------------------------------------------

def test_export_docker_writes_dockerignore_once(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    result = export_docker(paths)
    assert result["dockerignore_written"] is True
    dockerignore = paths.root / ".dockerignore"
    assert dockerignore.exists()
    assert "corpus/" in dockerignore.read_text(encoding="utf-8")

    # second run must not overwrite an existing file
    result2 = export_docker(paths)
    assert result2["dockerignore_written"] is False


# --- L4: queries directory --------------------------------------------------

def test_ensure_base_dirs_creates_lightrag_queries(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    assert paths.lightrag_queries.is_dir()


# --- Source/original pages --------------------------------------------------

def test_source_pages_render_with_source_type_and_nav_group(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_src / "index.md").write_text(
        "---\ntitle: 首页\ntype: index\nsources: []\n---\n\n# 首页\n\n- [[原文页]]\n",
        encoding="utf-8",
    )
    (paths.wiki_src / "concepts" / "moat.md").write_text(
        "---\ntitle: 护城河\ntype: concept\n---\n\n# 护城河\n\n## 摘要\n\n基于语料归纳。\n",
        encoding="utf-8",
    )
    (paths.wiki_src / "entities" / "buffett.md").write_text(
        "---\ntitle: 沃伦·巴菲特\ntype: entity\n---\n\n# 沃伦·巴菲特\n\n## 摘要\n\n语料中的人物。\n",
        encoding="utf-8",
    )
    source = paths.wiki_src / "sources" / "doc.md"
    source.write_text(
        "---\ntitle: 原文页\ntype: source\n---\n\n"
        "# 原文页\n\n## 摘要\n\n基于原文。\n\n## 原文内容\n\n完整原文  提到 [[护城河]] 与 [[沃伦·巴菲特]] 。\n",
        encoding="utf-8",
    )
    report = render_wiki(paths, EvoConfig())
    html = (paths.wiki_dist / "sources" / "doc.html").read_text(encoding="utf-8")

    assert report["status"] == "success"
    assert '<span class="type-badge type-source">原文</span>' in html
    assert '<details class="nav-group"><summary class="nav-group-title"><span>原文</span>' in html
    assert 'href="../concepts/moat.html">护城河</a>' in html
    assert 'href="../entities/buffett.html">沃伦·巴菲特</a>' in html
    assert '完整原文提到<a class="wikilink" href="../concepts/moat.html">护城河</a>与<a class="wikilink" href="../entities/buffett.html">沃伦·巴菲特</a>。' in html
    assert '<aside class="page-aside"><div class="related-panel">' in html
    assert "链接到本页" in html
    assert "## Sources" not in html


def test_render_wiki_writes_progress_and_lint_metadata(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_src / "index.md").write_text(
        "---\ntitle: 首页\ntype: index\nsources: []\n---\n\n# 首页\n\n- [[概念页]]\n",
        encoding="utf-8",
    )
    (paths.wiki_src / "concepts" / "concept.md").write_text(
        "---\ntitle: 概念页\ntype: concept\nsources:\n  - corpus/raw/doc.md\n---\n\n# 概念页\n\n## 摘要\n\n基于语料归纳。\n",
        encoding="utf-8",
    )

    report = render_wiki(paths, EvoConfig())
    progress = json.loads((paths.wiki / "progress.json").read_text(encoding="utf-8"))
    manifest = json.loads((paths.wiki / "manifest.json").read_text(encoding="utf-8"))

    assert progress["status"] == "success"
    assert progress["current_phase"] == "complete"
    assert len(progress["completed_pages"]) == report["page_count"]
    assert any(phase["phase"] == "lint_wiki" for phase in progress["phases"])
    assert report["progress"] == "artifacts/wiki/progress.json"
    assert report["lint"]["report"] == "artifacts/wiki/reports/wiki-health.json"
    assert manifest["progress"] == "artifacts/wiki/progress.json"
    assert manifest["lint_report"] == "artifacts/wiki/reports/wiki-health.json"


def test_lint_detects_concept_conflict_and_bad_source_page(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_src / "index.md").write_text("# 首页\n\n- [[重复概念]]\n", encoding="utf-8")
    (paths.wiki_src / "concepts" / "a.md").write_text(
        "---\ntitle: 重复概念\ntype: concept\nsources: []\n---\n\n# 重复概念\n",
        encoding="utf-8",
    )
    (paths.wiki_src / "concepts" / "b.md").write_text(
        "---\ntitle: 重复概念\ntype: concept\nsources: []\n---\n\n# 重复概念\n",
        encoding="utf-8",
    )
    (paths.wiki_src / "sources" / "bad.md").write_text(
        "---\ntitle: 坏原文页\ntype: source\nsources: []\n---\n\n# 坏原文页\n\n没有标准结构。\n",
        encoding="utf-8",
    )

    health = lint_wiki_artifacts(paths.root, paths.wiki_src, paths.wiki_audit, paths.wiki_log)
    codes = {issue["code"] for issue in health["issues"]}

    assert "concept_conflict" in codes
    assert "duplicate_page_title" in codes
    assert "source_missing_summary" in codes
    assert "source_missing_original" in codes

"""Unit tests for the design-review fixes (H1, H2, M1-M4, L1-L4)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from evo_wiki.config import EvoConfig, deep_merge
from evo_wiki.corpus import (
    CorpusFile,
    diff_against_previous,
    persist_corpus_state,
    scan_corpus,
)
from evo_wiki.cli import lane_state_path, merge_change_sets
from evo_wiki.lightrag_lane import (
    LightRAGBuildError,
    LightRAGServiceClient,
    build_lightrag,
    parse_lightrag_capabilities,
    probe_lightrag_service,
)
from evo_wiki.paths import ProjectPaths
from evo_wiki.platform_export import export_platform
from evo_wiki.state import StateError
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
    get_requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            get_requests.append(self.path)
            if self.path == "/health":
                body = {
                    "status": "healthy",
                    "configuration": {"workspace": "evo_wiki"},
                }
            elif self.path == "/openapi.json":
                body = {
                    "components": {"schemas": {}},
                    "paths": {"/documents/track_status/{track_id}": {"get": {}}},
                }
            elif self.path == "/documents/track_status/insert-1":
                body = {
                    "track_id": "insert-1",
                    "total_count": 1,
                    "documents": [
                        {
                            "track_id": "insert-1",
                            "status": "processed",
                            "chunks_count": 2,
                        }
                    ],
                }
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

        def do_POST(self):  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            requests.append((self.path, payload))
            if self.path == "/documents/text":
                body = {"status": "success", "message": "accepted", "track_id": "insert-1"}
            elif self.path == "/query":
                body = {
                    "response": "smoke answer",
                    "references": [
                        {"file_path": "corpus/raw/doc.md", "content": "supporting chunk"},
                        {"file_path": "corpus/raw/other.md", "content": ["first", "second"]},
                    ],
                }
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
        report = build_lightrag(
            paths,
            smoke_query="hello?",
            config={"base_url": base_url, "workspace": "evo_wiki"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert report["status"] == "success"
    assert report["service"]["base_url"] == base_url
    assert report["service"]["workspace"] == "evo_wiki"
    assert report["service_track_ids"] == [{"source_path": "corpus/raw/doc.md", "status": "success", "track_id": "insert-1"}]
    assert report["track_status"] == [
        {
            "source_path": "corpus/raw/doc.md",
            "track_id": "insert-1",
            "state": "processed",
            "document_count": 1,
            "status_counts": {"processed": 1},
            "total_chunks": 2,
            "unknown_statuses": [],
            "error_code": None,
        }
    ]
    assert get_requests == [
        "/health",
        "/openapi.json",
        "/documents/track_status/insert-1",
    ]
    assert requests == [
        ("/documents/text", {"text": "hello service", "file_source": "corpus/raw/doc.md"}),
        (
            "/query",
            {
                "query": "hello?",
                "mode": "hybrid",
                "include_references": True,
                "include_chunk_content": True,
            },
        ),
    ]
    smoke = json.loads((paths.lightrag_queries / "smoke-test.json").read_text(encoding="utf-8"))
    assert smoke["answer"] == "smoke answer"
    assert smoke["references"] == [
        {"file_path": "corpus/raw/doc.md", "content": ["supporting chunk"]},
        {"file_path": "corpus/raw/other.md", "content": ["first", "second"]},
    ]
    ledger = json.loads((paths.lightrag_state / "lightrag-import-ledger.json").read_text(encoding="utf-8"))
    assert ledger["documents"]["doc-id"]["service_track_id"] == "insert-1"


def test_lightrag_build_rejects_workspace_mismatch_before_submission(tmp_path: Path, monkeypatch):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    write_json(paths.lightrag_input / "manifest.json", {"status": "prepared", "document_count": 1})
    doc = {"id": "doc-id", "source_path": "corpus/raw/doc.md", "sha256": "sha256:doc", "text": "hello service"}
    (paths.lightrag_input / "documents.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def fake_request(self, method, path, payload=None):
        calls.append((method, path))
        assert payload is None
        assert path == "/health"
        return {
            "status": "healthy",
            "configuration": {"workspace": "other_workspace"},
        }

    monkeypatch.setattr(LightRAGServiceClient, "request_json", fake_request)

    with pytest.raises(LightRAGBuildError, match="workspace does not match"):
        build_lightrag(
            paths,
            config={"base_url": "http://127.0.0.1:9621", "workspace": "evo_wiki"},
        )

    assert calls == [("GET", "/health")]
    assert not (paths.lightrag_state / "lightrag-import-ledger.json").exists()
    report = json.loads((paths.lightrag_reports / "lightrag-report.json").read_text(encoding="utf-8"))
    assert report["failure_code"] == "WORKSPACE_MISMATCH"
    assert report["imported"] == []


def test_lightrag_build_does_not_commit_ledger_when_track_fails(tmp_path: Path, monkeypatch):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    write_json(paths.lightrag_input / "manifest.json", {"status": "prepared", "document_count": 1})
    doc = {"id": "doc-id", "source_path": "corpus/raw/doc.md", "sha256": "sha256:doc", "text": "hello service"}
    (paths.lightrag_input / "documents.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")

    def fake_request(self, method, path, payload=None):
        if (method, path) == ("GET", "/health"):
            return {"status": "healthy", "configuration": {"workspace": "evo_wiki"}}
        if (method, path) == ("GET", "/openapi.json"):
            return {
                "components": {"schemas": {}},
                "paths": {"/documents/track_status/{track_id}": {"get": {}}},
            }
        if (method, path) == ("POST", "/documents/text"):
            return {"status": "success", "track_id": "failed-track"}
        if (method, path) == ("GET", "/documents/track_status/failed-track"):
            return {
                "track_id": "failed-track",
                "total_count": 1,
                "documents": [{"track_id": "failed-track", "status": "failed", "chunks_count": 0}],
            }
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(LightRAGServiceClient, "request_json", fake_request)

    with pytest.raises(LightRAGBuildError, match="reported failed status"):
        build_lightrag(
            paths,
            config={"base_url": "http://127.0.0.1:9621", "workspace": "evo_wiki"},
        )

    assert not (paths.lightrag_state / "lightrag-import-ledger.json").exists()
    report = json.loads((paths.lightrag_reports / "lightrag-report.json").read_text(encoding="utf-8"))
    assert report["failure_code"] == "TRACK_FAILED"
    assert report["service_track_ids"] == [
        {"source_path": "corpus/raw/doc.md", "status": "success", "track_id": "failed-track"}
    ]


def test_capability_parser_keeps_unverified_values_unknown():
    capabilities = parse_lightrag_capabilities(
        {
            "status": "healthy",
            "core_version": "1.5.5",
            "api_version": "1.0",
        },
        None,
        expected_workspace="evo_wiki",
        requested_embedding_batch_size=8,
    )

    assert capabilities.authenticated_health is False
    assert capabilities.openapi_available is False
    assert capabilities.expected_workspace == "evo_wiki"
    assert capabilities.workspace is None
    assert capabilities.workspace_matches is None
    assert capabilities.storage_workspaces is None
    assert capabilities.storage_workspaces_available is None
    assert capabilities.storage_workspaces_match is None
    assert capabilities.remote_embedding_batch_size is None
    assert capabilities.embedding_batch_matches is None
    assert capabilities.supports_chunk_content is None
    assert capabilities.supports_conversation_history is None
    assert capabilities.supports_bypass is None
    assert capabilities.supports_graph_subgraph is None
    assert capabilities.supports_track_status is None
    assert capabilities.supports_document_delete is None
    assert capabilities.supports_document_inventory is None
    assert capabilities.supports_pipeline_status is None


def test_capability_parser_resolves_bypass_enum_reference():
    capabilities = parse_lightrag_capabilities(
        {
            "status": "healthy",
            "configuration": {
                "workspace": "evo_wiki",
                "storage_workspaces": {"graph": "evo_wiki"},
                "embedding_batch_num": 8,
            },
        },
        {
            "components": {
                "schemas": {
                    "QueryMode": {
                        "enum": [
                            "naive",
                            "local",
                            "global",
                            "hybrid",
                            "mix",
                            "bypass",
                        ]
                    },
                    "QueryRequest": {
                        "properties": {
                            "include_chunk_content": {"type": "boolean"},
                            "conversation_history": {"type": "array"},
                            "mode": {
                                "anyOf": [
                                    {
                                        "$ref": (
                                            "#/components/schemas/QueryMode"
                                        )
                                    }
                                ]
                            },
                        }
                    },
                }
            },
            "paths": {},
        },
        expected_workspace="evo_wiki",
        requested_embedding_batch_size=8,
    )

    assert capabilities.supports_bypass is True


@pytest.mark.parametrize(
    ("remote_configuration", "expected_status", "failure_code"),
    [
        ({}, "failed", "WORKSPACE_UNCONFIRMED"),
        ({"workspace": ""}, "failed", "WORKSPACE_UNCONFIRMED"),
        ({"workspace": "other"}, "failed", "WORKSPACE_MISMATCH"),
        (
            {
                "workspace": "evo_wiki",
                "storage_workspaces": {"graph_storage": "other"},
            },
            "failed",
            "STORAGE_WORKSPACE_MISMATCH",
        ),
    ],
)
def test_workspace_probe_fails_closed(
    monkeypatch,
    remote_configuration,
    expected_status,
    failure_code,
):
    health = {
        "status": "healthy",
        "configuration": {
            "embedding_batch_num": 8,
            **remote_configuration,
        },
    }

    def fake_request(self, method, path, payload=None):
        if path == "/health":
            return health
        if path == "/openapi.json":
            return {"components": {"schemas": {}}, "paths": {}}
        raise AssertionError(path)

    monkeypatch.setattr(LightRAGServiceClient, "request_json", fake_request)
    report = probe_lightrag_service(
        {
            "base_url": "http://127.0.0.1:9621",
            "workspace": "evo_wiki",
            "api_key": "do-not-leak-api-key",
            "bearer_token": "do-not-leak-bearer-token",
        }
    )

    assert report["status"] == expected_status
    assert report["failure_code"] == failure_code
    serialized = json.dumps(report)
    assert "do-not-leak-api-key" not in serialized
    assert "do-not-leak-bearer-token" not in serialized


@pytest.mark.parametrize(
    ("storage_workspaces", "warning"),
    [
        (None, "storage_workspaces_unknown"),
        (
            {
                "kv_storage": None,
                "doc_status_storage": None,
                "graph_storage": None,
                "vector_storage": None,
            },
            "storage_workspaces_unconfirmed",
        ),
    ],
)
def test_workspace_probe_warns_when_storage_mapping_cannot_be_confirmed(
    monkeypatch,
    storage_workspaces,
    warning,
):
    configuration = {
        "workspace": "evo_wiki",
        "embedding_batch_num": 8,
        "enable_rerank": True,
        "parser_routing": "pdf:mineru",
    }
    if storage_workspaces is not None:
        configuration["storage_workspaces"] = storage_workspaces

    def fake_request(self, method, path, payload=None):
        if path == "/health":
            return {"status": "healthy", "configuration": configuration}
        if path == "/openapi.json":
            return {
                "components": {
                    "schemas": {
                        "QueryRequest": {
                            "properties": {"include_chunk_content": {"type": "boolean"}}
                        }
                    }
                },
                "paths": {
                    "/documents/track_status/{track_id}": {"get": {}},
                    "/documents/delete_document": {"delete": {}},
                },
            }
        raise AssertionError(path)

    monkeypatch.setattr(LightRAGServiceClient, "request_json", fake_request)
    report = probe_lightrag_service(
        {"base_url": "http://127.0.0.1:9621", "workspace": "evo_wiki"}
    )

    assert report["status"] == "warning"
    assert warning in report["warnings"]


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
        "---\ntitle: 护城河\ntype: concept\n---\n\n# 护城河\n\n## 摘要\n\n基于语料归纳。\n\n## 相关页面\n\n- [[沃伦·巴菲特]]\n",
        encoding="utf-8",
    )
    (paths.wiki_src / "entities" / "buffett.md").write_text(
        "---\ntitle: 沃伦·巴菲特\ntype: entity\n---\n\n# 沃伦·巴菲特\n\n## 摘要\n\n语料中的人物。\n\n## 关联概念\n\n- [[护城河]]\n",
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
    concept_html = (paths.wiki_dist / "concepts" / "moat.html").read_text(encoding="utf-8")
    entity_html = (paths.wiki_dist / "entities" / "buffett.html").read_text(encoding="utf-8")

    assert report["status"] == "success"
    assert '<span class="type-badge type-source">原文</span>' in html
    assert '<details class="nav-group"><summary class="nav-group-title"><span>原文</span>' in html
    assert 'href="../concepts/moat.html">护城河</a>' in html
    assert 'href="../entities/buffett.html">沃伦·巴菲特</a>' in html
    assert '完整原文提到<a class="wikilink" href="../concepts/moat.html">护城河</a>与<a class="wikilink" href="../entities/buffett.html">沃伦·巴菲特</a>。' in html
    assert '<aside class="page-aside"><div class="related-panel">' in html
    assert "链接到本页" in html
    assert '<span class="related-summary-preview">基于语料归纳。</span>' in html
    assert '<span class="related-summary-preview">语料中的人物。</span>' in html
    assert "## Sources" not in html
    assert "链接到本页" in concept_html
    assert '<summary>实体 <span class="related-count">1</span></summary>' in concept_html
    assert '<span class="related-item-title">沃伦·巴菲特</span>' in concept_html
    assert '<span class="related-summary-preview">语料中的人物。</span>' in concept_html
    assert 'href="../entities/buffett.html">查看页面 →</a>' in concept_html
    assert "链接到本页" in entity_html
    assert '<summary>概念 <span class="related-count">1</span></summary>' in entity_html
    assert '<span class="related-item-title">护城河</span>' in entity_html
    assert '<span class="related-summary-preview">基于语料归纳。</span>' in entity_html
    assert 'href="../concepts/moat.html">查看页面 →</a>' in entity_html


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


def test_spa_shell_references_generated_assets(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_src / "index.md").write_text(
        "---\ntitle: 首页\ntype: index\nsources: []\n---\n\n# 首页\n",
        encoding="utf-8",
    )

    render_wiki(paths, EvoConfig())
    app_index = (paths.wiki_dist / "app" / "index.html").read_text(encoding="utf-8")

    assert 'href="./app.css"' in app_index
    assert 'src="./app.js"' in app_index
    assert (paths.wiki_dist / "app" / "app.css").exists()
    assert (paths.wiki_dist / "app" / "app.js").exists()
    app_js = (paths.wiki_dist / "app" / "app.js").read_text(encoding="utf-8")
    assert "data.citations" in app_js
    assert "未检索到知识库依据，此回答由模型通用知识生成" in app_js
    assert "回答生成失败" in app_js
    assert 'href="../assets/app.css"' not in app_index
    assert 'src="../assets/app/app.js"' not in app_index


def test_export_platform_allows_readonly_graph_label_api(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_dist / "index.html").write_text("<!doctype html><title>Wiki</title>", encoding="utf-8")
    (paths.wiki_dist / "status").mkdir()
    (paths.wiki_dist / "status" / "accidental.json").write_text('{"secret": true}', encoding="utf-8")
    write_json(paths.lightrag / "manifest.json", {"status": "success"})
    write_json(paths.lightrag_reports / "lightrag-report.json", {"status": "success"})
    ledger_path = paths.lightrag_state / "lightrag-import-ledger.json"
    write_json(ledger_path, {"documents": {"private": {"source_path": "/internal/private.md"}}})

    result = export_platform(
        paths,
        EvoConfig(
            project={
                "lightrag": {"base_url": "http://127.0.0.1:9621"},
                "query_gateway": {
                    "mode": "shadow",
                    "listen": "127.0.0.1:8765",
                },
                "security": {
                    "auth_mode": "trusted_proxy",
                    "default_domain": "default",
                },
            }
        ),
    )
    nginx_conf = (paths.platform / "nginx.conf").read_text(encoding="utf-8")
    readme = (paths.platform / "README.md").read_text(encoding="utf-8")

    assert "location /api/query" in nginx_conf
    assert "location /api/graphs" in nginx_conf
    assert "location /api/graph/label/" in nginx_conf
    assert "127.0.0.1:8765/api/graph/label/" in nginx_conf
    assert "LightRAG 地址或凭据" in readme
    assert "LIGHTRAG_API_KEY" not in nginx_conf
    assert "9621" not in nginx_conf
    assert "location ^~ /status/" in nginx_conf
    assert "deny all;" in nginx_conf
    assert "return 404;" in nginx_conf
    assert "location = /nginx.conf { return 404; }" in nginx_conf
    assert "location = /README.md { return 404; }" in nginx_conf
    assert not (paths.platform / "status").exists()
    assert ledger_path.exists()
    assert result["status_baked"] == []
    assert result["status_public"] is False


def test_export_platform_requires_successful_lightrag_lane(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_dist / "index.html").write_text("<!doctype html><title>Wiki</title>", encoding="utf-8")

    try:
        export_platform(paths, EvoConfig())
    except RuntimeError as exc:
        assert "LightRAG lane has not completed successfully" in str(exc)
    else:
        raise AssertionError("export_platform should require successful LightRAG artifacts")


def test_export_platform_failure_preserves_previous_complete_output(
    tmp_path: Path,
    monkeypatch,
):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_dist / "index.html").write_text(
        "<!doctype html><title>new</title>",
        encoding="utf-8",
    )
    write_json(paths.lightrag / "manifest.json", {"status": "success"})
    write_json(
        paths.lightrag_reports / "lightrag-report.json",
        {"status": "success"},
    )
    marker = paths.platform / "previous.txt"
    marker.write_text("previous complete platform", encoding="utf-8")

    def fail_copy(_src: Path, _dst: Path) -> None:
        raise OSError("injected staging failure")

    monkeypatch.setattr(
        "evo_wiki.platform_export._copy_tree",
        fail_copy,
    )
    config = EvoConfig(
        project={
            "profile": "local-platform",
            "lightrag": {
                "base_url": "http://127.0.0.1:9621",
                "workspace": "atomic",
            },
            "query_gateway": {
                "mode": "shadow",
                "listen": "127.0.0.1:8765",
            },
            "security": {
                "auth_mode": "local_single_user",
                "default_domain": "default",
            },
        }
    )

    with pytest.raises(OSError, match="injected staging failure"):
        export_platform(paths, config)

    assert marker.read_text(encoding="utf-8") == (
        "previous complete platform"
    )
    assert not list(paths.artifacts.glob(".platform-staging-*"))


def test_spa_shell_contains_governed_query_controls_and_shared_style(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_src / "index.md").write_text(
        "---\ntitle: 首页\ntype: index\nsources: []\n---\n\n# 首页\n",
        encoding="utf-8",
    )

    render_wiki(paths, EvoConfig())
    app_css = (paths.wiki_dist / "app" / "app.css").read_text(encoding="utf-8")
    app_js = (paths.wiki_dist / "app" / "app.js").read_text(encoding="utf-8")

    assert "grid-template-columns:minmax(0, 820px) 280px" in app_css
    assert "font-family:var(--serif)" in app_css
    assert "qa-mode" in app_js
    assert "schema_version: 2" in app_js
    assert "include_chunk_content" not in app_js
    assert "refContent(ref.excerpts || ref.content)" in app_js
    assert "data.citations" in app_js
    assert "data.answer" in app_js
    assert "/api/graph/label/popular" in app_js
    assert "/api/graph/label/search" in app_js
    assert "节点详情" in app_js
    assert "conversation_history" in app_js
    assert "shadow_failed" not in app_js
    assert "<details class=\"spa-shadow\"" not in app_js
    assert "部分依据待核验" in app_js
    assert "已引用知识库资料" in app_js
    assert "依据待核验" in app_js
    assert "回答断言 [" in app_js
    assert "依据片段：" in app_js
    assert "safeMarkdown" in app_js
    assert "引用关联知识子图" in app_js
    assert "EVIDENCE_GRAPH_MAX_DEPTH = 1" in app_js
    assert "EVIDENCE_GRAPH_MAX_NODES = 24" in app_js
    assert "mapped && mapped.graph_labels" in app_js
    assert "hydrateEvidenceSubgraph" in app_js
    assert "evidenceSubgraphSeeds(refs, question)" in app_js
    assert "evidenceSeedScore(question, ref, value, mapped)" in app_js
    assert "right.score - left.score" in app_js
    assert "normalizedText(nodeLabel(node)) === normalizedSeed" in app_js
    assert "if (!root) return false" in app_js
    assert "mapped && mapped.title,\n        '引用文档'" not in app_js
    assert "nodeLabel(node).toLowerCase().indexOf(normalizedSeed)" not in app_js
    assert "GRAPH_MAX_DEPTH = 2" in app_js
    assert "GRAPH_MAX_NODES = 50" in app_js
    assert "spa-graph-node-group" in app_js
    assert "location.hash = '#entity/'" not in app_js
    assert "min-width:720px" not in app_css


def test_wiki_registry_drives_entity_and_source_links(tmp_path: Path):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    source_name = "case-source.txt"
    (paths.wiki_src / "index.md").write_text(
        "---\ntitle: 首页\ntype: index\nsources: []\n---\n\n"
        "# 首页\n\n- [[人物甲]]\n",
        encoding="utf-8",
    )
    (paths.wiki_src / "entities" / "person-a.md").write_text(
        "---\ntitle: 人物甲\ntype: entity\n"
        "graph_label: PERSON_A\naliases:\n  - 甲某\n"
        f"sources:\n  - /private/workspace/{source_name}\n---\n\n"
        "# 人物甲\n\n人物甲参见 [[人物甲]]。\n",
        encoding="utf-8",
    )
    (paths.wiki_src / "sources" / "case.md").write_text(
        "---\ntitle: 案例原文\ntype: source\n"
        f"sources:\n  - corpus/raw/legal/{source_name}\n---\n\n"
        "# 案例原文\n\n## 摘要\n\n摘要。\n\n## 原文内容\n\n"
        "一、基本案情\n\n（一）争议焦点\n",
        encoding="utf-8",
    )

    render_wiki(paths, EvoConfig())

    registry = json.loads(
        (paths.wiki_dist / "wiki-registry.json").read_text(
            encoding="utf-8"
        )
    )
    entity_html = (
        paths.wiki_dist / "entities" / "person-a.html"
    ).read_text(encoding="utf-8")
    source_html = (
        paths.wiki_dist / "sources" / "case.html"
    ).read_text(encoding="utf-8")
    serialized = json.dumps(registry, ensure_ascii=False)

    assert registry["entities"] == [
        {
            "title": "人物甲",
            "graph_label": "PERSON_A",
            "aliases": ["甲某"],
            "wiki_path": "entities/person-a.html",
        }
    ]
    assert registry["sources"][source_name]["wiki_path"] == (
        "sources/case.html"
    )
    assert registry["sources"][source_name]["graph_labels"] == [
        "PERSON_A"
    ]
    assert "/private/workspace" not in serialized
    assert "corpus/raw" not in serialized
    assert "/app#entity/PERSON_A" in entity_html
    assert "来源依据" in entity_html
    assert 'href="../sources/case.html"' in entity_html
    assert '<span class="wikilink self">人物甲</span>' in entity_html
    assert "<h3 id=" in source_html and "一、基本案情</h3>" in source_html
    assert "<h4 id=" in source_html and "（一）争议焦点</h4>" in source_html


def test_duplicate_entity_graph_label_blocks_registry_generation(
    tmp_path: Path,
):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_src / "index.md").write_text(
        "# 首页\n",
        encoding="utf-8",
    )
    for slug, title in (("a", "实体甲"), ("b", "实体乙")):
        (paths.wiki_src / "entities" / f"{slug}.md").write_text(
            f"---\ntitle: {title}\ntype: entity\n"
            "graph_label: DUPLICATE\n---\n\n"
            f"# {title}\n",
            encoding="utf-8",
        )

    with pytest.raises(StateError) as caught:
        render_wiki(paths, EvoConfig())

    assert caught.value.error_code == "WIKI_REGISTRY_MAPPING_INVALID"


def test_wiki_mobile_drawer_search_and_current_group_are_accessible(
    tmp_path: Path,
):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_src / "index.md").write_text(
        "# 首页\n\n- [[概念页]]\n",
        encoding="utf-8",
    )
    (paths.wiki_src / "concepts" / "item.md").write_text(
        "---\ntitle: 概念页\ntype: concept\n---\n\n# 概念页\n",
        encoding="utf-8",
    )

    render_wiki(paths, EvoConfig())

    page = (
        paths.wiki_dist / "concepts" / "item.html"
    ).read_text(encoding="utf-8")
    app_js = (paths.wiki_dist / "assets" / "app.js").read_text(
        encoding="utf-8"
    )
    style = (paths.wiki_dist / "assets" / "style.css").read_text(
        encoding="utf-8"
    )
    assert 'aria-controls="wiki-sidebar"' in page
    assert 'role="combobox"' in page
    assert 'role="listbox"' in page
    assert "ArrowDown" in app_js and "ArrowUp" in app_js
    assert "activeNav.closest('details.nav-group')" in app_js
    assert "event.key==='Escape'" in app_js
    assert "transform:translateX(-105%)" in style


def test_wiki_and_spa_apply_validated_presentation_configuration(
    tmp_path: Path,
):
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    (paths.wiki_src / "index.md").write_text(
        "---\ntitle: 首页\ntype: index\nsources: []\n---\n\n# 首页\n",
        encoding="utf-8",
    )
    logo = tmp_path / "branding" / "logo.svg"
    logo.parent.mkdir(parents=True)
    logo.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        encoding="utf-8",
    )
    config = EvoConfig()
    config.wiki = deep_merge(
        config.wiki,
        {
            "title": "研发知识底座",
            "description": "面向二次开发的知识平台",
            "brand": {
                "logo_path": "branding/logo.svg",
                "primary_color": "#123abc",
            },
            "navigation": {
                "wiki": True,
                "qa": True,
                "graph": False,
                "entity_hub": False,
            },
            "query_defaults": {
                "mode": "local",
                "top_k": 7,
                "history_turns": 2,
            },
            "graph_defaults": {
                "max_depth": 1,
                "max_nodes": 30,
                "popular_limit": 8,
            },
        },
    )

    render_wiki(paths, config)

    wiki_html = (paths.wiki_dist / "index.html").read_text(
        encoding="utf-8"
    )
    spa_html = (paths.wiki_dist / "app" / "index.html").read_text(
        encoding="utf-8"
    )
    spa_js = (paths.wiki_dist / "app" / "app.js").read_text(
        encoding="utf-8"
    )
    theme = (
        paths.wiki_dist / "assets" / "shared" / "theme.css"
    ).read_text(encoding="utf-8")
    assert "研发知识底座" in wiki_html
    assert "brand-logo.svg" in wiki_html
    assert 'data-nav-graph="false"' in spa_html
    assert "面向二次开发的知识平台" in spa_html
    assert "value=\"7\"" in spa_js
    assert "value = 'local'" in spa_js
    assert "HISTORY_TURNS = 2" in spa_js
    assert "GRAPH_MAX_DEPTH = 1" in spa_js
    assert "GRAPH_MAX_NODES = 30" in spa_js
    assert "GRAPH_POPULAR_LIMIT = 8" in spa_js
    assert "--accent:#123abc" in theme
    assert (
        paths.wiki_dist / "assets" / "shared" / "brand-logo.svg"
    ).read_text(encoding="utf-8") == logo.read_text(encoding="utf-8")


def test_presentation_configuration_rejects_invalid_dependencies_and_paths(
    tmp_path: Path,
):
    invalid_navigation = EvoConfig()
    invalid_navigation.wiki = deep_merge(
        invalid_navigation.wiki,
        {
            "navigation": {
                "wiki": True,
                "qa": False,
                "graph": True,
                "entity_hub": True,
            }
        },
    )
    with pytest.raises(StateError) as dependency_error:
        invalid_navigation.validate(tmp_path, target="platform")
    assert dependency_error.value.error_code == "STATE_CONFIG_INVALID"

    escaping_logo = EvoConfig()
    escaping_logo.wiki = deep_merge(
        escaping_logo.wiki,
        {"brand": {"logo_path": "../outside.svg"}},
    )
    with pytest.raises(StateError) as path_error:
        escaping_logo.validate(tmp_path)
    assert path_error.value.error_code == "STATE_PATH_INVALID"

    invalid_history = EvoConfig()
    invalid_history.wiki = deep_merge(
        invalid_history.wiki,
        {"query_defaults": {"history_turns": 4}},
    )
    with pytest.raises(StateError) as history_error:
        invalid_history.validate(tmp_path)
    assert history_error.value.error_code == "STATE_CONFIG_INVALID"

    invalid_graph = EvoConfig()
    invalid_graph.wiki = deep_merge(
        invalid_graph.wiki,
        {"graph_defaults": {"max_nodes": 201}},
    )
    with pytest.raises(StateError) as graph_error:
        invalid_graph.validate(tmp_path)
    assert graph_error.value.error_code == "STATE_CONFIG_INVALID"


def test_lint_is_demo_style_plus_html_source_structure(tmp_path: Path):
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

    assert "concept_conflict" not in codes
    assert "duplicate_page_title" not in codes
    assert "source_missing_summary" in codes
    assert "source_missing_original" in codes

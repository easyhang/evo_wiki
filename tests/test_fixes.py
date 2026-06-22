"""Unit tests for the design-review fixes (H1, H2, M1-M4, L1-L4)."""
from __future__ import annotations

import json
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
from evo_wiki.wiki import markdown_to_html, parse_sources
from evo_wiki.wiki_health import parse_yaml_frontmatter


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
    base = {"lightrag": {"mode": "direct", "working_dir": "ws", "input_file": "in"}}
    override = {"lightrag": {"working_dir": "custom"}}
    merged = deep_merge(base, override)
    assert merged["lightrag"] == {"mode": "direct", "working_dir": "custom", "input_file": "in"}
    # base must not be mutated
    assert base["lightrag"]["working_dir"] == "ws"


def test_config_load_deep_merges_user_overrides(tmp_path: Path):
    write_json(tmp_path / "project.json", {"lightrag": {"working_dir": "custom/ws"}})
    config = EvoConfig.load(tmp_path)
    assert config.project["lightrag"]["working_dir"] == "custom/ws"
    # other default nested keys preserved
    assert config.project["lightrag"]["mode"] == "direct_dependency"
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

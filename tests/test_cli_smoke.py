import json
import os
import subprocess
import sys
from pathlib import Path


def run_cli(tmp_path: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(env_root / "src")
    return subprocess.run(
        [sys.executable, "-m", "evo_wiki.cli", *args],
        cwd=cwd or env_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_default_root_uses_workspace_directory(tmp_path: Path):
    result = run_cli(tmp_path, "init", cwd=tmp_path)
    assert result.returncode == 0, result.stderr

    runtime = tmp_path / "workspace"
    assert (runtime / "corpus" / "raw").exists()
    assert (runtime / "artifacts" / "wiki" / "wiki-src" / "index.md").exists()
    assert (runtime / "project.json").exists()
    assert (runtime / "wiki.json").exists()

    assert not (tmp_path / "corpus").exists()
    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / "project.json").exists()
    assert not (tmp_path / "wiki.json").exists()



def test_wiki_lane_smoke(tmp_path: Path):
    project = tmp_path / "project"
    result = run_cli(tmp_path, "init", "--root", str(project))
    assert result.returncode == 0, result.stderr

    raw = project / "corpus" / "raw" / "intro.md"
    raw.write_text("# Intro\n\nEvo wiki supports Wiki-first workflows and LightRAG.\n", encoding="utf-8")
    src = project / "artifacts" / "wiki" / "wiki-src" / "index.md"
    src.write_text(
        "---\ntitle: Home\ntype: index\nsources:\n  - corpus/raw/intro.md\n---\n\n"
        "# Home\n\nEvo wiki supports **Wiki-first** workflows. See [[LightRAG]].\n\n"
        "```mermaid\ngraph LR\n  Corpus --> Wiki\n```\n\n"
        "Inline math $x+y$.\n",
        encoding="utf-8",
    )
    concept = project / "artifacts" / "wiki" / "wiki-src" / "concepts" / "lightrag.md"
    concept.parent.mkdir(parents=True, exist_ok=True)
    concept.write_text(
        "---\ntitle: LightRAG\ntype: concept\nsources:\n  - corpus/raw/intro.md\n---\n\n"
        "# LightRAG\n\nLightRAG is the optional GraphRAG lane.\n",
        encoding="utf-8",
    )

    result = run_cli(tmp_path, "run", "--root", str(project), "--lane", "wiki")
    assert result.returncode == 0, result.stderr
    html_path = project / "artifacts" / "wiki" / "dist" / "index.html"
    assert html_path.exists()
    html = html_path.read_text(encoding="utf-8")
    assert "class=\"mermaid\"" in html
    assert "katex" in html
    assert "concepts/lightrag.html" in html
    assert (project / "artifacts" / "wiki" / "dist" / "concepts" / "lightrag.html").exists()
    health = json.loads((project / "artifacts" / "wiki" / "reports" / "wiki-health.json").read_text(encoding="utf-8"))
    assert health["issue_count"] == 0
    manifest = json.loads((project / "artifacts" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["selected_lanes"] == ["wiki"]
    assert manifest["lanes"]["lightrag"]["status"] == "not_requested"

    result = run_cli(tmp_path, "lint-wiki", "--root", str(project))
    assert result.returncode == 0, result.stderr


def test_lightrag_prepare_dry_run(tmp_path: Path):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    (project / "corpus" / "raw" / "doc.md").write_text("LightRAG input text", encoding="utf-8")

    wiki_result = run_cli(tmp_path, "run", "--root", str(project), "--lane", "wiki")
    assert wiki_result.returncode == 0, wiki_result.stderr

    result = run_cli(tmp_path, "prepare-lightrag", "--root", str(project))
    assert result.returncode == 0, result.stderr
    assert (project / "artifacts" / "lightrag" / "input" / "documents.jsonl").exists()

    result = run_cli(tmp_path, "run", "--root", str(project), "--lane", "lightrag", "--lightrag-dry-run")
    assert result.returncode == 0, result.stderr
    report = json.loads((project / "artifacts" / "lightrag" / "reports" / "lightrag-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "dry_run"
    manifest = json.loads((project / "artifacts" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["selected_lanes"] == ["lightrag"]
    assert manifest["lanes"]["wiki"]["status"] == "success"
    assert manifest["lanes"]["wiki"]["from_previous_run"] is True

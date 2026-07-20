import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from evo_wiki.config import EvoConfig
from evo_wiki.corpus import scan_corpus
from evo_wiki.state import ActionGate, RemoteStatus, StateStore


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


def read_run_journals(project: Path) -> list[list[dict]]:
    paths = sorted(
        (project / "artifacts" / "logs" / "runs").glob(
            "*/events-000001.jsonl"
        )
    )
    return [
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for path in paths
    ]


def read_run_event_files(project: Path) -> list[Path]:
    return sorted(
        (project / "artifacts" / "logs" / "runs").glob("*/events-*.jsonl")
    )


def workspace_snapshot(project: Path) -> dict[str, tuple[int, int, bytes]]:
    return {
        path.relative_to(project).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.read_bytes(),
        )
        for path in sorted(project.rglob("*"))
        if path.is_file()
    }


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


def test_generate_wiki_dry_run_is_zero_write(tmp_path: Path):
    project = tmp_path / "project"
    initialized = run_cli(
        tmp_path,
        "init",
        "--root",
        str(project),
        "--profile",
        "wiki-only",
    )
    assert initialized.returncode == 0, initialized.stderr
    (project / "corpus" / "raw" / "doc.md").write_text(
        "可交付知识库资料。",
        encoding="utf-8",
    )
    index = project / "artifacts" / "wiki" / "wiki-src" / "index.md"
    index.write_text(
        "---\ntitle: 首页\ntype: index\nsources:\n"
        "  - corpus/raw/doc.md\n---\n\n# 首页\n\n可交付知识库内容。\n",
        encoding="utf-8",
    )
    before = workspace_snapshot(project)

    result = run_cli(
        tmp_path,
        "generate",
        "--root",
        str(project),
        "--target",
        "wiki",
        "--dry-run",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["mode"] == "dry_run"
    assert payload["workspace_mutated"] is False
    assert payload["remote_mutated"] is False
    assert workspace_snapshot(project) == before


def test_generate_platform_rejects_stub_before_remote_write(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    assert run_cli(
        tmp_path,
        "init",
        "--root",
        str(project),
    ).returncode == 0
    (project / "corpus" / "raw" / "doc.md").write_text(
        "平台资料。",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "EVO_WIKI_QUERY_AUDIT_KEY",
        "0123456789abcdef0123456789abcdef",
    )

    result = run_cli(
        tmp_path,
        "generate",
        "--root",
        str(project),
        "--json",
    )

    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["error_code"] == "GENERATION_WIKI_STUB"
    report = json.loads(
        (
            project / "artifacts" / "generation" / "report.json"
        ).read_text(encoding="utf-8")
    )
    assert report["status"] == "failed"
    assert report["error_code"] == "GENERATION_WIKI_STUB"
    assert not (
        project
        / "artifacts"
        / "lightrag"
        / "reports"
        / "lightrag-report.json"
    ).exists()


def test_generate_blocks_current_unknown_binding_before_wiki_mutation(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    assert run_cli(
        tmp_path,
        "init",
        "--root",
        str(project),
    ).returncode == 0
    source = project / "corpus" / "raw" / "blocked.md"
    source.write_text("当前受控语料。", encoding="utf-8")
    index = (
        project / "artifacts" / "wiki" / "wiki-src" / "index.md"
    )
    index.write_text(
        "---\ntitle: 首页\ntype: index\nsources:\n"
        "  - corpus/raw/blocked.md\n---\n\n"
        "# 首页\n\n当前受控语料。\n",
        encoding="utf-8",
    )
    project_json = project / "project.json"
    raw_config = json.loads(project_json.read_text(encoding="utf-8"))
    raw_config["lightrag"]["base_url"] = "http://127.0.0.1:9"
    raw_config["lightrag"]["workspace"] = "blocked_test"
    project_json.write_text(
        json.dumps(raw_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    config = EvoConfig.load(project)
    item = scan_corpus(project, project / "corpus")[0]
    store = StateStore(project)
    store.stage_files([item])
    partition_id, fingerprint = store.ensure_partition(
        config.project["lightrag"]
    )
    binding_id = store.mark_submission_started(
        source_path=item.path,
        sha256=item.sha256,
        partition_id=partition_id,
        backend_fingerprint=fingerprint,
    )
    marker = project / "artifacts" / "wiki" / "dist" / "marker.html"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("previous Wiki", encoding="utf-8")
    database = (
        project / "artifacts" / "state" / "evo_wiki.sqlite3"
    )
    database_before = database.read_bytes()
    marker_before = marker.read_bytes()
    monkeypatch.setenv(
        "EVO_WIKI_QUERY_AUDIT_KEY",
        "0123456789abcdef0123456789abcdef",
    )

    dry_run = run_cli(
        tmp_path,
        "generate",
        "--root",
        str(project),
        "--dry-run",
        "--json",
    )

    assert dry_run.returncode == 5, dry_run.stderr
    dry_payload = json.loads(dry_run.stdout)
    assert dry_payload["status"] == "blocked"
    assert dry_payload["error_code"] == (
        "GENERATION_RECONCILE_REQUIRED"
    )
    assert dry_payload["blocked_binding_count"] == 1
    assert dry_payload["workspace_mutated"] is False
    assert dry_payload["remote_mutated"] is False
    assert dry_payload["recovery_commands"] == {
        "review": (
            "evo-wiki state reconcile --root <workspace> --json"
        ),
        "apply": (
            "evo-wiki state reconcile --root <workspace> --apply --json"
        ),
        "retry": (
            "evo-wiki generate --root <workspace> "
            "--target platform --dry-run --json"
        ),
    }
    assert database.read_bytes() == database_before
    assert marker.read_bytes() == marker_before
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()

    apply = run_cli(
        tmp_path,
        "generate",
        "--root",
        str(project),
        "--json",
    )

    assert apply.returncode == 5, apply.stderr
    apply_payload = json.loads(apply.stdout)
    assert apply_payload["status"] == "blocked"
    assert apply_payload["blocked_binding_count"] == 1
    assert marker.read_bytes() == marker_before
    report = json.loads(
        (
            project / "artifacts" / "generation" / "report.json"
        ).read_text(encoding="utf-8")
    )
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["status"] == "blocked"
    assert binding_id not in serialized
    assert str(project) not in serialized
    assert item.path not in serialized

    store.mark_binding_observation(
        binding_id,
        remote_status=RemoteStatus.PROCESSED,
        action_gate=ActionGate.OPEN,
        gate_reason=None,
        chunk_count=1,
    )
    reconciled_dry_run = run_cli(
        tmp_path,
        "generate",
        "--root",
        str(project),
        "--dry-run",
        "--json",
    )
    assert reconciled_dry_run.returncode == 0
    assert json.loads(reconciled_dry_run.stdout)["status"] == "ready"


def test_generate_platform_runs_complete_pipeline_against_mock_lightrag_protocol(
    tmp_path: Path,
    monkeypatch,
):
    track_id = "track-generate"

    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self._send(
                    {
                        "status": "healthy",
                        "configuration": {
                            "workspace": "generate_test",
                            "embedding_batch_num": 8,
                            "storage_workspaces": {
                                "kv_storage": "generate_test",
                                "vector_storage": "generate_test",
                                "graph_storage": "generate_test",
                                "doc_status_storage": "generate_test",
                            },
                        },
                    }
                )
                return
            if self.path == "/openapi.json":
                self._send(
                    {
                        "components": {
                            "schemas": {
                                "QueryRequest": {
                                        "properties": {
                                            "include_chunk_content": {
                                                "type": "boolean"
                                            },
                                            "conversation_history": {
                                                "type": "array"
                                            },
                                            "mode": {
                                                "type": "string",
                                                "enum": [
                                                    "mix",
                                                    "hybrid",
                                                    "bypass",
                                                ],
                                            },
                                        }
                                }
                            }
                        },
                        "paths": {
                            "/query": {"post": {}},
                            "/graphs": {"get": {}},
                            "/documents/track_status/{track_id}": {
                                "get": {}
                            },
                        },
                    }
                )
                return
            if self.path == f"/documents/track_status/{track_id}":
                self._send(
                    {
                        "track_id": track_id,
                        "total_count": 1,
                        "documents": [
                            {
                                "track_id": track_id,
                                "status": "processed",
                                "chunks_count": 1,
                            }
                        ],
                    }
                )
                return
            self._send({"error": "not found"}, status=404)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            if self.path == "/documents/text":
                self._send(
                    {
                        "status": "success",
                        "track_id": track_id,
                    }
                )
                return
            self._send({"error": "not found"}, status=404)

        def log_message(self, *_args):  # noqa: D401
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        project = tmp_path / "project"
        assert run_cli(
            tmp_path,
            "init",
            "--root",
            str(project),
        ).returncode == 0
        (project / "corpus" / "raw" / "doc.md").write_text(
            "自动生成完整问答平台。",
            encoding="utf-8",
        )
        index = (
            project / "artifacts" / "wiki" / "wiki-src" / "index.md"
        )
        index.write_text(
            "---\ntitle: 首页\ntype: index\nsources:\n"
            "  - corpus/raw/doc.md\n---\n\n"
            "# 首页\n\n自动生成完整问答平台。\n",
            encoding="utf-8",
        )
        project_json = project / "project.json"
        config = json.loads(project_json.read_text(encoding="utf-8"))
        config["lightrag"]["base_url"] = (
            f"http://127.0.0.1:{server.server_port}"
        )
        config["lightrag"]["workspace"] = "generate_test"
        config["lightrag"]["sync"] = {
            "poll_interval_seconds": 0.1,
            "poll_timeout_seconds": 5,
        }
        project_json.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "EVO_WIKI_QUERY_AUDIT_KEY",
            "0123456789abcdef0123456789abcdef",
        )

        result = run_cli(
            tmp_path,
            "generate",
            "--root",
            str(project),
            "--json",
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert payload["target"] == "platform"
        assert payload["gateway"] == {
            "status": "ready",
            "mode": "shadow",
        }
        assert payload["remote_mutated"] is True
        platform = project / "artifacts" / "platform"
        assert (platform / "index.html").is_file()
        assert (platform / "app" / "index.html").is_file()
        assert (platform / "nginx.conf").is_file()
        binding = StateStore(project).list_lightrag_documents()[
            "corpus__raw__doc.md"
        ]
        assert binding["revision_status"] == "ACTIVE"
        assert (
            project / "artifacts" / "generation" / "report.json"
        ).is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_generate_deletion_blocks_before_remote_and_preserves_platform(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    assert run_cli(
        tmp_path,
        "init",
        "--root",
        str(project),
    ).returncode == 0
    old_source = project / "corpus" / "raw" / "old.md"
    old_source.write_text("即将删除的旧语料。", encoding="utf-8")
    store = StateStore(project)
    old_item = scan_corpus(project, project / "corpus")[0]
    store.stage_files([old_item])
    lightrag_config = {
        "mode": "service",
        "base_url": "http://127.0.0.1:9",
        "workspace": "deletion_gate",
    }
    partition_id, fingerprint = store.ensure_partition(lightrag_config)
    binding_id = store.mark_submission_started(
        source_path=old_item.path,
        sha256=old_item.sha256,
        partition_id=partition_id,
        backend_fingerprint=fingerprint,
    )
    store.mark_submission_acknowledged(
        binding_id,
        track_id="old-track",
    )
    store.mark_binding_observation(
        binding_id,
        remote_status=RemoteStatus.PROCESSED,
        action_gate=ActionGate.OPEN,
        gate_reason=None,
        chunk_count=1,
    )

    old_source.unlink()
    new_source = project / "corpus" / "raw" / "new.md"
    new_source.write_text("保留的新语料。", encoding="utf-8")
    index = project / "artifacts" / "wiki" / "wiki-src" / "index.md"
    index.write_text(
        "---\ntitle: 首页\ntype: index\nsources:\n"
        "  - corpus/raw/new.md\n---\n\n# 首页\n\n保留的新语料。\n",
        encoding="utf-8",
    )
    project_json = project / "project.json"
    config = json.loads(project_json.read_text(encoding="utf-8"))
    config["lightrag"].update(lightrag_config)
    project_json.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    platform_marker = project / "artifacts" / "platform" / "previous.txt"
    platform_marker.parent.mkdir(parents=True, exist_ok=True)
    platform_marker.write_text(
        "previous complete platform",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "EVO_WIKI_QUERY_AUDIT_KEY",
        "0123456789abcdef0123456789abcdef",
    )

    result = run_cli(
        tmp_path,
        "generate",
        "--root",
        str(project),
        "--json",
    )

    assert result.returncode == 6
    payload = json.loads(result.stdout)
    assert payload["error_code"] == "GENERATION_REBUILD_REQUIRED"
    assert platform_marker.read_text(encoding="utf-8") == (
        "previous complete platform"
    )
    assert not (
        project
        / "artifacts"
        / "lightrag"
        / "reports"
        / "lightrag-report.json"
    ).exists()



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


def test_run_appends_parseable_workflow_events(tmp_path: Path):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    (project / "corpus" / "raw" / "doc.md").write_text("workflow log", encoding="utf-8")

    first = run_cli(tmp_path, "run", "--root", str(project), "--lane", "wiki")
    second = run_cli(tmp_path, "run", "--root", str(project), "--lane", "wiki")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    journals = read_run_journals(project)
    assert len(journals) == 2
    assert all(
        [event["event_type"] for event in events]
        == ["orchestration.run_started", "orchestration.run_completed"]
        for events in journals
    )
    assert all([event["sequence_no"] for event in events] == [1, 2] for events in journals)
    assert all(events[0]["previous_event_hash"] is None for events in journals)
    assert all(
        events[1]["previous_event_hash"] == events[0]["event_hash"]
        for events in journals
    )
    assert all(
        events[0]["safe_payload"]["selected_lanes"] == ["wiki"]
        for events in journals
    )
    assert all(events[1]["safe_payload"]["exit_code"] == 0 for events in journals)
    verification = run_cli(
        tmp_path,
        "logs",
        "verify",
        "--root",
        str(project),
    )
    assert verification.returncode == 0, verification.stderr
    assert json.loads(verification.stdout)["status"] == "ok"


def test_run_uses_configured_journal_rotation_limits(tmp_path: Path):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    project_config = project / "project.json"
    config = json.loads(project_config.read_text(encoding="utf-8"))
    config["journal"] = {
        "max_events_per_file": 1,
        "max_bytes_per_file": 65536,
    }
    project_config.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    result = run_cli(tmp_path, "run", "--root", str(project), "--lane", "wiki")
    assert result.returncode == 0, result.stderr

    event_files = read_run_event_files(project)
    assert [path.name for path in event_files] == [
        "events-000001.jsonl",
        "events-000002.jsonl",
    ]
    first = [json.loads(line) for line in event_files[0].read_text(encoding="utf-8").splitlines()]
    second = [json.loads(line) for line in event_files[1].read_text(encoding="utf-8").splitlines()]
    assert first[0]["sequence_no"] == 1
    assert second[0]["sequence_no"] == 2
    assert second[0]["previous_event_hash"] == first[0]["event_hash"]
    verification = run_cli(tmp_path, "logs", "verify", "--root", str(project))
    assert verification.returncode == 0, verification.stderr
    assert json.loads(verification.stdout)["status"] == "ok"


def test_lightrag_dry_run_log_excludes_secrets_and_query(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    (project / "corpus" / "raw" / "doc.md").write_text("LightRAG input", encoding="utf-8")
    monkeypatch.setenv("LIGHTRAG_API_KEY", "do-not-log-api-key")
    monkeypatch.setenv("LIGHTRAG_BEARER_TOKEN", "do-not-log-bearer-token")

    result = run_cli(
        tmp_path,
        "run",
        "--root",
        str(project),
        "--lane",
        "lightrag",
        "--lightrag-dry-run",
        "--smoke-query",
        "do-not-log-query",
    )
    assert result.returncode == 0, result.stderr

    journals = read_run_journals(project)
    assert len(journals) == 1
    events = journals[0]
    log_text = json.dumps(events, ensure_ascii=False)
    assert [event["event_type"] for event in events] == [
        "orchestration.run_started",
        "orchestration.run_completed",
    ]
    assert all(
        event["safe_payload"]["selected_lanes"] == ["lightrag"]
        for event in events
    )
    assert "do-not-log-api-key" not in log_text
    assert "do-not-log-bearer-token" not in log_text
    assert "do-not-log-query" not in log_text
    assert "doc.md" not in log_text


def test_run_logs_wiki_health_failure_exit_code(tmp_path: Path):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    index = project / "artifacts" / "wiki" / "wiki-src" / "index.md"
    index.write_text(index.read_text(encoding="utf-8") + "\n[[missing-page]]\n", encoding="utf-8")

    result = run_cli(tmp_path, "run", "--root", str(project), "--lane", "wiki")
    assert result.returncode == 3, result.stderr

    journals = read_run_journals(project)
    assert len(journals) == 1
    events = journals[0]
    finished = events[-1]
    assert finished["event_type"] == "orchestration.run_failed"
    assert finished["status"] == "FAILED"
    assert finished["safe_payload"]["exit_code"] == 3
    assert finished["safe_payload"]["error_code"] == "WIKI_HEALTH_FAILED"


def test_run_logs_lightrag_failure_without_remote_call(tmp_path: Path):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    (project / "corpus" / "raw" / "doc.md").write_text(
        "LightRAG input",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "run",
        "--root",
        str(project),
        "--lane",
        "lightrag",
    )

    assert result.returncode == 6
    journals = read_run_journals(project)
    assert len(journals) == 1
    finished = journals[0][-1]
    assert finished["event_type"] == "orchestration.run_failed"
    assert finished["safe_payload"]["exit_code"] == 6
    assert finished["safe_payload"]["error_code"] == "LIGHTRAG_BUILD_FAILED"
    verification = run_cli(
        tmp_path,
        "logs",
        "verify",
        "--root",
        str(project),
    )
    assert verification.returncode == 0
    assert json.loads(verification.stdout)["status"] == "ok"


def test_logs_migrate_legacy_cli_is_explicit_and_idempotent(tmp_path: Path):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    logs_root = project / "artifacts" / "logs"
    logs_root.mkdir(parents=True)
    legacy = logs_root / "evo-wiki-events.jsonl"
    original = (
        json.dumps(
            {
                "event": "run_started",
                "status": "running",
                "selected_lanes": ["wiki"],
                "change_set": {"added": ["private.md"]},
                "query": "SYNTHETIC_SECRET_DO_NOT_COPY",
            }
        )
        + "\n"
    )
    legacy.write_text(original, encoding="utf-8")

    dry_run = run_cli(
        tmp_path,
        "logs",
        "migrate-legacy",
        "--root",
        str(project),
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert json.loads(dry_run.stdout)["result"] == "ready"
    assert legacy.exists()

    applied = run_cli(
        tmp_path,
        "logs",
        "migrate-legacy",
        "--root",
        str(project),
        "--apply",
    )
    assert applied.returncode == 0, applied.stderr
    applied_report = json.loads(applied.stdout)
    assert applied_report["result"] == "applied"
    assert not legacy.exists()
    migrated = (
        logs_root
        / "runs"
        / applied_report["target_run_id"]
        / "events-000001.jsonl"
    ).read_text(encoding="utf-8")
    assert "SYNTHETIC_SECRET_DO_NOT_COPY" not in migrated
    assert "private.md" not in migrated

    repeated = run_cli(
        tmp_path,
        "logs",
        "migrate-legacy",
        "--root",
        str(project),
        "--apply",
    )
    assert repeated.returncode == 0
    assert json.loads(repeated.stdout)["result"] == "already_applied"


def test_doctor_fails_when_lightrag_base_url_missing(tmp_path: Path):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0

    result = run_cli(tmp_path, "doctor", "--root", str(project))
    assert result.returncode == 4, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "failed"
    names = {c["name"] for c in report["checks"]}
    assert "lightrag_config" in names
    cfg_check = next(c for c in report["checks"] if c["name"] == "lightrag_config")
    assert cfg_check["status"] == "failed"
    # 不应泄漏 secret，只暴露是否配置
    assert "auth" in cfg_check
    assert set(cfg_check["auth"].keys()) == {"api_key_configured", "bearer_token_configured"}
    # documents.jsonl 不存在应是 warning，不应触发 failed
    input_check = next(c for c in report["checks"] if c["name"] == "lightrag_input")
    assert input_check["status"] == "warning"


def test_doctor_passes_when_base_url_configured(tmp_path: Path):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    # 在 project.json 里配置 base_url
    import json as _json
    project_json = project / "project.json"
    data = _json.loads(project_json.read_text(encoding="utf-8"))
    data["lightrag"]["base_url"] = "http://127.0.0.1:9621"
    data["lightrag"]["workspace"] = "evo_wiki"
    project_json.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (project / "corpus" / "raw").mkdir(parents=True, exist_ok=True)
    (project / "corpus" / "raw" / "doc.md").write_text("hello", encoding="utf-8")

    result = run_cli(tmp_path, "doctor", "--root", str(project))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "ok"
    cfg_check = next(c for c in report["checks"] if c["name"] == "lightrag_config")
    assert cfg_check["status"] == "ok"
    assert cfg_check["workspace"] == "evo_wiki"
    # documents.jsonl 仍未准备，应只 warning，不应让整体失败
    input_check = next(c for c in report["checks"] if c["name"] == "lightrag_input")
    assert input_check["status"] == "warning"


def test_doctor_rejects_invalid_local_batch_constraint(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("LIGHTRAG_BASE_URL", raising=False)
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    project_json = project / "project.json"
    data = json.loads(project_json.read_text(encoding="utf-8"))
    data["lightrag"]["base_url"] = "http://127.0.0.1:9621"
    data["lightrag"]["workspace"] = "evo_wiki"
    data["lightrag"]["embedding"]["batch_size"] = 11
    project_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_cli(tmp_path, "doctor", "--root", str(project))

    assert result.returncode == 4, result.stderr
    report = json.loads(result.stdout)
    config_check = next(c for c in report["checks"] if c["name"] == "lightrag_config")
    assert config_check["status"] == "failed"
    assert "configuration is invalid" in config_check["detail"]


def test_doctor_check_service_discovers_capabilities(tmp_path: Path, monkeypatch):
    requested_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            requested_paths.append(self.path)
            if self.path == "/health":
                body = {
                    "status": "healthy",
                    "core_version": "1.5.5",
                    "api_version": "1.0",
                    "configuration": {
                        "workspace": "department_public",
                        "storage_workspaces": {"graph_storage": "department_public"},
                        "embedding_batch_num": 8,
                        "enable_rerank": True,
                        "parser_routing": "pdf:mineru",
                    },
                }
            elif self.path == "/openapi.json":
                body = {
                    "components": {
                        "schemas": {
                            "QueryRequest": {
                                    "properties": {
                                        "include_chunk_content": {
                                            "type": "boolean"
                                        },
                                        "conversation_history": {
                                            "type": "array"
                                        },
                                        "mode": {
                                            "type": "string",
                                            "enum": [
                                                "mix",
                                                "hybrid",
                                                "bypass",
                                            ],
                                        },
                                    }
                            }
                        }
                    },
                    "paths": {
                        "/graphs": {"get": {}},
                        "/documents/track_status/{track_id}": {"get": {}},
                        "/documents/delete_document": {"delete": {}},
                        "/documents/paginated": {"post": {}},
                        "/documents/pipeline_status": {"get": {}},
                    },
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

    monkeypatch.delenv("LIGHTRAG_BASE_URL", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        project = tmp_path / "project"
        assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
        project_json = project / "project.json"
        data = json.loads(project_json.read_text(encoding="utf-8"))
        data["lightrag"]["base_url"] = f"http://127.0.0.1:{server.server_port}"
        data["lightrag"]["workspace"] = "department_public"
        project_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        result = run_cli(
            tmp_path,
            "doctor",
            "--root",
            str(project),
            "--check-service",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    service_check = next(c for c in report["checks"] if c["name"] == "lightrag_service")
    assert service_check["status"] == "ok"
    assert service_check["warnings"] == []
    assert service_check["capabilities"] == {
        "core_version": "1.5.5",
        "api_version": "1.0",
        "authenticated_health": True,
        "openapi_available": True,
        "expected_workspace": "department_public",
        "workspace": "department_public",
        "workspace_matches": True,
        "storage_workspaces": {"graph_storage": "department_public"},
        "storage_workspaces_available": True,
        "storage_workspaces_match": True,
        "requested_embedding_batch_size": 8,
        "remote_embedding_batch_size": 8,
        "embedding_batch_matches": True,
        "rerank_enabled": True,
        "parser_routing_available": True,
                "supports_chunk_content": True,
                "supports_conversation_history": True,
                "supports_bypass": True,
                "supports_graph_subgraph": True,
        "supports_track_status": True,
        "supports_document_delete": True,
        "supports_document_inventory": True,
        "supports_pipeline_status": True,
    }
    assert requested_paths == ["/health", "/openapi.json"]


def test_doctor_requires_explicit_workspace(tmp_path: Path):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    project_json = project / "project.json"
    data = json.loads(project_json.read_text(encoding="utf-8"))
    data["lightrag"]["base_url"] = "http://127.0.0.1:9621"
    project_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_cli(tmp_path, "doctor", "--root", str(project))

    assert result.returncode == 4
    report = json.loads(result.stdout)
    config_check = next(c for c in report["checks"] if c["name"] == "lightrag_config")
    assert config_check["status"] == "failed"
    assert "workspace" in config_check["detail"]


@pytest.mark.parametrize("workspace", ["department-public", "with space", "../escape", "部门"])
def test_doctor_rejects_invalid_workspace(tmp_path: Path, workspace: str):
    project = tmp_path / "project"
    assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
    project_json = project / "project.json"
    data = json.loads(project_json.read_text(encoding="utf-8"))
    data["lightrag"]["base_url"] = "http://127.0.0.1:9621"
    data["lightrag"]["workspace"] = workspace
    project_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_cli(tmp_path, "doctor", "--root", str(project))

    assert result.returncode == 4
    report = json.loads(result.stdout)
    config_check = next(c for c in report["checks"] if c["name"] == "lightrag_config")
    assert config_check["status"] == "failed"


def test_evidence_subgraph_query_cli_is_retrieval_only(tmp_path: Path, monkeypatch):
    requested_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            requested_paths.append(self.path)
            if self.path == "/health":
                body = {
                    "status": "healthy",
                    "configuration": {
                        "workspace": "evo_wiki",
                        "storage_workspaces": {"graph_storage": "evo_wiki"},
                    },
                }
            elif self.path == "/openapi.json":
                body = {
                    "components": {"schemas": {}},
                    "paths": {"/graphs": {"get": {}}},
                }
            elif self.path.startswith("/graphs?"):
                body = {
                    "nodes": [
                        {
                            "id": "韩永仁",
                            "labels": ["韩永仁"],
                            "properties": {
                                "description": "not evidence",
                                "source_id": "remote-a",
                                "file_path": "102_韩永仁故意伤害案.txt",
                            },
                        }
                    ],
                    "edges": [],
                    "is_truncated": False,
                }
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002 - stdlib handler API
            return

    monkeypatch.delenv("LIGHTRAG_BASE_URL", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        project = tmp_path / "project"
        assert run_cli(tmp_path, "init", "--root", str(project)).returncode == 0
        project_json = project / "project.json"
        project_data = json.loads(project_json.read_text(encoding="utf-8"))
        project_data["lightrag"]["base_url"] = f"http://127.0.0.1:{server.server_port}"
        project_data["lightrag"]["workspace"] = "evo_wiki"
        project_json.write_text(
            json.dumps(project_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        documents = [
            {
                "id": "doc-a",
                "source_path": "corpus/raw/102_韩永仁故意伤害案.txt",
                "sha256": "sha-a",
                "text": "韩永仁留在现场等待公安人员，并如实供述，因此认定为自首。",
            },
            {
                "id": "doc-b",
                "source_path": "corpus/raw/other.txt",
                "sha256": "sha-b",
                "text": "无关的其他材料。",
            },
        ]
        input_path = project / "artifacts" / "lightrag" / "input" / "documents.jsonl"
        input_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in documents),
            encoding="utf-8",
        )
        ledger_path = (
            project / "artifacts" / "lightrag" / "state" / "lightrag-import-ledger.json"
        )
        ledger_path.write_text(
            json.dumps(
                {
                    "service": {"workspace": "evo_wiki"},
                    "documents": {
                        "doc-a": {"sha256": "sha-a", "service_track_id": "track-a"},
                        "doc-b": {"sha256": "sha-b", "service_track_id": "track-b"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = run_cli(
            tmp_path,
            "query",
            "--root",
            str(project),
            "--skill",
            "evidence-subgraph",
            "--only-context",
            "--query",
            "韩永仁为什么认定自首？",
            "--seed",
            "韩永仁",
            "--explain-retrieval",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["mode"] == "retrieval_only"
    assert output["generation_enabled"] is False
    assert output["scope"]["candidate_reduction_ratio"] > 0
    assert output["evidence"]
    assert not any(path == "/query" for path in requested_paths)

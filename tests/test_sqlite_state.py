from __future__ import annotations

import json
import shutil
import sqlite3
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError

import pytest

from evo_wiki.cli import main as cli_main
from evo_wiki.corpus import persist_corpus_state, scan_corpus
from evo_wiki.lightrag_lane import (
    LightRAGBuildError,
    build_lightrag,
    prepare_lightrag_input,
)
from evo_wiki.journal import verify_logs_root
from evo_wiki.paths import ProjectPaths
from evo_wiki.state import (
    ActionGate,
    RemoteStatus,
    ReplacementOperationService,
    ReplacementPlanner,
    StateBackupService,
    StateExporter,
    StateMigrator,
    StateReconciler,
    StateSchemaMigrator,
    StateStore,
    StateVerifier,
)
from evo_wiki.state.contracts import StateError
from evo_wiki.state import operations as state_operations

from test_cli_smoke import run_cli


def _tree_snapshot(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        )
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    return directories, files


def _durable_tree_snapshot(
    root: Path,
) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories, files = _tree_snapshot(root)
    return directories, {
        path: content
        for path, content in files.items()
        if not path.endswith(("-wal", "-shm"))
    }


def _legacy_workspace(tmp_path: Path, *, change_source_after_state: bool = False) -> Path:
    project = tmp_path / "legacy-project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr

    project_json = project / "project.json"
    config = json.loads(project_json.read_text(encoding="utf-8"))
    config.pop("state", None)
    project_json.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    wiki_json = project / "wiki.json"
    wiki_config = json.loads(wiki_json.read_text(encoding="utf-8"))
    wiki_config.pop("content_contract_version", None)
    wiki_json.write_text(
        json.dumps(wiki_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.rmtree(project / "artifacts" / "state")

    source = project / "corpus" / "raw" / "legacy.md"
    source.write_text("# Legacy\n\nOriginal bytes.\n", encoding="utf-8")
    files = scan_corpus(project, project / "corpus")
    for path in (
        project / "artifacts" / "corpus-state.json",
        project / "artifacts" / "wiki" / "state" / "corpus-state.json",
        project / "artifacts" / "lightrag" / "state" / "corpus-state.json",
    ):
        persist_corpus_state(files, path)
    ledger = {
        "documents": {
            "corpus__raw__legacy.md": {
                "source_path": files[0].path,
                "sha256": files[0].sha256,
                "service_track_id": "legacy-track",
                "submitted_at": "2026-01-01T00:00:00Z",
                "processed_at": "2026-01-01T00:01:00Z",
            }
        },
        "service": {
            "mode": "service",
            "workspace": "legacy",
            "base_url": "http://127.0.0.1:9621",
        },
    }
    ledger_path = (
        project
        / "artifacts"
        / "lightrag"
        / "state"
        / "lightrag-import-ledger.json"
    )
    ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if change_source_after_state:
        source.write_text("# Legacy\n\nNew bytes are not historical bytes.\n", encoding="utf-8")
    return project


def _blocked_binding(project: Path) -> tuple[StateStore, str]:
    source = project / "corpus" / "raw" / "binding.md"
    source.write_text("binding source", encoding="utf-8")
    item = scan_corpus(project, project / "corpus")[0]
    store = StateStore(project)
    store.initialize()
    partition_id, fingerprint = store.ensure_partition(
        {
            "mode": "service",
            "workspace": "reconcile",
            "base_url": "http://127.0.0.1:9621",
        }
    )
    binding_id = store.import_legacy_binding(
        source_path=item.path,
        sha256=item.sha256,
        size=item.size,
        suffix=item.suffix,
        text_like=item.text_like,
        track_id="track-1",
        submitted_at=None,
        processed_at=None,
        partition_id=partition_id,
        backend_fingerprint=fingerprint,
    )
    return store, binding_id


def _replacement_conflict(
    tmp_path: Path,
) -> tuple[Path, StateStore, dict, str, str]:
    project = tmp_path / "replacement-project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    project_config_path = project / "project.json"
    project_config = json.loads(
        project_config_path.read_text(encoding="utf-8")
    )
    project_config["query_gateway"]["mode"] = "disabled"
    project_config_path.write_text(
        json.dumps(project_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    config = {
        "mode": "service",
        "base_url": "http://127.0.0.1:9621",
        "workspace": "replacement",
    }
    source = project / "corpus" / "raw" / "replace.md"
    source.write_text("old remote bytes", encoding="utf-8")
    store = StateStore(project)
    old_item = scan_corpus(project, project / "corpus")[0]
    store.stage_files([old_item])
    partition_id, fingerprint = store.ensure_partition(config)
    old_binding = store.mark_submission_started(
        source_path=old_item.path,
        sha256=old_item.sha256,
        partition_id=partition_id,
        backend_fingerprint=fingerprint,
    )
    store.mark_submission_acknowledged(
        old_binding,
        track_id="old-track",
    )
    store.mark_binding_observation(
        old_binding,
        remote_status=RemoteStatus.PROCESSED,
        action_gate=ActionGate.OPEN,
        gate_reason=None,
        chunk_count=3,
    )

    source.write_text("new target bytes", encoding="utf-8")
    target_item = scan_corpus(project, project / "corpus")[0]
    store.stage_files([target_item])
    target_binding = store.mark_submission_started(
        source_path=target_item.path,
        sha256=target_item.sha256,
        partition_id=partition_id,
        backend_fingerprint=fingerprint,
    )
    store.mark_submission_conflict(target_binding)
    return project, store, config, old_binding, target_binding


def _install_replacement_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace: str = "replacement",
    paths: dict | None = None,
    pipeline: dict | None = None,
    documents: list[dict] | None = None,
    pagination_override: dict | None = None,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    openapi_paths = (
        paths
        if paths is not None
        else {
            "/documents/delete_document": {"delete": {}},
            "/documents/paginated": {"post": {}},
            "/documents/pipeline_status": {"get": {}},
        }
    )
    pipeline_payload = (
        pipeline
        if pipeline is not None
        else {
            "busy": False,
            "scanning": False,
            "scanning_exclusive": False,
            "destructive_busy": False,
            "pending_enqueues": 0,
        }
    )
    document_payload = (
        documents
        if documents is not None
        else [
            {
                "id": "remote-doc",
                "file_path": "replace.md",
                "status": "processed",
                "track_id": "old-track",
                "chunks_count": 3,
            }
        ]
    )

    class ReplacementClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def request_json(self, method, path, payload=None):
            calls.append((method, path))
            if (method, path) == ("GET", "/health"):
                return {
                    "status": "healthy",
                    "configuration": {
                        "workspace": workspace,
                        "storage_workspaces": {
                            "graph_storage": workspace,
                        },
                    },
                }
            if (method, path) == ("GET", "/openapi.json"):
                return {
                    "components": {"schemas": {}},
                    "paths": openapi_paths,
                }
            if (method, path) == (
                "GET",
                "/documents/pipeline_status",
            ):
                return pipeline_payload
            if (method, path) == ("POST", "/documents/paginated"):
                pagination = {
                    "page": payload["page"],
                    "page_size": payload["page_size"],
                    "total_count": len(document_payload),
                    "total_pages": 1 if document_payload else 0,
                    "has_next": False,
                    "has_prev": False,
                    **(pagination_override or {}),
                }
                return {
                    "documents": document_payload,
                    "pagination": pagination,
                    "status_counts": {},
                }
            raise AssertionError((method, path, payload))

    monkeypatch.setattr(
        "evo_wiki.lightrag_lane.LightRAGServiceClient",
        ReplacementClient,
    )
    return calls


def _install_replacement_executor_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_failed: bool = False,
    smoke_passes: bool = True,
    delete_raises: bool = False,
    delete_interrupts: bool = False,
) -> tuple[list[tuple[str, str]], dict]:
    calls: list[tuple[str, str]] = []
    state = {
        "documents": [
            {
                "id": "remote-doc",
                "file_path": "replace.md",
                "status": "processed",
                "track_id": "old-track",
                "chunks_count": 3,
            }
        ],
        "target_failed": target_failed,
    }

    class ReplacementExecutorClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def post_json(self, path, payload):
            return self.request_json("POST", path, payload)

        def request_json(self, method, path, payload=None):
            calls.append((method, path))
            if (method, path) == ("GET", "/health"):
                return {
                    "status": "healthy",
                    "configuration": {
                        "workspace": "replacement",
                        "storage_workspaces": {
                            "graph_storage": "replacement",
                        },
                    },
                }
            if (method, path) == ("GET", "/openapi.json"):
                return {
                    "components": {"schemas": {}},
                    "paths": {
                        "/documents/delete_document": {"delete": {}},
                        "/documents/paginated": {"post": {}},
                        "/documents/pipeline_status": {"get": {}},
                        "/documents/track_status/{track_id}": {
                            "get": {}
                        },
                        "/documents/text": {"post": {}},
                        "/query": {"post": {}},
                    },
                }
            if (method, path) == (
                "GET",
                "/documents/pipeline_status",
            ):
                return {
                    "busy": False,
                    "scanning": False,
                    "scanning_exclusive": False,
                    "destructive_busy": False,
                    "pending_enqueues": 0,
                }
            if (method, path) == ("POST", "/documents/paginated"):
                documents = list(state["documents"])
                return {
                    "documents": documents,
                    "pagination": {
                        "page": payload["page"],
                        "page_size": payload["page_size"],
                        "total_count": len(documents),
                        "total_pages": 1 if documents else 0,
                        "has_next": False,
                        "has_prev": False,
                    },
                    "status_counts": {},
                }
            if (method, path) == (
                "DELETE",
                "/documents/delete_document",
            ):
                if delete_interrupts:
                    raise KeyboardInterrupt
                if delete_raises:
                    raise RuntimeError(
                        "private delete response and credential"
                    )
                doc_ids = set(payload["doc_ids"])
                state["documents"] = [
                    item
                    for item in state["documents"]
                    if item["id"] not in doc_ids
                ]
                return {"status": "deletion_started"}
            if (method, path) == ("POST", "/documents/text"):
                if payload["text"] == "new target bytes":
                    track_id = "target-track"
                    status = "failed" if state["target_failed"] else "processed"
                    chunks = 0 if state["target_failed"] else 4
                    doc_id = "target-doc"
                else:
                    track_id = "rollback-track"
                    status = "processed"
                    chunks = 3
                    doc_id = "rollback-doc"
                state["documents"] = [
                    {
                        "id": doc_id,
                        "file_path": "replace.md",
                        "status": status,
                        "track_id": track_id,
                        "chunks_count": chunks,
                    }
                ]
                return {"status": "success", "track_id": track_id}
            if method == "GET" and path.startswith(
                "/documents/track_status/"
            ):
                track_id = path.rsplit("/", 1)[-1]
                matching = [
                    item
                    for item in state["documents"]
                    if item["track_id"] == track_id
                ]
                return {
                    "track_id": track_id,
                    "total_count": len(matching),
                    "documents": [
                        {
                            "track_id": track_id,
                            "status": item["status"],
                            "chunks_count": item["chunks_count"],
                        }
                        for item in matching
                    ],
                }
            if (method, path) == ("POST", "/query"):
                return {
                    "response": "private answer is never journaled",
                    "references": (
                        [
                            {
                                "file_path": "corpus/raw/replace.md",
                                "content": ["new target bytes"],
                            }
                        ]
                        if smoke_passes
                        else []
                    ),
                }
            raise AssertionError((method, path, payload))

    monkeypatch.setattr(
        "evo_wiki.lightrag_lane.LightRAGServiceClient",
        ReplacementExecutorClient,
    )
    return calls, state


def _downgrade_empty_workspace_to_schema_v1(project: Path) -> None:
    database = (
        project / "artifacts" / "state" / "evo_wiki.sqlite3"
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE notification_attempt")
        connection.execute("DROP TABLE notification_outbox")
        connection.execute("DROP TABLE audit_event")
        connection.execute("DROP TABLE audit_item")
        connection.execute("DROP TABLE gateway_instance")
        connection.execute("DROP TABLE maintenance_fence")
        connection.execute("DROP TABLE query_run")
        connection.execute("DROP INDEX idx_source_revision_one_active")
        connection.execute(
            "DROP INDEX idx_replacement_operation_status"
        )
        connection.execute(
            "DROP INDEX idx_replacement_operation_active_source"
        )
        connection.execute("DROP TABLE replacement_operation")
        connection.execute("DELETE FROM schema_meta WHERE version = 5")
        connection.execute("DELETE FROM schema_meta WHERE version = 4")
        connection.execute("DELETE FROM schema_meta WHERE version = 3")
        connection.execute("DELETE FROM schema_meta WHERE version = 2")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


def _downgrade_empty_workspace_to_schema_v4(project: Path) -> None:
    database = (
        project / "artifacts" / "state" / "evo_wiki.sqlite3"
    )
    connection = sqlite3.connect(database)
    try:
        for column in (
            "generation_status",
            "answer_origin",
            "evidence_status",
            "review_status",
        ):
            connection.execute(
                f"ALTER TABLE query_run DROP COLUMN {column}"
            )
        connection.execute("DELETE FROM schema_meta WHERE version = 5")
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
    finally:
        connection.close()


def _prepare_generate_wiki(project: Path) -> None:
    source = project / "corpus" / "raw" / "generate.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("自动生成平台的测试资料。", encoding="utf-8")
    index = project / "artifacts" / "wiki" / "wiki-src" / "index.md"
    index.write_text(
        "---\ntitle: 首页\ntype: index\nsources:\n"
        "  - corpus/raw/generate.md\n---\n\n"
        "# 首页\n\n自动生成平台的可交付内容。\n\n"
        "- [[生成测试来源]]\n",
        encoding="utf-8",
    )
    source_page = (
        project
        / "artifacts"
        / "wiki"
        / "wiki-src"
        / "sources"
        / "generate.md"
    )
    source_page.parent.mkdir(parents=True, exist_ok=True)
    source_page.write_text(
        "---\ntitle: 生成测试来源\ntype: source\nsources:\n"
        "  - corpus/raw/generate.md\n---\n\n"
        "# 生成测试来源\n\n## 摘要\n\n测试摘要。\n\n"
        "## 原文内容\n\n自动生成平台的测试资料。\n",
        encoding="utf-8",
    )


def test_generate_auto_upgrades_sqlite_schema_once(tmp_path: Path):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    _prepare_generate_wiki(project)
    _downgrade_empty_workspace_to_schema_v1(project)

    first = run_cli(
        tmp_path,
        "generate",
        "--root",
        str(project),
        "--target",
        "wiki",
        "--json",
    )
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert first_payload["state"]["database_schema_before"] == 1
    assert first_payload["state"]["database_schema_after"] == 5
    assert first_payload["state"]["backup_id"]
    backups = list(
        (project / "artifacts" / "state" / "backups").glob("*.sqlite3")
    )
    assert len(backups) == 1

    repeated = run_cli(
        tmp_path,
        "generate",
        "--root",
        str(project),
        "--target",
        "wiki",
        "--json",
    )
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["state"]["status"] == "already_current"
    assert len(
        list(
            (project / "artifacts" / "state" / "backups").glob(
                "*.sqlite3"
            )
        )
    ) == 1


def test_generate_auto_cuts_over_legacy_json_state(tmp_path: Path):
    project = _legacy_workspace(tmp_path)
    _prepare_generate_wiki(project)

    result = run_cli(
        tmp_path,
        "generate",
        "--root",
        str(project),
        "--target",
        "wiki",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"]["backend_before"] == "legacy_json"
    assert payload["state"]["backend_after"] == "sqlite"
    assert payload["state"]["database_schema_after"] == 5
    assert payload["state"]["legacy_backup_created"] is True
    persisted = json.loads(
        (project / "project.json").read_text(encoding="utf-8")
    )
    assert persisted["state"]["backend"] == "sqlite"
    assert list(
        (project / "artifacts" / "migration-backup").glob(
            "*-json-to-sqlite/manifest.json"
        )
    )


def test_migration_dry_run_is_zero_workspace_write(tmp_path: Path):
    project = _legacy_workspace(tmp_path)
    before = _tree_snapshot(project)

    result = run_cli(
        tmp_path,
        "migrate-state",
        "--root",
        str(project),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["mode"] == "dry_run"
    assert payload["workspace_mutated"] is False
    assert payload["migratable"] is True
    assert _tree_snapshot(project) == before


def test_migration_preview_rejects_duplicate_json_keys_without_writes(
    tmp_path: Path,
):
    project = _legacy_workspace(tmp_path)
    ledger = (
        project
        / "artifacts"
        / "lightrag"
        / "state"
        / "lightrag-import-ledger.json"
    )
    ledger.write_text(
        '{"documents": {}, "documents": {}}\n',
        encoding="utf-8",
    )
    before = _tree_snapshot(project)

    result = run_cli(
        tmp_path,
        "migrate-state",
        "--root",
        str(project),
        "--json",
    )

    assert result.returncode == 5
    payload = json.loads(result.stdout)
    assert payload["error_code"] == "STATE_LEGACY_JSON_INVALID"
    assert payload["state_committed"] is False
    assert _tree_snapshot(project) == before


def test_init_does_not_auto_migrate_an_existing_legacy_workspace(
    tmp_path: Path,
):
    project = _legacy_workspace(tmp_path)

    result = run_cli(tmp_path, "init", "--root", str(project))

    assert result.returncode == 0, result.stderr
    assert "Existing legacy state was preserved" in result.stdout
    assert not (
        project / "artifacts" / "state" / "evo_wiki.sqlite3"
    ).exists()
    config = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert "state" not in config


def test_apply_is_true_noop_for_an_already_active_sqlite_workspace(
    tmp_path: Path,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store = StateStore(project)
    before_seq = store.state_commit_seq()
    before_export_seq = store.last_exported_state_commit_seq()

    result = StateMigrator(project).apply()

    assert result.status == "already_applied"
    assert result.workspace_mutated is False
    assert not (project / "artifacts" / "state" / "migration.lock").exists()
    assert store.state_commit_seq() == before_seq
    assert store.last_exported_state_commit_seq() == before_export_seq


def test_migration_apply_preserves_backup_and_marks_legacy_bindings_blocked(
    tmp_path: Path,
):
    project = _legacy_workspace(tmp_path)
    original_project = (project / "project.json").read_bytes()

    result = run_cli(
        tmp_path,
        "migrate-state",
        "--root",
        str(project),
        "--apply",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "applied"
    assert payload["workspace_mutated"] is True
    config = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert config["state"]["backend"] == "sqlite"
    database = project / config["state"]["database"]
    assert database.exists()

    manifests = list(
        (project / "artifacts" / "migration-backup").glob(
            "*-json-to-sqlite/manifest.json"
        )
    )
    assert len(manifests) == 1
    backup_root = manifests[0].parent
    assert (backup_root / "project.json").read_bytes() == original_project

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        binding = connection.execute(
            """
            SELECT remote_status, action_gate, gate_reason
            FROM lightrag_binding
            """
        ).fetchone()
        legacy_run = connection.execute(
            """
            SELECT run_origin, verification_status, side_effects_executed
            FROM lane_run WHERE run_origin = 'LEGACY_MIGRATION' LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    assert dict(binding) == {
        "remote_status": "UNKNOWN",
        "action_gate": "BLOCKED",
        "gate_reason": "LEGACY_UNVERIFIED",
    }
    assert dict(legacy_run) == {
        "run_origin": "LEGACY_MIGRATION",
        "verification_status": "UNVERIFIED",
        "side_effects_executed": 0,
    }


def test_migration_marks_unavailable_historical_bytes_explicitly(tmp_path: Path):
    project = _legacy_workspace(tmp_path, change_source_after_state=True)

    result = run_cli(
        tmp_path,
        "migrate-state",
        "--root",
        str(project),
        "--apply",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    database = project / "artifacts" / "state" / "evo_wiki.sqlite3"
    connection = sqlite3.connect(database)
    try:
        statuses = {
            row[0]
            for row in connection.execute(
                "SELECT snapshot_status FROM source_revision"
            )
        }
    finally:
        connection.close()
    assert statuses == {"UNAVAILABLE_LEGACY"}


def test_migration_preserves_a_global_baseline_without_lane_baselines(
    tmp_path: Path,
):
    project = _legacy_workspace(tmp_path)
    for path in (
        project / "artifacts" / "wiki" / "state" / "corpus-state.json",
        project / "artifacts" / "lightrag" / "state" / "corpus-state.json",
    ):
        persist_corpus_state([], path)
    ledger_path = (
        project
        / "artifacts"
        / "lightrag"
        / "state"
        / "lightrag-import-ledger.json"
    )
    ledger_path.write_text(
        json.dumps({"documents": {}}, indent=2) + "\n",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "migrate-state",
        "--root",
        str(project),
        "--apply",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    exported = json.loads(
        (project / "artifacts" / "corpus-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["path"] for item in exported["files"]] == [
        "corpus/raw/legacy.md"
    ]
    store = StateStore(project)
    assert len(store.latest_lane_files("global")) == 1
    assert store.latest_lane_files("wiki") == []
    assert store.latest_lane_files("lightrag") == []


def test_cutover_resumes_after_database_install_before_config_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = _legacy_workspace(tmp_path)
    migrator = StateMigrator(project)
    activate = state_operations._activate_sqlite_config

    def interrupt_cutover(*_args, **_kwargs):
        raise RuntimeError("injected cutover crash")

    monkeypatch.setattr(
        state_operations,
        "_activate_sqlite_config",
        interrupt_cutover,
    )
    with pytest.raises(RuntimeError, match="injected cutover crash"):
        migrator.apply()

    assert (project / "artifacts" / "state" / "evo_wiki.sqlite3").exists()
    config = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert "state" not in config

    monkeypatch.setattr(state_operations, "_activate_sqlite_config", activate)
    resumed = StateMigrator(project).apply()
    assert resumed.status == "applied"
    config = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert config["state"]["backend"] == "sqlite"


def test_failed_import_removes_temporary_candidate_and_keeps_legacy_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = _legacy_workspace(tmp_path)
    migrator = StateMigrator(project)

    def fail_import(*_args, **_kwargs):
        raise StateError(
            "injected import failure",
            error_code="STATE_MIGRATION_IMPORT_FAILED",
        )

    monkeypatch.setattr(migrator, "_import_inventory", fail_import)
    with pytest.raises(StateError) as captured:
        migrator.apply()

    assert captured.value.error_code == "STATE_MIGRATION_IMPORT_FAILED"
    assert not (
        project / "artifacts" / "state" / "evo_wiki.sqlite3"
    ).exists()
    assert not list(
        (project / "artifacts" / "state").glob(
            ".evo_wiki.*.candidate.sqlite3*"
        )
    )
    config = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert "state" not in config


def test_cutover_finalizes_after_config_switch_before_active_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = _legacy_workspace(tmp_path)
    original_record = StateStore.record_migration

    def interrupt_active_record(self, **kwargs):
        if kwargs["status"] == "SQLITE_ACTIVE":
            raise RuntimeError("injected post-config crash")
        return original_record(self, **kwargs)

    monkeypatch.setattr(
        StateStore,
        "record_migration",
        interrupt_active_record,
    )
    with pytest.raises(RuntimeError, match="injected post-config crash"):
        StateMigrator(project).apply()

    config = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert config["state"]["backend"] == "sqlite"

    monkeypatch.setattr(StateStore, "record_migration", original_record)
    resumed = StateMigrator(project, config["state"]).apply()
    assert resumed.status == "applied"
    store = StateStore(project, config["state"])
    assert store.latest_migration()["status"] == "SQLITE_ACTIVE"
    assert (
        store.last_exported_state_commit_seq()
        == store.state_commit_seq()
    )


def test_export_verify_and_backup_do_not_advance_business_commit_sequence(
    tmp_path: Path,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store = StateStore(project)
    store.initialize()
    before = store.state_commit_seq()

    exported = StateExporter(store).export()
    verified = StateVerifier(store).verify(include_journal=False)
    first = StateBackupService(store).backup()
    second = StateBackupService(store).backup()

    assert exported.state_commit_seq == before
    assert verified.state_commit_seq == before
    assert first.state_commit_seq == before
    assert second.state_commit_seq == before
    assert first.backup_path != second.backup_path
    assert store.state_commit_seq() == before


def test_explicit_schema_upgrade_is_backup_first_and_idempotent(
    tmp_path: Path,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    _downgrade_empty_workspace_to_schema_v1(project)
    store = StateStore(project)
    before = _durable_tree_snapshot(project)

    preview = StateSchemaMigrator(store).plan()

    assert preview.status == "ready"
    assert preview.workspace_mutated is False
    assert preview.database_schema_version == 1
    assert preview.pending_migrations == [
        "0002_replacement_operation",
        "0003_query_governance",
        "0004_notification_outbox",
        "0005_query_delivery_status",
    ]
    assert _durable_tree_snapshot(project) == before

    applied = StateSchemaMigrator(store).apply()
    repeated = StateSchemaMigrator(store).apply()

    assert applied.status == "applied"
    assert applied.database_schema_version == 5
    assert applied.backup_id is not None
    assert applied.backup_sha256 is not None
    assert repeated.status == "already_applied"
    assert repeated.workspace_mutated is False
    assert store.schema_version() == 5
    assert StateVerifier(store).verify(
        include_journal=False
    ).overall_status == "PASS"
    assert len(
        list(
            (
                project / "artifacts" / "state" / "backups"
            ).glob("*.sqlite3")
        )
    ) == 1


def test_schema_v4_to_v5_migration_preserves_historical_query_run(
    tmp_path: Path,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    _downgrade_empty_workspace_to_schema_v4(project)
    store = StateStore(project)
    partition_id, _ = store.ensure_partition(
        {
            "mode": "service",
            "workspace": "legacy-v4",
            "base_url": "http://127.0.0.1:9621",
        }
    )
    store.begin_query_run(
        request_id="qry-v4-history",
        retrieval_partition_id=partition_id,
        principal_hmac="hmac-sha256:" + "1" * 64,
        query_hmac="hmac-sha256:" + "2" * 64,
        request_mode="mix",
        gateway_mode="shadow",
        verification_level="legacy-v4",
        lease_seconds=30,
    )
    store.finish_query_run(
        "qry-v4-history",
        status="ANSWERED",
        verdict_code="LEGACY_PASSED",
        error_code=None,
        reference_count=1,
        active_reference_count=1,
        answer_sha256="sha256:" + "3" * 64,
        citation_set_sha256="sha256:" + "4" * 64,
    )

    preview = StateSchemaMigrator(store).plan()
    applied = StateSchemaMigrator(store).apply()
    historical = store.query_run("qry-v4-history")

    assert preview.pending_migrations == ["0005_query_delivery_status"]
    assert applied.database_schema_version == 5
    assert historical["status"] == "ANSWERED"
    assert historical["generation_status"] is None
    assert historical["answer_origin"] is None
    assert historical["evidence_status"] is None
    assert historical["review_status"] is None


def test_migrate_schema_cli_dry_run_and_apply(
    tmp_path: Path,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    _downgrade_empty_workspace_to_schema_v1(project)
    before = _durable_tree_snapshot(project)

    preview = run_cli(
        tmp_path,
        "state",
        "migrate-schema",
        "--root",
        str(project),
        "--json",
    )
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["status"] == "ready"
    assert _durable_tree_snapshot(project) == before

    applied = run_cli(
        tmp_path,
        "state",
        "migrate-schema",
        "--root",
        str(project),
        "--apply",
        "--json",
    )
    assert applied.returncode == 0, applied.stderr
    payload = json.loads(applied.stdout)
    assert payload["status"] == "applied"
    assert payload["database_schema_version"] == 5
    assert payload["backup_id"] is not None


def test_backup_journal_honors_project_rotation_settings(tmp_path: Path):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr

    StateBackupService(
        StateStore(project),
        {
            "max_events_per_file": 1,
            "max_bytes_per_file": 65536,
        },
    ).backup()

    runs = sorted(
        (project / "artifacts" / "logs" / "runs").glob("backup-*")
    )
    assert len(runs) == 1
    assert sorted(
        path.name for path in runs[0].glob("events-*.jsonl")
    ) == [
        "events-000001.jsonl",
        "events-000002.jsonl",
    ]
    assert verify_logs_root(
        project / "artifacts" / "logs"
    )["status"] == "ok"


def test_idempotent_store_calls_do_not_advance_business_commit_sequence(
    tmp_path: Path,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store = StateStore(project)
    partition_config = {
        "mode": "service",
        "workspace": "stable",
        "base_url": "http://127.0.0.1:9621",
    }
    store.ensure_partition(partition_config)
    after_partition = store.state_commit_seq()
    store.ensure_partition(partition_config)
    assert store.state_commit_seq() == after_partition

    source = project / "corpus" / "raw" / "stable.md"
    source.write_text("stable bytes", encoding="utf-8")
    files = scan_corpus(project, project / "corpus")
    store.stage_files(files)
    after_stage = store.state_commit_seq()
    store.stage_files(files)
    assert store.state_commit_seq() == after_stage


def test_export_failure_reports_committed_truth_without_advancing_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store = StateStore(project)
    before = store.state_commit_seq()

    def fail_export(*_args, **_kwargs):
        raise OSError("injected export failure")

    monkeypatch.setattr(
        state_operations,
        "write_json_atomic",
        fail_export,
    )
    with pytest.raises(StateError) as captured:
        StateExporter(store).export()

    assert captured.value.error_code == "STATE_EXPORT_FAILED"
    assert captured.value.committed is True
    assert captured.value.details == {
        "state_commit_seq": before,
        "export_succeeded": False,
    }
    assert store.state_commit_seq() == before


def test_separate_store_instances_serialize_concurrent_business_writers(
    tmp_path: Path,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    before = StateStore(project).state_commit_seq()

    def create_partition(index: int) -> str:
        store = StateStore(project)
        partition_id, _ = store.ensure_partition(
            {
                "mode": "service",
                "workspace": f"concurrent-{index}",
                "base_url": "http://127.0.0.1:9621",
            }
        )
        return partition_id

    with ThreadPoolExecutor(max_workers=4) as executor:
        partition_ids = list(executor.map(create_partition, range(8)))

    assert len(set(partition_ids)) == 8
    assert StateStore(project).state_commit_seq() == before + 8


def test_reconcile_preview_is_local_zero_write_and_apply_opens_only_processed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store, binding_id = _blocked_binding(project)

    class ProcessedClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def request_json(self, _method, _path):
            return {
                "track_id": "track-1",
                "total_count": 1,
                "documents": [
                    {
                        "track_id": "track-1",
                        "status": "processed",
                        "chunks_count": 2,
                    }
                ],
            }

    monkeypatch.setattr(
        "evo_wiki.lightrag_lane.LightRAGServiceClient",
        ProcessedClient,
    )
    config = {
        "base_url": "http://127.0.0.1:9621",
        "workspace": "reconcile",
    }
    before_seq = store.state_commit_seq()
    preview = StateReconciler(store, config).reconcile()
    assert preview.status == "ready"
    assert preview.workspace_mutated is False
    assert preview.observations[0].observed_remote_status.value == "PROCESSED"
    assert store.state_commit_seq() == before_seq
    assert store.list_lightrag_documents()[
        "corpus__raw__binding.md"
    ]["action_gate"] == "BLOCKED"

    applied = StateReconciler(store, config).reconcile(apply=True)
    assert applied.status == "applied"
    assert applied.workspace_mutated is True
    binding = store.list_lightrag_documents()[
        "corpus__raw__binding.md"
    ]
    assert binding["remote_status"] == "PROCESSED"
    assert binding["action_gate"] == "OPEN"
    assert binding["revision_status"] == "ACTIVE"
    active_seq = store.state_commit_seq()
    store.activate_processed_binding(binding_id)
    assert store.state_commit_seq() == active_seq


def test_reconcile_backend_failure_remains_blocked_and_reports_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from evo_wiki.lightrag_lane import LightRAGBuildError

    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store, _ = _blocked_binding(project)

    class FailingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def request_json(self, _method, _path):
            raise LightRAGBuildError("temporary backend failure")

    monkeypatch.setattr(
        "evo_wiki.lightrag_lane.LightRAGServiceClient",
        FailingClient,
    )
    result = StateReconciler(
        store,
        {
            "base_url": "http://127.0.0.1:9621",
            "workspace": "reconcile",
        },
    ).reconcile(apply=True)

    assert result.status == "failed"
    assert result.error_code == "REMOTE_RECONCILE_FAILED"
    binding = store.list_lightrag_documents()[
        "corpus__raw__binding.md"
    ]
    assert binding["remote_status"] == "UNKNOWN"
    assert binding["action_gate"] == "BLOCKED"
    assert binding["gate_reason"] == "REMOTE_STATUS_UNCONFIRMED"


def test_http_409_is_sanitized_and_binding_remains_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    source = project / "corpus" / "raw" / "conflict.md"
    source.write_text("new conflicting bytes", encoding="utf-8")
    paths = ProjectPaths.from_root(project)
    files = scan_corpus(project, paths.corpus)
    prepare_lightrag_input(paths, files)
    store = StateStore(project)
    store.stage_files(files)

    monkeypatch.setattr(
        "evo_wiki.lightrag_lane.preflight_lightrag_build",
        lambda *_args, **_kwargs: None,
    )
    secret_detail = b'{"detail":"private source and remote internals"}'

    def conflict_urlopen(*_args, **_kwargs):
        raise HTTPError(
            "http://127.0.0.1:9621/documents/text",
            409,
            "Conflict",
            {},
            BytesIO(secret_detail),
        )

    monkeypatch.setattr(
        "evo_wiki.lightrag_lane.urlopen",
        conflict_urlopen,
    )
    with pytest.raises(LightRAGBuildError) as caught:
        build_lightrag(
            paths,
            config={
                "base_url": "http://127.0.0.1:9621",
                "workspace": "conflict",
            },
            state_store=store,
        )

    assert caught.value.failure_code == "REMOTE_HTTP_409"
    assert caught.value.http_status == 409
    report = json.loads(
        (
            paths.lightrag_reports / "lightrag-report.json"
        ).read_text(encoding="utf-8")
    )
    serialized_report = json.dumps(report, ensure_ascii=False)
    assert report["failure_code"] == "REMOTE_HTTP_409"
    assert "private source" not in serialized_report
    assert "remote internals" not in serialized_report
    binding = store.list_lightrag_documents()[
        "corpus__raw__conflict.md"
    ]
    assert binding["remote_status"] == "UNKNOWN"
    assert binding["action_gate"] == "BLOCKED"
    assert binding["gate_reason"] == "REMOTE_HTTP_409"


def test_replacement_plan_ready_is_zero_write_and_never_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project, store, config, old_binding, target_binding = (
        _replacement_conflict(tmp_path)
    )
    calls = _install_replacement_client(monkeypatch)
    before_seq = store.state_commit_seq()
    before_tree = _tree_snapshot(project)

    result = ReplacementPlanner(store, config).plan()

    assert result.status == "ready"
    assert result.workspace_mutated is False
    assert result.delete_attempted is False
    assert result.error_code is None
    assert len(result.plans) == 1
    plan = result.plans[0]
    assert plan.binding_id == target_binding
    assert plan.plan_digest.startswith("sha256:")
    assert plan.review_status == "ready"
    assert plan.blockers == []
    assert plan.remote_document is not None
    assert plan.remote_document.doc_id == "remote-doc"
    assert plan.remote_document.track_id == "old-track"
    assert plan.impact.chunk_count == 3
    assert plan.impact.query_availability_gap is True
    assert plan.rollback.owner_binding_id == old_binding
    assert plan.rollback.available is True
    assert plan.execution_authorized is False
    assert plan.effect_envelope.max_delete_requests == 2
    assert plan.effect_envelope.max_submission_requests == 2
    assert plan.required_approvals == [
        "SQLITE_BACKUP_VERIFIED",
        "DELETE_EXPLICITLY_AUTHORIZED",
        "REPLACEMENT_REVIEW_APPROVED",
    ]
    assert "DELETE_REMOTE_DOCUMENT" in plan.steps
    assert store.state_commit_seq() == before_seq
    assert _tree_snapshot(project) == before_tree
    assert all(method != "DELETE" for method, _ in calls)


@pytest.mark.parametrize(
    ("pipeline", "documents", "expected_blocker"),
    [
        (
            {
                "busy": True,
                "scanning": False,
                "scanning_exclusive": False,
                "destructive_busy": False,
                "pending_enqueues": 0,
            },
            None,
            "REMOTE_PIPELINE_BUSY",
        ),
        (
            None,
            [
                {
                    "id": "remote-doc",
                    "file_path": "replace.md",
                    "status": "processing",
                    "track_id": "old-track",
                    "chunks_count": 0,
                }
            ],
            "REMOTE_DOCUMENT_NOT_TERMINAL",
        ),
        (None, [], "REMOTE_DOCUMENT_NOT_FOUND"),
        (
            None,
            [
                {
                    "id": "remote-a",
                    "file_path": "replace.md",
                    "status": "processed",
                    "track_id": "old-track",
                    "chunks_count": 3,
                },
                {
                    "id": "remote-b",
                    "file_path": "folder/replace.md",
                    "status": "processed",
                    "track_id": "other-track",
                    "chunks_count": 2,
                },
            ],
            "REMOTE_DOCUMENT_AMBIGUOUS",
        ),
        (
            None,
            [
                {
                    "id": "remote-doc",
                    "file_path": "replace.md",
                    "status": "processed",
                    "track_id": "unowned-track",
                    "chunks_count": 3,
                }
            ],
            "REMOTE_OWNER_UNVERIFIED",
        ),
    ],
)
def test_replacement_plan_blocks_unsafe_remote_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pipeline: dict | None,
    documents: list[dict] | None,
    expected_blocker: str,
):
    _project, store, config, _old, _target = _replacement_conflict(
        tmp_path
    )
    calls = _install_replacement_client(
        monkeypatch,
        pipeline=pipeline,
        documents=documents,
    )

    result = ReplacementPlanner(store, config).plan()

    assert result.status == "blocked"
    assert result.error_code == "REPLACE_PLAN_BLOCKED"
    assert expected_blocker in result.plans[0].blockers
    assert result.plans[0].execution_authorized is False
    assert all(method != "DELETE" for method, _ in calls)


@pytest.mark.parametrize(
    ("remove_owner_snapshot", "remove_target_snapshot", "expected_blocker"),
    [
        (True, False, "ROLLBACK_SNAPSHOT_UNAVAILABLE"),
        (False, True, "TARGET_SNAPSHOT_UNAVAILABLE"),
    ],
)
def test_replacement_plan_requires_verified_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remove_owner_snapshot: bool,
    remove_target_snapshot: bool,
    expected_blocker: str,
):
    _project, store, config, old_binding, target_binding = (
        _replacement_conflict(tmp_path)
    )
    connection = store.connect(read_only=True)
    try:
        rows = {
            row["id"]: dict(row)
            for row in connection.execute(
                """
                SELECT b.id, r.snapshot_path
                FROM lightrag_binding b
                JOIN source_revision r ON r.id = b.revision_id
                WHERE b.id IN (?, ?)
                """,
                (old_binding, target_binding),
            ).fetchall()
        }
    finally:
        connection.close()
    if remove_owner_snapshot:
        (store.root / rows[old_binding]["snapshot_path"]).unlink()
    if remove_target_snapshot:
        (store.root / rows[target_binding]["snapshot_path"]).unlink()
    calls = _install_replacement_client(monkeypatch)

    result = ReplacementPlanner(store, config).plan()

    assert result.status == "blocked"
    assert expected_blocker in result.plans[0].blockers
    assert all(method != "DELETE" for method, _ in calls)


def test_replacement_plan_blocks_unconfirmed_capabilities_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project, store, config, _old, _target = _replacement_conflict(
        tmp_path
    )
    calls = _install_replacement_client(monkeypatch, paths={})
    before_seq = store.state_commit_seq()
    before_tree = _tree_snapshot(project)

    result = ReplacementPlanner(store, config).plan()

    assert result.status == "blocked"
    assert result.plans[0].blockers[:3] == [
        "DOCUMENT_DELETE_CAPABILITY_UNCONFIRMED",
        "DOCUMENT_INVENTORY_CAPABILITY_UNCONFIRMED",
        "PIPELINE_STATUS_CAPABILITY_UNCONFIRMED",
    ]
    assert calls == [
        ("GET", "/health"),
        ("GET", "/openapi.json"),
    ]
    assert store.state_commit_seq() == before_seq
    assert _tree_snapshot(project) == before_tree


def test_replacement_plan_fails_closed_on_workspace_or_inventory_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, _old, _target = _replacement_conflict(
        tmp_path
    )
    _install_replacement_client(monkeypatch, workspace="other")
    mismatched = ReplacementPlanner(store, config).plan()
    assert mismatched.status == "failed"
    assert mismatched.error_code == "WORKSPACE_MISMATCH"

    _install_replacement_client(
        monkeypatch,
        pagination_override={"total_count": 2},
    )
    invalid = ReplacementPlanner(store, config).plan()
    assert invalid.status == "failed"
    assert invalid.error_code == "REMOTE_REPLACE_PLAN_RESPONSE_INVALID"


def test_replace_plan_cli_no_conflicts_is_zero_write(tmp_path: Path):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    before = _durable_tree_snapshot(project)

    result = run_cli(
        tmp_path,
        "state",
        "replace-plan",
        "--root",
        str(project),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "schema_version": 1,
        "status": "no_conflicts",
        "mode": "dry_run",
        "workspace_mutated": False,
        "delete_attempted": False,
        "plans": [],
        "error_code": None,
    }
    assert _durable_tree_snapshot(project) == before


def test_replace_status_cli_is_read_only_with_no_operations(
    tmp_path: Path,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    before = _durable_tree_snapshot(project)

    result = run_cli(
        tmp_path,
        "state",
        "replace-status",
        "--root",
        str(project),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "no_operations"
    assert payload["mode"] == "read_only"
    assert payload["workspace_mutated"] is False
    assert payload["delete_attempted"] is False
    assert payload["operations"] == []
    assert _durable_tree_snapshot(project) == before


def test_replace_plan_cli_returns_remote_error_for_blocked_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    project, _store, config, _old, _target = _replacement_conflict(
        tmp_path
    )
    project_json = project / "project.json"
    project_config = json.loads(project_json.read_text(encoding="utf-8"))
    project_config["lightrag"].update(config)
    project_json.write_text(
        json.dumps(project_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    calls = _install_replacement_client(monkeypatch, paths={})

    exit_code = cli_main(
        [
            "state",
            "replace-plan",
            "--root",
            str(project),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 6
    assert payload["status"] == "blocked"
    assert payload["workspace_mutated"] is False
    assert payload["delete_attempted"] is False
    assert all(method != "DELETE" for method, _ in calls)


def test_replace_execute_cli_returns_stable_sanitized_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    project, store, config, _old, _target = _replacement_conflict(
        tmp_path
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    config["sync"] = {"poll_interval_seconds": 0.1}
    project_json = project / "project.json"
    project_config = json.loads(project_json.read_text(encoding="utf-8"))
    project_config["lightrag"].update(config)
    project_json.write_text(
        json.dumps(project_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _install_replacement_executor_client(monkeypatch)
    plan = ReplacementPlanner(store, config).plan().plans[0]

    exit_code = cli_main(
        [
            "state",
            "replace-execute",
            "--root",
            str(project),
            "--plan-id",
            plan.plan_id,
            "--confirm-digest",
            plan.plan_digest,
            "--smoke-query",
            "new target",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert exit_code == 0
    assert payload["status"] == "completed"
    assert payload["operation"]["phase"] == "COMPLETED"
    assert payload["delete_attempted"] is True
    assert "private answer" not in serialized
    assert "new target bytes" not in serialized


def test_json_state_error_envelope_is_stable(tmp_path: Path):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    (project / "artifacts" / "state" / "evo_wiki.sqlite3").unlink()

    result = run_cli(
        tmp_path,
        "state",
        "verify",
        "--root",
        str(project),
        "--json",
    )

    assert result.returncode == 5
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "failed",
        "operation": "state.verify",
        "error_code": "STATE_DATABASE_MISSING",
        "state_committed": False,
    }


def test_response_lost_after_submit_preserves_precise_block_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    source = project / "corpus" / "raw" / "uncertain.md"
    source.write_text("uncertain submission", encoding="utf-8")
    paths = ProjectPaths.from_root(project)
    files = scan_corpus(project, paths.corpus)
    prepare_lightrag_input(paths, files)
    store = StateStore(project)
    store.stage_files(files)

    monkeypatch.setattr(
        "evo_wiki.lightrag_lane.preflight_lightrag_build",
        lambda *_args, **_kwargs: None,
    )

    def lose_response(*_args, **_kwargs):
        raise LightRAGBuildError("connection lost after request write")

    monkeypatch.setattr(
        "evo_wiki.lightrag_lane.LightRAGServiceClient.post_json",
        lose_response,
    )
    with pytest.raises(LightRAGBuildError):
        build_lightrag(
            paths,
            config={
                "base_url": "http://127.0.0.1:9621",
                "workspace": "uncertain",
            },
            state_store=store,
        )

    binding = store.list_lightrag_documents()[
        "corpus__raw__uncertain.md"
    ]
    assert binding["remote_status"] == "UNKNOWN"
    assert binding["action_gate"] == "BLOCKED"
    assert binding["gate_reason"] == "RESPONSE_LOST_AFTER_SUBMIT"


def test_replacement_execute_completes_once_with_verified_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, old_binding, target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    config["sync"] = {"poll_interval_seconds": 0.1}
    calls, _state = _install_replacement_executor_client(monkeypatch)
    plan = ReplacementPlanner(store, config).plan().plans[0]

    result = ReplacementOperationService(
        store,
        config,
    ).execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert result.status == "completed"
    assert result.delete_attempted is True
    assert result.operation.delete_attempts == 1
    assert result.operation.submit_attempts == 1
    assert result.operation.backup.backup_sha256.startswith("sha256:")
    assert calls.count(("DELETE", "/documents/delete_document")) == 1
    assert calls.count(("POST", "/documents/text")) == 1
    assert store.list_replacement_operations()[0]["phase"] == "COMPLETED"
    context = store.replacement_execution_context(
        target_binding_id=target_binding,
        owner_binding_id=old_binding,
    )
    connection = store.connect(read_only=True)
    try:
        revisions = {
            row["id"]: row["status"]
            for row in connection.execute(
                "SELECT id, status FROM source_revision"
            )
        }
        bindings = {
            row["id"]: (row["remote_status"], row["action_gate"])
            for row in connection.execute(
                """
                SELECT id, remote_status, action_gate
                FROM lightrag_binding
                """
            )
        }
    finally:
        connection.close()
    assert revisions[context["target_revision_id"]] == "ACTIVE"
    assert revisions[context["owner_revision_id"]] == "SUPERSEDED"
    assert bindings[target_binding] == ("PROCESSED", "OPEN")
    assert bindings[old_binding] == ("MISSING", "BLOCKED")


def test_replacement_drains_gateway_queries_before_delete_and_closes_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, old_binding, target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    config["sync"] = {"poll_interval_seconds": 0.1}
    context = store.replacement_execution_context(
        target_binding_id=target_binding,
        owner_binding_id=old_binding,
    )
    store.register_gateway_instance(
        instance_id="gateway-drain-test",
        retrieval_partition_id=context["retrieval_partition_id"],
        gateway_mode="enforce",
        version="test",
    )
    calls, _state = _install_replacement_executor_client(monkeypatch)
    plan = ReplacementPlanner(store, config).plan().plans[0]

    result = ReplacementOperationService(
        store,
        config,
        query_gateway_config={
            "mode": "enforce",
            "drain_timeout_seconds": 1,
        },
    ).execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert result.status == "completed"
    remote_writes = [
        call
        for call in calls
        if call
        in {
            ("DELETE", "/documents/delete_document"),
            ("POST", "/documents/text"),
        }
    ]
    assert remote_writes[0] == (
        "DELETE",
        "/documents/delete_document",
    )
    assert store.active_maintenance_fences() == []
    connection = store.connect(read_only=True)
    try:
        fence = connection.execute(
            "SELECT state FROM maintenance_fence"
        ).fetchone()
    finally:
        connection.close()
    assert fence["state"] == "CLOSED"


def test_replacement_query_drain_timeout_blocks_before_remote_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, old_binding, target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    context = store.replacement_execution_context(
        target_binding_id=target_binding,
        owner_binding_id=old_binding,
    )
    store.register_gateway_instance(
        instance_id="gateway-busy-test",
        retrieval_partition_id=context["retrieval_partition_id"],
        gateway_mode="enforce",
        version="test",
    )
    store.begin_query_run(
        request_id="qry-still-running",
        retrieval_partition_id=context["retrieval_partition_id"],
        principal_hmac="hmac-sha256:" + "1" * 64,
        query_hmac="hmac-sha256:" + "2" * 64,
        request_mode="mix",
        gateway_mode="enforce",
        verification_level="provenance_critical_fact_v1",
        lease_seconds=30,
    )
    calls, _state = _install_replacement_executor_client(monkeypatch)
    plan = ReplacementPlanner(store, config).plan().plans[0]

    result = ReplacementOperationService(
        store,
        config,
        query_gateway_config={
            "mode": "enforce",
            "drain_timeout_seconds": 0.2,
        },
    ).execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert result.status == "blocked"
    assert result.error_code == "QUERY_DRAIN_TIMEOUT"
    assert not any(method == "DELETE" for method, _ in calls)
    assert store.active_maintenance_fences()[0]["state"] == "FAILED"


def test_required_maintenance_notification_blocks_before_remote_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, old_binding, target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    context = store.replacement_execution_context(
        target_binding_id=target_binding,
        owner_binding_id=old_binding,
    )
    store.register_gateway_instance(
        instance_id="gateway-notification-test",
        retrieval_partition_id=context["retrieval_partition_id"],
        gateway_mode="enforce",
        version="test",
    )
    monkeypatch.setenv(
        "TEST_OPS_WEBHOOK_URL",
        "http://127.0.0.1:9/events",
    )
    monkeypatch.setenv("TEST_OPS_WEBHOOK_KEY", "k" * 32)
    project_config = {
        "security": {"default_domain": "default"},
        "operations": {
            "notifications": {
                "enabled": True,
                "webhook_url_env": "TEST_OPS_WEBHOOK_URL",
                "signing_key_env": "TEST_OPS_WEBHOOK_KEY",
                "required_delivery_timeout_seconds": 1,
            }
        },
    }
    calls, _state = _install_replacement_executor_client(monkeypatch)
    plan = ReplacementPlanner(store, config).plan().plans[0]

    result = ReplacementOperationService(
        store,
        config,
        query_gateway_config={
            "mode": "enforce",
            "drain_timeout_seconds": 1,
        },
        project_config=project_config,
    ).execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert result.status == "blocked"
    assert result.error_code == (
        "OPS_NOTIFICATION_REQUIRED_UNDELIVERED"
    )
    assert not any(method == "DELETE" for method, _ in calls)
    assert store.active_maintenance_fences()[0]["state"] == "FAILED"
    notifications = store.list_notifications()
    assert {
        item["event_type"] for item in notifications
    } == {"MAINTENANCE_DRAINING", "MAINTENANCE_FAILED"}
    draining = next(
        item
        for item in notifications
        if item["event_type"] == "MAINTENANCE_DRAINING"
    )
    assert draining["delivery_required"] is True


def test_replacement_execute_resumes_known_pre_write_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, old_binding, target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    calls, _state = _install_replacement_executor_client(monkeypatch)
    plan = ReplacementPlanner(store, config).plan().plans[0]
    service = ReplacementOperationService(
        store,
        config,
        query_gateway_config={
            "mode": "enforce",
            "drain_timeout_seconds": 1,
        },
    )

    blocked = service.execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )
    assert blocked.status == "blocked"
    assert blocked.error_code == "QUERY_GATEWAY_HEARTBEAT_STALE"
    assert not any(method == "DELETE" for method, _ in calls)

    context = store.replacement_execution_context(
        target_binding_id=target_binding,
        owner_binding_id=old_binding,
    )
    store.register_gateway_instance(
        instance_id="gateway-resume-test",
        retrieval_partition_id=context["retrieval_partition_id"],
        gateway_mode="enforce",
        version="test",
    )
    completed = service.execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert completed.status == "completed"
    assert sum(method == "DELETE" for method, _ in calls) == 1
    assert sum(
        method == "POST" and path == "/documents/text"
        for method, path in calls
    ) == 1


def test_replacement_execute_compensates_known_target_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, old_binding, target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    config["sync"] = {"poll_interval_seconds": 0.1}
    calls, _state = _install_replacement_executor_client(
        monkeypatch,
        target_failed=True,
    )
    plan = ReplacementPlanner(store, config).plan().plans[0]

    result = ReplacementOperationService(
        store,
        config,
    ).execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert result.status == "rolled_back"
    assert result.operation.delete_attempts == 2
    assert result.operation.submit_attempts == 2
    assert calls.count(("DELETE", "/documents/delete_document")) == 2
    assert calls.count(("POST", "/documents/text")) == 2
    context = store.replacement_execution_context(
        target_binding_id=target_binding,
        owner_binding_id=old_binding,
    )
    connection = store.connect(read_only=True)
    try:
        statuses = {
            row["id"]: row["status"]
            for row in connection.execute(
                "SELECT id, status FROM source_revision"
            )
        }
    finally:
        connection.close()
    assert statuses[context["owner_revision_id"]] == "ACTIVE"
    assert statuses[context["target_revision_id"]] == "REJECTED"


def test_replacement_execute_compensates_smoke_evidence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, _old_binding, _target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    config["sync"] = {"poll_interval_seconds": 0.1}
    calls, _state = _install_replacement_executor_client(
        monkeypatch,
        smoke_passes=False,
    )
    plan = ReplacementPlanner(store, config).plan().plans[0]

    result = ReplacementOperationService(
        store,
        config,
    ).execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert result.status == "rolled_back"
    assert calls.count(("DELETE", "/documents/delete_document")) == 2
    assert calls.count(("POST", "/documents/text")) == 2


def test_replacement_unknown_delete_is_never_replayed_and_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project, store, config, _old_binding, _target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    config["sync"] = {"poll_interval_seconds": 0.1}
    calls, _state = _install_replacement_executor_client(
        monkeypatch,
        delete_raises=True,
    )
    plan = ReplacementPlanner(store, config).plan().plans[0]
    service = ReplacementOperationService(store, config)

    first = service.execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )
    second = service.execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert first.status == "needs_audit"
    assert second.status == "needs_audit"
    assert first.error_code == "REMOTE_DELETE_EFFECT_UNKNOWN"
    assert calls.count(("DELETE", "/documents/delete_document")) == 1
    assert calls.count(("POST", "/documents/text")) == 0
    with pytest.raises(StateError) as blocked:
        store.ensure_partition(
            {
                "mode": "service",
                "workspace": "other",
                "base_url": "http://127.0.0.1:9621",
            }
        )
    assert blocked.value.error_code == "STATE_REPLACEMENT_WRITE_GATE"
    journal_bytes = b"".join(
        path.read_bytes()
        for path in (
            project / "artifacts" / "logs" / "runs"
        ).rglob("events-*.jsonl")
    )
    assert b"private delete response" not in journal_bytes
    assert b"credential" not in journal_bytes


def test_replacement_crash_at_delete_intent_never_replays_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, _old_binding, _target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    config["sync"] = {"poll_interval_seconds": 0.1}
    calls, _state = _install_replacement_executor_client(
        monkeypatch,
        delete_interrupts=True,
    )
    plan = ReplacementPlanner(store, config).plan().plans[0]
    service = ReplacementOperationService(store, config)

    with pytest.raises(KeyboardInterrupt):
        service.execute(
            plan_id=plan.plan_id,
            confirm_digest=plan.plan_digest,
            smoke_query="new target",
        )
    resumed = service.execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert resumed.status == "needs_audit"
    assert resumed.operation.phase == "NEEDS_AUDIT"
    assert calls.count(("DELETE", "/documents/delete_document")) == 1
    assert calls.count(("POST", "/documents/text")) == 0


def test_replacement_crash_after_delete_acceptance_resumes_without_redelete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, _old_binding, _target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    config["sync"] = {"poll_interval_seconds": 0.1}
    calls, _state = _install_replacement_executor_client(monkeypatch)
    plan = ReplacementPlanner(store, config).plan().plans[0]
    original = ReplacementOperationService._confirm_deletion

    def interrupt_confirmation(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        ReplacementOperationService,
        "_confirm_deletion",
        interrupt_confirmation,
    )
    service = ReplacementOperationService(store, config)
    with pytest.raises(KeyboardInterrupt):
        service.execute(
            plan_id=plan.plan_id,
            confirm_digest=plan.plan_digest,
            smoke_query="new target",
        )

    monkeypatch.setattr(
        ReplacementOperationService,
        "_confirm_deletion",
        original,
    )
    resumed = service.execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert resumed.status == "completed"
    assert calls.count(("DELETE", "/documents/delete_document")) == 1
    assert calls.count(("POST", "/documents/text")) == 1


def test_replacement_crash_after_submit_acceptance_resumes_without_resubmit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _project, store, config, _old_binding, _target_binding = (
        _replacement_conflict(tmp_path)
    )
    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    config["sync"] = {"poll_interval_seconds": 0.1}
    calls, _state = _install_replacement_executor_client(monkeypatch)
    plan = ReplacementPlanner(store, config).plan().plans[0]
    original = ReplacementOperationService._wait_target

    def interrupt_polling(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        ReplacementOperationService,
        "_wait_target",
        interrupt_polling,
    )
    service = ReplacementOperationService(store, config)
    with pytest.raises(KeyboardInterrupt):
        service.execute(
            plan_id=plan.plan_id,
            confirm_digest=plan.plan_digest,
            smoke_query="new target",
        )

    monkeypatch.setattr(
        ReplacementOperationService,
        "_wait_target",
        original,
    )
    resumed = service.execute(
        plan_id=plan.plan_id,
        confirm_digest=plan.plan_digest,
        smoke_query="new target",
    )

    assert resumed.status == "completed"
    assert calls.count(("DELETE", "/documents/delete_document")) == 1
    assert calls.count(("POST", "/documents/text")) == 1


def test_replacement_requires_enablement_and_exact_plan_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project, store, config, _old_binding, _target_binding = (
        _replacement_conflict(tmp_path)
    )
    calls, _state = _install_replacement_executor_client(monkeypatch)
    plan = ReplacementPlanner(store, config).plan().plans[0]
    before = _durable_tree_snapshot(project)

    with pytest.raises(StateError) as disabled:
        ReplacementOperationService(store, config).execute(
            plan_id=plan.plan_id,
            confirm_digest=plan.plan_digest,
            smoke_query="new target",
        )
    assert disabled.value.error_code == "REPLACEMENT_DISABLED"
    assert _durable_tree_snapshot(project) == before

    config["replacement"] = {
        "enabled": True,
        "maintenance_window_seconds": 10,
        "absence_confirmations": 1,
        "auto_compensate": True,
    }
    with pytest.raises(StateError) as mismatch:
        ReplacementOperationService(store, config).execute(
            plan_id=plan.plan_id,
            confirm_digest="sha256:" + ("0" * 64),
            smoke_query="new target",
        )
    assert mismatch.value.error_code == "REPLACE_CONFIRMATION_MISMATCH"
    assert all(method != "DELETE" for method, _path in calls)
    assert all(
        not (method == "POST" and path == "/documents/text")
        for method, path in calls
    )

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path


VERSION = "2.0.1"
BUNDLE_NAME = f"evo-wiki-legal-demo-{VERSION}"
INDEX_FILES = (
    "graph_chunk_entity_relation.graphml",
    "kv_store_doc_status.json",
    "kv_store_entity_chunks.json",
    "kv_store_full_docs.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
    "kv_store_relation_chunks.json",
    "kv_store_text_chunks.json",
    "vdb_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
)
CLEAN_TABLES = (
    "notification_attempt",
    "notification_outbox",
    "audit_event",
    "audit_item",
    "gateway_instance",
    "maintenance_fence",
    "replacement_operation",
    "query_run",
    "lane_run_revision",
    "lane_run",
)


def _write(path: Path, content: str = "{}\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _create_state_database(workspace: Path) -> None:
    database = (
        workspace / "artifacts" / "state" / "evo_wiki.sqlite3"
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            PRAGMA user_version = 5;
            CREATE TABLE source_document (
              id TEXT PRIMARY KEY
            );
            CREATE TABLE source_revision (
              id TEXT PRIMARY KEY,
              snapshot_status TEXT NOT NULL,
              snapshot_path TEXT
            );
            CREATE TABLE lightrag_binding (
              id TEXT PRIMARY KEY,
              remote_status TEXT NOT NULL,
              action_gate TEXT NOT NULL
            );
            CREATE TABLE notification_attempt (id TEXT PRIMARY KEY);
            CREATE TABLE notification_outbox (id TEXT PRIMARY KEY);
            CREATE TABLE audit_event (id TEXT PRIMARY KEY);
            CREATE TABLE audit_item (id TEXT PRIMARY KEY);
            CREATE TABLE gateway_instance (id TEXT PRIMARY KEY);
            CREATE TABLE maintenance_fence (id TEXT PRIMARY KEY);
            CREATE TABLE replacement_operation (id TEXT PRIMARY KEY);
            CREATE TABLE query_run (id TEXT PRIMARY KEY, private_text TEXT);
            CREATE TABLE lane_run_revision (id TEXT PRIMARY KEY);
            CREATE TABLE lane_run (id TEXT PRIMARY KEY);
            """
        )
        for index in range(9):
            source_id = f"source-{index}"
            revision_id = f"revision-{index}"
            snapshot = (
                "artifacts/state/snapshots/"
                f"{index:02d}/snapshot-{index}"
            )
            connection.execute(
                "INSERT INTO source_document(id) VALUES (?)",
                (source_id,),
            )
            connection.execute(
                """
                INSERT INTO source_revision(
                  id, snapshot_status, snapshot_path
                ) VALUES (?, 'AVAILABLE', ?)
                """,
                (revision_id, snapshot),
            )
            connection.execute(
                """
                INSERT INTO lightrag_binding(
                  id, remote_status, action_gate
                ) VALUES (?, 'PROCESSED', 'OPEN')
                """,
                (f"binding-{index}",),
            )
            _write(workspace / snapshot, f"snapshot {index}\n")
        for table in CLEAN_TABLES:
            if table == "query_run":
                connection.execute(
                    """
                    INSERT INTO query_run(id, private_text)
                    VALUES ('query-1', ?)
                    """,
                    (f"{workspace.resolve()}/private-query",),
                )
            else:
                connection.execute(
                    f'INSERT INTO "{table}"(id) VALUES (?)',
                    (f"{table}-1",),
                )
        connection.commit()
    finally:
        connection.close()


def _create_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    for relative in (
        "project.json",
        "wiki.json",
        "artifacts/corpus-state.json",
        "artifacts/manifest.json",
        "artifacts/wiki/manifest.json",
        "artifacts/wiki/progress.json",
        "artifacts/lightrag/manifest.json",
        "artifacts/wiki/reports/wiki-health.json",
        "artifacts/wiki/state/corpus-state.json",
        "artifacts/lightrag/input/documents.jsonl",
        "artifacts/lightrag/state/corpus-state.json",
        "artifacts/lightrag/state/lightrag-import-ledger.json",
        "artifacts/platform/index.html",
        "artifacts/wiki/dist/index.html",
    ):
        _write(workspace / relative)
    for index in range(9):
        _write(
            workspace
            / "corpus"
            / "raw"
            / "legal_docs"
            / f"case-{index}.txt",
            f"case {index}\n",
        )
    for index in range(22):
        _write(
            workspace
            / "artifacts"
            / "wiki"
            / "wiki-src"
            / f"page-{index}.md",
            f"# page {index}\n",
        )
    _write(
        workspace / "artifacts" / "generation" / "report.json",
        f'{{"private": "{workspace.resolve()}"}}\n',
    )
    _write(
        workspace / "artifacts" / "logs" / "run.log",
        "private run log\n",
    )
    _create_state_database(workspace)
    return workspace


def _create_lightrag_workspace(root: Path) -> Path:
    workspace = root / "lightrag" / "evo_wiki"
    for name in INDEX_FILES:
        _write(workspace / name)
    _write(
        workspace / "kv_store_llm_response_cache.json",
        "PRIVATE_CACHE_CONTENT\n",
    )
    return workspace


def _run_builder(
    root: Path,
    *,
    workspace: Path,
    lightrag_workspace: Path,
    output: Path,
) -> subprocess.CompletedProcess[str]:
    repository = Path(__file__).resolve().parents[1]
    wheel = root / f"evo_wiki-{VERSION}-py3-none-any.whl"
    license_file = root / "LightRAG-LICENSE"
    wheel.write_bytes(b"synthetic wheel for layout test")
    _write(license_file, "synthetic LightRAG license\n")
    return subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "build_demo_bundle.py"),
            "--workspace",
            str(workspace),
            "--lightrag-workspace",
            str(lightrag_workspace),
            "--lightrag-license",
            str(license_file),
            "--wheel",
            str(wheel),
            "--output-dir",
            str(output),
        ],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )


def test_demo_bundle_is_sanitized_and_verifiable(tmp_path: Path):
    workspace = _create_workspace(tmp_path)
    lightrag_workspace = _create_lightrag_workspace(tmp_path)
    output = tmp_path / "dist"

    result = _run_builder(
        tmp_path,
        workspace=workspace,
        lightrag_workspace=lightrag_workspace,
        output=output,
    )

    assert result.returncode == 0, result.stderr
    release = output / BUNDLE_NAME
    archive = output / f"{BUNDLE_NAME}.zip"
    archive_checksum = output / f"{BUNDLE_NAME}.zip.sha256"
    assert release.is_dir()
    assert archive.is_file()
    assert archive_checksum.is_file()
    assert (release / ".env.example").is_file()
    assert not (release / ".env").exists()
    assert (release / "start.sh").stat().st_mode & 0o111
    assert (release / "packages" / f"evo_wiki-{VERSION}-py3-none-any.whl").is_file()

    assert len(
        list(
            (
                release
                / "workspace"
                / "corpus"
                / "raw"
                / "legal_docs"
            ).glob("*.txt")
        )
    ) == 9
    assert len(
        list(
            (
                release
                / "workspace"
                / "artifacts"
                / "wiki"
                / "wiki-src"
            ).glob("*.md")
        )
    ) == 22
    assert not list(release.rglob("kv_store_llm_response_cache.json"))
    assert not list(release.rglob("*.log"))
    assert not list(release.rglob("*-wal"))
    assert not list(release.rglob("*-shm"))
    assert not list(release.rglob(".DS_Store"))
    assert not (release / "workspace" / "artifacts" / "generation").exists()

    bundled_bytes = b"\n".join(
        path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    )
    assert str(workspace.resolve()).encode() not in bundled_bytes
    assert b"PRIVATE_CACHE_CONTENT" not in bundled_bytes

    database = (
        release
        / "workspace"
        / "artifacts"
        / "state"
        / "evo_wiki.sqlite3"
    )
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0] == "ok"
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM source_document"
        ).fetchone()[0] == 9
        assert connection.execute(
            """
            SELECT COUNT(*) FROM lightrag_binding
            WHERE remote_status = 'PROCESSED'
              AND action_gate = 'OPEN'
            """
        ).fetchone()[0] == 9
        for table in CLEAN_TABLES:
            assert connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0] == 0
    finally:
        connection.close()

    for line in (release / "SHA256SUMS").read_text(
        encoding="utf-8"
    ).splitlines():
        digest, relative = line.split("  ", 1)
        assert hashlib.sha256(
            (release / relative).read_bytes()
        ).hexdigest() == digest
    outer_digest, outer_name = archive_checksum.read_text(
        encoding="utf-8"
    ).strip().split("  ", 1)
    assert outer_name == archive.name
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == outer_digest

    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        assert names == sorted(names)
        assert f"{BUNDLE_NAME}/README.md" in names
        assert not any(name.endswith("/.env") for name in names)


def test_demo_bundle_refuses_to_overwrite(tmp_path: Path):
    workspace = _create_workspace(tmp_path)
    lightrag_workspace = _create_lightrag_workspace(tmp_path)
    output = tmp_path / "dist"
    first = _run_builder(
        tmp_path,
        workspace=workspace,
        lightrag_workspace=lightrag_workspace,
        output=output,
    )
    assert first.returncode == 0, first.stderr

    second = _run_builder(
        tmp_path,
        workspace=workspace,
        lightrag_workspace=lightrag_workspace,
        output=output,
    )
    assert second.returncode == 1
    assert "bundle target already exists" in second.stderr

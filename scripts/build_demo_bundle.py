#!/usr/bin/env python3
"""Build the sanitized, runnable Evo Wiki legal demonstration bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "scripts" / "demo_bundle"
VERSION_FILE = ROOT / "src" / "evo_wiki" / "version.py"
BUNDLE_PREFIX = "evo-wiki-legal-demo"
LIGHTRAG_IMAGE = (
    "ghcr.io/hkuds/lightrag@"
    "sha256:de09cd75e32b6b45b104625a9fb229f84f3dec4827ecffc825aa4438b196cbe6"
)
EXPECTED_SOURCE_COUNT = 9
EXPECTED_WIKI_PAGE_COUNT = 22
EXPECTED_BINDING_COUNT = 9
EXPECTED_SCHEMA_VERSION = 5

LIGHTRAG_INDEX_FILES = (
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

WORKSPACE_FILES = (
    "project.json",
    "wiki.json",
    "artifacts/corpus-state.json",
    "artifacts/manifest.json",
    "artifacts/wiki/manifest.json",
    "artifacts/wiki/progress.json",
    "artifacts/lightrag/manifest.json",
)

WORKSPACE_TREES: dict[str, Callable[[Path], bool]] = {
    "corpus/raw/legal_docs": lambda path: path.is_file(),
    "artifacts/platform": lambda path: path.is_file(),
    "artifacts/wiki/wiki-src": lambda path: (
        path.is_file() and path.suffix == ".md"
    ),
    "artifacts/wiki/dist": lambda path: path.is_file(),
    "artifacts/wiki/reports": lambda path: (
        path.is_file() and path.suffix == ".json"
    ),
    "artifacts/wiki/state": lambda path: (
        path.is_file() and path.suffix == ".json"
    ),
    "artifacts/lightrag/input": lambda path: path.is_file(),
}

LIGHTRAG_STATE_FILES = (
    "artifacts/lightrag/state/corpus-state.json",
    "artifacts/lightrag/state/lightrag-import-ledger.json",
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

EXCLUDED_NAMES = frozenset(
    {
        ".DS_Store",
        "__pycache__",
        "backups",
        "logs",
        "query-audit",
        "kv_store_llm_response_cache.json",
    }
)


class BundleError(RuntimeError):
    """Raised when a source bundle cannot be built safely."""


def project_version() -> str:
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"',
        VERSION_FILE.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        raise BundleError("cannot read Evo Wiki version")
    return match.group(1)


def source_revision() -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return "not_git", True


def _required_file(path: Path) -> Path:
    if not path.is_file():
        raise BundleError(f"required source file is missing: {path}")
    return path


def _safe_relative(relative: Path) -> None:
    if relative.is_absolute() or ".." in relative.parts:
        raise BundleError(f"unsafe relative path: {relative}")
    if any(part in EXCLUDED_NAMES for part in relative.parts):
        raise BundleError(f"excluded path selected for bundle: {relative}")
    if relative.name.endswith((".pyc", "-wal", "-shm", ".lock")):
        raise BundleError(f"runtime-only file selected for bundle: {relative}")


def _copy_file(source: Path, destination: Path) -> None:
    _required_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_tree(
    source: Path,
    destination: Path,
    predicate: Callable[[Path], bool],
) -> None:
    if not source.is_dir():
        raise BundleError(f"required source directory is missing: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in EXCLUDED_NAMES for part in relative.parts):
            continue
        if path.is_dir():
            continue
        if not predicate(path):
            continue
        _safe_relative(relative)
        _copy_file(path, destination / relative)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sanitize_lightrag_config(destination: Path) -> None:
    _write_json(
        destination,
        {
            "mode": "service",
            "base_url": "http://127.0.0.1:9621",
            "workspace": "evo_wiki",
            "api_key_env": "LIGHTRAG_API_KEY",
            "bearer_token_env": "LIGHTRAG_BEARER_TOKEN",
            "timeout_seconds": 30,
            "embedding": {"batch_size": 8},
        },
    )


def _copy_workspace(source: Path, destination: Path) -> dict[str, int]:
    if not source.is_dir():
        raise BundleError(f"workspace is missing: {source}")
    for relative in WORKSPACE_FILES:
        _safe_relative(Path(relative))
        _copy_file(source / relative, destination / relative)
    for relative, predicate in WORKSPACE_TREES.items():
        _safe_relative(Path(relative))
        _copy_tree(
            source / relative,
            destination / relative,
            predicate,
        )
    for relative in LIGHTRAG_STATE_FILES:
        _safe_relative(Path(relative))
        _copy_file(source / relative, destination / relative)
    _sanitize_lightrag_config(destination / "lightrag-config.json")

    raw_files = sorted(
        (destination / "corpus" / "raw" / "legal_docs").glob("*.txt")
    )
    wiki_pages = sorted(
        (destination / "artifacts" / "wiki" / "wiki-src").rglob("*.md")
    )
    if len(raw_files) != EXPECTED_SOURCE_COUNT:
        raise BundleError(
            "legal corpus must contain exactly "
            f"{EXPECTED_SOURCE_COUNT} .txt files; found {len(raw_files)}"
        )
    if len(wiki_pages) != EXPECTED_WIKI_PAGE_COUNT:
        raise BundleError(
            "wiki source must contain exactly "
            f"{EXPECTED_WIKI_PAGE_COUNT} Markdown pages; found {len(wiki_pages)}"
        )
    return {
        "corpus_files": len(raw_files),
        "wiki_source_pages": len(wiki_pages),
        "platform_files": sum(
            1
            for path in (
                destination / "artifacts" / "platform"
            ).rglob("*")
            if path.is_file()
        ),
    }


def _copy_lightrag_index(source: Path, bundle_root: Path) -> int:
    destination = (
        bundle_root
        / "lightrag-data"
        / "rag_storage"
        / "evo_wiki"
    )
    destination.mkdir(parents=True, exist_ok=True)
    for name in LIGHTRAG_INDEX_FILES:
        _copy_file(source / name, destination / name)
    (bundle_root / "lightrag-data" / "inputs" / "evo_wiki").mkdir(
        parents=True,
        exist_ok=True,
    )
    (bundle_root / "lightrag-data" / "prompts").mkdir(
        parents=True,
        exist_ok=True,
    )
    return len(LIGHTRAG_INDEX_FILES)


def _sqlite_connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _sanitize_database(
    source_database: Path,
    destination_database: Path,
) -> dict[str, int]:
    _required_file(source_database)
    destination_database.parent.mkdir(parents=True, exist_ok=True)
    if destination_database.exists():
        raise BundleError(
            f"database destination already exists: {destination_database}"
        )

    source = _sqlite_connect_read_only(source_database)
    destination = sqlite3.connect(destination_database)
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()

    connection = sqlite3.connect(destination_database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        tables = _table_names(connection)
        required_tables = {
            "source_document",
            "source_revision",
            "lightrag_binding",
            *CLEAN_TABLES,
        }
        missing = sorted(required_tables - tables)
        if missing:
            raise BundleError(
                "state database is missing required tables: "
                + ", ".join(missing)
            )
        with connection:
            for table in CLEAN_TABLES:
                connection.execute(f'DELETE FROM "{table}"')
        connection.execute("VACUUM")
        connection.execute("PRAGMA journal_mode = DELETE")

        integrity = str(
            connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
        foreign_key_rows = list(
            connection.execute("PRAGMA foreign_key_check")
        )
        schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        source_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM source_document"
            ).fetchone()[0]
        )
        binding_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM lightrag_binding
                WHERE remote_status = 'PROCESSED'
                  AND action_gate = 'OPEN'
                """
            ).fetchone()[0]
        )
        operational_count = sum(
            int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            )
            for table in CLEAN_TABLES
        )
        if integrity != "ok":
            raise BundleError(
                f"sanitized SQLite integrity check failed: {integrity}"
            )
        if foreign_key_rows:
            raise BundleError(
                "sanitized SQLite foreign key check failed"
            )
        if schema_version != EXPECTED_SCHEMA_VERSION:
            raise BundleError(
                "sanitized SQLite schema must be "
                f"{EXPECTED_SCHEMA_VERSION}; found {schema_version}"
            )
        if source_count != EXPECTED_SOURCE_COUNT:
            raise BundleError(
                "sanitized SQLite must contain "
                f"{EXPECTED_SOURCE_COUNT} sources; found {source_count}"
            )
        if binding_count != EXPECTED_BINDING_COUNT:
            raise BundleError(
                "sanitized SQLite must contain "
                f"{EXPECTED_BINDING_COUNT} processed/open bindings; "
                f"found {binding_count}"
            )
        if operational_count != 0:
            raise BundleError(
                "sanitized SQLite still contains operational history"
            )
    finally:
        connection.close()

    os.chmod(destination_database.parent, 0o700)
    os.chmod(destination_database, 0o600)
    return {
        "schema_version": schema_version,
        "sources": source_count,
        "processed_open_bindings": binding_count,
        "operational_history_rows": operational_count,
    }


def _copy_snapshots(source_workspace: Path, destination_workspace: Path) -> int:
    source = (
        source_workspace / "artifacts" / "state" / "snapshots"
    )
    destination = (
        destination_workspace / "artifacts" / "state" / "snapshots"
    )
    _copy_tree(source, destination, lambda path: path.is_file())

    database = (
        destination_workspace
        / "artifacts"
        / "state"
        / "evo_wiki.sqlite3"
    )
    connection = _sqlite_connect_read_only(database)
    try:
        rows = list(
            connection.execute(
                """
                SELECT snapshot_path
                FROM source_revision
                WHERE snapshot_status = 'AVAILABLE'
                ORDER BY snapshot_path
                """
            )
        )
    finally:
        connection.close()
    for (raw_path,) in rows:
        relative = Path(str(raw_path))
        _safe_relative(relative)
        if not (destination_workspace / relative).is_file():
            raise BundleError(
                f"required state snapshot is missing: {relative}"
            )
    return len(rows)


def _build_python_wheel(destination: Path) -> Path:
    with tempfile.TemporaryDirectory(
        prefix="evo-wiki-demo-wheel-"
    ) as directory:
        output = Path(directory)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--outdir",
                str(output),
            ],
            cwd=ROOT,
            check=True,
        )
        wheels = sorted(output.glob("*.whl"))
        if len(wheels) != 1:
            raise BundleError(
                "Python build must produce exactly one wheel"
            )
        destination.mkdir(parents=True, exist_ok=True)
        result = destination / wheels[0].name
        shutil.copy2(wheels[0], result)
        return result


def _copy_python_wheel(source: Path, destination: Path) -> Path:
    version = project_version()
    expected = f"evo_wiki-{version}-py3-none-any.whl"
    if source.name != expected:
        raise BundleError(
            f"provided wheel must be named {expected}; found {source.name}"
        )
    result = destination / expected
    _copy_file(source, result)
    return result


def _render_template(
    source: Path,
    destination: Path,
    replacements: dict[str, str],
) -> None:
    content = _required_file(source).read_text(encoding="utf-8")
    for marker, value in replacements.items():
        content = content.replace(marker, value)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", content)))
    if unresolved:
        raise BundleError(
            "unresolved template markers in "
            f"{source.name}: {', '.join(unresolved)}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def _copy_templates(
    bundle_root: Path,
    *,
    version: str,
    commit: str,
    wheel_name: str,
) -> None:
    replacements = {
        "{{VERSION}}": version,
        "{{SOURCE_COMMIT}}": commit,
        "{{LIGHTRAG_IMAGE}}": LIGHTRAG_IMAGE,
        "{{WHEEL_NAME}}": wheel_name,
    }
    for name in (
        "README.md",
        ".env.example",
        "docker-compose.yml",
        "requirements.txt",
        "start.sh",
        "check.sh",
        "stop.sh",
        "THIRD_PARTY_NOTICES.md",
    ):
        _render_template(
            TEMPLATE_ROOT / name,
            bundle_root / name,
            replacements,
        )
    for name in ("start.sh", "check.sh", "stop.sh"):
        os.chmod(bundle_root / name, 0o755)


def _write_checksums(root: Path) -> None:
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    (root / "SHA256SUMS").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _assert_clean_bundle(root: Path, forbidden_tokens: tuple[bytes, ...]) -> None:
    forbidden_paths: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_NAMES for part in relative.parts):
            forbidden_paths.append(relative.as_posix())
        if path.name in {".env"} or path.name.endswith(
            ("-wal", "-shm", ".pyc", ".lock")
        ):
            forbidden_paths.append(relative.as_posix())
    if forbidden_paths:
        raise BundleError(
            "bundle contains forbidden paths: "
            + ", ".join(sorted(set(forbidden_paths)))
        )

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        for token in forbidden_tokens:
            if token and token in content:
                raise BundleError(
                    f"bundle contains forbidden source path in {path}"
                )


def _zip_deterministic(source: Path, archive: Path) -> None:
    fixed_time = (2020, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as output:
        paths = [source, *sorted(source.rglob("*"))]
        for path in paths:
            relative = Path(source.name) / path.relative_to(source)
            name = relative.as_posix()
            if path.is_dir():
                name = name.rstrip("/") + "/"
                mode = stat.S_IFDIR | (path.stat().st_mode & 0o777)
                data = b""
            else:
                mode = stat.S_IFREG | (path.stat().st_mode & 0o777)
                data = path.read_bytes()
            info = zipfile.ZipInfo(name, date_time=fixed_time)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, data)


def build_demo_bundle(
    *,
    workspace: Path,
    lightrag_workspace: Path,
    lightrag_license: Path,
    output_dir: Path,
    wheel: Path | None = None,
) -> tuple[Path, Path, Path]:
    version = project_version()
    release_name = f"{BUNDLE_PREFIX}-{version}"
    output_dir.mkdir(parents=True, exist_ok=True)
    release = output_dir / release_name
    archive = output_dir / f"{release_name}.zip"
    archive_checksum = output_dir / f"{release_name}.zip.sha256"
    existing = [
        path
        for path in (release, archive, archive_checksum)
        if path.exists()
    ]
    if existing:
        raise BundleError(
            "bundle target already exists: "
            + ", ".join(str(path) for path in existing)
        )

    commit, dirty = source_revision()
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{release_name}-",
            dir=output_dir,
        )
    )
    try:
        workspace_destination = staging / "workspace"
        counts = _copy_workspace(
            workspace.resolve(),
            workspace_destination,
        )
        database_counts = _sanitize_database(
            workspace
            / "artifacts"
            / "state"
            / "evo_wiki.sqlite3",
            workspace_destination
            / "artifacts"
            / "state"
            / "evo_wiki.sqlite3",
        )
        snapshot_count = _copy_snapshots(
            workspace.resolve(),
            workspace_destination,
        )
        index_count = _copy_lightrag_index(
            lightrag_workspace.resolve(),
            staging,
        )

        packages = staging / "packages"
        if wheel is None:
            wheel_path = _build_python_wheel(packages)
        else:
            wheel_path = _copy_python_wheel(
                wheel.resolve(),
                packages,
            )
        _copy_templates(
            staging,
            version=version,
            commit=commit,
            wheel_name=wheel_path.name,
        )
        _copy_file(ROOT / "LICENSE", staging / "LICENSE")
        _copy_file(
            lightrag_license.resolve(),
            staging / "licenses" / "LightRAG-LICENSE",
        )
        manifest = {
            "schema_version": 1,
            "name": release_name,
            "evo_wiki_version": version,
            "source": {
                "commit": commit,
                "dirty": dirty,
            },
            "runtime": {
                "python": ">=3.10",
                "platforms": ["macOS", "Linux"],
                "lightrag_image": LIGHTRAG_IMAGE,
                "lightrag_workspace": "evo_wiki",
                "llm_model": "qwen-plus",
                "embedding_model": "text-embedding-v3",
                "embedding_dimension": 1024,
            },
            "contents": {
                **counts,
                **database_counts,
                "state_snapshots": snapshot_count,
                "lightrag_index_files": index_count,
            },
            "excluded": [
                "credentials and .env",
                "LightRAG LLM response cache",
                "query and audit history",
                "notifications and gateway instances",
                "run journals, logs, backups, locks, WAL and SHM",
                "machine-specific reports and absolute paths",
            ],
        }
        _write_json(staging / "bundle-manifest.json", manifest)

        forbidden_tokens = (
            str(workspace.resolve()).encode(),
            str(ROOT.resolve()).encode(),
            str(ROOT.parent.resolve()).encode(),
        )
        _assert_clean_bundle(staging, forbidden_tokens)
        _write_checksums(staging)
        staging.replace(release)
        _zip_deterministic(release, archive)
        archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive_checksum.write_text(
            f"{archive_digest}  {archive.name}\n",
            encoding="utf-8",
        )
        return release, archive, archive_checksum
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the sanitized Evo Wiki legal demo bundle.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=ROOT / "workspace" / "ui-demo",
    )
    parser.add_argument(
        "--lightrag-workspace",
        type=Path,
        default=(
            ROOT.parent
            / "lightrag"
            / "data"
            / "rag_storage"
            / "evo_wiki"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist",
    )
    parser.add_argument(
        "--lightrag-license",
        type=Path,
        default=ROOT.parent / "lightrag" / "LICENSE",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        help="Use an existing 2.0.1 wheel instead of building one.",
    )
    args = parser.parse_args()
    try:
        release, archive, checksum = build_demo_bundle(
            workspace=args.workspace.resolve(),
            lightrag_workspace=args.lightrag_workspace.resolve(),
            lightrag_license=args.lightrag_license.resolve(),
            output_dir=args.output_dir.resolve(),
            wheel=args.wheel,
        )
    except (BundleError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(release)
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
import getpass
import json
import os
import shutil
import socket
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..corpus import CorpusFile, corpus_hash
from ..journal import (
    DEFAULT_MAX_BYTES_PER_FILE,
    DEFAULT_MAX_EVENTS_PER_FILE,
    RunJournalWriter,
    verify_logs_root,
)
from ..lightrag_sync import RemoteTrackState, parse_track_status
from .notifications import (
    build_notification,
    notification_settings,
)
from ..utils import read_json, utc_now, write_json_atomic
from .contracts import (
    ActionGate,
    BackupResult,
    ExportResult,
    MigrationResult,
    ReconcileObservation,
    ReconcileResult,
    ReplacementImpact,
    ReplacementBackupSummary,
    ReplacementExecutionResult,
    ReplacementMaintenance,
    ReplacementOperationSummary,
    ReplacementPlan,
    ReplacementPlanResult,
    ReplacementRemoteDocument,
    ReplacementRollback,
    ReplacementStatusResult,
    RemoteStatus,
    SchemaMigrationResult,
    StateError,
    VerificationCheck,
    VerificationResult,
)
from .schema import (
    INITIAL_SCHEMA_VERSION,
    MIGRATIONS,
    QUERY_GOVERNANCE_SCHEMA_VERSION,
    REPLACEMENT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    migration_checksums,
    required_indexes_for_version,
    required_tables_for_version,
)
from .store import (
    DEFAULT_DATABASE,
    StateStore,
    _canonical_json,
    _deterministic_id,
    _fsync_directory,
    _private_directory,
    _private_file,
    _sha256_json,
    lightrag_backend_identity,
    normalize_workspace_relative_path,
    resolve_under_root,
)


LEGACY_PATHS = (
    "project.json",
    "artifacts/corpus-state.json",
    "artifacts/wiki/state/corpus-state.json",
    "artifacts/lightrag/state/corpus-state.json",
    "artifacts/lightrag/state/lightrag-import-ledger.json",
    "artifacts/manifest.json",
    "artifacts/wiki/progress.json",
)


def _utc_filename() -> str:
    return (
        datetime.now(timezone.utc)
        .strftime("%Y%m%dT%H%M%S.%fZ")
    )


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return "sha256:" + hasher.hexdigest()


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _safe_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
    ) as exc:
        raise StateError(
            "legacy state JSON is invalid",
            error_code="STATE_LEGACY_JSON_INVALID",
            details={"relative_path": path.name},
        ) from exc


def _parse_corpus_files(
    raw: Any,
    *,
    label: str,
) -> tuple[list[CorpusFile], list[dict[str, Any]]]:
    if not isinstance(raw, dict):
        raise StateError(
            f"{label} must be a JSON object",
            error_code="STATE_LEGACY_SCHEMA_UNSUPPORTED",
        )
    raw_files = raw.get("files", [])
    if not isinstance(raw_files, list):
        raise StateError(
            f"{label}.files must be a list",
            error_code="STATE_LEGACY_SCHEMA_UNSUPPORTED",
        )
    files: list[CorpusFile] = []
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            raise StateError(
                f"{label}.files[{index}] must be an object",
                error_code="STATE_LEGACY_SCHEMA_UNSUPPORTED",
            )
        path = item.get("path")
        sha256 = item.get("sha256")
        size = item.get("size")
        suffix = item.get("suffix")
        text_like = item.get("text_like")
        normalized_path = None
        if isinstance(path, str) and path:
            try:
                normalized_path = normalize_workspace_relative_path(
                    path,
                    field_name=f"{label}.files[{index}].path",
                )
            except StateError:
                normalized_path = None
        digest = sha256.removeprefix("sha256:") if isinstance(sha256, str) else ""
        if (
            not isinstance(path, str)
            or not path
            or normalized_path != path
            or not isinstance(sha256, str)
            or not sha256.startswith("sha256:")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(suffix, str)
            or not isinstance(text_like, bool)
        ):
            raise StateError(
                f"{label}.files[{index}] has invalid fields",
                error_code="STATE_LEGACY_SCHEMA_UNSUPPORTED",
            )
        if path in seen:
            raise StateError(
                f"{label} contains a duplicate source path",
                error_code="STATE_LEGACY_DUPLICATE_PATH",
            )
        seen.add(path)
        files.append(
            CorpusFile(
                path=path,
                sha256=sha256,
                size=size,
                suffix=suffix,
                text_like=text_like,
            )
        )
    expected_hash = raw.get("corpus_hash")
    actual_hash = corpus_hash(files)
    if isinstance(expected_hash, str) and expected_hash != actual_hash:
        warnings.append(
            {
                "code": "LEGACY_CORPUS_HASH_MISMATCH",
                "scope": label,
                "expected": expected_hash,
                "actual": actual_hash,
            }
        )
    return files, warnings


class LegacyInventory:
    def __init__(self, root: Path):
        self.root = root
        self.raw_bytes: dict[str, bytes] = {}
        for relative in LEGACY_PATHS:
            path = root / relative
            if path.exists():
                self.raw_bytes[relative] = path.read_bytes()
        fingerprint_input = [
            {
                "path": relative,
                "sha256": _sha256_bytes(self.raw_bytes[relative]),
                "size": len(self.raw_bytes[relative]),
            }
            for relative in sorted(self.raw_bytes)
        ]
        self.fingerprint = _sha256_json(fingerprint_input)

        global_raw = _safe_json(
            root / "artifacts/corpus-state.json",
            {"files": []},
        )
        wiki_raw = _safe_json(
            root / "artifacts/wiki/state/corpus-state.json",
            {"files": []},
        )
        lightrag_raw = _safe_json(
            root / "artifacts/lightrag/state/corpus-state.json",
            {"files": []},
        )
        self.global_files, global_warnings = _parse_corpus_files(
            global_raw,
            label="global_corpus_state",
        )
        self.wiki_files, wiki_warnings = _parse_corpus_files(
            wiki_raw,
            label="wiki_corpus_state",
        )
        self.lightrag_files, lightrag_warnings = _parse_corpus_files(
            lightrag_raw,
            label="lightrag_corpus_state",
        )
        self.warnings = global_warnings + wiki_warnings + lightrag_warnings

        self.ledger = _safe_json(
            root / "artifacts/lightrag/state/lightrag-import-ledger.json",
            {"documents": {}},
        )
        if not isinstance(self.ledger, dict) or not isinstance(
            self.ledger.get("documents", {}),
            dict,
        ):
            raise StateError(
                "legacy LightRAG ledger has an unsupported shape",
                error_code="STATE_LEGACY_SCHEMA_UNSUPPORTED",
            )

    @property
    def counts(self) -> dict[str, int]:
        return {
            "legacy_files": len(self.raw_bytes),
            "global_files": len(self.global_files),
            "wiki_files": len(self.wiki_files),
            "lightrag_files": len(self.lightrag_files),
            "lightrag_bindings": len(self.ledger.get("documents", {})),
        }

    def metadata_for(
        self,
        source_path: str,
        sha256: str,
    ) -> CorpusFile:
        for item in (
            self.lightrag_files
            + self.global_files
            + self.wiki_files
        ):
            if item.path == source_path and item.sha256 == sha256:
                return item
        source = resolve_under_root(self.root, source_path)
        size = source.stat().st_size if source.exists() else 0
        return CorpusFile(
            path=source_path,
            sha256=sha256,
            size=size,
            suffix=source.suffix.lower(),
            text_like=source.suffix.lower()
            in {".md", ".txt", ".html", ".htm", ".csv", ".json", ".yaml", ".yml"},
        )


@contextmanager
def operation_lock(lock_path: Path) -> Iterator[None]:
    """Hold an OS advisory lock for migration/cutover operations."""
    _private_directory(lock_path.parent)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        elif os.name == "nt":
            import msvcrt

            os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        yield
    finally:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(descriptor)


def _state_backend(root: Path) -> str:
    project_path = root / "project.json"
    project = read_json(project_path, {}) if project_path.exists() else {}
    if not isinstance(project, dict):
        raise StateError(
            "project.json must contain a JSON object",
            error_code="STATE_CONFIG_INVALID",
        )
    if "state" not in project:
        return "legacy_json"
    state = project["state"]
    if not isinstance(state, dict):
        raise StateError(
            "project state configuration must be an object",
            error_code="STATE_CONFIG_INVALID",
        )
    backend = state.get("backend")
    if backend is None:
        return "legacy_json"
    if backend not in {"legacy_json", "sqlite"}:
        raise StateError(
            "state.backend must be legacy_json or sqlite",
            error_code="STATE_CONFIG_INVALID",
        )
    return backend


def _activate_sqlite_config(root: Path, state_config: dict[str, Any]) -> None:
    project_path = root / "project.json"
    project = read_json(project_path, {})
    if not isinstance(project, dict):
        raise StateError(
            "project.json must contain a JSON object",
            error_code="STATE_CONFIG_INVALID",
        )
    current_state = project.get("state")
    merged_state = dict(current_state) if isinstance(current_state, dict) else {}
    merged_state.update(
        {
            "backend": "sqlite",
            "database": state_config.get("database", DEFAULT_DATABASE),
            "busy_timeout_seconds": state_config.get(
                "busy_timeout_seconds",
                15,
            ),
        }
    )
    project["state"] = merged_state
    write_json_atomic(project_path, project)
    if os.name == "posix":
        project_path.chmod(0o600)
    _fsync_directory(project_path.parent)


def _operation_journal(
    root: Path,
    *,
    run_id: str,
    config: dict[str, Any],
) -> RunJournalWriter:
    return RunJournalWriter(
        root / "artifacts/logs",
        run_id=run_id,
        max_events_per_file=config.get(
            "max_events_per_file",
            DEFAULT_MAX_EVENTS_PER_FILE,
        ),
        max_bytes_per_file=config.get(
            "max_bytes_per_file",
            DEFAULT_MAX_BYTES_PER_FILE,
        ),
    )


def _journal_config(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise StateError(
            "project journal configuration must be an object",
            error_code="STATE_CONFIG_INVALID",
        )
    return dict(value)


class StateMigrator:
    def __init__(
        self,
        root: Path,
        state_config: dict[str, Any] | None = None,
        *,
        lightrag_config: dict[str, Any] | None = None,
        journal_config: dict[str, Any] | None = None,
    ):
        self.root = root.resolve()
        self.state_config = dict(state_config or {})
        self.lightrag_config = dict(lightrag_config or {})
        self.journal_config = _journal_config(journal_config)
        self.final_store = StateStore(self.root, self.state_config)

    def dry_run(self) -> MigrationResult:
        backend = _state_backend(self.root)
        if backend == "sqlite":
            if not self.final_store.exists:
                raise StateError(
                    "project selects SQLite but the state database is missing",
                    error_code="STATE_DATABASE_MISSING",
                )
            verification = StateVerifier(self.final_store).verify(
                include_journal=False
            )
            latest = (
                self.final_store.latest_migration()
                if verification.overall_status != "FAIL"
                else None
            )
            return MigrationResult(
                status=(
                    "failed"
                    if verification.overall_status == "FAIL"
                    else "already_applied"
                ),
                mode="dry_run",
                workspace_mutated=False,
                migratable=verification.overall_status != "FAIL",
                legacy_input_fingerprint=(
                    latest["source_fingerprint"]
                    if latest is not None
                    else _sha256_json([])
                ),
                database=self.final_store.database_relative,
                state_commit_seq=verification.state_commit_seq,
                imported_counts=(
                    json.loads(latest["imported_counts_json"])
                    if latest is not None
                    else {}
                ),
                warnings=[
                    {"code": check.code}
                    for check in verification.checks
                    if check.status == "WARN"
                ],
                error_code=(
                    "STATE_MIGRATION_VERIFY_FAILED"
                    if verification.overall_status == "FAIL"
                    else None
                ),
            )
        inventory = LegacyInventory(self.root)
        warnings = list(inventory.warnings)
        if self.final_store.exists:
            verification = StateVerifier(self.final_store).verify(
                include_journal=False
            )
            if verification.overall_status == "FAIL":
                return MigrationResult(
                    status="failed",
                    mode="dry_run",
                    workspace_mutated=False,
                    migratable=False,
                    legacy_input_fingerprint=inventory.fingerprint,
                    database=self.final_store.database_relative,
                    state_commit_seq=verification.state_commit_seq,
                    imported_counts=inventory.counts,
                    warnings=warnings,
                    error_code="STATE_MIGRATION_VERIFY_FAILED",
                )
            installed = self.final_store.migration_for_fingerprint(
                inventory.fingerprint
            )
            if installed is None:
                return MigrationResult(
                    status="failed",
                    mode="dry_run",
                    workspace_mutated=False,
                    migratable=False,
                    legacy_input_fingerprint=inventory.fingerprint,
                    database=self.final_store.database_relative,
                    state_commit_seq=verification.state_commit_seq,
                    imported_counts=inventory.counts,
                    warnings=warnings,
                    error_code="STATE_CUTOVER_CONFLICT",
                )
            warnings.append({"code": "STATE_CUTOVER_RESUME_REQUIRED"})
            return MigrationResult(
                status="ready",
                mode="dry_run",
                workspace_mutated=False,
                migratable=True,
                legacy_input_fingerprint=inventory.fingerprint,
                database=self.final_store.database_relative,
                state_commit_seq=verification.state_commit_seq,
                imported_counts=inventory.counts,
                warnings=warnings,
            )
        with tempfile.TemporaryDirectory(prefix="evo-wiki-migrate-") as directory:
            candidate = Path(directory) / "candidate.sqlite3"
            store = StateStore(
                self.root,
                self.state_config,
                database_path=candidate,
            )
            store.initialize()
            self._import_inventory(store, inventory, backup_manifest_path=None)
            verification = StateVerifier(store).verify(include_journal=False)
            if verification.overall_status == "FAIL":
                return MigrationResult(
                    status="failed",
                    mode="dry_run",
                    workspace_mutated=False,
                    migratable=False,
                    legacy_input_fingerprint=inventory.fingerprint,
                    imported_counts=inventory.counts,
                    warnings=warnings,
                    error_code="STATE_MIGRATION_VERIFY_FAILED",
                )
        return MigrationResult(
            status="ready",
            mode="dry_run",
            workspace_mutated=False,
            migratable=True,
            legacy_input_fingerprint=inventory.fingerprint,
            imported_counts=inventory.counts,
            warnings=warnings,
        )

    def apply(self) -> MigrationResult:
        lock_path = self.final_store.state_root / "migration.lock"
        if _state_backend(self.root) == "sqlite":
            return self._apply_active_sqlite(lock_path=lock_path)
        with operation_lock(lock_path):
            backend = _state_backend(self.root)
            if backend == "sqlite":
                return self._apply_active_sqlite(
                    lock_path=lock_path,
                    lock_held=True,
                )

            inventory = LegacyInventory(self.root)
            journal = _operation_journal(
                self.root,
                run_id=f"migration-{uuid.uuid4().hex}",
                config=self.journal_config,
            )
            journal.append(
                event_type="state.migration_started",
                phase="backup",
                status="RUNNING",
                lane="operations",
                safe_payload={
                    "operation": "migrate_state",
                    "schema_version": SCHEMA_VERSION,
                    "input_fingerprint": inventory.fingerprint,
                },
            )
            candidate: Path | None = None
            try:
                if self.final_store.exists:
                    return self._resume_installed_candidate(
                        inventory,
                        journal=journal,
                    )

                backup_manifest = self._backup_legacy(inventory)
                _private_directory(self.final_store.state_root)
                descriptor, candidate_name = tempfile.mkstemp(
                    prefix=".evo_wiki.",
                    suffix=".candidate.sqlite3",
                    dir=self.final_store.state_root,
                )
                os.close(descriptor)
                candidate = Path(candidate_name)
                candidate.unlink()
                candidate_store = StateStore(
                    self.root,
                    self.state_config,
                    database_path=candidate,
                )
                candidate_store.initialize()
                self._import_inventory(
                    candidate_store,
                    inventory,
                    backup_manifest_path=backup_manifest,
                )
                candidate_store.record_migration(
                    migration_id=_deterministic_id(
                        "migration",
                        inventory.fingerprint,
                    ),
                    source_fingerprint=inventory.fingerprint,
                    status="CANDIDATE_VERIFIED",
                    backup_manifest_path=backup_manifest,
                    imported_counts=inventory.counts,
                )
                verification = StateVerifier(candidate_store).verify(
                    include_journal=False,
                )
                if verification.overall_status == "FAIL":
                    raise StateError(
                        "candidate SQLite failed migration verification",
                        error_code="STATE_MIGRATION_VERIFY_FAILED",
                    )
                self._checkpoint_candidate(candidate_store)
                _private_file(candidate)
                os.replace(candidate, self.final_store.database_path)
                _private_file(self.final_store.database_path)
                _fsync_directory(self.final_store.state_root)
                self.final_store.initialize()
                self.final_store.record_migration(
                    migration_id=_deterministic_id(
                        "migration",
                        inventory.fingerprint,
                    ),
                    source_fingerprint=inventory.fingerprint,
                    status="DB_INSTALLED_CONFIG_LEGACY",
                    backup_manifest_path=backup_manifest,
                    imported_counts=inventory.counts,
                )
                _activate_sqlite_config(
                    self.root,
                    self.state_config,
                )
                self.final_store.record_migration(
                    migration_id=_deterministic_id(
                        "migration",
                        inventory.fingerprint,
                    ),
                    source_fingerprint=inventory.fingerprint,
                    status="SQLITE_ACTIVE",
                    backup_manifest_path=backup_manifest,
                    imported_counts=inventory.counts,
                )
                export = StateExporter(self.final_store).export()
                journal.append(
                    event_type="state.migration_completed",
                    phase="cutover",
                    status="SUCCEEDED",
                    lane="operations",
                    safe_payload={
                        "operation": "migrate_state",
                        "schema_version": SCHEMA_VERSION,
                        "state_commit_seq": self.final_store.state_commit_seq(),
                        "export_succeeded": export.export_succeeded,
                    },
                )
                return MigrationResult(
                    status="applied",
                    mode="apply",
                    workspace_mutated=True,
                    migratable=True,
                    legacy_input_fingerprint=inventory.fingerprint,
                    database=self.final_store.database_relative,
                    state_commit_seq=self.final_store.state_commit_seq(),
                    imported_counts=inventory.counts,
                    warnings=inventory.warnings,
                )
            except Exception as exc:
                if candidate is not None:
                    try:
                        for path in (
                            candidate,
                            candidate.with_name(candidate.name + "-wal"),
                            candidate.with_name(candidate.name + "-shm"),
                        ):
                            if path.exists():
                                path.unlink()
                        _fsync_directory(self.final_store.state_root)
                    except OSError:
                        pass
                try:
                    journal.append(
                        event_type="state.migration_failed",
                        phase="cutover",
                        status="FAILED",
                        lane="operations",
                        safe_payload={
                            "operation": "migrate_state",
                            "schema_version": SCHEMA_VERSION,
                            "error_code": (
                                exc.error_code
                                if isinstance(exc, StateError)
                                else "STATE_MIGRATION_FAILED"
                            ),
                        },
                    )
                except Exception:
                    pass
                raise

    def _apply_active_sqlite(
        self,
        *,
        lock_path: Path,
        lock_held: bool = False,
    ) -> MigrationResult:
        if not self.final_store.exists:
            raise StateError(
                "project selects SQLite but the state database is missing",
                error_code="STATE_DATABASE_MISSING",
            )
        verification = StateVerifier(self.final_store).verify(
            include_journal=False
        )
        if verification.overall_status == "FAIL":
            raise StateError(
                "active SQLite state failed verification",
                error_code="STATE_MIGRATION_VERIFY_FAILED",
            )
        pending = self.final_store.latest_migration()
        export_stale = (
            self.final_store.last_exported_state_commit_seq()
            != self.final_store.state_commit_seq()
        )
        recovery_required = (
            pending is not None
            and pending["status"] != "SQLITE_ACTIVE"
        ) or export_stale
        if recovery_required and not lock_held:
            with operation_lock(lock_path):
                return self._apply_active_sqlite(
                    lock_path=lock_path,
                    lock_held=True,
                )
        fingerprint = (
            pending["source_fingerprint"]
            if pending is not None
            else _sha256_json([])
        )
        imported_counts = (
            json.loads(pending["imported_counts_json"])
            if pending is not None
            else {}
        )
        if not recovery_required:
            return MigrationResult(
                status="already_applied",
                mode="apply",
                workspace_mutated=False,
                migratable=True,
                legacy_input_fingerprint=fingerprint,
                database=self.final_store.database_relative,
                state_commit_seq=self.final_store.state_commit_seq(),
                imported_counts=imported_counts,
                warnings=[
                    {"code": check.code}
                    for check in verification.checks
                    if check.status == "WARN"
                ],
            )

        recovery_journal = _operation_journal(
            self.root,
            run_id=f"migration-recover-{uuid.uuid4().hex}",
            config=self.journal_config,
        )
        recovery_journal.append(
            event_type="state.migration_started",
            phase="cutover_recovery",
            status="RUNNING",
            lane="operations",
            safe_payload={
                "operation": "migrate_state",
                "schema_version": SCHEMA_VERSION,
                "database_active": True,
                "export_stale": export_stale,
            },
        )
        if pending is not None:
            self.final_store.record_migration(
                migration_id=pending["id"],
                source_fingerprint=pending["source_fingerprint"],
                status="SQLITE_ACTIVE",
                backup_manifest_path=pending.get("backup_manifest_path"),
                imported_counts=imported_counts,
            )
        StateExporter(self.final_store).export()
        recovery_journal.append(
            event_type="state.migration_completed",
            phase="cutover_recovery",
            status="SUCCEEDED",
            lane="operations",
            safe_payload={
                "operation": "migrate_state",
                "state_commit_seq": self.final_store.state_commit_seq(),
                "export_succeeded": True,
            },
        )
        return MigrationResult(
            status="applied",
            mode="apply",
            workspace_mutated=True,
            migratable=True,
            legacy_input_fingerprint=fingerprint,
            database=self.final_store.database_relative,
            state_commit_seq=self.final_store.state_commit_seq(),
            imported_counts=imported_counts,
            warnings=[
                {"code": check.code}
                for check in verification.checks
                if check.status == "WARN"
            ],
        )

    def abort_candidate(self) -> MigrationResult:
        lock_path = self.final_store.state_root / "migration.lock"
        with operation_lock(lock_path):
            if _state_backend(self.root) == "sqlite":
                raise StateError(
                    "cannot abort an active SQLite state database",
                    error_code="STATE_CANDIDATE_ABORT_REFUSED",
                )
            inventory = LegacyInventory(self.root)
            if not self.final_store.exists:
                return MigrationResult(
                    status="ready",
                    mode="abort_candidate",
                    workspace_mutated=True,
                    migratable=True,
                    legacy_input_fingerprint=inventory.fingerprint,
                    imported_counts=inventory.counts,
                    warnings=[{"code": "STATE_CANDIDATE_NOT_FOUND"}],
                )
            orphan_root = self.final_store.state_root / "orphaned"
            _private_directory(orphan_root)
            orphan = (
                orphan_root
                / f"evo_wiki-{_utc_filename()}-{uuid.uuid4().hex[:8]}.sqlite3"
            )
            os.replace(self.final_store.database_path, orphan)
            _private_file(orphan)
            for suffix in ("-wal", "-shm"):
                companion = self.final_store.database_path.with_name(
                    self.final_store.database_path.name + suffix
                )
                if companion.exists():
                    os.replace(
                        companion,
                        orphan.with_name(orphan.name + suffix),
                    )
            _fsync_directory(orphan_root)
            journal = _operation_journal(
                self.root,
                run_id=f"migration-abort-{uuid.uuid4().hex}",
                config=self.journal_config,
            )
            journal.append(
                event_type="state.migration_candidate_aborted",
                phase="cutover",
                status="SUCCEEDED",
                lane="operations",
                safe_payload={
                    "operation": "abort_candidate",
                    "schema_version": SCHEMA_VERSION,
                    "orphan_id": orphan.stem,
                },
            )
            return MigrationResult(
                status="ready",
                mode="abort_candidate",
                workspace_mutated=True,
                migratable=True,
                legacy_input_fingerprint=inventory.fingerprint,
                imported_counts=inventory.counts,
                warnings=[{"code": "STATE_CANDIDATE_MOVED_TO_ORPHANED"}],
            )

    def _resume_installed_candidate(
        self,
        inventory: LegacyInventory,
        *,
        journal: RunJournalWriter,
    ) -> MigrationResult:
        self.final_store.initialize()
        migration = self.final_store.migration_for_fingerprint(
            inventory.fingerprint
        )
        if migration is None:
            raise StateError(
                "installed SQLite candidate does not match current legacy input",
                error_code="STATE_CUTOVER_CONFLICT",
            )
        verification = StateVerifier(self.final_store).verify(
            include_journal=False,
        )
        if verification.overall_status == "FAIL":
            raise StateError(
                "installed SQLite candidate failed verification",
                error_code="STATE_MIGRATION_VERIFY_FAILED",
            )
        _activate_sqlite_config(self.root, self.state_config)
        self.final_store.record_migration(
            migration_id=migration["id"],
            source_fingerprint=inventory.fingerprint,
            status="SQLITE_ACTIVE",
            backup_manifest_path=migration.get("backup_manifest_path"),
            imported_counts=json.loads(migration["imported_counts_json"]),
        )
        export = StateExporter(self.final_store).export()
        journal.append(
            event_type="state.migration_completed",
            phase="cutover_resume",
            status="SUCCEEDED",
            lane="operations",
            safe_payload={
                "operation": "migrate_state",
                "resumed_candidate": True,
                "state_commit_seq": self.final_store.state_commit_seq(),
                "export_succeeded": export.export_succeeded,
            },
        )
        return MigrationResult(
            status="applied",
            mode="apply",
            workspace_mutated=True,
            migratable=True,
            legacy_input_fingerprint=inventory.fingerprint,
            database=self.final_store.database_relative,
            state_commit_seq=self.final_store.state_commit_seq(),
            imported_counts=inventory.counts,
            warnings=inventory.warnings,
        )

    def _backup_legacy(self, inventory: LegacyInventory) -> str:
        backup_root = (
            self.root
            / "artifacts/migration-backup"
            / f"{_utc_filename()}-{uuid.uuid4().hex[:8]}-json-to-sqlite"
        )
        _private_directory(backup_root)
        manifest_files = []
        for relative, content in sorted(inventory.raw_bytes.items()):
            destination = backup_root / relative
            _private_directory(destination.parent)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if os.name == "posix":
                    temporary.chmod(0o600)
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
            finally:
                if temporary.exists():
                    temporary.unlink()
            manifest_files.append(
                {
                    "path": relative,
                    "sha256": _sha256_bytes(content),
                    "size_bytes": len(content),
                }
            )
        manifest = {
            "schema_version": 1,
            "created_at": utc_now(),
            "source_fingerprint": inventory.fingerprint,
            "files": manifest_files,
        }
        manifest_path = backup_root / "manifest.json"
        write_json_atomic(manifest_path, manifest)
        _private_file(manifest_path)
        _fsync_directory(backup_root)
        _fsync_directory(backup_root.parent)
        return manifest_path.relative_to(self.root).as_posix()

    def _import_inventory(
        self,
        store: StateStore,
        inventory: LegacyInventory,
        *,
        backup_manifest_path: str | None,
    ) -> None:
        if inventory.wiki_files:
            store.import_legacy_lane_baseline(
                lane="wiki",
                files=inventory.wiki_files,
                source_fingerprint=inventory.fingerprint,
            )
        if inventory.lightrag_files:
            store.import_legacy_lane_baseline(
                lane="lightrag",
                files=inventory.lightrag_files,
                source_fingerprint=inventory.fingerprint,
            )
        if inventory.global_files:
            store.import_legacy_lane_baseline(
                lane="global",
                files=inventory.global_files,
                source_fingerprint=inventory.fingerprint,
            )
        legacy_documents = inventory.ledger.get("documents", {})
        if not legacy_documents:
            store.record_migration(
                migration_id=_deterministic_id(
                    "migration",
                    inventory.fingerprint,
                ),
                source_fingerprint=inventory.fingerprint,
                status="CANDIDATE_VERIFIED",
                backup_manifest_path=backup_manifest_path,
                imported_counts=inventory.counts,
            )
            return
        partition_id, backend_fingerprint = store.ensure_partition(
            self.lightrag_config or inventory.ledger.get("service")
        )
        seen_bindings: set[tuple[str, str]] = set()
        for raw in legacy_documents.values():
            if not isinstance(raw, dict):
                raise StateError(
                    "legacy LightRAG ledger contains an invalid record",
                    error_code="STATE_LEGACY_SCHEMA_UNSUPPORTED",
                )
            source_path = raw.get("source_path")
            sha256 = raw.get("sha256")
            digest = (
                sha256.removeprefix("sha256:")
                if isinstance(sha256, str)
                else ""
            )
            try:
                normalized_source = normalize_workspace_relative_path(
                    source_path,
                    field_name="legacy_lightrag_binding.source_path",
                )
            except StateError as exc:
                raise StateError(
                    "legacy LightRAG ledger source path is invalid",
                    error_code="STATE_LEGACY_SCHEMA_UNSUPPORTED",
                ) from exc
            if (
                normalized_source != source_path
                or not isinstance(sha256, str)
                or not sha256.startswith("sha256:")
                or len(digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in digest
                )
            ):
                raise StateError(
                    "legacy LightRAG ledger record is missing source_path or sha256",
                    error_code="STATE_LEGACY_SCHEMA_UNSUPPORTED",
                )
            binding_key = (source_path, sha256)
            if binding_key in seen_bindings:
                raise StateError(
                    "legacy LightRAG ledger contains a duplicate binding",
                    error_code="STATE_LEGACY_DUPLICATE_ID",
                )
            seen_bindings.add(binding_key)
            item = inventory.metadata_for(source_path, sha256)
            store.import_legacy_binding(
                source_path=item.path,
                sha256=item.sha256,
                size=item.size,
                suffix=item.suffix,
                text_like=item.text_like,
                track_id=(
                    raw.get("service_track_id")
                    if isinstance(raw.get("service_track_id"), str)
                    else None
                ),
                submitted_at=(
                    raw.get("submitted_at")
                    if isinstance(raw.get("submitted_at"), str)
                    else None
                ),
                processed_at=(
                    raw.get("processed_at")
                    if isinstance(raw.get("processed_at"), str)
                    else None
                ),
                partition_id=partition_id,
                backend_fingerprint=backend_fingerprint,
            )
        store.record_migration(
            migration_id=_deterministic_id(
                "migration",
                inventory.fingerprint,
            ),
            source_fingerprint=inventory.fingerprint,
            status="CANDIDATE_VERIFIED",
            backup_manifest_path=backup_manifest_path,
            imported_counts=inventory.counts,
        )

    @staticmethod
    def _checkpoint_candidate(store: StateStore) -> None:
        connection = store.connect()
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        for suffix in ("-wal", "-shm"):
            companion = store.database_path.with_name(
                store.database_path.name + suffix
            )
            if companion.exists():
                companion.unlink()
        _fsync_directory(store.database_path.parent)


class StateExporter:
    def __init__(self, store: StateStore):
        self.store = store

    def export(self) -> ExportResult:
        state_seq = self.store.state_commit_seq()
        generated_at = utc_now()
        try:
            projection = self.store.export_rows()
            state_seq = projection["state_commit_seq"]
            metadata = {
                "exported_from": "sqlite",
                "schema_version": 1,
                "db_commit_seq": state_seq,
                "generated_at": generated_at,
            }
            exported: list[str] = []
            all_files = [
                CorpusFile(**item) for item in projection["all_files"]
            ]
            global_state = {
                "files": projection["all_files"],
                "corpus_hash": corpus_hash(all_files),
                **metadata,
            }
            exports: list[tuple[Path, dict[str, Any]]] = [
                (
                    self.store.root / "artifacts/corpus-state.json",
                    global_state,
                )
            ]
            for lane in ("wiki", "lightrag"):
                lane_raw = projection["lane_files"][lane]
                lane_files = [CorpusFile(**item) for item in lane_raw]
                exports.append(
                    (
                        self.store.root
                        / f"artifacts/{lane}/state/corpus-state.json",
                        {
                            "files": lane_raw,
                            "corpus_hash": corpus_hash(lane_files),
                            **metadata,
                        },
                    )
                )
            exports.append(
                (
                    self.store.root
                    / "artifacts/lightrag/state/lightrag-import-ledger.json",
                    {
                        "documents": projection["lightrag_documents"],
                        **metadata,
                    },
                )
            )
            manifest_entries = []
            for path, payload in exports:
                write_json_atomic(path, payload)
                _private_file(path)
                _fsync_directory(path.parent)
                relative = path.relative_to(self.store.root).as_posix()
                exported.append(relative)
                manifest_entries.append(
                    {
                        "path": relative,
                        "sha256": _sha256_file(path),
                    }
                )
            manifest_sha256 = _sha256_json(manifest_entries)
            self.store.set_export_metadata(
                state_commit_seq=state_seq,
                generated_at=generated_at,
                export_manifest_sha256=manifest_sha256,
            )
            return ExportResult(
                status="success",
                state_committed=True,
                state_commit_seq=state_seq,
                export_succeeded=True,
                exported_paths=exported,
            )
        except Exception as exc:
            raise StateError(
                "SQLite state committed but compatibility export failed",
                error_code="STATE_EXPORT_FAILED",
                committed=True,
                details={
                    "state_commit_seq": state_seq,
                    "export_succeeded": False,
                },
            ) from exc


class StateVerifier:
    def __init__(self, store: StateStore):
        self.store = store

    def verify(self, *, include_journal: bool = True) -> VerificationResult:
        checks: list[VerificationCheck] = []
        if not self.store.database_path.exists():
            checks.append(
                VerificationCheck(
                    name="database_exists",
                    status="FAIL",
                    code="STATE_DATABASE_MISSING",
                )
            )
            return self._result(checks, None, None, None)

        schema_version: int | None = None
        state_seq: int | None = None
        exported_seq: int | None = None
        try:
            connection = self.store.connect(read_only=True)
            try:
                integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
                integrity_values = [row[0] for row in integrity]
                checks.append(
                    VerificationCheck(
                        name="sqlite_integrity",
                        status=(
                            "PASS"
                            if integrity_values == ["ok"]
                            else "FAIL"
                        ),
                        code=(
                            "STATE_INTEGRITY_OK"
                            if integrity_values == ["ok"]
                            else "STATE_INTEGRITY_FAILED"
                        ),
                    )
                )
                foreign_keys = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                checks.append(
                    VerificationCheck(
                        name="foreign_keys",
                        status="PASS" if not foreign_keys else "FAIL",
                        code=(
                            "STATE_FOREIGN_KEYS_OK"
                            if not foreign_keys
                            else "STATE_FOREIGN_KEYS_FAILED"
                        ),
                    )
                )
                meta_rows = connection.execute(
                    """
                    SELECT version, name, checksum
                    FROM schema_meta ORDER BY version
                    """
                ).fetchall()
                user_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                schema_version = (
                    None
                    if not meta_rows
                    else int(meta_rows[-1]["version"])
                )
                checksums = migration_checksums()
                expected_chain = {
                    migration.version: (
                        migration.name,
                        checksums[migration.version],
                    )
                    for migration in MIGRATIONS
                    if migration.version <= user_version
                }
                observed_chain = {
                    int(row["version"]): (
                        str(row["name"]),
                        str(row["checksum"]),
                    )
                    for row in meta_rows
                }
                schema_ok = (
                    INITIAL_SCHEMA_VERSION
                    <= user_version
                    <= SCHEMA_VERSION
                    and schema_version == user_version
                    and observed_chain == expected_chain
                )
                schema_current = schema_ok and user_version == SCHEMA_VERSION
                checks.append(
                    VerificationCheck(
                        name="schema_version",
                        status=(
                            "PASS"
                            if schema_current
                            else "WARN"
                            if schema_ok
                            else "FAIL"
                        ),
                        code=(
                            "STATE_SCHEMA_OK"
                            if schema_current
                            else "STATE_SCHEMA_UPGRADE_AVAILABLE"
                            if schema_ok
                            else "STATE_SCHEMA_MISMATCH"
                        ),
                    )
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table'
                        """
                    ).fetchall()
                }
                indexes = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'index'
                        """
                    ).fetchall()
                }
                required_tables = (
                    required_tables_for_version(user_version)
                    if schema_ok
                    else set()
                )
                required_indexes = (
                    required_indexes_for_version(user_version)
                    if schema_ok
                    else set()
                )
                missing_tables = sorted(required_tables - tables)
                missing_indexes = sorted(required_indexes - indexes)
                checks.append(
                    VerificationCheck(
                        name="required_objects",
                        status=(
                            "PASS"
                            if not missing_tables and not missing_indexes
                            else "FAIL"
                        ),
                        code=(
                            "STATE_OBJECTS_OK"
                            if not missing_tables and not missing_indexes
                            else "STATE_OBJECTS_MISSING"
                        ),
                        detail=(
                            None
                            if not missing_tables and not missing_indexes
                            else _canonical_json(
                                {
                                    "missing_tables": missing_tables,
                                    "missing_indexes": missing_indexes,
                                }
                            )
                        ),
                    )
                )
                invalid_runs = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT id FROM lane_run
                        WHERE (
                          status IN (
                            'SUCCEEDED', 'FAILED', 'NEEDS_AUDIT', 'CANCELLED'
                          )
                          AND finished_at IS NULL
                        )
                        OR (status = 'RUNNING' AND finished_at IS NOT NULL)
                        LIMIT 20
                        """
                    ).fetchall()
                ]
                invalid_bindings = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT b.id
                        FROM lightrag_binding b
                        JOIN retrieval_partition p
                          ON p.id = b.retrieval_partition_id
                        JOIN source_revision r ON r.id = b.revision_id
                        JOIN source_document d ON d.id = r.source_id
                        WHERE b.backend_fingerprint != p.backend_fingerprint
                           OR d.security_domain_id != p.security_domain_id
                        LIMIT 20
                        """
                    ).fetchall()
                ]
                relationship_failures = {
                    "run_ids": invalid_runs,
                    "binding_ids": invalid_bindings,
                }
                relationships_ok = not invalid_runs and not invalid_bindings
                checks.append(
                    VerificationCheck(
                        name="core_relationships",
                        status="PASS" if relationships_ok else "FAIL",
                        code=(
                            "STATE_RELATIONSHIPS_OK"
                            if relationships_ok
                            else "STATE_RELATIONSHIP_INVALID"
                        ),
                        detail=(
                            None
                            if relationships_ok
                            else _canonical_json(relationship_failures)
                        ),
                    )
                )
                snapshot_failures = []
                for row in connection.execute(
                    """
                    SELECT id, sha256, snapshot_path
                    FROM source_revision
                    WHERE snapshot_status = 'AVAILABLE'
                    """
                ).fetchall():
                    raw_snapshot = row["snapshot_path"]
                    if not isinstance(raw_snapshot, str):
                        snapshot_failures.append(row["id"])
                        continue
                    snapshot = Path(raw_snapshot)
                    if snapshot.is_absolute():
                        try:
                            self.store.database_path.relative_to(
                                self.store.root
                            )
                        except ValueError:
                            pass
                        else:
                            snapshot_failures.append(row["id"])
                            continue
                    else:
                        try:
                            normalized = normalize_workspace_relative_path(
                                raw_snapshot,
                                field_name="source_revision.snapshot_path",
                            )
                            snapshot = resolve_under_root(
                                self.store.root,
                                normalized,
                            )
                        except StateError:
                            snapshot_failures.append(row["id"])
                            continue
                    if (
                        not snapshot.exists()
                        or not snapshot.is_file()
                        or _sha256_file(snapshot) != row["sha256"]
                    ):
                        snapshot_failures.append(row["id"])
                checks.append(
                    VerificationCheck(
                        name="immutable_snapshots",
                        status=(
                            "PASS"
                            if not snapshot_failures
                            else "FAIL"
                        ),
                        code=(
                            "STATE_SNAPSHOTS_OK"
                            if not snapshot_failures
                            else "STATE_SNAPSHOT_INTEGRITY_FAILED"
                        ),
                        detail=(
                            None
                            if not snapshot_failures
                            else _canonical_json(
                                {"revision_ids": snapshot_failures[:20]}
                            )
                        ),
                    )
                )
                state_seq = int(
                    connection.execute(
                        """
                        SELECT state_commit_seq FROM state_clock
                        WHERE singleton = 1
                        """
                    ).fetchone()[0]
                )
                export_row = connection.execute(
                    """
                    SELECT last_exported_state_commit_seq
                    FROM compatibility_export WHERE singleton = 1
                    """
                ).fetchone()
                exported_seq = (
                    None
                    if export_row is None or export_row[0] is None
                    else int(export_row[0])
                )
                checks.append(
                    VerificationCheck(
                        name="compatibility_export_freshness",
                        status=(
                            "PASS"
                            if exported_seq == state_seq
                            else "WARN"
                        ),
                        code=(
                            "STATE_EXPORT_CURRENT"
                            if exported_seq == state_seq
                            else "STATE_EXPORT_STALE"
                        ),
                    )
                )
                export_paths = (
                    "artifacts/corpus-state.json",
                    "artifacts/wiki/state/corpus-state.json",
                    "artifacts/lightrag/state/corpus-state.json",
                    "artifacts/lightrag/state/lightrag-import-ledger.json",
                )
                export_issues = []
                for relative in export_paths:
                    path = self.store.root / relative
                    try:
                        payload = _safe_json(path, None)
                    except StateError:
                        payload = None
                    if (
                        not isinstance(payload, dict)
                        or payload.get("exported_from") != "sqlite"
                        or payload.get("db_commit_seq") != exported_seq
                    ):
                        export_issues.append(relative)
                checks.append(
                    VerificationCheck(
                        name="compatibility_export_files",
                        status="PASS" if not export_issues else "WARN",
                        code=(
                            "STATE_EXPORT_FILES_OK"
                            if not export_issues
                            else "STATE_EXPORT_FILES_MISSING_OR_INVALID"
                        ),
                        detail=(
                            None
                            if not export_issues
                            else _canonical_json({"paths": export_issues})
                        ),
                    )
                )
                journal_mode = str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower()
                checks.append(
                    VerificationCheck(
                        name="journal_mode",
                        status="PASS" if journal_mode == "wal" else "WARN",
                        code=(
                            "STATE_WAL_ENABLED"
                            if journal_mode == "wal"
                            else "STATE_WAL_NOT_ENABLED"
                        ),
                    )
                )
            finally:
                connection.close()
        except (sqlite3.Error, StateError) as exc:
            checks.append(
                VerificationCheck(
                    name="database_read",
                    status="FAIL",
                    code=(
                        exc.error_code
                        if isinstance(exc, StateError)
                        else "STATE_DATABASE_READ_FAILED"
                    ),
                )
            )

        checks.append(self._permission_check())
        if include_journal:
            logs_report = verify_logs_root(
                self.store.root / "artifacts/logs"
            )
            checks.append(
                VerificationCheck(
                    name="operation_journal",
                    status=(
                        "PASS"
                        if logs_report["status"] == "ok"
                        else "WARN"
                    ),
                    code=(
                        "STATE_JOURNAL_OK"
                        if logs_report["status"] == "ok"
                        else "STATE_JOURNAL_INCOMPLETE"
                    ),
                )
            )
        return self._result(
            checks,
            schema_version,
            state_seq,
            exported_seq,
        )

    def _permission_check(self) -> VerificationCheck:
        if os.name != "posix":
            return VerificationCheck(
                name="filesystem_permissions",
                status="WARN",
                code="STATE_PERMISSIONS_BEST_EFFORT",
                detail=os.name,
            )
        database_mode = stat.S_IMODE(self.store.database_path.stat().st_mode)
        directory_mode = stat.S_IMODE(self.store.state_root.stat().st_mode)
        good = database_mode == 0o600 and directory_mode == 0o700
        return VerificationCheck(
            name="filesystem_permissions",
            status="PASS" if good else "WARN",
            code=(
                "STATE_PERMISSIONS_ENFORCED"
                if good
                else "STATE_PERMISSIONS_WEAK"
            ),
            detail=f"database={oct(database_mode)},directory={oct(directory_mode)}",
        )

    def _result(
        self,
        checks: list[VerificationCheck],
        schema_version: int | None,
        state_seq: int | None,
        exported_seq: int | None,
    ) -> VerificationResult:
        statuses = {check.status for check in checks}
        overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
        return VerificationResult(
            overall_status=overall,
            database=self.store.database_relative,
            database_schema_version=schema_version,
            state_commit_seq=state_seq,
            last_exported_state_commit_seq=exported_seq,
            checks=checks,
        )


class StateBackupService:
    def __init__(
        self,
        store: StateStore,
        journal_config: dict[str, Any] | None = None,
    ):
        self.store = store
        self.journal_config = _journal_config(journal_config)

    def backup(self) -> BackupResult:
        backup_id = f"backup-{uuid.uuid4().hex}"
        created_at = utc_now()
        observed_state_seq = self.store.state_commit_seq()
        backups_root = self.store.state_root / "backups"
        _private_directory(backups_root)
        timestamp = _utc_filename()
        temporary = backups_root / f".evo_wiki-{timestamp}.{backup_id}.tmp"
        journal = _operation_journal(
            self.store.root,
            run_id=f"backup-{uuid.uuid4().hex}",
            config=self.journal_config,
        )
        journal.append(
            event_type="state.backup_started",
            phase="backup",
            status="RUNNING",
            lane="operations",
            safe_payload={
                "operation": "state_backup",
                "backup_id": backup_id,
                "state_commit_seq": observed_state_seq,
                "schema_version": self.store.schema_version(),
            },
        )
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
            os.close(descriptor)
            source = self.store.connect(read_only=True)
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
                source.close()
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            backup_store = StateStore(
                self.store.root,
                {
                    "database": self.store.database_relative,
                    "busy_timeout_seconds": self.store.busy_timeout_ms / 1000,
                },
                database_path=temporary,
            )
            verification = StateVerifier(backup_store).verify(
                include_journal=False
            )
            if verification.overall_status == "FAIL":
                raise StateError(
                    "SQLite backup failed post-copy verification",
                    error_code="STATE_BACKUP_VERIFY_FAILED",
                )
            if verification.state_commit_seq is None:
                raise StateError(
                    "SQLite backup is missing its business state sequence",
                    error_code="STATE_BACKUP_VERIFY_FAILED",
                )
            state_seq = verification.state_commit_seq
            filename = (
                f"evo_wiki-{timestamp}-s{state_seq:08d}-"
                f"{backup_id[-8:]}.sqlite3"
            )
            final_path = backups_root / filename
            try:
                os.link(temporary, final_path)
            except FileExistsError as exc:
                raise StateError(
                    "refusing to overwrite an existing SQLite backup",
                    error_code="STATE_BACKUP_COLLISION",
                ) from exc
            temporary.unlink()
            _private_file(final_path)
            _fsync_directory(backups_root)
            result = BackupResult(
                status="success",
                backup_id=backup_id,
                backup_path=final_path.relative_to(self.store.root).as_posix(),
                created_at=created_at,
                database_schema_version=verification.database_schema_version,
                state_commit_seq=state_seq,
                size_bytes=final_path.stat().st_size,
                sha256=_sha256_file(final_path),
                verification_status=verification.overall_status,
            )
            journal.append(
                event_type="state.backup_completed",
                phase="backup",
                status="SUCCEEDED",
                lane="operations",
                safe_payload={
                    "operation": "state_backup",
                    "backup_id": backup_id,
                    "state_commit_seq": state_seq,
                    "size_bytes": result.size_bytes,
                    "verification_status": result.verification_status,
                },
            )
            return result
        except Exception as exc:
            if temporary.exists():
                temporary.unlink()
            journal.append(
                event_type="state.backup_failed",
                phase="backup",
                status="FAILED",
                lane="operations",
                safe_payload={
                    "operation": "state_backup",
                    "backup_id": backup_id,
                    "state_commit_seq": observed_state_seq,
                    "error_code": (
                        exc.error_code
                        if isinstance(exc, StateError)
                        else "STATE_BACKUP_FAILED"
                    ),
                },
            )
            if isinstance(exc, StateError):
                raise
            raise StateError(
                "SQLite backup failed",
                error_code="STATE_BACKUP_FAILED",
            ) from exc


class StateSchemaMigrator:
    """Explicit, backup-first migration of an existing SQLite workspace."""

    def __init__(
        self,
        store: StateStore,
        journal_config: dict[str, Any] | None = None,
    ):
        self.store = store
        self.journal_config = _journal_config(journal_config)

    def plan(self) -> SchemaMigrationResult:
        current = self.store.schema_version()
        pending = [
            migration.name
            for migration in self.store.pending_schema_migrations()
        ]
        return SchemaMigrationResult(
            status="ready" if pending else "already_applied",
            mode="dry_run",
            workspace_mutated=False,
            database_schema_version=current,
            target_database_schema_version=SCHEMA_VERSION,
            pending_migrations=pending,
            state_commit_seq=self.store.state_commit_seq(),
        )

    def apply(self) -> SchemaMigrationResult:
        preview = self.plan()
        if not preview.pending_migrations:
            return SchemaMigrationResult(
                status="already_applied",
                mode="apply",
                workspace_mutated=False,
                database_schema_version=preview.database_schema_version,
                target_database_schema_version=SCHEMA_VERSION,
                pending_migrations=[],
                state_commit_seq=preview.state_commit_seq,
            )

        journal = _operation_journal(
            self.store.root,
            run_id=f"schema-migrate-{uuid.uuid4().hex}",
            config=self.journal_config,
        )
        journal.append(
            event_type="state.schema_migration_started",
            phase="schema_migration",
            status="RUNNING",
            lane="operations",
            safe_payload={
                "operation": "migrate_schema",
                "from_version": preview.database_schema_version,
                "to_version": SCHEMA_VERSION,
                "pending_count": len(preview.pending_migrations),
            },
        )
        backup = StateBackupService(
            self.store,
            self.journal_config,
        ).backup()
        if (
            backup.verification_status == "FAIL"
            or backup.state_commit_seq is None
            or backup.sha256 is None
        ):
            raise StateError(
                "schema migration backup could not be verified",
                error_code="STATE_SCHEMA_BACKUP_FAILED",
            )
        if self.store.state_commit_seq() != backup.state_commit_seq:
            raise StateError(
                "business state changed after the schema migration backup",
                error_code="STATE_SCHEMA_BACKUP_STALE",
            )
        try:
            applied = self.store.apply_pending_schema_migrations()
            verification = StateVerifier(self.store).verify(
                include_journal=False
            )
            if (
                verification.overall_status == "FAIL"
                or verification.database_schema_version != SCHEMA_VERSION
            ):
                raise StateError(
                    "migrated SQLite schema failed verification",
                    error_code="STATE_SCHEMA_MIGRATION_VERIFY_FAILED",
                )
            journal.append(
                event_type="state.schema_migration_completed",
                phase="schema_migration",
                status="SUCCEEDED",
                lane="operations",
                safe_payload={
                    "operation": "migrate_schema",
                    "from_version": preview.database_schema_version,
                    "to_version": SCHEMA_VERSION,
                    "applied_count": len(applied),
                    "backup_id": backup.backup_id,
                    "state_commit_seq": verification.state_commit_seq,
                },
            )
            return SchemaMigrationResult(
                status="applied",
                mode="apply",
                workspace_mutated=True,
                database_schema_version=SCHEMA_VERSION,
                target_database_schema_version=SCHEMA_VERSION,
                pending_migrations=applied,
                backup_id=backup.backup_id,
                backup_sha256=backup.sha256,
                state_commit_seq=verification.state_commit_seq,
            )
        except Exception as exc:
            journal.append(
                event_type="state.schema_migration_failed",
                phase="schema_migration",
                status="FAILED",
                lane="operations",
                safe_payload={
                    "operation": "migrate_schema",
                    "from_version": preview.database_schema_version,
                    "to_version": SCHEMA_VERSION,
                    "backup_id": backup.backup_id,
                    "error_code": (
                        exc.error_code
                        if isinstance(exc, StateError)
                        else "STATE_SCHEMA_MIGRATION_FAILED"
                    ),
                },
            )
            if isinstance(exc, StateError):
                raise
            raise StateError(
                "SQLite schema migration failed",
                error_code="STATE_SCHEMA_MIGRATION_FAILED",
            ) from exc


class StateReconciler:
    def __init__(
        self,
        store: StateStore,
        lightrag_config: dict[str, Any],
        journal_config: dict[str, Any] | None = None,
    ):
        self.store = store
        self.lightrag_config = lightrag_config
        self.journal_config = _journal_config(journal_config)

    def reconcile(self, *, apply: bool = False) -> ReconcileResult:
        from ..lightrag_lane import (
            LightRAGBuildError,
            LightRAGServiceClient,
            resolve_lightrag_service_config,
        )

        bindings = self.store.bindings_for_reconcile()
        if not bindings:
            return ReconcileResult(
                status="applied" if apply else "ready",
                mode="apply" if apply else "dry_run",
                workspace_mutated=False,
                observations=[],
            )
        service = resolve_lightrag_service_config(self.lightrag_config)
        client = LightRAGServiceClient(
            service["base_url"],
            headers=service["headers"],
            timeout=service["timeout_seconds"],
        )
        journal: RunJournalWriter | None = None
        if apply:
            journal = _operation_journal(
                self.store.root,
                run_id=f"reconcile-{uuid.uuid4().hex}",
                config=self.journal_config,
            )
            journal.append(
                event_type="state.reconcile_started",
                phase="observe",
                status="RUNNING",
                lane="operations",
                safe_payload={
                    "operation": "state_reconcile",
                    "binding_count": len(bindings),
                },
            )
        observations: list[ReconcileObservation] = []
        backend_failed = False
        try:
            for binding in bindings:
                before_remote = RemoteStatus(binding["remote_status"])
                before_gate = ActionGate(binding["action_gate"])
                track_id = binding["track_id"]
                if not isinstance(track_id, str) or not track_id:
                    observed = RemoteStatus.UNKNOWN
                    resulting_gate = ActionGate.BLOCKED
                    gate_reason = "REMOTE_STATUS_UNCONFIRMED"
                    total_chunks = None
                    error_code = "TRACK_ID_MISSING"
                else:
                    try:
                        payload = client.request_json(
                            "GET",
                            f"/documents/track_status/{track_id}",
                        )
                        snapshot = parse_track_status(payload, track_id)
                        observed, resulting_gate, gate_reason = _map_snapshot(
                            snapshot.state
                        )
                        total_chunks = snapshot.total_chunks
                        error_code = snapshot.error_code
                        if snapshot.state is RemoteTrackState.INVALID:
                            backend_failed = True
                            error_code = (
                                snapshot.error_code
                                or "REMOTE_STATUS_INVALID"
                            )
                        if binding.get("gate_reason") == "REMOTE_HTTP_409":
                            resulting_gate = ActionGate.BLOCKED
                            gate_reason = "REMOTE_HTTP_409"
                            error_code = "REMOTE_HTTP_409"
                    except LightRAGBuildError as exc:
                        if "HTTP 404" in str(exc):
                            observed = RemoteStatus.MISSING
                            gate_reason = "REMOTE_MISSING"
                            error_code = "REMOTE_TRACK_MISSING"
                        else:
                            observed = RemoteStatus.UNKNOWN
                            gate_reason = "REMOTE_STATUS_UNCONFIRMED"
                            error_code = "REMOTE_STATUS_REQUEST_FAILED"
                            backend_failed = True
                        resulting_gate = ActionGate.BLOCKED
                        total_chunks = None
                observation = ReconcileObservation(
                    binding_id=binding["id"],
                    track_id=track_id,
                    before_remote_status=before_remote,
                    observed_remote_status=observed,
                    before_action_gate=before_gate,
                    resulting_action_gate=resulting_gate,
                    gate_reason=gate_reason,
                    total_chunks=total_chunks,
                    error_code=error_code,
                )
                observations.append(observation)
                if apply:
                    self.store.mark_binding_observation(
                        binding["id"],
                        remote_status=observed,
                        action_gate=resulting_gate,
                        gate_reason=gate_reason,
                        chunk_count=total_chunks,
                        error_code=error_code,
                    )
                    if resulting_gate is ActionGate.OPEN:
                        self.store.activate_processed_binding(
                            binding["id"]
                        )
            if journal is not None:
                journal.append(
                    event_type=(
                        "state.reconcile_failed"
                        if backend_failed
                        else "state.reconcile_completed"
                    ),
                    phase="observe",
                    status="FAILED" if backend_failed else "SUCCEEDED",
                    lane="operations",
                    safe_payload={
                        "operation": "state_reconcile",
                        "binding_count": len(observations),
                        "processed_count": sum(
                            item.observed_remote_status
                            is RemoteStatus.PROCESSED
                            for item in observations
                        ),
                        "error_code": (
                            "REMOTE_RECONCILE_FAILED"
                            if backend_failed
                            else None
                        ),
                    },
                )
            return ReconcileResult(
                status=(
                    "failed"
                    if backend_failed
                    else "applied"
                    if apply
                    else "ready"
                ),
                mode="apply" if apply else "dry_run",
                workspace_mutated=apply and bool(observations),
                observations=observations,
                error_code=(
                    "REMOTE_RECONCILE_FAILED"
                    if backend_failed
                    else None
                ),
            )
        except Exception as exc:
            if journal is not None:
                journal.append(
                    event_type="state.reconcile_failed",
                    phase="observe",
                    status="FAILED",
                    lane="operations",
                    safe_payload={
                        "operation": "state_reconcile",
                        "error_code": (
                            exc.error_code
                            if isinstance(exc, StateError)
                            else "STATE_RECONCILE_FAILED"
                        ),
                    },
                )
            raise


def _map_snapshot(
    state: RemoteTrackState,
) -> tuple[RemoteStatus, ActionGate, str | None]:
    if state is RemoteTrackState.PROCESSED:
        return RemoteStatus.PROCESSED, ActionGate.OPEN, None
    if state is RemoteTrackState.FAILED:
        return RemoteStatus.FAILED, ActionGate.BLOCKED, "REMOTE_FAILED"
    if state is RemoteTrackState.PROCESSING:
        return (
            RemoteStatus.PROCESSING,
            ActionGate.BLOCKED,
            "REMOTE_STATUS_UNCONFIRMED",
        )
    if state is RemoteTrackState.WAITING:
        return (
            RemoteStatus.PENDING,
            ActionGate.BLOCKED,
            "REMOTE_STATUS_UNCONFIRMED",
        )
    return (
        RemoteStatus.UNKNOWN,
        ActionGate.BLOCKED,
        "REMOTE_STATUS_UNCONFIRMED",
    )


_REPLACEMENT_REQUIRED_APPROVALS = [
    "SQLITE_BACKUP_VERIFIED",
    "DELETE_EXPLICITLY_AUTHORIZED",
    "REPLACEMENT_REVIEW_APPROVED",
]
_REPLACEMENT_STEPS = [
    "VERIFY_SQLITE_BACKUP",
    "DELETE_REMOTE_DOCUMENT",
    "WAIT_REMOTE_DOCUMENT_MISSING",
    "SUBMIT_TARGET_REVISION",
    "WAIT_TARGET_PROCESSED",
    "RUN_SMOKE_EVIDENCE_CHECK",
    "COMMIT_REVISION_SWITCH",
    "DELETE_FAILED_TARGET_IF_ATTRIBUTABLE",
    "ROLL_BACK_FROM_OWNER_SNAPSHOT_ON_FAILURE",
]
_REMOTE_DOCUMENT_STATUSES = {
    "pending",
    "parsing",
    "analyzing",
    "processing",
    "preprocessed",
    "processed",
    "failed",
}
_REMOTE_TERMINAL_STATUSES = {"processed", "failed"}
_MAX_DOCUMENT_INVENTORY_PAGES = 10_000


class ReplacementPlanner:
    """Build a deterministic, zero-write replacement review plan."""

    def __init__(
        self,
        store: StateStore,
        lightrag_config: dict[str, Any],
    ):
        self.store = store
        self.lightrag_config = lightrag_config

    def plan(self) -> ReplacementPlanResult:
        from ..lightrag_lane import (
            LightRAGBuildError,
            LightRAGServiceClient,
            parse_lightrag_capabilities,
            resolve_lightrag_service_config,
        )

        bindings = self.store.bindings_for_replace_plan()
        if not bindings:
            return ReplacementPlanResult(status="no_conflicts")

        try:
            service = resolve_lightrag_service_config(self.lightrag_config)
            client = LightRAGServiceClient(
                service["base_url"],
                headers=service["headers"],
                timeout=service["timeout_seconds"],
            )
            health = client.request_json("GET", "/health")
            if not isinstance(health, dict) or health.get("status") != "healthy":
                return ReplacementPlanResult(
                    status="failed",
                    error_code="SERVICE_UNHEALTHY",
                )
            try:
                openapi = client.request_json("GET", "/openapi.json")
            except LightRAGBuildError:
                openapi = None
            capabilities = parse_lightrag_capabilities(
                health,
                openapi,
                expected_workspace=service["workspace"],
                requested_embedding_batch_size=service[
                    "embedding_batch_size"
                ],
            )
            if capabilities.workspace is None:
                return ReplacementPlanResult(
                    status="failed",
                    error_code="WORKSPACE_UNCONFIRMED",
                )
            if capabilities.workspace_matches is False:
                return ReplacementPlanResult(
                    status="failed",
                    error_code="WORKSPACE_MISMATCH",
                )
            if capabilities.storage_workspaces_match is False:
                return ReplacementPlanResult(
                    status="failed",
                    error_code="STORAGE_WORKSPACE_MISMATCH",
                )

            capability_blockers: list[str] = []
            if capabilities.supports_document_delete is not True:
                capability_blockers.append(
                    "DOCUMENT_DELETE_CAPABILITY_UNCONFIRMED"
                )
            if capabilities.supports_document_inventory is not True:
                capability_blockers.append(
                    "DOCUMENT_INVENTORY_CAPABILITY_UNCONFIRMED"
                )
            if capabilities.supports_pipeline_status is not True:
                capability_blockers.append(
                    "PIPELINE_STATUS_CAPABILITY_UNCONFIRMED"
                )

            documents: list[ReplacementRemoteDocument] = []
            inventory_available = (
                capabilities.supports_document_inventory is True
            )
            if inventory_available:
                documents = _load_remote_document_inventory(client)

            pipeline_idle: bool | None = None
            if capabilities.supports_pipeline_status is True:
                pipeline = client.request_json(
                    "GET",
                    "/documents/pipeline_status",
                )
                pipeline_idle = _parse_pipeline_idle(pipeline)

            expected_namespace, expected_fingerprint = (
                lightrag_backend_identity(self.lightrag_config)
            )
            plans = [
                self._plan_binding(
                    binding,
                    documents=documents,
                    inventory_available=inventory_available,
                    pipeline_idle=pipeline_idle,
                    capability_blockers=capability_blockers,
                    expected_namespace=expected_namespace,
                    expected_fingerprint=expected_fingerprint,
                )
                for binding in bindings
            ]
        except LightRAGBuildError as exc:
            return ReplacementPlanResult(
                status="failed",
                error_code=(
                    exc.failure_code
                    or "REMOTE_REPLACE_PLAN_REQUEST_FAILED"
                ),
            )
        except (StateError, ValueError):
            return ReplacementPlanResult(
                status="failed",
                error_code="REMOTE_REPLACE_PLAN_RESPONSE_INVALID",
            )

        blocked = any(plan.review_status == "blocked" for plan in plans)
        return ReplacementPlanResult(
            status="blocked" if blocked else "ready",
            plans=plans,
            error_code="REPLACE_PLAN_BLOCKED" if blocked else None,
        )

    def _plan_binding(
        self,
        binding: dict[str, Any],
        *,
        documents: list[ReplacementRemoteDocument],
        inventory_available: bool,
        pipeline_idle: bool | None,
        capability_blockers: list[str],
        expected_namespace: str,
        expected_fingerprint: str,
    ) -> ReplacementPlan:
        blockers = list(capability_blockers)
        if pipeline_idle is False:
            _append_once(blockers, "REMOTE_PIPELINE_BUSY")
        elif pipeline_idle is None:
            _append_once(blockers, "PIPELINE_STATE_UNCONFIRMED")
        if binding["namespace"] != expected_namespace:
            _append_once(blockers, "PARTITION_WORKSPACE_MISMATCH")
        if binding["backend_fingerprint"] != expected_fingerprint:
            _append_once(blockers, "BACKEND_FINGERPRINT_MISMATCH")

        source_path = str(binding["file_source"])
        canonical_basename = _canonical_basename(source_path)
        matches = (
            [
                document
                for document in documents
                if _canonical_basename(document.file_path)
                == canonical_basename
            ]
            if inventory_available
            else []
        )
        remote_document = matches[0] if len(matches) == 1 else None
        if inventory_available and not matches:
            _append_once(blockers, "REMOTE_DOCUMENT_NOT_FOUND")
        elif len(matches) > 1:
            _append_once(blockers, "REMOTE_DOCUMENT_AMBIGUOUS")
        if (
            remote_document is not None
            and remote_document.status not in _REMOTE_TERMINAL_STATUSES
        ):
            _append_once(blockers, "REMOTE_DOCUMENT_NOT_TERMINAL")

        if not _workspace_file_matches(
            self.store.root,
            source_path,
            str(binding["sha256"]),
        ):
            _append_once(blockers, "TARGET_SOURCE_HASH_MISMATCH")
        if not _snapshot_matches(
            self.store.root,
            snapshot_path=binding["snapshot_path"],
            snapshot_status=binding["snapshot_status"],
            expected_sha256=str(binding["sha256"]),
        ):
            _append_once(blockers, "TARGET_SNAPSHOT_UNAVAILABLE")

        owner: dict[str, Any] | None = None
        if remote_document is None or remote_document.track_id is None:
            _append_once(blockers, "REMOTE_OWNER_TRACK_UNCONFIRMED")
        else:
            owner_candidates = self.store.replacement_owner_candidates(
                source_path=source_path,
                target_binding_id=str(binding["id"]),
                retrieval_partition_id=str(
                    binding["retrieval_partition_id"]
                ),
                backend_fingerprint=str(
                    binding["backend_fingerprint"]
                ),
            )
            matching_owners = [
                candidate
                for candidate in owner_candidates
                if candidate["track_id"] == remote_document.track_id
            ]
            if len(matching_owners) == 1:
                owner = matching_owners[0]
            elif not matching_owners:
                _append_once(blockers, "REMOTE_OWNER_UNVERIFIED")
            else:
                _append_once(blockers, "REMOTE_OWNER_AMBIGUOUS")

        rollback_available = False
        if owner is not None:
            rollback_available = _snapshot_matches(
                self.store.root,
                snapshot_path=owner["snapshot_path"],
                snapshot_status=owner["snapshot_status"],
                expected_sha256=str(owner["sha256"]),
            )
            if not rollback_available:
                _append_once(blockers, "ROLLBACK_SNAPSHOT_UNAVAILABLE")

        plan_id = _deterministic_id(
            "replace-plan",
            str(binding["id"]),
            (
                remote_document.doc_id
                if remote_document is not None
                else ""
            ),
            str(owner["revision_id"]) if owner is not None else "",
            str(binding["sha256"]),
        )
        digest_payload = {
            "schema_version": 1,
            "plan_id": plan_id,
            "binding_id": str(binding["id"]),
            "target_revision_id": str(binding["revision_id"]),
            "target_sha256": str(binding["sha256"]),
            "retrieval_partition_id": str(
                binding["retrieval_partition_id"]
            ),
            "backend_fingerprint": str(binding["backend_fingerprint"]),
            "expected_namespace": expected_namespace,
            "expected_backend_fingerprint": expected_fingerprint,
            "remote_document": (
                remote_document.model_dump(mode="json")
                if remote_document is not None
                else None
            ),
            "owner_binding_id": (
                str(owner["id"]) if owner is not None else None
            ),
            "owner_revision_id": (
                str(owner["revision_id"]) if owner is not None else None
            ),
            "owner_revision_sha256": (
                str(owner["sha256"]) if owner is not None else None
            ),
            "rollback_available": rollback_available,
            "blockers": blockers,
            "steps": _REPLACEMENT_STEPS,
            "max_delete_requests": 2,
            "max_submission_requests": 2,
        }
        return ReplacementPlan(
            plan_id=plan_id,
            plan_digest=_sha256_json(digest_payload),
            binding_id=str(binding["id"]),
            source_path=source_path,
            target_revision_id=str(binding["revision_id"]),
            target_sha256=str(binding["sha256"]),
            review_status="blocked" if blockers else "ready",
            blockers=blockers,
            remote_document=remote_document,
            impact=ReplacementImpact(
                chunk_count=(
                    remote_document.chunks_count
                    if remote_document is not None
                    else None
                ),
            ),
            rollback=ReplacementRollback(
                owner_binding_id=(
                    str(owner["id"]) if owner is not None else None
                ),
                owner_revision_id=(
                    str(owner["revision_id"])
                    if owner is not None
                    else None
                ),
                snapshot_status=(
                    str(owner["snapshot_status"])
                    if owner is not None
                    else None
                ),
                available=rollback_available,
            ),
            required_approvals=list(_REPLACEMENT_REQUIRED_APPROVALS),
            steps=list(_REPLACEMENT_STEPS),
        )


class ReplacementOperationService:
    """Execute and inspect one durable, fail-closed 409 replacement."""

    _TERMINAL_PHASES = {"COMPLETED", "ROLLED_BACK", "FAILED"}
    _COMPENSATION_PHASES = {
        "COMPENSATION_REQUIRED",
        "TARGET_DELETE_INTENT",
        "TARGET_DELETE_ACCEPTED",
        "TARGET_DELETE_CONFIRMED",
        "OWNER_SUBMIT_INTENT",
        "OWNER_SUBMIT_ACCEPTED",
        "OWNER_PROCESSED",
        "ROLLED_BACK",
    }

    def __init__(
        self,
        store: StateStore,
        lightrag_config: dict[str, Any],
        journal_config: dict[str, Any] | None = None,
        query_gateway_config: dict[str, Any] | None = None,
        project_config: dict[str, Any] | None = None,
    ):
        self.store = store
        self.lightrag_config = lightrag_config
        self.journal_config = _journal_config(journal_config)
        self.settings = _replacement_settings(lightrag_config)
        self.query_gateway_config = query_gateway_config or {}
        self.project_config = project_config or {}
        self.notification_settings = notification_settings(
            self.project_config
        )

    def status(
        self,
        *,
        operation_id: str | None = None,
    ) -> ReplacementStatusResult:
        database_schema_version = self.store.schema_version()
        rows = self.store.list_replacement_operations(
            operation_id=operation_id
        )
        summaries = [_replacement_summary(row) for row in rows]
        if not summaries:
            status = "no_operations"
            error_code = (
                "STATE_SCHEMA_UPGRADE_REQUIRED"
                if database_schema_version < REPLACEMENT_SCHEMA_VERSION
                else None
            )
        elif any(item.status == "NEEDS_AUDIT" for item in summaries):
            status = "needs_audit"
            error_code = next(
                (
                    item.error_code
                    for item in summaries
                    if item.status == "NEEDS_AUDIT"
                ),
                "REPLACE_NEEDS_AUDIT",
            )
        elif any(item.status == "BLOCKED" for item in summaries):
            status = "blocked"
            error_code = next(
                (
                    item.error_code
                    for item in summaries
                    if item.status == "BLOCKED"
                ),
                "REPLACE_BLOCKED",
            )
        elif any(item.status == "FAILED" for item in summaries):
            status = "failed"
            error_code = next(
                (
                    item.error_code
                    for item in summaries
                    if item.status == "FAILED"
                ),
                "REPLACE_FAILED",
            )
        elif any(item.status == "IN_PROGRESS" for item in summaries):
            status = "in_progress"
            error_code = None
        elif all(item.status == "ROLLED_BACK" for item in summaries):
            status = "rolled_back"
            error_code = None
        elif all(item.status == "COMPLETED" for item in summaries):
            status = "completed"
            error_code = None
        else:
            status = "ready"
            error_code = None
        return ReplacementStatusResult(
            status=status,
            operations=summaries,
            database_schema_version=database_schema_version,
            error_code=error_code,
        )

    def execute(
        self,
        *,
        plan_id: str,
        confirm_digest: str,
        smoke_query: str,
    ) -> ReplacementExecutionResult:
        self._require_enabled()
        self.store.require_schema_version(REPLACEMENT_SCHEMA_VERSION)
        if (
            not isinstance(smoke_query, str)
            or not smoke_query.strip()
            or len(smoke_query) > 10_000
        ):
            raise StateError(
                "replacement smoke query must be non-empty and bounded",
                error_code="REPLACE_SMOKE_QUERY_INVALID",
            )
        smoke_query = smoke_query.strip()
        smoke_hash = _sha256_bytes(smoke_query.encode("utf-8"))

        active = self.store.replacement_operation_for_plan(plan_id)
        if active is None:
            operation = self._prepare_operation(
                plan_id=plan_id,
                confirm_digest=confirm_digest,
                smoke_query_sha256=smoke_hash,
            )
        else:
            if active["plan_digest"] != confirm_digest:
                raise StateError(
                    "replacement confirmation digest does not match",
                    error_code="REPLACE_CONFIRMATION_MISMATCH",
                )
            if active["smoke_query_sha256"] != smoke_hash:
                raise StateError(
                    "replacement smoke query changed during resume",
                    error_code="REPLACE_SMOKE_QUERY_MISMATCH",
                )
            operation = active
            if active["status"] == "BLOCKED":
                phase = str(active["phase"])
                if (
                    active["effect_certainty"] == "UNKNOWN"
                    or phase.endswith("_INTENT")
                ):
                    raise StateError(
                        "replacement side effect requires an operator audit",
                        error_code="REPLACE_NEEDS_AUDIT",
                    )
                operation = self.store.transition_replacement_operation(
                    str(active["id"]),
                    expected_phases={phase},
                    phase=phase,
                    status="IN_PROGRESS",
                    effect_certainty=str(active["effect_certainty"]),
                    last_effect=active.get("last_effect"),
                    error_code=None,
                    next_action=None,
                )

        journal = _operation_journal(
            self.store.root,
            run_id=(
                f"replace-{operation['id'][-24:]}-"
                f"{uuid.uuid4().hex[:8]}"
            ),
            config=self.journal_config,
        )
        journal.append(
            event_type="state.replacement_started",
            phase=str(operation["phase"]).lower(),
            status="RUNNING",
            lane="operations",
            safe_payload=_replacement_journal_payload(operation),
        )
        try:
            result = self._drive(
                operation_id=str(operation["id"]),
                smoke_query=smoke_query,
                journal=journal,
            )
        except StateError as exc:
            if not _is_replacement_remote_blocker(exc.error_code):
                raise
            current = self.store.replacement_operation(
                str(operation["id"])
            )
            if current["status"] in {
                "IN_PROGRESS",
                "BLOCKED",
            }:
                current = self._block(
                    current,
                    error_code=exc.error_code,
                    next_action=(
                        "RUN_REPLACE_RECOVER_ROLLBACK"
                        if current.get("maintenance_started_at")
                        else "RESUME_WHEN_REMOTE_IS_READY"
                    ),
                )
            result = self._execution_result(current)
        self._close_query_fence_if_terminal(result.operation.model_dump())
        terminal_event = {
            "completed": "state.replacement_completed",
            "rolled_back": "state.replacement_rolled_back",
            "needs_audit": "state.replacement_needs_audit",
            "blocked": "state.replacement_blocked",
            "failed": "state.replacement_failed",
        }[result.status]
        journal.append(
            event_type=terminal_event,
            phase=result.operation.phase.lower(),
            status=(
                "SUCCEEDED"
                if result.status in {"completed", "rolled_back"}
                else "FAILED"
            ),
            lane="operations",
            safe_payload=_replacement_journal_payload(
                result.operation.model_dump(mode="json")
            ),
        )
        return result

    def _query_drain_enabled(self) -> bool:
        return self.query_gateway_config.get("mode") in {
            "shadow",
            "enforce",
        }

    def _maintenance_notification(
        self,
        operation: dict[str, Any],
        *,
        event_type: str,
        state: str,
        error_code: str | None = None,
        delivery_required: bool = False,
        counts: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        if not self.notification_settings.enabled:
            return None
        operation_id = str(
            operation.get("id", operation.get("operation_id"))
        )
        return build_notification(
            root=self.store.root,
            event_type=event_type,
            severity="CRITICAL",
            subject_type="replacement_operation",
            subject_id=operation_id,
            dedupe_key=f"{event_type}:{operation_id}:{state}",
            security_domain=str(
                (self.project_config.get("security") or {}).get(
                    "default_domain",
                    "default",
                )
            ),
            state=state,
            error_code=error_code,
            counts=counts,
            delivery_required=delivery_required,
            max_attempts=self.notification_settings.max_attempts,
        )

    def _await_required_notification(
        self,
        *,
        dedupe_key: str,
    ) -> None:
        deadline = time.monotonic() + (
            self.notification_settings.required_delivery_timeout_seconds
        )
        while True:
            item = self.store.notification_by_dedupe(dedupe_key)
            if item["status"] == "DELIVERED":
                return
            if item["status"] == "FAILED" or time.monotonic() >= deadline:
                raise StateError(
                    "required maintenance notification was not delivered",
                    error_code="OPS_NOTIFICATION_REQUIRED_UNDELIVERED",
                )
            time.sleep(0.1)

    def _ensure_query_drained(
        self,
        operation: dict[str, Any],
    ) -> None:
        if not self._query_drain_enabled():
            return
        self.store.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        status = self.store.gateway_status()
        ready_instances = [
            instance
            for instance in status["instances"]
            if instance["retrieval_partition_id"]
            == operation["retrieval_partition_id"]
            and instance["process_status"] == "READY"
            and _iso_age_seconds(str(instance["heartbeat_at"])) <= 15
        ]
        if not ready_instances:
            raise StateError(
                "replacement requires a fresh query gateway heartbeat",
                error_code="QUERY_GATEWAY_HEARTBEAT_STALE",
            )
        fence_id = _deterministic_id(
            "fence",
            str(operation["id"]),
            str(operation["retrieval_partition_id"]),
        )
        draining_notification = self._maintenance_notification(
            operation,
            event_type="MAINTENANCE_DRAINING",
            state="DRAINING",
            delivery_required=(
                self.notification_settings.enabled
                and self.notification_settings.maintenance_delivery_required
            ),
        )
        self.store.open_maintenance_fence(
            fence_id=fence_id,
            retrieval_partition_id=str(
                operation["retrieval_partition_id"]
            ),
            replacement_operation_id=str(operation["id"]),
            reason_code="LIGHTRAG_REPLACEMENT",
            deadline_seconds=float(
                self.query_gateway_config.get(
                    "drain_timeout_seconds",
                    30,
                )
            ),
            notification=draining_notification,
        )
        deadline = time.monotonic() + float(
            self.query_gateway_config.get("drain_timeout_seconds", 30)
        )
        while True:
            drain = self.store.query_drain_status(
                str(operation["retrieval_partition_id"])
            )
            if drain["stale"]:
                self.store.transition_maintenance_fence(
                    fence_id,
                    state="FAILED",
                    notification=self._maintenance_notification(
                        operation,
                        event_type="MAINTENANCE_FAILED",
                        state="FAILED",
                        error_code="QUERY_DRAIN_STALE_LEASE",
                        counts=drain,
                    ),
                )
                raise StateError(
                    "query drain found a stale lease",
                    error_code="QUERY_DRAIN_STALE_LEASE",
                )
            if drain["active"] == 0:
                if (
                    draining_notification is not None
                    and self.notification_settings
                    .maintenance_delivery_required
                ):
                    try:
                        self._await_required_notification(
                            dedupe_key=str(
                                draining_notification["dedupe_key"]
                            )
                        )
                    except StateError:
                        self.store.transition_maintenance_fence(
                            fence_id,
                            state="FAILED",
                            notification=self._maintenance_notification(
                                operation,
                                event_type="MAINTENANCE_FAILED",
                                state="FAILED",
                                error_code=(
                                    "OPS_NOTIFICATION_REQUIRED_UNDELIVERED"
                                ),
                            ),
                        )
                        raise
                self.store.transition_maintenance_fence(
                    fence_id,
                    state="ACTIVE",
                    notification=self._maintenance_notification(
                        operation,
                        event_type="MAINTENANCE_ACTIVE",
                        state="ACTIVE",
                        counts=drain,
                    ),
                )
                return
            if time.monotonic() >= deadline:
                self.store.transition_maintenance_fence(
                    fence_id,
                    state="FAILED",
                    notification=self._maintenance_notification(
                        operation,
                        event_type="MAINTENANCE_FAILED",
                        state="FAILED",
                        error_code="QUERY_DRAIN_TIMEOUT",
                        counts=drain,
                    ),
                )
                raise StateError(
                    "query drain timed out before remote deletion",
                    error_code="QUERY_DRAIN_TIMEOUT",
                )
            time.sleep(0.1)

    def _close_query_fence_if_terminal(
        self,
        operation: dict[str, Any],
    ) -> None:
        if operation.get("status") not in {"COMPLETED", "ROLLED_BACK"}:
            return
        for fence in self.store.active_maintenance_fences():
            if fence.get("replacement_operation_id") == operation.get(
                "operation_id",
                operation.get("id"),
            ):
                self.store.transition_maintenance_fence(
                    str(fence["id"]),
                    state="CLOSED",
                    notification=self._maintenance_notification(
                        operation,
                        event_type="MAINTENANCE_CLOSED",
                        state="CLOSED",
                    ),
                )

    def recover_rollback(
        self,
        *,
        operation_id: str,
        confirm: str,
    ) -> ReplacementExecutionResult:
        self._require_enabled()
        self.store.require_schema_version(REPLACEMENT_SCHEMA_VERSION)
        if confirm != operation_id:
            raise StateError(
                "replacement recovery confirmation does not match",
                error_code="REPLACE_RECOVERY_CONFIRMATION_MISMATCH",
            )
        operation = self.store.replacement_operation(operation_id)
        if operation["phase"] in self._TERMINAL_PHASES:
            return self._execution_result(operation)
        if operation["effect_certainty"] == "UNKNOWN":
            return self._recover_unknown_effect(operation)
        if operation["phase"] != "COMPENSATION_REQUIRED":
            operation = self.store.transition_replacement_operation(
                operation_id,
                expected_phases={str(operation["phase"])},
                phase="COMPENSATION_REQUIRED",
                status="IN_PROGRESS",
                effect_certainty="KNOWN",
                error_code=operation.get("error_code"),
                next_action="ROLL_BACK_FROM_OWNER_SNAPSHOT",
                business_fact=True,
            )
        service, client = self._remote()
        operation = self._drive_compensation(
            operation,
            service=service,
            client=client,
        )
        if operation["phase"] == "ROLLED_BACK":
            StateExporter(self.store).export()
        result = self._execution_result(operation)
        self._close_query_fence_if_terminal(
            result.operation.model_dump()
        )
        return result

    def _prepare_operation(
        self,
        *,
        plan_id: str,
        confirm_digest: str,
        smoke_query_sha256: str,
    ) -> dict[str, Any]:
        verification = StateVerifier(self.store).verify()
        if verification.overall_status == "FAIL":
            raise StateError(
                "SQLite state verification failed before replacement",
                error_code="REPLACE_STATE_VERIFY_FAILED",
            )
        plan_result = ReplacementPlanner(
            self.store,
            self.lightrag_config,
        ).plan()
        plan = next(
            (item for item in plan_result.plans if item.plan_id == plan_id),
            None,
        )
        if (
            plan_result.status != "ready"
            or plan is None
            or plan.review_status != "ready"
        ):
            raise StateError(
                "replacement plan is no longer ready",
                error_code="REPLACE_PLAN_STALE",
            )
        if plan.plan_digest != confirm_digest:
            raise StateError(
                "replacement confirmation digest does not match",
                error_code="REPLACE_CONFIRMATION_MISMATCH",
            )
        if (
            plan.remote_document is None
            or plan.remote_document.track_id is None
            or plan.rollback.owner_binding_id is None
            or plan.rollback.owner_revision_id is None
        ):
            raise StateError(
                "replacement plan lacks executable owner facts",
                error_code="REPLACE_PLAN_INCOMPLETE",
            )

        context = self.store.replacement_execution_context(
            target_binding_id=plan.binding_id,
            owner_binding_id=plan.rollback.owner_binding_id,
        )
        if (
            context["target_revision_id"] != plan.target_revision_id
            or context["owner_revision_id"]
            != plan.rollback.owner_revision_id
            or context["stored_owner_track_id"]
            != plan.remote_document.track_id
        ):
            raise StateError(
                "replacement SQLite ownership changed after review",
                error_code="REPLACE_PLAN_STALE",
            )

        backup = StateBackupService(
            self.store,
            self.journal_config,
        ).backup()
        if (
            backup.verification_status == "FAIL"
            or backup.backup_path is None
            or backup.sha256 is None
            or backup.state_commit_seq is None
        ):
            raise StateError(
                "replacement backup could not be verified",
                error_code="REPLACE_BACKUP_FAILED",
            )
        if (
            verification.state_commit_seq is None
            or backup.state_commit_seq != verification.state_commit_seq
        ):
            raise StateError(
                "business state changed while preparing replacement",
                error_code="REPLACE_BACKUP_STALE",
            )

        # Re-read the zero-write plan after the backup so remote/local drift
        # cannot be hidden between review and the destructive intent.
        refreshed = ReplacementPlanner(
            self.store,
            self.lightrag_config,
        ).plan()
        refreshed_plan = next(
            (
                item
                for item in refreshed.plans
                if item.plan_id == plan_id
            ),
            None,
        )
        if (
            refreshed.status != "ready"
            or refreshed_plan is None
            or refreshed_plan.plan_digest != confirm_digest
        ):
            raise StateError(
                "replacement plan changed after the verified backup",
                error_code="REPLACE_PLAN_STALE",
            )

        operation_id = f"replace-{uuid.uuid4().hex}"
        return self.store.create_replacement_operation(
            operation_id=operation_id,
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            context=context,
            owner_remote_doc_id=plan.remote_document.doc_id,
            owner_remote_track_id=plan.remote_document.track_id,
            backup_id=backup.backup_id,
            backup_path=backup.backup_path,
            backup_sha256=backup.sha256,
            backup_state_commit_seq=backup.state_commit_seq,
            maintenance_window_seconds=self.settings[
                "maintenance_window_seconds"
            ],
            absence_confirmations=self.settings[
                "absence_confirmations"
            ],
            auto_compensate=self.settings["auto_compensate"],
            smoke_query_sha256=smoke_query_sha256,
            confirmed_by=_safe_operator_name(),
            confirmed_host=_safe_hostname(),
        )

    def _drive(
        self,
        *,
        operation_id: str,
        smoke_query: str,
        journal: RunJournalWriter,
    ) -> ReplacementExecutionResult:
        service, client = self._remote()
        while True:
            operation = self.store.replacement_operation(operation_id)
            phase = str(operation["phase"])
            if operation["status"] == "BLOCKED":
                return self._execution_result(operation)
            if phase in self._TERMINAL_PHASES or phase == "NEEDS_AUDIT":
                if phase == "COMPLETED":
                    StateExporter(self.store).export()
                elif phase == "ROLLED_BACK":
                    StateExporter(self.store).export()
                return self._execution_result(operation)
            if phase.endswith("_INTENT"):
                operation = self._needs_audit(
                    operation,
                    error_code=_unknown_effect_code(phase),
                    next_action="OBSERVE_REMOTE_STATE_WITHOUT_REPLAY",
                )
                return self._execution_result(operation)
            if phase == "PREPARED":
                self._ensure_query_drained(operation)
                self._delete_owner(
                    operation,
                    service=service,
                    client=client,
                    journal=journal,
                )
                continue
            if phase == "DELETE_ACCEPTED":
                self._confirm_deletion(
                    operation,
                    client=client,
                    target="owner",
                )
                continue
            if phase == "DELETE_CONFIRMED":
                self._submit_snapshot(
                    operation,
                    client=client,
                    owner=False,
                    journal=journal,
                )
                continue
            if phase == "SUBMIT_ACCEPTED":
                self._wait_target(
                    operation,
                    client=client,
                )
                continue
            if phase == "TARGET_PROCESSED":
                self._validate_target(
                    operation,
                    client=client,
                    smoke_query=smoke_query,
                )
                continue
            if phase == "VALIDATED":
                self.store.complete_replacement(operation_id)
                continue
            if phase in self._COMPENSATION_PHASES:
                operation = self._compensate(
                    operation,
                    service=service,
                    client=client,
                )
                if operation["phase"] in {
                    "ROLLED_BACK",
                    "NEEDS_AUDIT",
                    "FAILED",
                }:
                    if operation["phase"] == "ROLLED_BACK":
                        StateExporter(self.store).export()
                    return self._execution_result(operation)
                continue
            operation = self._needs_audit(
                operation,
                error_code="REPLACE_PHASE_UNSUPPORTED",
                next_action="INSPECT_OPERATION",
            )
            return self._execution_result(operation)

    def _delete_owner(
        self,
        operation: dict[str, Any],
        *,
        service: dict[str, Any],
        client: Any,
        journal: RunJournalWriter,
    ) -> None:
        try:
            self._assert_idle(client)
        except StateError as exc:
            self._block(
                operation,
                error_code=exc.error_code,
                next_action="RESUME_WHEN_REMOTE_PIPELINE_IS_IDLE",
            )
            return
        documents = _load_remote_document_inventory(client)
        matches = _matching_documents(
            documents,
            source_path=str(operation["source_path"]),
        )
        if (
            len(matches) != 1
            or matches[0].doc_id != operation["owner_remote_doc_id"]
            or matches[0].track_id != operation["owner_remote_track_id"]
            or matches[0].status not in _REMOTE_TERMINAL_STATUSES
        ):
            self._block(
                operation,
                error_code="REPLACE_OWNER_DRIFT",
                next_action="REGENERATE_REPLACE_PLAN",
            )
            return
        operation = self.store.transition_replacement_operation(
            str(operation["id"]),
            expected_phases={"PREPARED"},
            phase="DELETE_INTENT",
            status="IN_PROGRESS",
            effect_certainty="NONE",
            last_effect="OWNER_DELETE",
            next_action="SEND_OWNER_DELETE_ONCE",
            increment_delete=True,
        )
        journal.append(
            event_type="state.replacement_delete_intent",
            phase="delete_intent",
            status="RUNNING",
            lane="operations",
            safe_payload=_replacement_journal_payload(operation),
        )
        try:
            response = client.request_json(
                "DELETE",
                "/documents/delete_document",
                {
                    "doc_ids": [str(operation["owner_remote_doc_id"])],
                    "delete_file": True,
                    "delete_llm_cache": False,
                },
            )
        except Exception:
            self._needs_audit(
                operation,
                error_code="REMOTE_DELETE_EFFECT_UNKNOWN",
                next_action="OBSERVE_OWNER_DOCUMENT_WITHOUT_RETRY",
            )
            return
        response_status = (
            response.get("status")
            if isinstance(response, dict)
            else None
        )
        if response_status == "busy":
            self._fail_no_remote_change(
                operation,
                error_code="REMOTE_PIPELINE_BUSY",
            )
            return
        if response_status not in {
            "deletion_started",
            "success",
            "accepted",
        }:
            self._needs_audit(
                operation,
                error_code="REMOTE_DELETE_RESPONSE_INVALID",
                next_action="OBSERVE_OWNER_DOCUMENT_WITHOUT_RETRY",
            )
            return
        operation = self.store.transition_replacement_operation(
            str(operation["id"]),
            expected_phases={"DELETE_INTENT"},
            phase="DELETE_ACCEPTED",
            status="IN_PROGRESS",
            effect_certainty="KNOWN",
            last_effect="OWNER_DELETE",
            next_action="WAIT_OWNER_DOCUMENT_MISSING",
            business_fact=True,
        )
        journal.append(
            event_type="state.replacement_delete_accepted",
            phase="delete_accepted",
            status="RUNNING",
            lane="operations",
            safe_payload=_replacement_journal_payload(operation),
        )

    def _confirm_deletion(
        self,
        operation: dict[str, Any],
        *,
        client: Any,
        target: str,
    ) -> None:
        expected_doc_id = str(
            operation[
                "owner_remote_doc_id"
                if target == "owner"
                else "target_remote_doc_id"
            ]
        )
        required = int(operation["absence_confirmations"])
        deadline = time.monotonic() + float(
            operation["maintenance_window_seconds"]
        )
        consecutive = 0
        while time.monotonic() <= deadline:
            try:
                idle = _parse_pipeline_idle(
                    client.request_json(
                        "GET",
                        "/documents/pipeline_status",
                    )
                )
                documents = _load_remote_document_inventory(client)
            except Exception:
                idle = False
                documents = []
            present = any(
                document.doc_id == expected_doc_id
                or _canonical_basename(document.file_path)
                == _canonical_basename(str(operation["source_path"]))
                for document in documents
            )
            consecutive = consecutive + 1 if idle and not present else 0
            if consecutive >= required:
                if target == "owner":
                    self.store.mark_replacement_delete_confirmed(
                        str(operation["id"])
                    )
                else:
                    self.store.mark_compensation_target_deleted(
                        str(operation["id"])
                    )
                return
            time.sleep(
                min(
                    float(self.settings["poll_interval_seconds"]),
                    max(0.0, deadline - time.monotonic()),
                )
            )
        self._needs_audit(
            operation,
            error_code=(
                "REMOTE_DELETE_TIMEOUT"
                if target == "owner"
                else "REMOTE_ROLLBACK_DELETE_TIMEOUT"
            ),
            next_action="OBSERVE_REMOTE_DELETION_WITHOUT_RETRY",
        )

    def _submit_snapshot(
        self,
        operation: dict[str, Any],
        *,
        client: Any,
        owner: bool,
        journal: RunJournalWriter | None = None,
    ) -> None:
        try:
            self._assert_idle(client)
            context = self.store.replacement_execution_context(
                target_binding_id=str(operation["target_binding_id"]),
                owner_binding_id=str(operation["owner_binding_id"]),
            )
            text, source_path = _replacement_snapshot_text(
                self.store.root,
                context,
                owner=owner,
            )
        except StateError as exc:
            if owner:
                self._needs_audit(
                    operation,
                    error_code=exc.error_code,
                    next_action="RESTORE_OWNER_SNAPSHOT_MANUALLY",
                )
            else:
                self.store.transition_replacement_operation(
                    str(operation["id"]),
                    expected_phases={"DELETE_CONFIRMED"},
                    phase="COMPENSATION_REQUIRED",
                    status="IN_PROGRESS",
                    effect_certainty="KNOWN",
                    error_code=exc.error_code,
                    next_action="ROLL_BACK_FROM_OWNER_SNAPSHOT",
                    business_fact=True,
                )
            return
        expected_phase = (
            "TARGET_DELETE_CONFIRMED" if owner else "DELETE_CONFIRMED"
        )
        intent_phase = "OWNER_SUBMIT_INTENT" if owner else "SUBMIT_INTENT"
        operation = self.store.transition_replacement_operation(
            str(operation["id"]),
            expected_phases={expected_phase},
            phase=intent_phase,
            status="IN_PROGRESS",
            effect_certainty="NONE",
            last_effect="OWNER_RESTORE" if owner else "TARGET_SUBMIT",
            next_action=(
                "SEND_OWNER_RESTORE_ONCE"
                if owner
                else "SEND_TARGET_SUBMIT_ONCE"
            ),
            increment_submit=True,
        )
        if journal is not None:
            journal.append(
                event_type="state.replacement_submit_intent",
                phase=intent_phase.lower(),
                status="RUNNING",
                lane="operations",
                safe_payload=_replacement_journal_payload(operation),
            )
        try:
            response = client.post_json(
                "/documents/text",
                {
                    "text": text,
                    "file_source": source_path,
                },
            )
        except Exception:
            self._needs_audit(
                operation,
                error_code=(
                    "REMOTE_ROLLBACK_SUBMIT_EFFECT_UNKNOWN"
                    if owner
                    else "REMOTE_SUBMIT_EFFECT_UNKNOWN"
                ),
                next_action="OBSERVE_REMOTE_SUBMISSION_WITHOUT_RETRY",
            )
            return
        track_id = response.get("track_id") if isinstance(response, dict) else None
        if not isinstance(track_id, str) or not track_id:
            self._needs_audit(
                operation,
                error_code=(
                    "REMOTE_ROLLBACK_TRACK_ID_MISSING"
                    if owner
                    else "REMOTE_TRACK_ID_MISSING"
                ),
                next_action="OBSERVE_REMOTE_SUBMISSION_WITHOUT_RETRY",
            )
            return
        if owner:
            self.store.mark_owner_restore_accepted(
                str(operation["id"]),
                track_id=track_id,
            )
        else:
            self.store.mark_replacement_submit_accepted(
                str(operation["id"]),
                track_id=track_id,
            )

    def _wait_target(
        self,
        operation: dict[str, Any],
        *,
        client: Any,
    ) -> None:
        observed = self._wait_processed_document(
            operation,
            client=client,
            track_id=str(operation["target_remote_track_id"]),
        )
        if observed["status"] == "processed":
            document = observed["document"]
            self.store.mark_replacement_target_processed(
                str(operation["id"]),
                remote_doc_id=document.doc_id,
                track_id=str(operation["target_remote_track_id"]),
                chunk_count=int(document.chunks_count),
            )
            return
        if observed["status"] == "failed":
            operation = self.store.transition_replacement_operation(
                str(operation["id"]),
                expected_phases={"SUBMIT_ACCEPTED"},
                phase="COMPENSATION_REQUIRED",
                status="IN_PROGRESS",
                effect_certainty="KNOWN",
                error_code="REPLACE_TARGET_PROCESSING_FAILED",
                next_action="ROLL_BACK_FROM_OWNER_SNAPSHOT",
                business_fact=True,
            )
            if not bool(operation["auto_compensate"]):
                self._block(
                    operation,
                    error_code="REPLACE_ROLLBACK_REQUIRED",
                    next_action="RUN_REPLACE_RECOVER_ROLLBACK",
                )
            return
        self._needs_audit(
            operation,
            error_code=str(observed["error_code"]),
            next_action="OBSERVE_TARGET_TRACK_WITHOUT_RETRY",
        )

    def _wait_processed_document(
        self,
        operation: dict[str, Any],
        *,
        client: Any,
        track_id: str,
    ) -> dict[str, Any]:
        deadline = _replacement_deadline(operation)
        last_error = "TRACK_POLL_TIMEOUT"
        while time.monotonic() <= deadline:
            try:
                payload = client.request_json(
                    "GET",
                    f"/documents/track_status/{track_id}",
                )
                snapshot = parse_track_status(payload, track_id)
                if snapshot.state is RemoteTrackState.FAILED:
                    return {"status": "failed", "error_code": "TRACK_FAILED"}
                if snapshot.state is RemoteTrackState.INVALID:
                    return {
                        "status": "unknown",
                        "error_code": (
                            snapshot.error_code or "TRACK_STATUS_INVALID"
                        ),
                    }
                if snapshot.state is RemoteTrackState.PROCESSED:
                    documents = _matching_documents(
                        _load_remote_document_inventory(client),
                        source_path=str(operation["source_path"]),
                    )
                    matches = [
                        document
                        for document in documents
                        if document.track_id == track_id
                        and document.status == "processed"
                        and document.chunks_count is not None
                        and document.chunks_count > 0
                    ]
                    if len(matches) == 1:
                        return {
                            "status": "processed",
                            "document": matches[0],
                            "error_code": None,
                        }
                    last_error = "REMOTE_PROCESSED_DOCUMENT_UNCONFIRMED"
            except Exception:
                last_error = "REMOTE_TRACK_READ_FAILED"
            time.sleep(
                min(
                    float(self.settings["poll_interval_seconds"]),
                    max(0.0, deadline - time.monotonic()),
                )
            )
        return {"status": "unknown", "error_code": last_error}

    def _validate_target(
        self,
        operation: dict[str, Any],
        *,
        client: Any,
        smoke_query: str,
    ) -> None:
        try:
            payload = client.post_json(
                "/query",
                {
                    "query": smoke_query,
                    "mode": "hybrid",
                    "include_references": True,
                    "include_chunk_content": True,
                },
            )
            from ..evidence import gate_lightrag_references
            from ..lightrag_lane import normalize_lightrag_references

            references = normalize_lightrag_references(
                payload.get("references")
                or payload.get("ref_results")
                if isinstance(payload, dict)
                else None
            )
            accepted, evidence = gate_lightrag_references(
                smoke_query,
                references,
            )
            expected_basename = _canonical_basename(
                str(operation["source_path"])
            )
            matching = [
                reference
                for reference in accepted
                if _canonical_basename(
                    str(
                        reference.get("file_path")
                        or reference.get("source_path")
                        or ""
                    )
                )
                == expected_basename
                and _reference_has_content(reference)
            ]
            passed = bool(matching) and evidence.get("status") in {
                "passed",
                "not_evaluated",
            }
        except Exception:
            passed = False
        if not passed:
            operation = self.store.transition_replacement_operation(
                str(operation["id"]),
                expected_phases={"TARGET_PROCESSED"},
                phase="COMPENSATION_REQUIRED",
                status="IN_PROGRESS",
                effect_certainty="KNOWN",
                error_code="REPLACE_ACCEPTANCE_FAILED",
                next_action="ROLL_BACK_FROM_OWNER_SNAPSHOT",
                business_fact=True,
            )
            if not bool(operation["auto_compensate"]):
                self._block(
                    operation,
                    error_code="REPLACE_ROLLBACK_REQUIRED",
                    next_action="RUN_REPLACE_RECOVER_ROLLBACK",
                )
            return
        self.store.transition_replacement_operation(
            str(operation["id"]),
            expected_phases={"TARGET_PROCESSED"},
            phase="VALIDATED",
            status="IN_PROGRESS",
            effect_certainty="KNOWN",
            error_code=None,
            next_action="COMMIT_REVISION_SWITCH",
            business_fact=True,
        )

    def _compensate(
        self,
        operation: dict[str, Any],
        *,
        service: dict[str, Any],
        client: Any,
    ) -> dict[str, Any]:
        del service
        phase = str(operation["phase"])
        if phase == "COMPENSATION_REQUIRED":
            try:
                self._assert_idle(client)
                matches = _matching_documents(
                    _load_remote_document_inventory(client),
                    source_path=str(operation["source_path"]),
                )
            except Exception:
                return self._needs_audit(
                    operation,
                    error_code="REMOTE_ROLLBACK_OBSERVATION_FAILED",
                    next_action="OBSERVE_TARGET_WITHOUT_RETRY",
                )
            if not matches:
                return self.store.mark_compensation_target_deleted(
                    str(operation["id"])
                )
            target_track = operation.get("target_remote_track_id")
            attributable = [
                document
                for document in matches
                if target_track is not None
                and document.track_id == target_track
                and document.status in _REMOTE_TERMINAL_STATUSES
            ]
            if len(attributable) != 1:
                return self._needs_audit(
                    operation,
                    error_code="REMOTE_ROLLBACK_TARGET_UNATTRIBUTABLE",
                    next_action="MANUAL_REMOTE_REVIEW_REQUIRED",
                )
            operation = self.store.transition_replacement_operation(
                str(operation["id"]),
                expected_phases={"COMPENSATION_REQUIRED"},
                phase="TARGET_DELETE_INTENT",
                status="IN_PROGRESS",
                effect_certainty="NONE",
                last_effect="TARGET_DELETE",
                target_remote_doc_id=attributable[0].doc_id,
                next_action="SEND_TARGET_DELETE_ONCE",
                increment_delete=True,
            )
            try:
                response = client.request_json(
                    "DELETE",
                    "/documents/delete_document",
                    {
                        "doc_ids": [
                            str(operation["target_remote_doc_id"])
                        ],
                        "delete_file": True,
                        "delete_llm_cache": False,
                    },
                )
            except Exception:
                return self._needs_audit(
                    operation,
                    error_code="REMOTE_ROLLBACK_DELETE_EFFECT_UNKNOWN",
                    next_action="OBSERVE_TARGET_DELETION_WITHOUT_RETRY",
                )
            response_status = (
                response.get("status")
                if isinstance(response, dict)
                else None
            )
            if response_status not in {
                "deletion_started",
                "success",
                "accepted",
            }:
                return self._needs_audit(
                    operation,
                    error_code="REMOTE_ROLLBACK_DELETE_RESPONSE_INVALID",
                    next_action="OBSERVE_TARGET_DELETION_WITHOUT_RETRY",
                )
            return self.store.transition_replacement_operation(
                str(operation["id"]),
                expected_phases={"TARGET_DELETE_INTENT"},
                phase="TARGET_DELETE_ACCEPTED",
                status="IN_PROGRESS",
                effect_certainty="KNOWN",
                last_effect="TARGET_DELETE",
                next_action="WAIT_TARGET_DOCUMENT_MISSING",
                business_fact=True,
            )
        if phase == "TARGET_DELETE_ACCEPTED":
            self._confirm_deletion(
                operation,
                client=client,
                target="target",
            )
            return self.store.replacement_operation(str(operation["id"]))
        if phase == "TARGET_DELETE_CONFIRMED":
            self._submit_snapshot(
                operation,
                client=client,
                owner=True,
            )
            return self.store.replacement_operation(str(operation["id"]))
        if phase == "OWNER_SUBMIT_ACCEPTED":
            # The owner restore track is stored on its binding, not in the
            # target track column. Read it from the aligned SQLite context.
            context = self.store.replacement_execution_context(
                target_binding_id=str(operation["target_binding_id"]),
                owner_binding_id=str(operation["owner_binding_id"]),
            )
            owner_track = context.get("stored_owner_track_id")
            if not isinstance(owner_track, str) or not owner_track:
                return self._needs_audit(
                    operation,
                    error_code="REMOTE_ROLLBACK_TRACK_ID_MISSING",
                    next_action="INSPECT_OWNER_BINDING",
                )
            observed = self._wait_processed_document(
                operation,
                client=client,
                track_id=owner_track,
            )
            if observed["status"] != "processed":
                return self._needs_audit(
                    operation,
                    error_code=str(observed["error_code"]),
                    next_action="OBSERVE_OWNER_TRACK_WITHOUT_RETRY",
                )
            document = observed["document"]
            return self.store.complete_replacement_rollback(
                str(operation["id"]),
                remote_doc_id=document.doc_id,
                track_id=owner_track,
                chunk_count=int(document.chunks_count),
            )
        if phase.endswith("_INTENT"):
            return self._needs_audit(
                operation,
                error_code=_unknown_effect_code(phase),
                next_action="OBSERVE_REMOTE_STATE_WITHOUT_REPLAY",
            )
        return operation

    def _drive_compensation(
        self,
        operation: dict[str, Any],
        *,
        service: dict[str, Any],
        client: Any,
    ) -> dict[str, Any]:
        for _ in range(12):
            if operation["phase"] in {
                "ROLLED_BACK",
                "NEEDS_AUDIT",
                "FAILED",
            } or operation["status"] == "BLOCKED":
                return operation
            before = str(operation["phase"])
            operation = self._compensate(
                operation,
                service=service,
                client=client,
            )
            if str(operation["phase"]) == before:
                return operation
        return self._needs_audit(
            operation,
            error_code="REPLACE_COMPENSATION_LOOP_LIMIT",
            next_action="INSPECT_OPERATION",
        )

    def _recover_unknown_effect(
        self,
        operation: dict[str, Any],
    ) -> ReplacementExecutionResult:
        # Recovery never overrides an unattributable remote write. It may only
        # prove that no target exists and then start the separately confirmed
        # compensation path.
        _service, client = self._remote()
        try:
            self._assert_idle(client)
            matches = _matching_documents(
                _load_remote_document_inventory(client),
                source_path=str(operation["source_path"]),
            )
        except Exception:
            return self._execution_result(operation)
        if matches:
            return self._execution_result(operation)
        operation = self.store.transition_replacement_operation(
            str(operation["id"]),
            expected_phases={"NEEDS_AUDIT"},
            phase="COMPENSATION_REQUIRED",
            status="IN_PROGRESS",
            effect_certainty="KNOWN",
            error_code=None,
            next_action="RESTORE_OWNER_SNAPSHOT",
            business_fact=True,
        )
        operation = self._drive_compensation(
            operation,
            service={},
            client=client,
        )
        return self._execution_result(operation)

    def _remote(self) -> tuple[dict[str, Any], Any]:
        from ..lightrag_lane import (
            LightRAGServiceClient,
            parse_lightrag_capabilities,
            resolve_lightrag_service_config,
        )

        service = resolve_lightrag_service_config(self.lightrag_config)
        client = LightRAGServiceClient(
            service["base_url"],
            headers=service["headers"],
            timeout=service["timeout_seconds"],
        )
        try:
            health = client.request_json("GET", "/health")
            openapi = client.request_json("GET", "/openapi.json")
            capabilities = parse_lightrag_capabilities(
                health,
                openapi,
                expected_workspace=service["workspace"],
                requested_embedding_batch_size=service[
                    "embedding_batch_size"
                ],
            )
        except Exception as exc:
            raise StateError(
                "replacement remote capabilities cannot be confirmed",
                error_code="REPLACE_REMOTE_PREFLIGHT_FAILED",
            ) from exc
        if (
            not isinstance(health, dict)
            or health.get("status") != "healthy"
            or capabilities.workspace_matches is not True
            or capabilities.storage_workspaces_match is not True
            or capabilities.supports_track_status is not True
            or capabilities.supports_document_delete is not True
            or capabilities.supports_document_inventory is not True
            or capabilities.supports_pipeline_status is not True
        ):
            raise StateError(
                "replacement remote capabilities are incomplete",
                error_code="REPLACE_REMOTE_CAPABILITY_BLOCKED",
            )
        paths = (
            openapi.get("paths")
            if isinstance(openapi, dict)
            else None
        )
        if (
            not isinstance(paths, dict)
            or not _openapi_has_operation(
                paths,
                "/documents/text",
                "post",
            )
            or not _openapi_has_operation(paths, "/query", "post")
        ):
            raise StateError(
                "replacement submit/query capabilities are unconfirmed",
                error_code="REPLACE_REMOTE_CAPABILITY_BLOCKED",
            )
        return service, client

    def _assert_idle(self, client: Any) -> None:
        try:
            idle = _parse_pipeline_idle(
                client.request_json(
                    "GET",
                    "/documents/pipeline_status",
                )
            )
        except Exception as exc:
            raise StateError(
                "LightRAG pipeline state cannot be confirmed",
                error_code="REMOTE_PIPELINE_STATE_UNCONFIRMED",
            ) from exc
        if not idle:
            raise StateError(
                "LightRAG pipeline is busy",
                error_code="REMOTE_PIPELINE_BUSY",
            )

    def _require_enabled(self) -> None:
        if self.settings["enabled"] is not True:
            raise StateError(
                "production replacement execution is disabled",
                error_code="REPLACEMENT_DISABLED",
            )

    def _needs_audit(
        self,
        operation: dict[str, Any],
        *,
        error_code: str,
        next_action: str,
    ) -> dict[str, Any]:
        return self.store.transition_replacement_operation(
            str(operation["id"]),
            expected_phases={str(operation["phase"])},
            phase="NEEDS_AUDIT",
            status="NEEDS_AUDIT",
            effect_certainty="UNKNOWN",
            last_effect=operation.get("last_effect"),
            error_code=error_code,
            next_action=next_action,
            business_fact=True,
        )

    def _block(
        self,
        operation: dict[str, Any],
        *,
        error_code: str,
        next_action: str,
    ) -> dict[str, Any]:
        return self.store.transition_replacement_operation(
            str(operation["id"]),
            expected_phases={str(operation["phase"])},
            phase=str(operation["phase"]),
            status="BLOCKED",
            effect_certainty=str(operation["effect_certainty"]),
            last_effect=operation.get("last_effect"),
            error_code=error_code,
            next_action=next_action,
            business_fact=True,
        )

    def _fail_no_remote_change(
        self,
        operation: dict[str, Any],
        *,
        error_code: str,
    ) -> dict[str, Any]:
        return self.store.transition_replacement_operation(
            str(operation["id"]),
            expected_phases={str(operation["phase"])},
            phase="FAILED",
            status="FAILED",
            effect_certainty="KNOWN",
            last_effect=operation.get("last_effect"),
            error_code=error_code,
            next_action="REGENERATE_REPLACE_PLAN",
            completed=True,
            business_fact=True,
        )

    def _execution_result(
        self,
        operation: dict[str, Any],
    ) -> ReplacementExecutionResult:
        summary = _replacement_summary(operation)
        result_status = {
            "COMPLETED": "completed",
            "ROLLED_BACK": "rolled_back",
            "NEEDS_AUDIT": "needs_audit",
            "BLOCKED": "blocked",
            "FAILED": "failed",
            "IN_PROGRESS": "blocked",
        }.get(summary.status, "failed")
        return ReplacementExecutionResult(
            status=result_status,
            workspace_mutated=True,
            delete_attempted=summary.delete_attempts > 0,
            operation=summary,
            state_commit_seq=self.store.state_commit_seq(),
            error_code=summary.error_code,
        )


def _replacement_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("replacement") or {}
    if not isinstance(raw, dict):
        raise StateError(
            "lightrag.replacement must be an object",
            error_code="STATE_CONFIG_INVALID",
        )
    enabled = raw.get("enabled", False)
    auto_compensate = raw.get("auto_compensate", True)
    if not isinstance(enabled, bool) or not isinstance(
        auto_compensate,
        bool,
    ):
        raise StateError(
            "replacement boolean settings are invalid",
            error_code="STATE_CONFIG_INVALID",
        )
    maintenance = raw.get("maintenance_window_seconds", 600)
    confirmations = raw.get("absence_confirmations", 2)
    sync = config.get("sync") or {}
    if not isinstance(sync, dict):
        raise StateError(
            "lightrag.sync must be an object",
            error_code="STATE_CONFIG_INVALID",
        )
    poll_interval = sync.get("poll_interval_seconds", 2)
    try:
        if (
            isinstance(maintenance, bool)
            or isinstance(confirmations, bool)
            or isinstance(poll_interval, bool)
        ):
            raise ValueError
        maintenance_value = float(maintenance)
        confirmations_value = int(confirmations)
        poll_interval_value = float(poll_interval)
    except (TypeError, ValueError) as exc:
        raise StateError(
            "replacement timing settings are invalid",
            error_code="STATE_CONFIG_INVALID",
        ) from exc
    if not 10 <= maintenance_value <= 86400:
        raise StateError(
            "replacement maintenance window is out of range",
            error_code="STATE_CONFIG_INVALID",
        )
    if not 1 <= confirmations_value <= 10:
        raise StateError(
            "replacement absence confirmations are out of range",
            error_code="STATE_CONFIG_INVALID",
        )
    if not 0.1 <= poll_interval_value <= 60:
        raise StateError(
            "replacement poll interval is out of range",
            error_code="STATE_CONFIG_INVALID",
        )
    return {
        "enabled": enabled,
        "maintenance_window_seconds": maintenance_value,
        "absence_confirmations": confirmations_value,
        "auto_compensate": auto_compensate,
        "poll_interval_seconds": poll_interval_value,
    }


def _replacement_summary(
    row: dict[str, Any],
) -> ReplacementOperationSummary:
    started_at = row.get("maintenance_started_at")
    elapsed = None
    if isinstance(started_at, str):
        try:
            started = datetime.fromisoformat(
                started_at.replace("Z", "+00:00")
            )
            completed_at = row.get("completed_at")
            ended = (
                datetime.fromisoformat(
                    completed_at.replace("Z", "+00:00")
                )
                if isinstance(completed_at, str)
                else datetime.now(timezone.utc)
            )
            elapsed = max(
                0.0,
                (ended - started).total_seconds(),
            )
        except ValueError:
            elapsed = None
    phase = str(row["phase"])
    status = str(row["status"])
    return ReplacementOperationSummary(
        operation_id=str(row.get("id") or row.get("operation_id")),
        plan_id=str(row["plan_id"]),
        plan_digest=str(row["plan_digest"]),
        source_path=str(row["source_path"]),
        phase=phase,
        status=status,
        effect_certainty=str(row["effect_certainty"]),
        delete_attempts=int(row["delete_attempts"]),
        submit_attempts=int(row["submit_attempts"]),
        backup=ReplacementBackupSummary(
            backup_id=str(
                row.get("backup_id")
                or (row.get("backup") or {}).get("backup_id")
            ),
            backup_sha256=str(
                row.get("backup_sha256")
                or (row.get("backup") or {}).get("backup_sha256")
            ),
            state_commit_seq=int(
                row.get("backup_state_commit_seq")
                if row.get("backup_state_commit_seq") is not None
                else (row.get("backup") or {}).get(
                    "state_commit_seq",
                    0,
                )
            ),
        ),
        maintenance=ReplacementMaintenance(
            active=(
                started_at is not None
                and status in {"IN_PROGRESS", "BLOCKED", "NEEDS_AUDIT"}
            ),
            started_at=started_at,
            elapsed_seconds=elapsed,
            limit_seconds=float(
                row.get("maintenance_window_seconds")
                or (row.get("maintenance") or {}).get(
                    "limit_seconds",
                    0,
                )
            ),
        ),
        compensation_active=(
            phase in ReplacementOperationService._COMPENSATION_PHASES
            and status in {"IN_PROGRESS", "BLOCKED", "NEEDS_AUDIT"}
        ),
        next_action=row.get("next_action"),
        error_code=row.get("error_code"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        completed_at=row.get("completed_at"),
    )


def _replacement_journal_payload(
    operation: dict[str, Any],
) -> dict[str, Any]:
    operation_id = operation.get("id") or operation.get("operation_id")
    backup = operation.get("backup")
    return {
        "operation": "replace_document",
        "operation_id": operation_id,
        "plan_id": operation.get("plan_id"),
        "plan_digest": operation.get("plan_digest"),
        "phase": operation.get("phase"),
        "status": operation.get("status"),
        "effect_certainty": operation.get("effect_certainty"),
        "delete_attempts": operation.get("delete_attempts", 0),
        "submit_attempts": operation.get("submit_attempts", 0),
        "backup_id": (
            backup.get("backup_id")
            if isinstance(backup, dict)
            else operation.get("backup_id")
        ),
        "error_code": operation.get("error_code"),
    }


def _safe_operator_name() -> str:
    try:
        value = getpass.getuser()
    except Exception:
        value = "unknown"
    return value[:128] if value else "unknown"


def _safe_hostname() -> str:
    try:
        value = socket.gethostname()
    except Exception:
        value = "unknown"
    return value[:255] if value else "unknown"


def _unknown_effect_code(phase: str) -> str:
    return {
        "DELETE_INTENT": "REMOTE_DELETE_EFFECT_UNKNOWN",
        "SUBMIT_INTENT": "REMOTE_SUBMIT_EFFECT_UNKNOWN",
        "TARGET_DELETE_INTENT": "REMOTE_ROLLBACK_DELETE_EFFECT_UNKNOWN",
        "OWNER_SUBMIT_INTENT": "REMOTE_ROLLBACK_SUBMIT_EFFECT_UNKNOWN",
    }.get(phase, "REMOTE_EFFECT_UNKNOWN")


def _is_replacement_remote_blocker(error_code: str) -> bool:
    return (
        error_code.startswith("REMOTE_")
        or error_code.startswith("REPLACE_REMOTE_")
        or error_code.startswith("QUERY_DRAIN_")
        or error_code.startswith("OPS_NOTIFICATION_REQUIRED_")
        or error_code == "QUERY_GATEWAY_HEARTBEAT_STALE"
    )


def _iso_age_seconds(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(
        0.0,
        (datetime.now(timezone.utc) - parsed).total_seconds(),
    )


def _matching_documents(
    documents: list[ReplacementRemoteDocument],
    *,
    source_path: str,
) -> list[ReplacementRemoteDocument]:
    basename = _canonical_basename(source_path)
    return [
        document
        for document in documents
        if _canonical_basename(document.file_path) == basename
    ]


def _replacement_snapshot_text(
    root: Path,
    context: dict[str, Any],
    *,
    owner: bool,
) -> tuple[str, str]:
    prefix = "owner" if owner else "target"
    if not bool(context[f"{prefix}_text_like"]):
        raise StateError(
            "replacement only supports text-like snapshots",
            error_code="REPLACE_SNAPSHOT_NOT_TEXT",
        )
    snapshot_path = context[f"{prefix}_snapshot_path"]
    expected_sha256 = str(context[f"{prefix}_sha256"])
    if not _snapshot_matches(
        root,
        snapshot_path=snapshot_path,
        snapshot_status=context[f"{prefix}_snapshot_status"],
        expected_sha256=expected_sha256,
    ):
        raise StateError(
            "replacement snapshot changed or is unavailable",
            error_code="REPLACE_SNAPSHOT_UNAVAILABLE",
        )
    normalized = normalize_workspace_relative_path(
        str(snapshot_path),
        field_name="snapshot.path",
    )
    path = resolve_under_root(root, normalized)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise StateError(
            "replacement snapshot is not valid UTF-8 text",
            error_code="REPLACE_SNAPSHOT_NOT_TEXT",
        ) from exc
    return text, str(context["source_path"])


def _reference_has_content(reference: dict[str, Any]) -> bool:
    content = reference.get("content")
    return (
        isinstance(content, list)
        and any(isinstance(part, str) and part for part in content)
    )


def _replacement_deadline(operation: dict[str, Any]) -> float:
    limit = float(operation["maintenance_window_seconds"])
    started_at = operation.get("maintenance_started_at")
    if not isinstance(started_at, str):
        return time.monotonic() + limit
    try:
        started = datetime.fromisoformat(
            started_at.replace("Z", "+00:00")
        )
        elapsed = max(
            0.0,
            (datetime.now(timezone.utc) - started).total_seconds(),
        )
    except ValueError:
        elapsed = limit
    return time.monotonic() + max(0.0, limit - elapsed)


def _openapi_has_operation(
    paths: dict[str, Any],
    path: str,
    method: str,
) -> bool:
    entry = paths.get(path)
    return isinstance(entry, dict) and isinstance(
        entry.get(method),
        dict,
    )


def _load_remote_document_inventory(
    client: Any,
) -> list[ReplacementRemoteDocument]:
    documents: list[ReplacementRemoteDocument] = []
    seen_ids: set[str] = set()
    expected_total: int | None = None
    page = 1
    while True:
        if page > _MAX_DOCUMENT_INVENTORY_PAGES:
            raise ValueError("remote document inventory exceeds page limit")
        payload = client.request_json(
            "POST",
            "/documents/paginated",
            {
                "page": page,
                "page_size": 200,
                "sort_field": "file_path",
                "sort_direction": "asc",
            },
        )
        if not isinstance(payload, dict):
            raise ValueError("document inventory payload is invalid")
        raw_documents = payload.get("documents")
        pagination = payload.get("pagination")
        if not isinstance(raw_documents, list) or not isinstance(
            pagination,
            dict,
        ):
            raise ValueError("document inventory shape is invalid")
        response_page = pagination.get("page")
        total_count = pagination.get("total_count")
        total_pages = pagination.get("total_pages")
        has_next = pagination.get("has_next")
        if (
            not _is_non_negative_int(total_count)
            or not _is_non_negative_int(total_pages)
            or not isinstance(response_page, int)
            or isinstance(response_page, bool)
            or response_page != page
            or not isinstance(has_next, bool)
        ):
            raise ValueError("document inventory pagination is invalid")
        if expected_total is None:
            expected_total = total_count
        elif expected_total != total_count:
            raise ValueError("document inventory total changed during read")
        if total_pages > _MAX_DOCUMENT_INVENTORY_PAGES:
            raise ValueError("document inventory exceeds page limit")

        for raw in raw_documents:
            document = _parse_remote_document(raw)
            if document.doc_id in seen_ids:
                raise ValueError("document inventory contains duplicate IDs")
            seen_ids.add(document.doc_id)
            documents.append(document)

        if not has_next:
            break
        if page >= total_pages:
            raise ValueError("document inventory pagination is inconsistent")
        page += 1

    if expected_total is None or len(documents) != expected_total:
        raise ValueError("document inventory total is inconsistent")
    return documents


def _parse_remote_document(value: Any) -> ReplacementRemoteDocument:
    if not isinstance(value, dict):
        raise ValueError("remote document is invalid")
    doc_id = value.get("id")
    file_path = value.get("file_path")
    status = value.get("status")
    track_id = value.get("track_id")
    chunks_count = value.get("chunks_count")
    if (
        not isinstance(doc_id, str)
        or not doc_id.strip()
        or not isinstance(file_path, str)
        or not file_path.strip()
        or not isinstance(status, str)
        or not status.strip()
    ):
        raise ValueError("remote document identity is invalid")
    normalized_status = status.strip().lower()
    if normalized_status not in _REMOTE_DOCUMENT_STATUSES:
        raise ValueError("remote document status is invalid")
    if track_id is not None and (
        not isinstance(track_id, str) or not track_id.strip()
    ):
        raise ValueError("remote document track ID is invalid")
    if chunks_count is not None and not _is_non_negative_int(chunks_count):
        raise ValueError("remote document chunk count is invalid")
    return ReplacementRemoteDocument(
        doc_id=doc_id.strip(),
        file_path=file_path.strip(),
        status=normalized_status,
        track_id=track_id.strip() if isinstance(track_id, str) else None,
        chunks_count=chunks_count,
    )


def _parse_pipeline_idle(value: Any) -> bool:
    if not isinstance(value, dict):
        raise ValueError("pipeline status payload is invalid")
    boolean_fields = (
        "busy",
        "scanning",
        "scanning_exclusive",
        "destructive_busy",
    )
    if any(not isinstance(value.get(field), bool) for field in boolean_fields):
        raise ValueError("pipeline status flags are invalid")
    pending_enqueues = value.get("pending_enqueues")
    if not _is_non_negative_int(pending_enqueues):
        raise ValueError("pipeline pending enqueue count is invalid")
    return (
        not any(value[field] for field in boolean_fields)
        and pending_enqueues == 0
    )


def _canonical_basename(value: str) -> str:
    return Path(value.replace("\\", "/")).name


def _workspace_file_matches(
    root: Path,
    relative_path: str,
    expected_sha256: str,
) -> bool:
    try:
        normalized = normalize_workspace_relative_path(
            relative_path,
            field_name="source.path",
        )
        path = resolve_under_root(root, normalized)
    except StateError:
        return False
    return (
        path.is_file()
        and _sha256_file(path) == expected_sha256
    )


def _snapshot_matches(
    root: Path,
    *,
    snapshot_path: Any,
    snapshot_status: Any,
    expected_sha256: str,
) -> bool:
    if (
        snapshot_status != "AVAILABLE"
        or not isinstance(snapshot_path, str)
    ):
        return False
    try:
        normalized = normalize_workspace_relative_path(
            snapshot_path,
            field_name="snapshot.path",
        )
        path = resolve_under_root(root, normalized)
    except StateError:
        return False
    return (
        path.is_file()
        and _sha256_file(path) == expected_sha256
    )


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _is_non_negative_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


__all__ = [
    "LegacyInventory",
    "ReplacementOperationService",
    "ReplacementPlanner",
    "StateBackupService",
    "StateExporter",
    "StateMigrator",
    "StateReconciler",
    "StateSchemaMigrator",
    "StateVerifier",
    "operation_lock",
]

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence

from ..corpus import CorpusFile, sha256_file
from ..utils import utc_now
from ..version import __version__
from .contracts import ActionGate, RemoteStatus, StateError
from .schema import (
    INITIAL_SCHEMA_VERSION,
    MIGRATIONS,
    MIGRATION_NAME,
    MIGRATION_SQL,
    NOTIFICATION_SCHEMA_VERSION,
    QUERY_DELIVERY_SCHEMA_VERSION,
    QUERY_GOVERNANCE_SCHEMA_VERSION,
    REPLACEMENT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    migration_checksum,
    migration_checksums,
)


DEFAULT_DATABASE = "artifacts/state/evo_wiki.sqlite3"
DEFAULT_BUSY_TIMEOUT_SECONDS = 15.0
DEFAULT_DOMAIN_ID = "domain-default"
DEFAULT_PARTITION_ID = "partition-lightrag-default"
DEFAULT_BACKEND_FINGERPRINT = "sha256:" + ("0" * 64)


def normalize_workspace_relative_path(raw_path: object, *, field_name: str) -> str:
    if not isinstance(raw_path, str) or not raw_path:
        raise StateError(
            f"{field_name} must be a non-empty workspace-relative path",
            error_code="STATE_CONFIG_INVALID",
        )
    normalized = raw_path.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or normalized != candidate.as_posix()
    ):
        raise StateError(
            f"{field_name} must be a normalized workspace-relative path",
            error_code="STATE_PATH_INVALID",
        )
    return normalized


def resolve_under_root(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / relative_path).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise StateError(
            "state path escapes the workspace",
            error_code="STATE_PATH_INVALID",
        ) from exc
    return candidate


def _deterministic_id(prefix: str, *parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:32]}"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_after(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=float(seconds))
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _public_audit_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    raw_evidence = result.pop("evidence_json", "{}")
    try:
        evidence = json.loads(str(raw_evidence))
    except json.JSONDecodeError:
        evidence = {"error_code": "QUERY_AUDIT_EVIDENCE_INVALID"}
    result["evidence"] = evidence if isinstance(evidence, dict) else {}
    return result


def _public_notification_row(
    row: sqlite3.Row | dict[str, Any],
) -> dict[str, Any]:
    result = dict(row)
    result.pop("payload_json", None)
    result["delivery_required"] = bool(result.get("delivery_required"))
    return result


def lightrag_backend_identity(
    config: dict[str, Any] | None,
) -> tuple[str, str]:
    safe_config = config or {}
    workspace = safe_config.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        workspace = "default"
    fingerprint_payload = {
        "schema_version": 1,
        "mode": safe_config.get("mode", "service"),
        "workspace": workspace,
        "base_url": safe_config.get("base_url", ""),
        "api_key_env": safe_config.get("api_key_env", "LIGHTRAG_API_KEY"),
        "bearer_token_env": safe_config.get(
            "bearer_token_env",
            "LIGHTRAG_BEARER_TOKEN",
        ),
    }
    return workspace, _sha256_json(fingerprint_payload)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)


def _private_file(path: Path) -> None:
    if path.exists() and os.name == "posix":
        path.chmod(0o600)


def _execute_sql_script(
    connection: sqlite3.Connection,
    script: str,
) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError(
            "schema migration contains an incomplete SQL statement"
        )


def _table_exists(
    connection: sqlite3.Connection,
    table_name: str,
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


class StateStore:
    """Small transactional repository for EvoWiki business state."""

    def __init__(
        self,
        root: Path,
        config: dict[str, Any] | None = None,
        *,
        database_path: Path | None = None,
    ):
        self.root = root.expanduser().resolve()
        raw_config = config or {}
        relative_database = normalize_workspace_relative_path(
            raw_config.get("database", DEFAULT_DATABASE),
            field_name="state.database",
        )
        self.database_relative = relative_database
        self.database_path = (
            database_path.resolve()
            if database_path is not None
            else resolve_under_root(self.root, relative_database)
        )
        raw_busy_seconds = raw_config.get(
            "busy_timeout_seconds",
            DEFAULT_BUSY_TIMEOUT_SECONDS,
        )
        try:
            if isinstance(raw_busy_seconds, bool):
                raise ValueError
            busy_seconds = float(raw_busy_seconds)
        except (TypeError, ValueError) as exc:
            raise StateError(
                "state.busy_timeout_seconds must be a number",
                error_code="STATE_CONFIG_INVALID",
            ) from exc
        if not 0.1 <= busy_seconds <= 120:
            raise StateError(
                "state.busy_timeout_seconds must be between 0.1 and 120",
                error_code="STATE_CONFIG_INVALID",
            )
        self.busy_timeout_ms = int(busy_seconds * 1000)
        self.state_root = self.database_path.parent
        self.snapshots_root = self.state_root / "snapshots"
        self._thread_lock = threading.RLock()

    @property
    def exists(self) -> bool:
        return self.database_path.exists()

    def initialize(self) -> None:
        _private_directory(self.state_root)
        _private_directory(self.snapshots_root)
        is_new = not self.database_path.exists()
        connection = self.connect()
        try:
            if is_new:
                self._apply_initial_migrations(connection)
                self._seed_defaults(connection)
            else:
                self._assert_supported_schema(connection)
        finally:
            connection.close()
        _private_file(self.database_path)
        _private_file(self.database_path.with_name(self.database_path.name + "-wal"))
        _private_file(self.database_path.with_name(self.database_path.name + "-shm"))

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"{self.database_path.as_uri()}?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
            )
        else:
            connection = sqlite3.connect(
                self.database_path,
                timeout=self.busy_timeout_ms / 1000,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        if not read_only:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA wal_autocheckpoint = 4000")
            connection.execute("PRAGMA journal_size_limit = 67108864")
            connection.execute("PRAGMA cache_size = -65536")
        return connection

    def _apply_initial_migrations(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        try:
            for migration in MIGRATIONS:
                self._apply_schema_migration(connection, migration)
        except sqlite3.Error as exc:
            raise StateError(
                "failed to initialize SQLite state schema",
                error_code="STATE_SCHEMA_INIT_FAILED",
            ) from exc

    def _apply_schema_migration(
        self,
        connection: sqlite3.Connection,
        migration: Any,
    ) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _execute_sql_script(connection, migration.sql)
            connection.execute(
                """
                INSERT INTO schema_meta(
                  version, name, checksum, applied_at, application_version
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    migration.version,
                    migration.name,
                    migration_checksum(migration.version),
                    utc_now(),
                    __version__,
                ),
            )
            connection.execute(
                f"PRAGMA user_version = {int(migration.version)}"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _seed_defaults(self, connection: sqlite3.Connection) -> None:
        with connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO security_domain(
                  id, name, classification, status, created_at
                ) VALUES (?, ?, ?, 'ACTIVE', ?)
                """,
                (
                    DEFAULT_DOMAIN_ID,
                    "default",
                    "LOCAL",
                    utc_now(),
                ),
            )

    def _assert_supported_schema(self, connection: sqlite3.Connection) -> None:
        try:
            rows = connection.execute(
                """
                SELECT version, name, checksum
                FROM schema_meta ORDER BY version
                """
            ).fetchall()
            user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
        except sqlite3.Error as exc:
            raise StateError(
                "SQLite state schema metadata is unavailable",
                error_code="STATE_SCHEMA_INVALID",
            ) from exc
        expected_checksums = migration_checksums()
        expected = {
            migration.version: (
                migration.name,
                expected_checksums[migration.version],
            )
            for migration in MIGRATIONS
            if migration.version <= user_version
        }
        observed = {
            int(row["version"]): (str(row["name"]), str(row["checksum"]))
            for row in rows
        }
        if (
            user_version < INITIAL_SCHEMA_VERSION
            or user_version > SCHEMA_VERSION
            or observed != expected
        ):
            raise StateError(
                "SQLite state schema is unsupported or has been modified",
                error_code="STATE_SCHEMA_UNSUPPORTED",
            )

    def schema_version(self) -> int:
        connection = self.connect(read_only=True)
        try:
            self._assert_supported_schema(connection)
            return int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
        finally:
            connection.close()

    def require_schema_version(self, minimum: int) -> None:
        current = self.schema_version()
        if current < minimum:
            raise StateError(
                "SQLite schema upgrade is required for this operation",
                error_code="STATE_SCHEMA_UPGRADE_REQUIRED",
                details={
                    "database_schema_version": current,
                    "required_schema_version": minimum,
                },
            )

    def pending_schema_migrations(self) -> list[Any]:
        current = self.schema_version()
        return [
            migration
            for migration in MIGRATIONS
            if migration.version > current
        ]

    def apply_pending_schema_migrations(self) -> list[str]:
        connection = self.connect()
        applied: list[str] = []
        try:
            self._assert_supported_schema(connection)
            current = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            for migration in MIGRATIONS:
                if migration.version <= current:
                    continue
                self._apply_schema_migration(connection, migration)
                applied.append(migration.name)
                current = migration.version
            self._assert_supported_schema(connection)
        except StateError:
            raise
        except sqlite3.Error as exc:
            raise StateError(
                "failed to apply SQLite schema migration",
                error_code="STATE_SCHEMA_MIGRATION_FAILED",
            ) from exc
        finally:
            connection.close()
        return applied

    @contextmanager
    def business_transaction(
        self,
        *,
        allow_replacement: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        with self._thread_lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                if (
                    not allow_replacement
                    and _table_exists(
                        connection,
                        "replacement_operation",
                    )
                ):
                    active = connection.execute(
                        """
                        SELECT id FROM replacement_operation
                        WHERE status IN (
                          'IN_PROGRESS', 'BLOCKED', 'NEEDS_AUDIT'
                        )
                        LIMIT 1
                        """
                    ).fetchone()
                    if active is not None:
                        raise StateError(
                            "business writes are blocked by an active "
                            "replacement operation",
                            error_code="STATE_REPLACEMENT_WRITE_GATE",
                            details={"operation_id": str(active["id"])},
                        )
                yield connection
                connection.execute(
                    """
                    UPDATE state_clock
                    SET state_commit_seq = state_commit_seq + 1,
                        updated_at = ?
                    WHERE singleton = 1
                    """,
                    (utc_now(),),
                )
                connection.commit()
            except sqlite3.OperationalError as exc:
                connection.rollback()
                code = (
                    "STATE_BUSY"
                    if "locked" in str(exc).lower() or "busy" in str(exc).lower()
                    else "STATE_TRANSACTION_FAILED"
                )
                raise StateError(
                    "SQLite state transaction failed",
                    error_code=code,
                ) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise StateError(
                    "SQLite state transaction failed",
                    error_code="STATE_TRANSACTION_FAILED",
                ) from exc
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        _private_file(self.database_path)
        _private_file(self.database_path.with_name(self.database_path.name + "-wal"))
        _private_file(self.database_path.with_name(self.database_path.name + "-shm"))

    @contextmanager
    def metadata_transaction(self) -> Iterator[sqlite3.Connection]:
        """Write operational metadata without advancing business state version."""
        with self._thread_lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except sqlite3.OperationalError as exc:
                connection.rollback()
                code = (
                    "STATE_BUSY"
                    if "locked" in str(exc).lower()
                    or "busy" in str(exc).lower()
                    else "STATE_METADATA_WRITE_FAILED"
                )
                raise StateError(
                    "SQLite operational metadata transaction failed",
                    error_code=code,
                ) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise StateError(
                    "SQLite operational metadata transaction failed",
                    error_code="STATE_METADATA_WRITE_FAILED",
                ) from exc
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def state_commit_seq(self) -> int:
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT state_commit_seq FROM state_clock WHERE singleton = 1"
            ).fetchone()
            return int(row[0])
        finally:
            connection.close()

    def last_exported_state_commit_seq(self) -> int | None:
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT last_exported_state_commit_seq
                FROM compatibility_export WHERE singleton = 1
                """
            ).fetchone()
            return None if row is None or row[0] is None else int(row[0])
        finally:
            connection.close()

    def set_export_metadata(
        self,
        *,
        state_commit_seq: int,
        generated_at: str,
        export_manifest_sha256: str,
    ) -> None:
        with self.metadata_transaction() as connection:
            connection.execute(
                """
                UPDATE compatibility_export
                SET last_exported_state_commit_seq = ?,
                    generated_at = ?,
                    export_manifest_sha256 = ?
                WHERE singleton = 1
                """,
                (state_commit_seq, generated_at, export_manifest_sha256),
            )

    def ensure_partition(self, config: dict[str, Any] | None) -> tuple[str, str]:
        safe_config = config or {}
        workspace, fingerprint = lightrag_backend_identity(safe_config)
        fingerprint_payload = {
            "schema_version": 1,
            "mode": safe_config.get("mode", "service"),
            "workspace": workspace,
            "base_url": safe_config.get("base_url", ""),
            "api_key_env": safe_config.get("api_key_env", "LIGHTRAG_API_KEY"),
            "bearer_token_env": safe_config.get(
                "bearer_token_env",
                "LIGHTRAG_BEARER_TOKEN",
            ),
        }
        partition_id = _deterministic_id("partition", fingerprint)
        connection = self.connect(read_only=True)
        try:
            if connection.execute(
                "SELECT 1 FROM retrieval_partition WHERE id = ?",
                (partition_id,),
            ).fetchone() is not None:
                return partition_id, fingerprint
        finally:
            connection.close()
        with self.business_transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO retrieval_partition(
                  id, security_domain_id, backend_kind, backend_alias,
                  namespace, status, config_json, backend_fingerprint, created_at
                ) VALUES (?, ?, 'lightrag', 'default', ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    partition_id,
                    DEFAULT_DOMAIN_ID,
                    workspace,
                    _canonical_json(fingerprint_payload),
                    fingerprint,
                    utc_now(),
                ),
            )
        return partition_id, fingerprint

    def stage_files(
        self,
        files: Sequence[CorpusFile],
        *,
        provenance: str = "native",
        allow_missing_snapshot: bool = False,
    ) -> dict[tuple[str, str], str]:
        prepared: list[tuple[CorpusFile, str | None, str]] = []
        for item in files:
            relative_path = normalize_workspace_relative_path(
                item.path,
                field_name="source.path",
            )
            source = resolve_under_root(self.root, relative_path)
            source_matches_revision = (
                source.exists()
                and source.is_file()
                and sha256_file(source) == item.sha256
            )
            if source_matches_revision:
                snapshot_path = self._ensure_snapshot(source, item.sha256)
                snapshot_status = "AVAILABLE"
            elif allow_missing_snapshot:
                snapshot_path = None
                snapshot_status = "UNAVAILABLE_LEGACY"
            else:
                raise StateError(
                    "source file disappeared before it could be staged",
                    error_code="STATE_SOURCE_MISSING",
                )
            prepared.append((item, snapshot_path, snapshot_status))

        result: dict[tuple[str, str], str] = {}
        missing_revision = False
        connection = self.connect(read_only=True)
        try:
            for item, _, _ in prepared:
                source_id = _deterministic_id(
                    "src",
                    DEFAULT_DOMAIN_ID,
                    item.path,
                )
                revision_id = _deterministic_id(
                    "rev",
                    source_id,
                    item.sha256,
                )
                result[(item.path, item.sha256)] = revision_id
                if connection.execute(
                    "SELECT 1 FROM source_revision WHERE id = ?",
                    (revision_id,),
                ).fetchone() is None:
                    missing_revision = True
        finally:
            connection.close()
        if not missing_revision:
            return result

        with self.business_transaction() as connection:
            for item, snapshot_path, snapshot_status in prepared:
                source_id = _deterministic_id(
                    "src",
                    DEFAULT_DOMAIN_ID,
                    item.path,
                )
                revision_id = _deterministic_id(
                    "rev",
                    source_id,
                    item.sha256,
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO source_document(
                      id, logical_key, canonical_path, security_domain_id,
                      media_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        item.path,
                        item.path,
                        DEFAULT_DOMAIN_ID,
                        item.suffix or "application/octet-stream",
                        utc_now(),
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO source_revision(
                      id, source_id, sha256, size_bytes, suffix, text_like,
                      snapshot_path, snapshot_status, status, provenance, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'STAGED', ?, ?)
                    """,
                    (
                        revision_id,
                        source_id,
                        item.sha256,
                        item.size,
                        item.suffix,
                        int(item.text_like),
                        snapshot_path,
                        snapshot_status,
                        provenance,
                        utc_now(),
                    ),
                )
        return result

    def _ensure_snapshot(self, source: Path, expected_sha256: str) -> str:
        digest = expected_sha256.removeprefix("sha256:")
        if (
            not expected_sha256.startswith("sha256:")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise StateError(
                "source hash is invalid",
                error_code="STATE_SOURCE_HASH_INVALID",
            )
        shard = self.snapshots_root / digest[:2]
        _private_directory(shard)
        target = shard / digest
        if target.exists():
            if sha256_file(target) != expected_sha256:
                raise StateError(
                    "existing immutable snapshot hash does not match",
                    error_code="STATE_SNAPSHOT_CORRUPT",
                )
            return self._snapshot_reference(target)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.",
            suffix=".tmp",
            dir=shard,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as destination, source.open("rb") as origin:
                shutil.copyfileobj(origin, destination)
                destination.flush()
                os.fsync(destination.fileno())
            if sha256_file(temporary) != expected_sha256:
                raise StateError(
                    "immutable snapshot hash verification failed",
                    error_code="STATE_SNAPSHOT_HASH_MISMATCH",
                )
            if os.name == "posix":
                temporary.chmod(0o600)
            os.replace(temporary, target)
            _fsync_directory(shard)
        finally:
            if temporary.exists():
                temporary.unlink()
        return self._snapshot_reference(target)

    def _snapshot_reference(self, target: Path) -> str:
        try:
            return target.relative_to(self.root).as_posix()
        except ValueError:
            # A dry-run candidate lives in a system temporary directory. Its
            # snapshot references are intentionally ephemeral and are never
            # installed into the workspace.
            return target.as_posix()

    def latest_lane_files(self, lane: str) -> list[CorpusFile]:
        connection = self.connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT d.canonical_path, r.sha256, r.size_bytes, r.suffix, r.text_like
                FROM lane_run lr
                JOIN lane_run_revision lrr ON lrr.run_id = lr.id
                JOIN source_revision r ON r.id = lrr.revision_id
                JOIN source_document d ON d.id = r.source_id
                WHERE lr.id = (
                  SELECT id FROM lane_run
                  WHERE lane = ? AND status = 'SUCCEEDED'
                  ORDER BY rowid DESC
                  LIMIT 1
                )
                AND lrr.role = 'INPUT'
                ORDER BY d.canonical_path
                """,
                (lane,),
            ).fetchall()
        finally:
            connection.close()
        return [
            CorpusFile(
                path=row["canonical_path"],
                sha256=row["sha256"],
                size=int(row["size_bytes"]),
                suffix=row["suffix"],
                text_like=bool(row["text_like"]),
            )
            for row in rows
        ]

    def begin_lane_run(
        self,
        *,
        run_id: str,
        journal_run_id: str | None,
        lane: str,
        operation: str = "run",
    ) -> None:
        now = utc_now()
        with self.business_transaction() as connection:
            connection.execute(
                """
                INSERT INTO lane_run(
                  id, journal_run_id, lane, operation, idempotency_key,
                  status, run_origin, verification_status, side_effects_executed,
                  created_at, started_at
                ) VALUES (?, ?, ?, ?, ?, 'RUNNING', 'NATIVE', 'VERIFIED', 0, ?, ?)
                """,
                (
                    run_id,
                    journal_run_id,
                    lane,
                    operation,
                    f"{operation}:{run_id}",
                    now,
                    now,
                ),
            )

    def finish_lane_run(
        self,
        *,
        run_id: str,
        status: str,
        files: Sequence[CorpusFile] = (),
        revision_ids: dict[tuple[str, str], str] | None = None,
        error_code: str | None = None,
        side_effects_executed: bool = False,
    ) -> None:
        if status not in {
            "SUCCEEDED",
            "FAILED",
            "NEEDS_AUDIT",
            "CANCELLED",
        }:
            raise StateError(
                "lane run terminal status is invalid",
                error_code="STATE_RUN_STATUS_INVALID",
            )
        connection = self.connect(read_only=True)
        try:
            run = connection.execute(
                """
                SELECT status FROM lane_run WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
        if run is None:
            raise StateError(
                "lane run does not exist",
                error_code="STATE_RUN_NOT_FOUND",
            )
        if run["status"] != "RUNNING":
            return
        revision_ids = revision_ids or {}
        with self.business_transaction() as connection:
            connection.execute(
                """
                UPDATE lane_run
                SET status = ?, side_effects_executed = ?,
                    error_code = ?, finished_at = ?
                WHERE id = ? AND status = 'RUNNING'
                """,
                (
                    status,
                    int(side_effects_executed),
                    error_code,
                    utc_now(),
                    run_id,
                ),
            )
            if status == "SUCCEEDED":
                for item in files:
                    revision_id = revision_ids.get((item.path, item.sha256))
                    if revision_id is None:
                        revision_id = _deterministic_id(
                            "rev",
                            _deterministic_id(
                                "src",
                                DEFAULT_DOMAIN_ID,
                                item.path,
                            ),
                            item.sha256,
                        )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO lane_run_revision(
                          run_id, revision_id, role
                        ) VALUES (?, ?, 'INPUT')
                        """,
                        (run_id, revision_id),
                    )

    def import_legacy_lane_baseline(
        self,
        *,
        lane: str,
        files: Sequence[CorpusFile],
        source_fingerprint: str,
        legacy_observed_at: str | None = None,
    ) -> None:
        if not files:
            return
        revision_ids = self.stage_files(
            files,
            provenance="legacy_unverified",
            allow_missing_snapshot=True,
        )
        run_id = _deterministic_id("legacy-run", lane, source_fingerprint)
        connection = self.connect(read_only=True)
        try:
            if connection.execute(
                "SELECT 1 FROM lane_run WHERE id = ?",
                (run_id,),
            ).fetchone() is not None:
                return
        finally:
            connection.close()
        now = utc_now()
        with self.business_transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO lane_run(
                  id, lane, operation, idempotency_key, status, run_origin,
                  verification_status, side_effects_executed, imported_at,
                  legacy_observed_at, created_at, finished_at
                ) VALUES (?, ?, 'legacy_import', ?, 'SUCCEEDED',
                          'LEGACY_MIGRATION', 'UNVERIFIED', 0, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    lane,
                    f"legacy_import:{lane}:{source_fingerprint}",
                    now,
                    legacy_observed_at,
                    now,
                    now,
                ),
            )
            for item in files:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO lane_run_revision(
                      run_id, revision_id, role
                    ) VALUES (?, ?, 'INPUT')
                    """,
                    (run_id, revision_ids[(item.path, item.sha256)]),
                )

    def list_lightrag_documents(self) -> dict[str, dict[str, Any]]:
        connection = self.connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT b.id, b.file_source, b.track_id, b.remote_status,
                       b.action_gate, b.gate_reason, b.chunk_count,
                       b.submitted_at, b.processed_at, r.sha256,
                       r.status AS revision_status
                FROM lightrag_binding b
                JOIN source_revision r ON r.id = b.revision_id
                ORDER BY b.file_source
                """
            ).fetchall()
        finally:
            connection.close()
        documents: dict[str, dict[str, Any]] = {}
        for row in rows:
            doc_id = row["file_source"].replace("/", "__").replace(" ", "_")
            documents[doc_id] = {
                "binding_id": row["id"],
                "source_path": row["file_source"],
                "sha256": row["sha256"],
                "service_track_id": row["track_id"],
                "remote_status": row["remote_status"],
                "action_gate": row["action_gate"],
                "gate_reason": row["gate_reason"],
                "chunk_count": row["chunk_count"],
                "submitted_at": row["submitted_at"],
                "processed_at": row["processed_at"],
                "revision_status": row["revision_status"],
            }
        return documents

    def import_legacy_binding(
        self,
        *,
        source_path: str,
        sha256: str,
        size: int,
        suffix: str,
        text_like: bool,
        track_id: str | None,
        submitted_at: str | None,
        processed_at: str | None,
        partition_id: str,
        backend_fingerprint: str,
    ) -> str:
        item = CorpusFile(
            path=source_path,
            sha256=sha256,
            size=size,
            suffix=suffix,
            text_like=text_like,
        )
        revisions = self.stage_files(
            [item],
            provenance="legacy_unverified",
            allow_missing_snapshot=True,
        )
        revision_id = revisions[(source_path, sha256)]
        binding_id = _deterministic_id(
            "binding",
            revision_id,
            partition_id,
            backend_fingerprint,
        )
        connection = self.connect(read_only=True)
        try:
            if connection.execute(
                "SELECT 1 FROM lightrag_binding WHERE id = ?",
                (binding_id,),
            ).fetchone() is not None:
                return binding_id
        finally:
            connection.close()
        with self.business_transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO lightrag_binding(
                  id, revision_id, retrieval_partition_id, backend_fingerprint,
                  file_source, track_id, remote_status, action_gate, gate_reason,
                  submitted_at, processed_at, last_observed_at, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, 'UNKNOWN', 'BLOCKED',
                          'LEGACY_UNVERIFIED', ?, ?, ?, 'LEGACY_REMOTE_STATUS_UNKNOWN')
                """,
                (
                    binding_id,
                    revision_id,
                    partition_id,
                    backend_fingerprint,
                    source_path,
                    track_id,
                    submitted_at,
                    processed_at,
                    utc_now(),
                ),
            )
        return binding_id

    def mark_submission_started(
        self,
        *,
        source_path: str,
        sha256: str,
        partition_id: str,
        backend_fingerprint: str,
    ) -> str:
        revision_id = self.revision_id_for(source_path, sha256)
        if revision_id is None:
            raise StateError(
                "cannot submit a source revision that has not been staged",
                error_code="STATE_REVISION_NOT_STAGED",
            )
        binding_id = _deterministic_id(
            "binding",
            revision_id,
            partition_id,
            backend_fingerprint,
        )
        with self.business_transaction() as connection:
            existing = connection.execute(
                "SELECT action_gate, gate_reason FROM lightrag_binding WHERE id = ?",
                (binding_id,),
            ).fetchone()
            if existing is not None and existing["action_gate"] == "BLOCKED":
                raise StateError(
                    "LightRAG binding is blocked and must be reconciled",
                    error_code="LIGHTRAG_BINDING_BLOCKED",
                )
            connection.execute(
                """
                INSERT INTO lightrag_binding(
                  id, revision_id, retrieval_partition_id, backend_fingerprint,
                  file_source, remote_status, action_gate, gate_reason,
                  last_observed_at
                ) VALUES (?, ?, ?, ?, ?, 'UNKNOWN', 'BLOCKED',
                          'SUBMIT_IN_FLIGHT', ?)
                ON CONFLICT(id) DO UPDATE SET
                  remote_status = 'UNKNOWN',
                  action_gate = 'BLOCKED',
                  gate_reason = 'SUBMIT_IN_FLIGHT',
                  error_code = NULL,
                  last_observed_at = excluded.last_observed_at
                """,
                (
                    binding_id,
                    revision_id,
                    partition_id,
                    backend_fingerprint,
                    source_path,
                    utc_now(),
                ),
            )
        return binding_id

    def mark_submission_acknowledged(
        self,
        binding_id: str,
        *,
        track_id: str,
    ) -> None:
        with self.business_transaction() as connection:
            updated = connection.execute(
                """
                UPDATE lightrag_binding
                SET track_id = ?, remote_status = 'PENDING',
                    action_gate = 'BLOCKED', gate_reason = 'REMOTE_STATUS_UNCONFIRMED',
                    submitted_at = ?, last_observed_at = ?, error_code = NULL
                WHERE id = ?
                """,
                (track_id, utc_now(), utc_now(), binding_id),
            )
            if updated.rowcount != 1:
                raise StateError(
                    "LightRAG binding does not exist",
                    error_code="STATE_BINDING_NOT_FOUND",
                )

    def mark_binding_observation(
        self,
        binding_id: str,
        *,
        remote_status: RemoteStatus,
        action_gate: ActionGate,
        gate_reason: str | None,
        chunk_count: int | None = None,
        error_code: str | None = None,
    ) -> None:
        processed_at = utc_now() if remote_status is RemoteStatus.PROCESSED else None
        with self.business_transaction() as connection:
            updated = connection.execute(
                """
                UPDATE lightrag_binding
                SET remote_status = ?, action_gate = ?, gate_reason = ?,
                    chunk_count = ?, processed_at = COALESCE(?, processed_at),
                    last_observed_at = ?, error_code = ?
                WHERE id = ?
                """,
                (
                    remote_status.value,
                    action_gate.value,
                    gate_reason,
                    chunk_count,
                    processed_at,
                    utc_now(),
                    error_code,
                    binding_id,
                ),
            )
            if updated.rowcount != 1:
                raise StateError(
                    "LightRAG binding does not exist",
                    error_code="STATE_BINDING_NOT_FOUND",
                )

    def activate_processed_binding(self, binding_id: str) -> None:
        """Activate a normal ingestion revision after its binding is trusted."""
        connection = self.connect(read_only=True)
        try:
            existing = connection.execute(
                """
                SELECT r.status
                FROM lightrag_binding b
                JOIN source_revision r ON r.id = b.revision_id
                WHERE b.id = ?
                """,
                (binding_id,),
            ).fetchone()
        finally:
            connection.close()
        if existing is not None and existing["status"] == "ACTIVE":
            return
        with self.business_transaction() as connection:
            row = connection.execute(
                """
                SELECT b.revision_id, b.remote_status, b.action_gate,
                       b.gate_reason, r.source_id, r.status
                FROM lightrag_binding b
                JOIN source_revision r ON r.id = b.revision_id
                WHERE b.id = ?
                """,
                (binding_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "LightRAG binding does not exist",
                    error_code="STATE_BINDING_NOT_FOUND",
                )
            if (
                row["remote_status"] != "PROCESSED"
                or row["action_gate"] != "OPEN"
                or row["gate_reason"] is not None
            ):
                raise StateError(
                    "only a processed open binding can activate a revision",
                    error_code="STATE_BINDING_NOT_READY",
                )
            if row["status"] == "ACTIVE":
                return
            if row["status"] != "STAGED":
                raise StateError(
                    "only a staged revision can become active",
                    error_code="STATE_REVISION_NOT_STAGED",
                )
            connection.execute(
                """
                UPDATE source_revision
                SET status = 'SUPERSEDED'
                WHERE source_id = ? AND status = 'ACTIVE'
                  AND id != ?
                """,
                (row["source_id"], row["revision_id"]),
            )
            connection.execute(
                """
                UPDATE source_revision
                SET status = 'ACTIVE'
                WHERE id = ? AND status = 'STAGED'
                """,
                (row["revision_id"],),
            )

    def mark_submission_unknown(
        self,
        binding_id: str,
        *,
        error_code: str = "RESPONSE_LOST_AFTER_SUBMIT",
    ) -> None:
        self.mark_binding_observation(
            binding_id,
            remote_status=RemoteStatus.UNKNOWN,
            action_gate=ActionGate.BLOCKED,
            gate_reason="RESPONSE_LOST_AFTER_SUBMIT",
            error_code=error_code,
        )

    def mark_submission_conflict(self, binding_id: str) -> None:
        self.mark_binding_observation(
            binding_id,
            remote_status=RemoteStatus.UNKNOWN,
            action_gate=ActionGate.BLOCKED,
            gate_reason="REMOTE_HTTP_409",
            error_code="REMOTE_HTTP_409",
        )

    def revision_id_for(self, source_path: str, sha256: str) -> str | None:
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT r.id
                FROM source_revision r
                JOIN source_document d ON d.id = r.source_id
                WHERE d.canonical_path = ? AND r.sha256 = ?
                """,
                (source_path, sha256),
            ).fetchone()
            return None if row is None else str(row[0])
        finally:
            connection.close()

    def bindings_for_reconcile(self) -> list[dict[str, Any]]:
        connection = self.connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT id, file_source, track_id, remote_status,
                       action_gate, gate_reason
                FROM lightrag_binding
                WHERE action_gate = 'BLOCKED'
                ORDER BY file_source
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def bindings_for_replace_plan(self) -> list[dict[str, Any]]:
        connection = self.connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT b.id, b.file_source, b.track_id, b.remote_status,
                       b.action_gate, b.gate_reason, b.error_code,
                       b.retrieval_partition_id, b.backend_fingerprint,
                       r.id AS revision_id, r.source_id, r.sha256,
                       r.snapshot_path,
                       r.snapshot_status, r.status AS revision_status,
                       p.namespace
                FROM lightrag_binding b
                JOIN source_revision r ON r.id = b.revision_id
                JOIN retrieval_partition p
                  ON p.id = b.retrieval_partition_id
                WHERE b.action_gate = 'BLOCKED'
                  AND (
                    b.gate_reason = 'REMOTE_HTTP_409'
                    OR b.error_code = 'REMOTE_HTTP_409'
                  )
                ORDER BY b.file_source, b.id
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def replacement_execution_context(
        self,
        *,
        target_binding_id: str,
        owner_binding_id: str,
    ) -> dict[str, Any]:
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT
                  tb.id AS target_binding_id,
                  tb.file_source AS source_path,
                  tb.retrieval_partition_id,
                  tb.backend_fingerprint,
                  tr.id AS target_revision_id,
                  tr.source_id,
                  tr.sha256 AS target_sha256,
                  tr.snapshot_path AS target_snapshot_path,
                  tr.snapshot_status AS target_snapshot_status,
                  tr.text_like AS target_text_like,
                  tr.status AS target_revision_status,
                  ob.id AS owner_binding_id,
                  ob.remote_doc_id AS stored_owner_remote_doc_id,
                  ob.track_id AS stored_owner_track_id,
                  ob.remote_status AS owner_remote_status,
                  orv.id AS owner_revision_id,
                  orv.sha256 AS owner_sha256,
                  orv.snapshot_path AS owner_snapshot_path,
                  orv.snapshot_status AS owner_snapshot_status,
                  orv.text_like AS owner_text_like,
                  orv.status AS owner_revision_status,
                  p.namespace
                FROM lightrag_binding tb
                JOIN source_revision tr ON tr.id = tb.revision_id
                JOIN lightrag_binding ob ON ob.id = ?
                JOIN source_revision orv ON orv.id = ob.revision_id
                JOIN retrieval_partition p
                  ON p.id = tb.retrieval_partition_id
                WHERE tb.id = ?
                  AND tr.source_id = orv.source_id
                  AND tb.retrieval_partition_id =
                      ob.retrieval_partition_id
                  AND tb.backend_fingerprint = ob.backend_fingerprint
                """,
                (owner_binding_id, target_binding_id),
            ).fetchone()
            if row is None:
                raise StateError(
                    "replacement target and owner cannot be aligned",
                    error_code="REPLACE_CONTEXT_INVALID",
                )
            return dict(row)
        finally:
            connection.close()

    def create_replacement_operation(
        self,
        *,
        operation_id: str,
        plan_id: str,
        plan_digest: str,
        context: dict[str, Any],
        owner_remote_doc_id: str,
        owner_remote_track_id: str,
        backup_id: str,
        backup_path: str,
        backup_sha256: str,
        backup_state_commit_seq: int,
        maintenance_window_seconds: float,
        absence_confirmations: int,
        auto_compensate: bool,
        smoke_query_sha256: str,
        confirmed_by: str,
        confirmed_host: str,
    ) -> dict[str, Any]:
        self.require_schema_version(REPLACEMENT_SCHEMA_VERSION)
        now = utc_now()
        with self.metadata_transaction() as connection:
            current_seq = int(
                connection.execute(
                    """
                    SELECT state_commit_seq FROM state_clock
                    WHERE singleton = 1
                    """
                ).fetchone()[0]
            )
            if current_seq != backup_state_commit_seq:
                raise StateError(
                    "business state changed after the replacement backup",
                    error_code="REPLACE_BACKUP_STALE",
                )
            existing = connection.execute(
                """
                SELECT id FROM replacement_operation
                WHERE source_id = ? AND retrieval_partition_id = ?
                  AND status IN (
                    'IN_PROGRESS', 'BLOCKED', 'NEEDS_AUDIT'
                  )
                LIMIT 1
                """,
                (
                    context["source_id"],
                    context["retrieval_partition_id"],
                ),
            ).fetchone()
            if existing is not None:
                raise StateError(
                    "a replacement operation is already active",
                    error_code="REPLACE_OPERATION_ACTIVE",
                    details={"operation_id": str(existing["id"])},
                )
            connection.execute(
                """
                INSERT INTO replacement_operation(
                  id, plan_id, plan_digest, source_id, source_path,
                  target_revision_id, target_binding_id,
                  owner_revision_id, owner_binding_id,
                  retrieval_partition_id, backend_fingerprint,
                  owner_remote_doc_id, owner_remote_track_id,
                  phase, status, effect_certainty,
                  backup_id, backup_path, backup_sha256,
                  backup_state_commit_seq, prepared_state_commit_seq,
                  maintenance_window_seconds, absence_confirmations,
                  auto_compensate, smoke_query_sha256,
                  confirmed_by, confirmed_host,
                  created_at, updated_at
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  'PREPARED', 'IN_PROGRESS', 'NONE',
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    operation_id,
                    plan_id,
                    plan_digest,
                    context["source_id"],
                    context["source_path"],
                    context["target_revision_id"],
                    context["target_binding_id"],
                    context["owner_revision_id"],
                    context["owner_binding_id"],
                    context["retrieval_partition_id"],
                    context["backend_fingerprint"],
                    owner_remote_doc_id,
                    owner_remote_track_id,
                    backup_id,
                    backup_path,
                    backup_sha256,
                    backup_state_commit_seq,
                    current_seq,
                    maintenance_window_seconds,
                    absence_confirmations,
                    int(auto_compensate),
                    smoke_query_sha256,
                    confirmed_by,
                    confirmed_host,
                    now,
                    now,
                ),
            )
        return self.replacement_operation(operation_id)

    def replacement_operation(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        self.require_schema_version(REPLACEMENT_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT * FROM replacement_operation WHERE id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "replacement operation does not exist",
                    error_code="REPLACE_OPERATION_NOT_FOUND",
                )
            return dict(row)
        finally:
            connection.close()

    def replacement_operation_for_plan(
        self,
        plan_id: str,
    ) -> dict[str, Any] | None:
        if self.schema_version() < REPLACEMENT_SCHEMA_VERSION:
            return None
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT * FROM replacement_operation
                WHERE plan_id = ?
                  AND status IN (
                    'IN_PROGRESS', 'BLOCKED', 'NEEDS_AUDIT'
                  )
                ORDER BY created_at DESC LIMIT 1
                """,
                (plan_id,),
            ).fetchone()
            return None if row is None else dict(row)
        finally:
            connection.close()

    def list_replacement_operations(
        self,
        *,
        operation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.schema_version() < REPLACEMENT_SCHEMA_VERSION:
            return []
        connection = self.connect(read_only=True)
        try:
            if operation_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM replacement_operation
                    ORDER BY created_at DESC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM replacement_operation
                    WHERE id = ?
                    """,
                    (operation_id,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def transition_replacement_operation(
        self,
        operation_id: str,
        *,
        expected_phases: set[str],
        phase: str,
        status: str | None = None,
        effect_certainty: str | None = None,
        last_effect: str | None = None,
        error_code: str | None = None,
        next_action: str | None = None,
        target_remote_doc_id: str | None = None,
        target_remote_track_id: str | None = None,
        increment_delete: bool = False,
        increment_submit: bool = False,
        start_maintenance: bool = False,
        completed: bool = False,
        business_fact: bool = False,
    ) -> dict[str, Any]:
        manager = (
            self.business_transaction(allow_replacement=True)
            if business_fact
            else self.metadata_transaction()
        )
        with manager as connection:
            row = connection.execute(
                """
                SELECT * FROM replacement_operation WHERE id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "replacement operation does not exist",
                    error_code="REPLACE_OPERATION_NOT_FOUND",
                )
            if str(row["phase"]) not in expected_phases:
                raise StateError(
                    "replacement operation phase changed unexpectedly",
                    error_code="REPLACE_PHASE_CONFLICT",
                    details={
                        "operation_id": operation_id,
                        "phase": str(row["phase"]),
                    },
                )
            now = utc_now()
            new_delete_attempts = int(row["delete_attempts"]) + int(
                increment_delete
            )
            new_submit_attempts = int(row["submit_attempts"]) + int(
                increment_submit
            )
            connection.execute(
                """
                UPDATE replacement_operation
                SET phase = ?,
                    status = ?,
                    effect_certainty = ?,
                    last_effect = ?,
                    error_code = ?,
                    next_action = ?,
                    target_remote_doc_id = COALESCE(
                      ?, target_remote_doc_id
                    ),
                    target_remote_track_id = COALESCE(
                      ?, target_remote_track_id
                    ),
                    delete_attempts = ?,
                    submit_attempts = ?,
                    maintenance_started_at = CASE
                      WHEN ? = 1 THEN COALESCE(
                        maintenance_started_at, ?
                      )
                      ELSE maintenance_started_at
                    END,
                    updated_at = ?,
                    completed_at = CASE
                      WHEN ? = 1 THEN ?
                      ELSE completed_at
                    END
                WHERE id = ?
                """,
                (
                    phase,
                    status or str(row["status"]),
                    effect_certainty or str(row["effect_certainty"]),
                    last_effect,
                    error_code,
                    next_action,
                    target_remote_doc_id,
                    target_remote_track_id,
                    new_delete_attempts,
                    new_submit_attempts,
                    int(start_maintenance),
                    now,
                    now,
                    int(completed),
                    now,
                    operation_id,
                ),
            )
        return self.replacement_operation(operation_id)

    def mark_replacement_delete_confirmed(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        with self.business_transaction(
            allow_replacement=True
        ) as connection:
            operation = connection.execute(
                """
                SELECT * FROM replacement_operation WHERE id = ?
                """,
                (operation_id,),
            ).fetchone()
            if operation is None or operation["phase"] != "DELETE_ACCEPTED":
                raise StateError(
                    "owner deletion confirmation is out of sequence",
                    error_code="REPLACE_PHASE_CONFLICT",
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE lightrag_binding
                SET remote_status = 'MISSING',
                    action_gate = 'BLOCKED',
                    gate_reason = 'REPLACEMENT_IN_PROGRESS',
                    last_observed_at = ?,
                    error_code = NULL
                WHERE id = ?
                """,
                (now, operation["owner_binding_id"]),
            )
            connection.execute(
                """
                UPDATE replacement_operation
                SET phase = 'DELETE_CONFIRMED',
                    effect_certainty = 'KNOWN',
                    maintenance_started_at = COALESCE(
                      maintenance_started_at, ?
                    ),
                    error_code = NULL,
                    next_action = 'SUBMIT_TARGET_REVISION',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, operation_id),
            )
        return self.replacement_operation(operation_id)

    def mark_replacement_submit_accepted(
        self,
        operation_id: str,
        *,
        track_id: str,
    ) -> dict[str, Any]:
        with self.business_transaction(
            allow_replacement=True
        ) as connection:
            operation = connection.execute(
                """
                SELECT * FROM replacement_operation WHERE id = ?
                """,
                (operation_id,),
            ).fetchone()
            if operation is None or operation["phase"] != "SUBMIT_INTENT":
                raise StateError(
                    "target submission acknowledgement is out of sequence",
                    error_code="REPLACE_PHASE_CONFLICT",
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE lightrag_binding
                SET track_id = ?, remote_status = 'PENDING',
                    action_gate = 'BLOCKED',
                    gate_reason = 'REPLACEMENT_PROCESSING',
                    submitted_at = ?, last_observed_at = ?,
                    error_code = NULL
                WHERE id = ?
                """,
                (
                    track_id,
                    now,
                    now,
                    operation["target_binding_id"],
                ),
            )
            connection.execute(
                """
                UPDATE replacement_operation
                SET phase = 'SUBMIT_ACCEPTED',
                    effect_certainty = 'KNOWN',
                    target_remote_track_id = ?,
                    error_code = NULL,
                    next_action = 'WAIT_TARGET_PROCESSED',
                    updated_at = ?
                WHERE id = ?
                """,
                (track_id, now, operation_id),
            )
        return self.replacement_operation(operation_id)

    def mark_replacement_target_processed(
        self,
        operation_id: str,
        *,
        remote_doc_id: str,
        track_id: str,
        chunk_count: int,
    ) -> dict[str, Any]:
        with self.business_transaction(
            allow_replacement=True
        ) as connection:
            operation = connection.execute(
                """
                SELECT * FROM replacement_operation WHERE id = ?
                """,
                (operation_id,),
            ).fetchone()
            if operation is None or operation["phase"] != "SUBMIT_ACCEPTED":
                raise StateError(
                    "target processed observation is out of sequence",
                    error_code="REPLACE_PHASE_CONFLICT",
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE lightrag_binding
                SET remote_doc_id = ?, track_id = ?,
                    remote_status = 'PROCESSED',
                    action_gate = 'BLOCKED',
                    gate_reason = 'REPLACEMENT_VALIDATION_PENDING',
                    chunk_count = ?, processed_at = ?,
                    last_observed_at = ?, error_code = NULL
                WHERE id = ?
                """,
                (
                    remote_doc_id,
                    track_id,
                    chunk_count,
                    now,
                    now,
                    operation["target_binding_id"],
                ),
            )
            connection.execute(
                """
                UPDATE replacement_operation
                SET phase = 'TARGET_PROCESSED',
                    effect_certainty = 'KNOWN',
                    target_remote_doc_id = ?,
                    target_remote_track_id = ?,
                    error_code = NULL,
                    next_action = 'RUN_SMOKE_EVIDENCE_CHECK',
                    updated_at = ?
                WHERE id = ?
                """,
                (remote_doc_id, track_id, now, operation_id),
            )
        return self.replacement_operation(operation_id)

    def complete_replacement(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        with self.business_transaction(
            allow_replacement=True
        ) as connection:
            operation = connection.execute(
                """
                SELECT * FROM replacement_operation WHERE id = ?
                """,
                (operation_id,),
            ).fetchone()
            if operation is None or operation["phase"] != "VALIDATED":
                raise StateError(
                    "replacement completion is out of sequence",
                    error_code="REPLACE_PHASE_CONFLICT",
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE source_revision
                SET status = 'SUPERSEDED'
                WHERE source_id = ? AND status = 'ACTIVE'
                  AND id != ?
                """,
                (
                    operation["source_id"],
                    operation["target_revision_id"],
                ),
            )
            connection.execute(
                """
                UPDATE source_revision SET status = 'SUPERSEDED'
                WHERE id = ?
                """,
                (operation["owner_revision_id"],),
            )
            connection.execute(
                """
                UPDATE source_revision SET status = 'ACTIVE'
                WHERE id = ?
                """,
                (operation["target_revision_id"],),
            )
            connection.execute(
                """
                UPDATE lightrag_binding
                SET action_gate = 'OPEN', gate_reason = NULL,
                    error_code = NULL, last_observed_at = ?
                WHERE id = ?
                  AND remote_status = 'PROCESSED'
                """,
                (now, operation["target_binding_id"]),
            )
            connection.execute(
                """
                UPDATE replacement_operation
                SET phase = 'COMPLETED', status = 'COMPLETED',
                    effect_certainty = 'KNOWN',
                    error_code = NULL, next_action = NULL,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (now, now, operation_id),
            )
        return self.replacement_operation(operation_id)

    def mark_compensation_target_deleted(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        with self.business_transaction(
            allow_replacement=True
        ) as connection:
            operation = connection.execute(
                """
                SELECT * FROM replacement_operation WHERE id = ?
                """,
                (operation_id,),
            ).fetchone()
            if (
                operation is None
                or operation["phase"]
                not in {
                    "TARGET_DELETE_ACCEPTED",
                    "COMPENSATION_REQUIRED",
                }
            ):
                raise StateError(
                    "target deletion confirmation is out of sequence",
                    error_code="REPLACE_PHASE_CONFLICT",
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE lightrag_binding
                SET remote_status = 'MISSING',
                    action_gate = 'BLOCKED',
                    gate_reason = 'REPLACEMENT_ROLLBACK',
                    last_observed_at = ?,
                    error_code = NULL
                WHERE id = ?
                """,
                (now, operation["target_binding_id"]),
            )
            connection.execute(
                """
                UPDATE replacement_operation
                SET phase = 'TARGET_DELETE_CONFIRMED',
                    effect_certainty = 'KNOWN',
                    error_code = NULL,
                    next_action = 'RESTORE_OWNER_SNAPSHOT',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, operation_id),
            )
        return self.replacement_operation(operation_id)

    def mark_owner_restore_accepted(
        self,
        operation_id: str,
        *,
        track_id: str,
    ) -> dict[str, Any]:
        with self.business_transaction(
            allow_replacement=True
        ) as connection:
            operation = connection.execute(
                """
                SELECT * FROM replacement_operation WHERE id = ?
                """,
                (operation_id,),
            ).fetchone()
            if (
                operation is None
                or operation["phase"] != "OWNER_SUBMIT_INTENT"
            ):
                raise StateError(
                    "owner restore acknowledgement is out of sequence",
                    error_code="REPLACE_PHASE_CONFLICT",
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE lightrag_binding
                SET track_id = ?, remote_status = 'PENDING',
                    action_gate = 'BLOCKED',
                    gate_reason = 'REPLACEMENT_ROLLBACK',
                    submitted_at = ?, last_observed_at = ?,
                    error_code = NULL
                WHERE id = ?
                """,
                (
                    track_id,
                    now,
                    now,
                    operation["owner_binding_id"],
                ),
            )
            connection.execute(
                """
                UPDATE replacement_operation
                SET phase = 'OWNER_SUBMIT_ACCEPTED',
                    effect_certainty = 'KNOWN',
                    error_code = NULL,
                    next_action = 'WAIT_OWNER_PROCESSED',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, operation_id),
            )
        return self.replacement_operation(operation_id)

    def complete_replacement_rollback(
        self,
        operation_id: str,
        *,
        remote_doc_id: str,
        track_id: str,
        chunk_count: int,
    ) -> dict[str, Any]:
        with self.business_transaction(
            allow_replacement=True
        ) as connection:
            operation = connection.execute(
                """
                SELECT * FROM replacement_operation WHERE id = ?
                """,
                (operation_id,),
            ).fetchone()
            if (
                operation is None
                or operation["phase"] != "OWNER_SUBMIT_ACCEPTED"
            ):
                raise StateError(
                    "replacement rollback completion is out of sequence",
                    error_code="REPLACE_PHASE_CONFLICT",
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE source_revision
                SET status = 'SUPERSEDED'
                WHERE source_id = ? AND status = 'ACTIVE'
                  AND id != ?
                """,
                (
                    operation["source_id"],
                    operation["owner_revision_id"],
                ),
            )
            connection.execute(
                """
                UPDATE source_revision SET status = 'REJECTED'
                WHERE id = ?
                """,
                (operation["target_revision_id"],),
            )
            connection.execute(
                """
                UPDATE source_revision SET status = 'ACTIVE'
                WHERE id = ?
                """,
                (operation["owner_revision_id"],),
            )
            connection.execute(
                """
                UPDATE lightrag_binding
                SET remote_doc_id = ?, track_id = ?,
                    remote_status = 'PROCESSED',
                    action_gate = 'OPEN', gate_reason = NULL,
                    chunk_count = ?, processed_at = ?,
                    last_observed_at = ?, error_code = NULL
                WHERE id = ?
                """,
                (
                    remote_doc_id,
                    track_id,
                    chunk_count,
                    now,
                    now,
                    operation["owner_binding_id"],
                ),
            )
            connection.execute(
                """
                UPDATE replacement_operation
                SET phase = 'ROLLED_BACK', status = 'ROLLED_BACK',
                    effect_certainty = 'KNOWN',
                    error_code = NULL, next_action = NULL,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (now, now, operation_id),
            )
        return self.replacement_operation(operation_id)

    def replacement_owner_candidates(
        self,
        *,
        source_path: str,
        target_binding_id: str,
        retrieval_partition_id: str,
        backend_fingerprint: str,
    ) -> list[dict[str, Any]]:
        connection = self.connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT b.id, b.track_id, b.remote_doc_id, b.remote_status,
                       b.action_gate, b.gate_reason,
                       r.id AS revision_id, r.sha256, r.snapshot_path,
                       r.snapshot_status, r.status AS revision_status
                FROM lightrag_binding b
                JOIN source_revision r ON r.id = b.revision_id
                WHERE b.file_source = ?
                  AND b.id != ?
                  AND b.retrieval_partition_id = ?
                  AND b.backend_fingerprint = ?
                  AND b.track_id IS NOT NULL
                ORDER BY b.processed_at DESC, b.submitted_at DESC, b.id
                """,
                (
                    source_path,
                    target_binding_id,
                    retrieval_partition_id,
                    backend_fingerprint,
                ),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def query_partition(
        self,
        lightrag_config: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Resolve the one configured active retrieval partition without writing."""
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        workspace, fingerprint = lightrag_backend_identity(lightrag_config)
        connection = self.connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT p.*, d.name AS security_domain_name,
                       d.status AS security_domain_status
                FROM retrieval_partition p
                JOIN security_domain d ON d.id = p.security_domain_id
                WHERE p.backend_fingerprint = ?
                  AND p.namespace = ?
                  AND p.status = 'ACTIVE'
                  AND d.status = 'ACTIVE'
                ORDER BY p.id
                """,
                (fingerprint, workspace),
            ).fetchall()
        finally:
            connection.close()
        if len(rows) != 1:
            raise StateError(
                "configured query partition is missing or ambiguous",
                error_code=(
                    "QUERY_PARTITION_MISSING"
                    if not rows
                    else "QUERY_PARTITION_AMBIGUOUS"
                ),
                details={"partition_count": len(rows)},
            )
        return dict(rows[0])

    def query_reference_candidates(
        self,
        *,
        retrieval_partition_id: str,
        backend_fingerprint: str,
    ) -> list[dict[str, Any]]:
        """Return sanitized local ownership facts used by the evidence gate."""
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT b.id AS binding_id, b.file_source,
                       b.remote_doc_id, b.track_id, b.remote_status,
                       b.action_gate, b.chunk_count,
                       r.id AS revision_id, r.sha256 AS revision_sha256,
                       r.status AS revision_status,
                       d.id AS source_id, d.canonical_path,
                       d.security_domain_id
                FROM lightrag_binding b
                JOIN source_revision r ON r.id = b.revision_id
                JOIN source_document d ON d.id = r.source_id
                WHERE b.retrieval_partition_id = ?
                  AND b.backend_fingerprint = ?
                ORDER BY b.id
                """,
                (retrieval_partition_id, backend_fingerprint),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def begin_query_run(
        self,
        *,
        request_id: str,
        retrieval_partition_id: str,
        principal_hmac: str,
        query_hmac: str,
        request_mode: str,
        gateway_mode: str,
        verification_level: str,
        lease_seconds: float,
    ) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        now = utc_now()
        lease_expires_at = _utc_after(lease_seconds)
        with self.metadata_transaction() as connection:
            fence = connection.execute(
                """
                SELECT id, state, reason_code
                FROM maintenance_fence
                WHERE retrieval_partition_id = ?
                  AND state IN ('DRAINING', 'ACTIVE', 'FAILED')
                LIMIT 1
                """,
                (retrieval_partition_id,),
            ).fetchone()
            if fence is not None:
                raise StateError(
                    "queries are paused by a maintenance fence",
                    error_code="QUERY_MAINTENANCE_ACTIVE",
                    details={
                        "fence_id": str(fence["id"]),
                        "fence_state": str(fence["state"]),
                    },
                )
            connection.execute(
                """
                INSERT INTO query_run(
                  id, retrieval_partition_id, principal_hmac, query_hmac,
                  request_mode, gateway_mode, status, verification_level,
                  lease_expires_at, heartbeat_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'RETRIEVING', ?, ?, ?, ?)
                """,
                (
                    request_id,
                    retrieval_partition_id,
                    principal_hmac,
                    query_hmac,
                    request_mode,
                    gateway_mode,
                    verification_level,
                    lease_expires_at,
                    now,
                    now,
                ),
            )
        return self.query_run(request_id)

    def heartbeat_query_run(
        self,
        request_id: str,
        *,
        lease_seconds: float,
    ) -> None:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        with self.metadata_transaction() as connection:
            updated = connection.execute(
                """
                UPDATE query_run
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE id = ? AND status = 'RETRIEVING'
                """,
                (utc_now(), _utc_after(lease_seconds), request_id),
            ).rowcount
            if updated != 1:
                raise StateError(
                    "query lease is no longer active",
                    error_code="QUERY_LEASE_INACTIVE",
                )

    def finish_query_run(
        self,
        request_id: str,
        *,
        status: str,
        verdict_code: str | None,
        error_code: str | None,
        reference_count: int,
        active_reference_count: int,
        answer_sha256: str | None,
        citation_set_sha256: str | None,
        generation_status: str | None = None,
        answer_origin: str | None = None,
        evidence_status: str | None = None,
        review_status: str | None = None,
        audit_item: dict[str, Any] | None = None,
        audit_notification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        schema_version = self.schema_version()
        if audit_notification is not None and audit_item is None:
            raise StateError(
                "audit notification requires an audit item",
                error_code="QUERY_AUDIT_INVALID",
            )
        if any(
            value is not None
            for value in (
                generation_status,
                answer_origin,
                evidence_status,
                review_status,
                audit_item,
            )
        ):
            self.require_schema_version(QUERY_DELIVERY_SCHEMA_VERSION)
        if status not in {
            "ANSWERED",
            "REFUSED",
            "NEEDS_AUDIT",
            "FAILED",
            "ABANDONED",
        }:
            raise StateError(
                "query terminal status is invalid",
                error_code="QUERY_STATUS_INVALID",
            )
        with self.metadata_transaction() as connection:
            row = connection.execute(
                """
                SELECT retrieval_partition_id, status
                FROM query_run WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "query run does not exist",
                    error_code="QUERY_RUN_NOT_FOUND",
                )
            if row["status"] != "RETRIEVING":
                raise StateError(
                    "query run is already terminal",
                    error_code="QUERY_RUN_TERMINAL",
                )
            fence = connection.execute(
                """
                SELECT id FROM maintenance_fence
                WHERE retrieval_partition_id = ?
                  AND state IN ('DRAINING', 'ACTIVE', 'FAILED')
                LIMIT 1
                """,
                (str(row["retrieval_partition_id"]),),
            ).fetchone()
            if fence is not None and status == "ANSWERED":
                status = "REFUSED"
                verdict_code = "QUERY_MAINTENANCE_ACTIVE"
                error_code = "QUERY_MAINTENANCE_ACTIVE"
                answer_sha256 = None
                generation_status = "failed"
                answer_origin = None
                evidence_status = None
                review_status = "not_required"
            now = utc_now()
            if schema_version >= QUERY_DELIVERY_SCHEMA_VERSION:
                connection.execute(
                    """
                    UPDATE query_run
                    SET status = ?, verdict_code = ?, error_code = ?,
                        reference_count = ?, active_reference_count = ?,
                        answer_sha256 = ?, citation_set_sha256 = ?,
                        generation_status = ?, answer_origin = ?,
                        evidence_status = ?, review_status = ?,
                        heartbeat_at = ?, finished_at = ?
                    WHERE id = ? AND status = 'RETRIEVING'
                    """,
                    (
                        status,
                        verdict_code,
                        error_code,
                        reference_count,
                        active_reference_count,
                        answer_sha256,
                        citation_set_sha256,
                        generation_status,
                        answer_origin,
                        evidence_status,
                        review_status,
                        now,
                        now,
                        request_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE query_run
                    SET status = ?, verdict_code = ?, error_code = ?,
                        reference_count = ?, active_reference_count = ?,
                        answer_sha256 = ?, citation_set_sha256 = ?,
                        heartbeat_at = ?, finished_at = ?
                    WHERE id = ? AND status = 'RETRIEVING'
                    """,
                    (
                        status,
                        verdict_code,
                        error_code,
                        reference_count,
                        active_reference_count,
                        answer_sha256,
                        citation_set_sha256,
                        now,
                        now,
                        request_id,
                    ),
                )
            if status == "ANSWERED" and audit_item is not None:
                self._insert_audit_item_in_connection(
                    connection,
                    audit_id=str(audit_item["audit_id"]),
                    trigger_code=str(audit_item["trigger_code"]),
                    severity=str(audit_item["severity"]),
                    subject_type=str(audit_item["subject_type"]),
                    subject_id=str(audit_item["subject_id"]),
                    evidence=dict(audit_item["evidence"]),
                    source_lane=str(
                        audit_item.get("source_lane", "query")
                    ),
                    notification=audit_notification,
                    created_at=now,
                )
        return self.query_run(request_id)

    def query_run(self, request_id: str) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM query_run WHERE id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "query run does not exist",
                    error_code="QUERY_RUN_NOT_FOUND",
                )
            return dict(row)
        finally:
            connection.close()

    def rejected_query_history(
        self,
        *,
        query_hmac: str,
        answer_sha256: str,
    ) -> dict[str, Any]:
        """Return rejected-answer history without reading protected content."""
        self.require_schema_version(QUERY_DELIVERY_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT
                  COUNT(*) AS rejection_count,
                  COALESCE(SUM(
                    CASE WHEN q.answer_sha256 = ? THEN 1 ELSE 0 END
                  ), 0) AS exact_repeat_count
                FROM audit_item a
                JOIN query_run q
                  ON a.subject_type = 'query_run'
                 AND a.subject_id = q.id
                WHERE a.status = 'REJECTED'
                  AND q.query_hmac = ?
                  AND q.generation_status = 'succeeded'
                """,
                (answer_sha256, query_hmac),
            ).fetchone()
            return {
                "previous_rejection_count": int(row["rejection_count"]),
                "exact_rejected_answer_repeat": bool(
                    int(row["exact_repeat_count"])
                ),
            }
        finally:
            connection.close()

    def query_drain_status(
        self,
        retrieval_partition_id: str,
    ) -> dict[str, int]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        now = utc_now()
        connection = self.connect(read_only=True)
        try:
            active = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM query_run
                    WHERE retrieval_partition_id = ?
                      AND status = 'RETRIEVING'
                      AND lease_expires_at >= ?
                    """,
                    (retrieval_partition_id, now),
                ).fetchone()[0]
            )
            stale = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM query_run
                    WHERE retrieval_partition_id = ?
                      AND status = 'RETRIEVING'
                      AND lease_expires_at < ?
                    """,
                    (retrieval_partition_id, now),
                ).fetchone()[0]
            )
            return {"active": active, "stale": stale}
        finally:
            connection.close()

    def abandon_expired_query_lease(
        self,
        request_id: str,
    ) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        now = utc_now()
        with self.metadata_transaction() as connection:
            row = connection.execute(
                """
                SELECT status, lease_expires_at
                FROM query_run WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "query run does not exist",
                    error_code="QUERY_RUN_NOT_FOUND",
                )
            if row["status"] != "RETRIEVING":
                raise StateError(
                    "query run is already terminal",
                    error_code="QUERY_RUN_TERMINAL",
                )
            if str(row["lease_expires_at"]) >= now:
                raise StateError(
                    "query lease has not expired",
                    error_code="QUERY_LEASE_NOT_EXPIRED",
                )
            connection.execute(
                """
                UPDATE query_run
                SET status = 'ABANDONED',
                    verdict_code = 'QUERY_LEASE_OPERATOR_ABANDONED',
                    error_code = 'QUERY_LEASE_OPERATOR_ABANDONED',
                    heartbeat_at = ?, finished_at = ?
                WHERE id = ? AND status = 'RETRIEVING'
                """,
                (now, now, request_id),
            )
        return self.query_run(request_id)

    def open_maintenance_fence(
        self,
        *,
        fence_id: str,
        retrieval_partition_id: str,
        reason_code: str,
        deadline_seconds: float,
        replacement_operation_id: str | None = None,
        notification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        now = utc_now()
        with self.metadata_transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM maintenance_fence
                WHERE retrieval_partition_id = ?
                  AND state IN ('DRAINING', 'ACTIVE', 'FAILED')
                LIMIT 1
                """,
                (retrieval_partition_id,),
            ).fetchone()
            if existing is not None:
                if (
                    replacement_operation_id is not None
                    and existing["replacement_operation_id"]
                    == replacement_operation_id
                ):
                    if notification is not None:
                        self._enqueue_notification_in_connection(
                            connection,
                            notification,
                            created_at=now,
                        )
                    return dict(existing)
                raise StateError(
                    "a maintenance fence is already active",
                    error_code="QUERY_MAINTENANCE_ALREADY_ACTIVE",
                    details={"fence_id": str(existing["id"])},
                )
            connection.execute(
                """
                INSERT INTO maintenance_fence(
                  id, retrieval_partition_id, replacement_operation_id,
                  reason_code, state, pause_started_at, deadline_at,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'DRAINING', ?, ?, ?, ?)
                """,
                (
                    fence_id,
                    retrieval_partition_id,
                    replacement_operation_id,
                    reason_code,
                    now,
                    _utc_after(deadline_seconds),
                    now,
                    now,
                ),
            )
            if notification is not None:
                self._enqueue_notification_in_connection(
                    connection,
                    notification,
                    created_at=now,
                )
        return self.maintenance_fence(fence_id)

    def transition_maintenance_fence(
        self,
        fence_id: str,
        *,
        state: str,
        notification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        if state not in {"ACTIVE", "FAILED", "CLOSED"}:
            raise StateError(
                "maintenance fence state is invalid",
                error_code="QUERY_MAINTENANCE_STATE_INVALID",
            )
        now = utc_now()
        with self.metadata_transaction() as connection:
            updated = connection.execute(
                """
                UPDATE maintenance_fence
                SET state = ?, updated_at = ?,
                    closed_at = CASE WHEN ? = 'CLOSED' THEN ? ELSE closed_at END
                WHERE id = ? AND state != 'CLOSED'
                """,
                (state, now, state, now, fence_id),
            ).rowcount
            if updated != 1:
                raise StateError(
                    "maintenance fence does not exist or is closed",
                    error_code="QUERY_MAINTENANCE_FENCE_NOT_ACTIVE",
                )
            if notification is not None:
                self._enqueue_notification_in_connection(
                    connection,
                    notification,
                    created_at=now,
                )
        return self.maintenance_fence(fence_id)

    def maintenance_fence(self, fence_id: str) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM maintenance_fence WHERE id = ?",
                (fence_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "maintenance fence does not exist",
                    error_code="QUERY_MAINTENANCE_FENCE_NOT_FOUND",
                )
            return dict(row)
        finally:
            connection.close()

    def active_maintenance_fences(self) -> list[dict[str, Any]]:
        if self.schema_version() < QUERY_GOVERNANCE_SCHEMA_VERSION:
            return []
        connection = self.connect(read_only=True)
        try:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM maintenance_fence
                    WHERE state IN ('DRAINING', 'ACTIVE', 'FAILED')
                    ORDER BY created_at
                    """
                ).fetchall()
            ]
        finally:
            connection.close()

    def register_gateway_instance(
        self,
        *,
        instance_id: str,
        retrieval_partition_id: str,
        gateway_mode: str,
        version: str,
    ) -> None:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        now = utc_now()
        with self.metadata_transaction() as connection:
            connection.execute(
                """
                INSERT INTO gateway_instance(
                  id, retrieval_partition_id, gateway_mode, process_status,
                  version, started_at, heartbeat_at
                ) VALUES (?, ?, ?, 'READY', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  gateway_mode = excluded.gateway_mode,
                  process_status = 'READY',
                  version = excluded.version,
                  heartbeat_at = excluded.heartbeat_at,
                  stopped_at = NULL
                """,
                (
                    instance_id,
                    retrieval_partition_id,
                    gateway_mode,
                    version,
                    now,
                    now,
                ),
            )

    def heartbeat_gateway_instance(self, instance_id: str) -> None:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        with self.metadata_transaction() as connection:
            updated = connection.execute(
                """
                UPDATE gateway_instance
                SET heartbeat_at = ?, process_status = 'READY'
                WHERE id = ?
                """,
                (utc_now(), instance_id),
            ).rowcount
            if updated != 1:
                raise StateError(
                    "gateway instance is not registered",
                    error_code="QUERY_GATEWAY_INSTANCE_NOT_FOUND",
                )

    def stop_gateway_instance(self, instance_id: str) -> None:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        with self.metadata_transaction() as connection:
            connection.execute(
                """
                UPDATE gateway_instance
                SET process_status = 'STOPPING', stopped_at = ?,
                    heartbeat_at = ?
                WHERE id = ?
                """,
                (utc_now(), utc_now(), instance_id),
            )

    def gateway_status(self) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            instances = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM gateway_instance
                    ORDER BY heartbeat_at DESC
                    """
                ).fetchall()
            ]
            query_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM query_run GROUP BY status
                    """
                ).fetchall()
            }
            audit_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM audit_item GROUP BY status
                    """
                ).fetchall()
            }
            notification_counts = (
                {
                    str(row["status"]): int(row["count"])
                    for row in connection.execute(
                        """
                        SELECT status, COUNT(*) AS count
                        FROM notification_outbox GROUP BY status
                        """
                    ).fetchall()
                }
                if self.schema_version()
                >= NOTIFICATION_SCHEMA_VERSION
                else {}
            )
        finally:
            connection.close()
        return {
            "instances": instances,
            "query_counts": query_counts,
            "audit_counts": audit_counts,
            "notification_counts": notification_counts,
            "maintenance_fences": self.active_maintenance_fences(),
        }

    def _enqueue_notification_in_connection(
        self,
        connection: sqlite3.Connection,
        notification: dict[str, Any],
        *,
        created_at: str,
    ) -> str:
        if not _table_exists(connection, "notification_outbox"):
            raise StateError(
                "notification outbox schema upgrade is required",
                error_code="STATE_SCHEMA_UPGRADE_REQUIRED",
            )
        dedupe_key = str(notification.get("dedupe_key") or "")
        event_type = str(notification.get("event_type") or "")
        severity = str(notification.get("severity") or "")
        subject_type = str(notification.get("subject_type") or "")
        subject_id = str(notification.get("subject_id") or "")
        payload = notification.get("payload")
        max_attempts = notification.get("max_attempts", 3)
        if (
            not dedupe_key
            or not event_type
            or severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
            or not subject_type
            or not subject_id
            or not isinstance(payload, dict)
            or isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 20
        ):
            raise StateError(
                "notification event is invalid",
                error_code="OPS_NOTIFICATION_INVALID",
            )
        notification_id = _deterministic_id(
            "notification",
            dedupe_key,
        )
        connection.execute(
            """
            INSERT INTO notification_outbox(
              id, dedupe_key, event_type, severity, subject_type,
              subject_id, payload_json, status, delivery_required,
              max_attempts, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (
                notification_id,
                dedupe_key,
                event_type,
                severity,
                subject_type,
                subject_id,
                _canonical_json(payload),
                int(bool(notification.get("delivery_required", False))),
                max_attempts,
                created_at,
                created_at,
                created_at,
            ),
        )
        return notification_id

    def enqueue_notification(
        self,
        notification: dict[str, Any],
    ) -> dict[str, Any]:
        self.require_schema_version(NOTIFICATION_SCHEMA_VERSION)
        now = utc_now()
        with self.metadata_transaction() as connection:
            notification_id = self._enqueue_notification_in_connection(
                connection,
                notification,
                created_at=now,
            )
        return self.notification_status(notification_id)

    def notification_status(
        self,
        notification_id: str,
    ) -> dict[str, Any]:
        self.require_schema_version(NOTIFICATION_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT * FROM notification_outbox WHERE id = ?
                """,
                (notification_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "notification does not exist",
                    error_code="OPS_NOTIFICATION_NOT_FOUND",
                )
            result = _public_notification_row(row)
            result["attempts"] = [
                dict(attempt)
                for attempt in connection.execute(
                    """
                    SELECT id, attempt_number, outcome, http_status_class,
                           error_code, started_at, finished_at
                    FROM notification_attempt
                    WHERE notification_id = ?
                    ORDER BY attempt_number
                    """,
                    (notification_id,),
                ).fetchall()
            ]
            return result
        finally:
            connection.close()

    def notification_by_dedupe(
        self,
        dedupe_key: str,
    ) -> dict[str, Any]:
        self.require_schema_version(NOTIFICATION_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT id FROM notification_outbox WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "notification does not exist",
                    error_code="OPS_NOTIFICATION_NOT_FOUND",
                )
            notification_id = str(row["id"])
        finally:
            connection.close()
        return self.notification_status(notification_id)

    def list_notifications(
        self,
        *,
        notification_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.require_schema_version(NOTIFICATION_SCHEMA_VERSION)
        if notification_id is not None:
            return [self.notification_status(notification_id)]
        connection = self.connect(read_only=True)
        try:
            return [
                _public_notification_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM notification_outbox
                    ORDER BY created_at DESC, id
                    """
                ).fetchall()
            ]
        finally:
            connection.close()

    def claim_due_notifications(
        self,
        *,
        worker_id: str,
        limit: int,
        claim_seconds: float = 30,
    ) -> list[dict[str, Any]]:
        self.require_schema_version(NOTIFICATION_SCHEMA_VERSION)
        if (
            not worker_id
            or not 1 <= limit <= 100
            or not 0.1 <= claim_seconds <= 300
        ):
            raise StateError(
                "notification claim is invalid",
                error_code="OPS_NOTIFICATION_CLAIM_INVALID",
            )
        now = utc_now()
        claimed: list[dict[str, Any]] = []
        with self.metadata_transaction() as connection:
            expired = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE status = 'DELIVERING'
                  AND claim_expires_at <= ?
                ORDER BY created_at, id
                """,
                (now,),
            ).fetchall()
            for row in expired:
                attempt_number = int(row["attempt_count"])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_attempt(
                      id, notification_id, attempt_number, outcome,
                      http_status_class, error_code, started_at, finished_at
                    ) VALUES (?, ?, ?, 'RETRYABLE', NULL,
                              'OPS_NOTIFICATION_CLAIM_EXPIRED', ?, ?)
                    """,
                    (
                        _deterministic_id(
                            "notification-attempt",
                            str(row["id"]),
                            str(attempt_number),
                        ),
                        str(row["id"]),
                        attempt_number,
                        str(row["updated_at"]),
                        now,
                    ),
                )
                terminal = attempt_number >= int(row["max_attempts"])
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET status = ?, claimed_by = NULL,
                        claim_expires_at = NULL, next_attempt_at = ?,
                        last_error_code = 'OPS_NOTIFICATION_CLAIM_EXPIRED',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        "FAILED" if terminal else "RETRY_WAIT",
                        now,
                        now,
                        str(row["id"]),
                    ),
                )
            due = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE status IN ('PENDING', 'RETRY_WAIT')
                  AND next_attempt_at <= ?
                ORDER BY delivery_required DESC, created_at, id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            for row in due:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'DELIVERING',
                        attempt_count = attempt_count + 1,
                        claimed_by = ?, claim_expires_at = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND status IN ('PENDING', 'RETRY_WAIT')
                    """,
                    (
                        worker_id,
                        _utc_after(claim_seconds),
                        now,
                        str(row["id"]),
                    ),
                )
                current = connection.execute(
                    "SELECT * FROM notification_outbox WHERE id = ?",
                    (str(row["id"]),),
                ).fetchone()
                item = dict(current)
                try:
                    payload = json.loads(str(item.pop("payload_json")))
                except json.JSONDecodeError as exc:
                    raise StateError(
                        "notification payload is invalid",
                        error_code="OPS_NOTIFICATION_PAYLOAD_INVALID",
                    ) from exc
                if not isinstance(payload, dict):
                    raise StateError(
                        "notification payload is invalid",
                        error_code="OPS_NOTIFICATION_PAYLOAD_INVALID",
                    )
                item["payload"] = payload
                claimed.append(item)
        return claimed

    def finish_notification_attempt(
        self,
        *,
        notification_id: str,
        worker_id: str,
        outcome: str,
        http_status_class: str | None,
        error_code: str | None,
        retry_after_seconds: float,
    ) -> dict[str, Any]:
        self.require_schema_version(NOTIFICATION_SCHEMA_VERSION)
        if outcome not in {"DELIVERED", "RETRYABLE", "TERMINAL"}:
            raise StateError(
                "notification outcome is invalid",
                error_code="OPS_NOTIFICATION_OUTCOME_INVALID",
            )
        now = utc_now()
        with self.metadata_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE id = ?",
                (notification_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "DELIVERING"
                or row["claimed_by"] != worker_id
            ):
                raise StateError(
                    "notification claim is no longer active",
                    error_code="OPS_NOTIFICATION_CLAIM_LOST",
                )
            attempt_number = int(row["attempt_count"])
            connection.execute(
                """
                INSERT INTO notification_attempt(
                  id, notification_id, attempt_number, outcome,
                  http_status_class, error_code, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _deterministic_id(
                        "notification-attempt",
                        notification_id,
                        str(attempt_number),
                    ),
                    notification_id,
                    attempt_number,
                    outcome,
                    http_status_class,
                    error_code,
                    str(row["updated_at"]),
                    now,
                ),
            )
            if outcome == "DELIVERED":
                status = "DELIVERED"
                delivered_at = now
                next_attempt_at = now
                last_error_code = None
            elif (
                outcome == "RETRYABLE"
                and attempt_number < int(row["max_attempts"])
            ):
                status = "RETRY_WAIT"
                delivered_at = None
                next_attempt_at = _utc_after(retry_after_seconds)
                last_error_code = error_code
            else:
                status = "FAILED"
                delivered_at = None
                next_attempt_at = now
                last_error_code = error_code
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = ?, claimed_by = NULL, claim_expires_at = NULL,
                    next_attempt_at = ?, last_error_code = ?,
                    updated_at = ?, delivered_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    next_attempt_at,
                    last_error_code,
                    now,
                    delivered_at,
                    notification_id,
                ),
            )
        return self.notification_status(notification_id)

    def retry_notification(
        self,
        notification_id: str,
        *,
        additional_attempts: int,
    ) -> dict[str, Any]:
        self.require_schema_version(NOTIFICATION_SCHEMA_VERSION)
        if not 1 <= additional_attempts <= 10:
            raise StateError(
                "notification retry limit is invalid",
                error_code="OPS_NOTIFICATION_RETRY_INVALID",
            )
        now = utc_now()
        with self.metadata_transaction() as connection:
            row = connection.execute(
                """
                SELECT attempt_count, max_attempts, status
                FROM notification_outbox WHERE id = ?
                """,
                (notification_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "notification does not exist",
                    error_code="OPS_NOTIFICATION_NOT_FOUND",
                )
            if row["status"] != "FAILED":
                raise StateError(
                    "only failed notifications may be retried",
                    error_code="OPS_NOTIFICATION_NOT_RETRYABLE",
                )
            new_max = min(
                20,
                max(
                    int(row["max_attempts"]),
                    int(row["attempt_count"]) + additional_attempts,
                ),
            )
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = 'PENDING', max_attempts = ?,
                    next_attempt_at = ?, claimed_by = NULL,
                    claim_expires_at = NULL, last_error_code = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_max, now, now, notification_id),
            )
        return self.notification_status(notification_id)

    def create_audit_item(
        self,
        *,
        audit_id: str,
        trigger_code: str,
        severity: str,
        subject_type: str,
        subject_id: str,
        evidence: dict[str, Any],
        source_lane: str = "query",
        notification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        now = utc_now()
        with self.metadata_transaction() as connection:
            self._insert_audit_item_in_connection(
                connection,
                audit_id=audit_id,
                trigger_code=trigger_code,
                severity=severity,
                subject_type=subject_type,
                subject_id=subject_id,
                evidence=evidence,
                source_lane=source_lane,
                notification=notification,
                created_at=now,
            )
        return self.audit_item(audit_id)

    def _insert_audit_item_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        audit_id: str,
        trigger_code: str,
        severity: str,
        subject_type: str,
        subject_id: str,
        evidence: dict[str, Any],
        source_lane: str,
        notification: dict[str, Any] | None,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_item(
              id, source_lane, trigger_code, severity, status,
              subject_type, subject_id, evidence_json,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                source_lane,
                trigger_code,
                severity,
                subject_type,
                subject_id,
                _canonical_json(evidence),
                created_at,
                created_at,
            ),
        )
        if notification is not None:
            self._enqueue_notification_in_connection(
                connection,
                notification,
                created_at=created_at,
            )
        connection.execute(
            """
            INSERT INTO audit_event(
              id, audit_item_id, action, actor, payload_json, created_at
            ) VALUES (?, ?, 'OPENED', 'system', '{}', ?)
            """,
            (
                _deterministic_id(
                    "audit-event",
                    audit_id,
                    "OPENED",
                    created_at,
                ),
                audit_id,
                created_at,
            ),
        )

    def list_audit_items(
        self,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            if status is None:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_item
                    ORDER BY created_at DESC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_item
                    WHERE status = ? ORDER BY created_at DESC
                    """,
                    (status,),
                ).fetchall()
            return [_public_audit_row(row) for row in rows]
        finally:
            connection.close()

    def audit_item(self, audit_id: str) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM audit_item WHERE id = ?",
                (audit_id,),
            ).fetchone()
            if row is None:
                raise StateError(
                    "audit item does not exist",
                    error_code="QUERY_AUDIT_NOT_FOUND",
                )
            result = _public_audit_row(row)
            result["events"] = [
                dict(event)
                for event in connection.execute(
                    """
                    SELECT id, action, actor, payload_json, created_at
                    FROM audit_event
                    WHERE audit_item_id = ?
                    ORDER BY created_at, id
                    """,
                    (audit_id,),
                ).fetchall()
            ]
            return result
        finally:
            connection.close()

    def resolve_audit_item(
        self,
        *,
        audit_id: str,
        actor: str,
        resolution: str,
        notification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.require_schema_version(QUERY_GOVERNANCE_SCHEMA_VERSION)
        if resolution not in {"RESOLVED", "REJECTED", "WAIVED"}:
            raise StateError(
                "audit resolution is invalid",
                error_code="QUERY_AUDIT_RESOLUTION_INVALID",
            )
        now = utc_now()
        delivery_schema = (
            self.schema_version() >= QUERY_DELIVERY_SCHEMA_VERSION
        )
        with self.business_transaction(allow_replacement=True) as connection:
            audit = connection.execute(
                """
                SELECT subject_type, subject_id, status
                FROM audit_item WHERE id = ?
                """,
                (audit_id,),
            ).fetchone()
            if (
                audit is None
                or audit["status"] not in {"OPEN", "IN_REVIEW"}
            ):
                raise StateError(
                    "audit item cannot be resolved",
                    error_code="QUERY_AUDIT_NOT_RESOLVABLE",
                )
            updated = connection.execute(
                """
                UPDATE audit_item
                SET status = ?, updated_at = ?
                WHERE id = ? AND status IN ('OPEN', 'IN_REVIEW')
                """,
                (resolution, now, audit_id),
            ).rowcount
            if updated != 1:
                raise StateError(
                    "audit item cannot be resolved",
                    error_code="QUERY_AUDIT_NOT_RESOLVABLE",
                )
            if (
                delivery_schema
                and audit["subject_type"] == "query_run"
            ):
                review_status = {
                    "RESOLVED": "approved",
                    "REJECTED": "rejected",
                    "WAIVED": "not_required",
                }[resolution]
                connection.execute(
                    """
                    UPDATE query_run
                    SET review_status = ?
                    WHERE id = ? AND generation_status = 'succeeded'
                    """,
                    (review_status, str(audit["subject_id"])),
                )
            connection.execute(
                """
                INSERT INTO audit_event(
                  id, audit_item_id, action, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, '{}', ?)
                """,
                (
                    _deterministic_id(
                        "audit-event",
                        audit_id,
                        resolution,
                        now,
                    ),
                    audit_id,
                    resolution,
                    actor,
                    now,
                ),
            )
            if notification is not None:
                self._enqueue_notification_in_connection(
                    connection,
                    notification,
                    created_at=now,
                )
        return self.audit_item(audit_id)

    def record_migration(
        self,
        *,
        migration_id: str,
        source_fingerprint: str,
        status: str,
        backup_manifest_path: str | None,
        imported_counts: dict[str, int],
    ) -> None:
        with self.metadata_transaction() as connection:
            connection.execute(
                """
                INSERT INTO migration_record(
                  id, source_fingerprint, status, run_origin,
                  verification_status, side_effects_executed, imported_at,
                  backup_manifest_path, imported_counts_json
                ) VALUES (?, ?, ?, 'LEGACY_MIGRATION', 'UNVERIFIED', 0, ?, ?, ?)
                ON CONFLICT(source_fingerprint) DO UPDATE SET
                  status = excluded.status,
                  backup_manifest_path = COALESCE(
                    excluded.backup_manifest_path,
                    migration_record.backup_manifest_path
                  ),
                  imported_counts_json = excluded.imported_counts_json
                """,
                (
                    migration_id,
                    source_fingerprint,
                    status,
                    utc_now(),
                    backup_manifest_path,
                    _canonical_json(imported_counts),
                ),
            )

    def migration_for_fingerprint(
        self,
        source_fingerprint: str,
    ) -> dict[str, Any] | None:
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT * FROM migration_record
                WHERE source_fingerprint = ?
                """,
                (source_fingerprint,),
            ).fetchone()
            return None if row is None else dict(row)
        finally:
            connection.close()

    def latest_migration(self) -> dict[str, Any] | None:
        connection = self.connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT * FROM migration_record
                ORDER BY rowid DESC LIMIT 1
                """
            ).fetchone()
            return None if row is None else dict(row)
        finally:
            connection.close()

    def inspect(self) -> dict[str, Any]:
        connection = self.connect(read_only=True)
        try:
            counts = {}
            for table in (
                "source_document",
                "source_revision",
                "lane_run",
                "lightrag_binding",
            ):
                counts[table] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
            blocked = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM lightrag_binding
                    WHERE action_gate = 'BLOCKED'
                    """
                ).fetchone()[0]
            )
            unknown = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM lightrag_binding
                    WHERE remote_status = 'UNKNOWN'
                    """
                ).fetchone()[0]
            )
            return {
                "backend": "sqlite",
                "database": self.database_relative,
                "schema_version": int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                ),
                "state_commit_seq": self.state_commit_seq(),
                "last_exported_state_commit_seq": self.last_exported_state_commit_seq(),
                "counts": counts,
                "blocked_binding_count": blocked,
                "unknown_binding_count": unknown,
            }
        finally:
            connection.close()

    def export_rows(self) -> dict[str, Any]:
        """Return the small fact projection used by compatibility exports."""
        connection = self.connect(read_only=True)
        try:
            connection.execute("BEGIN")
            state_commit_seq = int(
                connection.execute(
                    """
                    SELECT state_commit_seq FROM state_clock
                    WHERE singleton = 1
                    """
                ).fetchone()[0]
            )
            source_rows = connection.execute(
                """
                SELECT d.canonical_path, r.sha256, r.size_bytes, r.suffix, r.text_like
                FROM lane_run_revision lrr
                JOIN source_revision r ON r.id = lrr.revision_id
                JOIN source_document d ON d.id = r.source_id
                WHERE lrr.run_id = (
                  SELECT id FROM lane_run
                  WHERE status = 'SUCCEEDED'
                  ORDER BY rowid DESC
                  LIMIT 1
                )
                AND lrr.role = 'INPUT'
                ORDER BY d.canonical_path
                """
            ).fetchall()
            all_sources = [
                {
                    "path": row["canonical_path"],
                    "sha256": row["sha256"],
                    "size": int(row["size_bytes"]),
                    "suffix": row["suffix"],
                    "text_like": bool(row["text_like"]),
                }
                for row in source_rows
            ]
            lane_files = {}
            for lane in ("wiki", "lightrag"):
                rows = connection.execute(
                    """
                    SELECT d.canonical_path, r.sha256, r.size_bytes,
                           r.suffix, r.text_like
                    FROM lane_run lr
                    JOIN lane_run_revision lrr ON lrr.run_id = lr.id
                    JOIN source_revision r ON r.id = lrr.revision_id
                    JOIN source_document d ON d.id = r.source_id
                    WHERE lr.id = (
                      SELECT id FROM lane_run
                      WHERE lane = ? AND status = 'SUCCEEDED'
                      ORDER BY rowid DESC
                      LIMIT 1
                    )
                    AND lrr.role = 'INPUT'
                    ORDER BY d.canonical_path
                    """,
                    (lane,),
                ).fetchall()
                lane_files[lane] = [
                    {
                        "path": row["canonical_path"],
                        "sha256": row["sha256"],
                        "size": int(row["size_bytes"]),
                        "suffix": row["suffix"],
                        "text_like": bool(row["text_like"]),
                    }
                    for row in rows
                ]
            binding_rows = connection.execute(
                """
                SELECT b.id, b.file_source, b.track_id, b.remote_status,
                       b.action_gate, b.gate_reason, b.chunk_count,
                       b.submitted_at, b.processed_at, r.sha256
                FROM lightrag_binding b
                JOIN source_revision r ON r.id = b.revision_id
                ORDER BY b.file_source
                """
            ).fetchall()
            lightrag_documents = {}
            for row in binding_rows:
                doc_id = row["file_source"].replace("/", "__").replace(" ", "_")
                lightrag_documents[doc_id] = {
                    "binding_id": row["id"],
                    "source_path": row["file_source"],
                    "sha256": row["sha256"],
                    "service_track_id": row["track_id"],
                    "remote_status": row["remote_status"],
                    "action_gate": row["action_gate"],
                    "gate_reason": row["gate_reason"],
                    "chunk_count": row["chunk_count"],
                    "submitted_at": row["submitted_at"],
                    "processed_at": row["processed_at"],
                }
            migrations = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, source_fingerprint, status, imported_at
                    FROM migration_record ORDER BY imported_at
                    """
                ).fetchall()
            ]
            connection.commit()
            return {
                "state_commit_seq": state_commit_seq,
                "all_files": all_sources,
                "lane_files": lane_files,
                "lightrag_documents": lightrag_documents,
                "migrations": migrations,
            }
        finally:
            connection.close()


__all__ = [
    "DEFAULT_DATABASE",
    "StateStore",
    "_canonical_json",
    "_deterministic_id",
    "_fsync_directory",
    "_private_directory",
    "_private_file",
    "_sha256_json",
    "normalize_workspace_relative_path",
    "resolve_under_root",
]

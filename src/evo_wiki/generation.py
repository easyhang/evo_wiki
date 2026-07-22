from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .config import EvoConfig
from .corpus import scan_corpus
from .journal import (
    DEFAULT_MAX_BYTES_PER_FILE,
    DEFAULT_MAX_EVENTS_PER_FILE,
    RunJournalWriter,
)
from .lightrag_lane import (
    LightRAGBuildError,
    LightRAGServiceClient,
    detect_lightrag_deletions,
    preflight_lightrag_build,
    resolve_lightrag_service_config,
)
from .orchestration import execute_lanes
from .paths import ProjectPaths
from .platform_export import export_platform
from .query_gateway import (
    TrustedQueryGateway,
    gateway_settings,
)
from .state import (
    StateError,
    StateMigrator,
    StateSchemaMigrator,
    StateStore,
    StateVerifier,
)
from .state.operations import operation_lock
from .state.store import lightrag_backend_identity
from .state.schema import (
    INITIAL_SCHEMA_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION,
    migration_checksums,
)
from .utils import utc_now, write_json_atomic


def _state_config(config: EvoConfig) -> dict[str, Any]:
    raw = config.project.get("state")
    return dict(raw) if isinstance(raw, dict) else {}


def _state_backend(config: EvoConfig) -> str:
    backend = _state_config(config).get("backend", "legacy_json")
    if backend not in {"legacy_json", "sqlite"}:
        raise StateError(
            "state.backend must be legacy_json or sqlite",
            error_code="STATE_CONFIG_INVALID",
        )
    return str(backend)


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, StateError):
        return exc.error_code
    if isinstance(exc, LightRAGBuildError):
        return exc.failure_code or "GENERATION_REMOTE_FAILED"
    return "GENERATION_FAILED"


def _stub_pages(paths: ProjectPaths) -> list[str]:
    if not paths.wiki_src.is_dir():
        return ["artifacts/wiki/wiki-src/index.md"]
    found = []
    markers = (
        "本页是 Evo wiki 生成的占位页",
        "待 Claude Code",
        "Claude Code should replace this stub",
    )
    for source in sorted(paths.wiki_src.rglob("*.md")):
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(marker in text for marker in markers):
            found.append(source.relative_to(paths.root).as_posix())
    return found


class GenerationService:
    def __init__(
        self,
        paths: ProjectPaths,
        config: EvoConfig,
        *,
        target: str = "platform",
        smoke_query: str | None = None,
    ):
        self.paths = paths
        self.config = config
        self.target = target
        self.smoke_query = smoke_query
        self.generation_id = f"generate-{uuid.uuid4().hex}"

    def plan(self) -> dict[str, Any]:
        """Return a zero-workspace-write, zero-remote-write generation plan."""
        try:
            preflight = self._preflight(
                check_service=False,
                zero_write=True,
            )
        except StateError as exc:
            if exc.error_code == "GENERATION_RECONCILE_REQUIRED":
                return self._reconcile_blocked_report(
                    mode="dry_run",
                    details=exc.details,
                )
            raise
        state = self._state_plan()
        steps = [
            self._step("preflight", "ready"),
            self._step(
                "state_migration",
                (
                    "planned"
                    if state["migration_required"]
                    else "already_current"
                ),
            ),
            self._step("state_verify", "planned"),
            self._step("wiki", "planned"),
        ]
        if self.target == "platform":
            steps.extend(
                [
                    self._step("lightrag", "planned"),
                    self._step("query_gateway", "planned"),
                    self._step("platform_export", "planned"),
                ]
            )
        return {
            "schema_version": 1,
            "operation": "generate",
            "generation_id": self.generation_id,
            "status": "ready",
            "mode": "dry_run",
            "target": self.target,
            "workspace_mutated": False,
            "remote_mutated": False,
            "state": state,
            "preflight": preflight,
            "steps": steps,
            "artifacts": {},
            "error_code": None,
            "next_command": (
                "rerun without --dry-run to generate the platform"
                if self.target == "platform"
                else "rerun without --dry-run to generate the Wiki"
            ),
        }

    def generate(self) -> dict[str, Any]:
        """Apply safe local migrations and generate the selected target."""
        try:
            preflight = self._preflight(
                check_service=True,
                zero_write=False,
            )
        except Exception as exc:
            report = self._failure_report(exc, steps=[])
            self._write_report(report)
            if (
                isinstance(exc, StateError)
                and exc.error_code
                == "GENERATION_RECONCILE_REQUIRED"
            ):
                return report
            raise

        steps: list[dict[str, Any]] = [
            self._step("preflight", "succeeded"),
        ]
        lock_path = self.paths.state / "generate.lock"
        with operation_lock(lock_path):
            journal = self._journal()
            journal.append(
                event_type="generation.started",
                phase="start",
                status="RUNNING",
                lane="orchestration",
                safe_payload={
                    "command": "generate",
                    "target": self.target,
                    "profile": preflight["profile"],
                },
            )
            try:
                migration_summary = self._apply_state_migrations()
                steps.append(
                    self._step(
                        "state_migration",
                        migration_summary["status"],
                        {
                            "database_schema_before": (
                                migration_summary[
                                    "database_schema_before"
                                ]
                            ),
                            "database_schema_after": (
                                migration_summary[
                                    "database_schema_after"
                                ]
                            ),
                            "backup_id": migration_summary["backup_id"],
                        },
                    )
                )
                self.config = EvoConfig.load(self.paths.root)
                store = StateStore(
                    self.paths.root,
                    _state_config(self.config),
                )
                store.initialize()
                verification = StateVerifier(store).verify()
                if verification.overall_status == "FAIL":
                    raise StateError(
                        "SQLite state verification failed during generation",
                        error_code="GENERATION_STATE_VERIFY_FAILED",
                    )
                steps.append(
                    self._step(
                        "state_verify",
                        "succeeded",
                        {
                            "database_schema_version": (
                                verification.database_schema_version
                            ),
                            "overall_status": verification.overall_status,
                        },
                    )
                )

                lanes = (
                    ["wiki", "lightrag"]
                    if self.target == "platform"
                    else ["wiki"]
                )
                lane_result = execute_lanes(
                    self.paths,
                    self.config,
                    store=store,
                    lanes=lanes,
                    smoke_query=self.smoke_query,
                    command_name="generate",
                    reason=f"generate_{self.target}",
                    stop_on_lane_failure=True,
                )
                wiki_status = lane_result.payload["lane_status"]["wiki"]
                steps.append(self._step("wiki", wiki_status))
                if self.target == "platform":
                    lightrag_status = lane_result.payload["lane_status"][
                        "lightrag"
                    ]
                    remote_mutated = bool(
                        lane_result.lightrag_report
                        and lane_result.lightrag_report.get("imported")
                    )
                    steps.append(
                        self._step(
                            "lightrag",
                            lightrag_status,
                            {"remote_mutated": remote_mutated},
                        )
                    )
                if lane_result.exit_code == 3:
                    raise StateError(
                        "Wiki quality checks failed during generation",
                        error_code="GENERATION_WIKI_FAILED",
                    )
                if lane_result.exit_code != 0:
                    raise StateError(
                        "LightRAG generation failed",
                        error_code="GENERATION_REMOTE_FAILED",
                    )

                gateway_result = None
                platform_result = None
                if self.target == "platform":
                    if (
                        lane_result.lightrag_report
                        and lane_result.lightrag_report.get(
                            "requires_rebuild"
                        )
                    ):
                        raise StateError(
                            "LightRAG rebuild is required before platform export",
                            error_code="GENERATION_REBUILD_REQUIRED",
                        )
                    gateway_result = TrustedQueryGateway(
                        store,
                        self.config.project,
                    ).check()
                    steps.append(
                        self._step("query_gateway", "succeeded")
                    )
                    platform_result = export_platform(
                        self.paths,
                        self.config,
                    )
                    steps.append(
                        self._step("platform_export", "succeeded")
                    )

                remote_mutated = bool(
                    self.target == "platform"
                    and lane_result.lightrag_report
                    and lane_result.lightrag_report.get("imported")
                )
                report = {
                    "schema_version": 1,
                    "operation": "generate",
                    "generation_id": self.generation_id,
                    "generated_at": utc_now(),
                    "status": "success",
                    "mode": "apply",
                    "target": self.target,
                    "workspace_mutated": True,
                    "remote_mutated": remote_mutated,
                    "state": migration_summary,
                    "preflight": preflight,
                    "steps": steps,
                    "artifacts": {
                        "wiki": str(
                            self.paths.wiki_dist / "index.html"
                        ),
                        "platform": (
                            platform_result["path"]
                            if platform_result is not None
                            else None
                        ),
                        "report": str(self.paths.generation_report),
                    },
                    "gateway": (
                        {
                            "status": gateway_result["status"],
                            "mode": gateway_result["mode"],
                        }
                        if gateway_result is not None
                        else None
                    ),
                    "error_code": None,
                    "next_command": (
                        f"evo-wiki serve --root {self.paths.root}"
                        if self.target == "platform"
                        else "open artifacts/wiki/dist/index.html"
                    ),
                }
                self._write_report(report)
                journal.append(
                    event_type="generation.completed",
                    phase="finish",
                    status="SUCCEEDED",
                    lane="orchestration",
                    safe_payload={
                        "command": "generate",
                        "target": self.target,
                        "database_schema_version": SCHEMA_VERSION,
                        "remote_mutated": remote_mutated,
                    },
                )
                return report
            except Exception as exc:
                error_code = _safe_error_code(exc)
                report = self._failure_report(exc, steps=steps)
                self._write_report(report)
                journal.append(
                    event_type="generation.failed",
                    phase="finish",
                    status="FAILED",
                    lane="orchestration",
                    safe_payload={
                        "command": "generate",
                        "target": self.target,
                        "error_code": error_code,
                    },
                )
                raise

    def _preflight(
        self,
        *,
        check_service: bool,
        zero_write: bool,
    ) -> dict[str, Any]:
        if not (self.paths.root / "project.json").is_file() or not (
            self.paths.root / "wiki.json"
        ).is_file():
            raise StateError(
                "workspace is not initialized; run evo-wiki init first",
                error_code="GENERATION_NOT_INITIALIZED",
            )
        presentation = self.config.validate(
            self.paths.root,
            target=self.target,
        )
        files = scan_corpus(self.paths.root, self.paths.corpus)
        if not files:
            raise StateError(
                "corpus is empty; add source files before generation",
                error_code="GENERATION_CORPUS_EMPTY",
            )
        if not (self.paths.wiki_src / "index.md").is_file():
            raise StateError(
                "wiki-src/index.md is missing",
                error_code="GENERATION_WIKI_SOURCE_MISSING",
            )
        stubs = _stub_pages(self.paths)
        if self.target == "platform" and stubs:
            raise StateError(
                "Wiki content still contains generated stubs",
                error_code="GENERATION_WIKI_STUB",
                details={"stub_page_count": len(stubs)},
            )

        remote_status = "not_requested"
        gateway_mode = "disabled"
        deleted_count = 0
        if self.target == "platform":
            service = resolve_lightrag_service_config(
                self.config.project.get("lightrag", {})
            )
            settings = gateway_settings(self.config.project)
            if settings.mode == "disabled":
                raise StateError(
                    "query gateway is disabled",
                    error_code="QUERY_GATEWAY_DISABLED",
                )
            audit_key = os.environ.get(settings.audit_hmac_key_env)
            if not audit_key or len(audit_key.encode("utf-8")) < 16:
                raise StateError(
                    "query audit HMAC key is missing or too short",
                    error_code="QUERY_AUDIT_KEY_MISSING",
                )
            gateway_mode = settings.mode

            store = None
            if (
                _state_backend(self.config) == "sqlite"
                and not zero_write
            ):
                candidate = StateStore(
                    self.paths.root,
                    _state_config(self.config),
                )
                if candidate.exists:
                    store = candidate
            deleted = detect_lightrag_deletions(
                self.paths,
                files,
                state_store=store,
            )
            deleted_count = len(deleted)
            if deleted:
                raise StateError(
                    "corpus deletions require an explicit LightRAG rebuild",
                    error_code="GENERATION_REBUILD_REQUIRED",
                    details={"deleted_document_count": len(deleted)},
                )
            blocked_binding_count = (
                self._immutable_blocked_current_bindings(files)
                if _state_backend(self.config) == "sqlite"
                else 0
            )
            if blocked_binding_count:
                raise StateError(
                    "current LightRAG bindings require reconciliation",
                    error_code="GENERATION_RECONCILE_REQUIRED",
                    details={
                        "blocked_binding_count": blocked_binding_count,
                        "recovery_commands": self._reconcile_commands(),
                    },
                )
            if check_service:
                client = LightRAGServiceClient(
                    service["base_url"],
                    headers=service["headers"],
                    timeout=min(
                        float(service["timeout_seconds"]),
                        5.0,
                    ),
                )
                preflight_lightrag_build(client, service)
                remote_status = "ready"
            else:
                remote_status = "configured"

        return {
            "profile": presentation["profile"],
            "corpus_file_count": len(files),
            "stub_page_count": len(stubs),
            "deleted_document_count": deleted_count,
            "lightrag": remote_status,
            "query_gateway_mode": gateway_mode,
        }

    def _immutable_blocked_current_bindings(
        self,
        files: list[Any],
    ) -> int:
        """Count current blocked bindings without WAL/SHM or raw-row output."""
        store = StateStore(
            self.paths.root,
            _state_config(self.config),
        )
        if not store.database_path.is_file():
            return 0
        workspace, fingerprint = lightrag_backend_identity(
            self.config.project.get("lightrag", {})
        )
        current = {
            (str(item.path), str(item.sha256))
            for item in files
        }
        connection = sqlite3.connect(
            f"{store.database_path.as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT d.canonical_path, r.sha256,
                       b.remote_status, b.action_gate
                FROM lightrag_binding b
                JOIN source_revision r ON r.id = b.revision_id
                JOIN source_document d ON d.id = r.source_id
                JOIN retrieval_partition p
                  ON p.id = b.retrieval_partition_id
                WHERE b.backend_fingerprint = ?
                  AND p.namespace = ?
                """,
                (fingerprint, workspace),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateError(
                "SQLite binding readiness is unavailable",
                error_code="STATE_SCHEMA_INVALID",
            ) from exc
        finally:
            connection.close()
        return sum(
            1
            for row in rows
            if (
                (str(row["canonical_path"]), str(row["sha256"]))
                in current
                and (
                    row["remote_status"] == "UNKNOWN"
                    or row["action_gate"] == "BLOCKED"
                )
            )
        )

    @staticmethod
    def _reconcile_commands() -> dict[str, str]:
        return {
            "review": (
                "evo-wiki state reconcile --root <workspace> --json"
            ),
            "apply": (
                "evo-wiki state reconcile --root <workspace> "
                "--apply --json"
            ),
            "retry": (
                "evo-wiki generate --root <workspace> "
                "--target platform --dry-run --json"
            ),
        }

    def _reconcile_blocked_report(
        self,
        *,
        mode: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        commands = details.get("recovery_commands")
        if not isinstance(commands, dict):
            commands = self._reconcile_commands()
        blocked_count = int(details.get("blocked_binding_count", 0))
        return {
            "schema_version": 1,
            "operation": "generate",
            "generation_id": self.generation_id,
            "status": "blocked",
            "mode": mode,
            "target": self.target,
            "workspace_mutated": False,
            "remote_mutated": False,
            "blocked_binding_count": blocked_count,
            "steps": [
                self._step(
                    "preflight",
                    "blocked",
                    {"blocked_binding_count": blocked_count},
                )
            ],
            "artifacts": {},
            "error_code": "GENERATION_RECONCILE_REQUIRED",
            "recovery_commands": commands,
            "next_command": commands["review"],
        }

    def _state_plan(self) -> dict[str, Any]:
        backend = _state_backend(self.config)
        database_schema_before = None
        pending: list[str] = []
        if backend == "sqlite":
            store = StateStore(
                self.paths.root,
                _state_config(self.config),
            )
            (
                database_schema_before,
                pending,
            ) = self._immutable_schema_plan(store)
        else:
            migrator = StateMigrator(
                self.paths.root,
                _state_config(self.config),
                lightrag_config=self.config.project.get("lightrag", {}),
                journal_config=self.config.project.get("journal", {}),
            )
            cutover = migrator.dry_run()
            if cutover.status == "failed" or not cutover.migratable:
                raise StateError(
                    "workspace state cannot be migrated safely",
                    error_code=(
                        cutover.error_code
                        or "GENERATION_STATE_MIGRATION_FAILED"
                    ),
                )
        return {
            "backend_before": backend,
            "backend_after": "sqlite",
            "database_schema_before": database_schema_before,
            "database_schema_after": SCHEMA_VERSION,
            "migration_required": (
                backend != "sqlite" or bool(pending)
            ),
            "pending_schema_migrations": pending,
            "backup_required": (
                backend != "sqlite" or bool(pending)
            ),
        }

    @staticmethod
    def _immutable_schema_plan(
        store: StateStore,
    ) -> tuple[int, list[str]]:
        """Inspect schema metadata without creating WAL/SHM sidecars."""
        if not store.database_path.is_file():
            raise StateError(
                "SQLite state database is missing",
                error_code="STATE_DATABASE_MISSING",
            )
        connection = sqlite3.connect(
            f"{store.database_path.as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT version, name, checksum
                FROM schema_meta ORDER BY version
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateError(
                "SQLite state schema metadata is unavailable",
                error_code="STATE_SCHEMA_INVALID",
            ) from exc
        finally:
            connection.close()
        checksums = migration_checksums()
        expected = {
            migration.version: (
                migration.name,
                checksums[migration.version],
            )
            for migration in MIGRATIONS
            if migration.version <= version
        }
        observed = {
            int(row["version"]): (
                str(row["name"]),
                str(row["checksum"]),
            )
            for row in rows
        }
        if (
            version < INITIAL_SCHEMA_VERSION
            or version > SCHEMA_VERSION
            or observed != expected
        ):
            raise StateError(
                "SQLite state schema is unsupported or modified",
                error_code="STATE_SCHEMA_UNSUPPORTED",
            )
        pending = [
            migration.name
            for migration in MIGRATIONS
            if migration.version > version
        ]
        return version, pending

    def _apply_state_migrations(self) -> dict[str, Any]:
        state_before = self._state_plan()
        migrator = StateMigrator(
            self.paths.root,
            _state_config(self.config),
            lightrag_config=self.config.project.get("lightrag", {}),
            journal_config=self.config.project.get("journal", {}),
        )
        cutover = migrator.apply()
        self.config = EvoConfig.load(self.paths.root)
        store = StateStore(
            self.paths.root,
            _state_config(self.config),
        )
        store.initialize()
        schema = StateSchemaMigrator(
            store,
            self.config.project.get("journal", {}),
        ).apply()
        status = (
            "applied"
            if cutover.workspace_mutated or schema.workspace_mutated
            else "already_current"
        )
        return {
            "status": status,
            "backend_before": state_before["backend_before"],
            "backend_after": "sqlite",
            "database_schema_before": state_before[
                "database_schema_before"
            ],
            "database_schema_after": store.schema_version(),
            "backup_id": schema.backup_id,
            "legacy_backup_created": (
                cutover.status == "applied"
                and state_before["backend_before"] == "legacy_json"
            ),
        }

    def _journal(self) -> RunJournalWriter:
        raw = self.config.project.get("journal", {})
        config = raw if isinstance(raw, dict) else {}
        return RunJournalWriter(
            self.paths.artifacts / "logs",
            run_id=self.generation_id,
            max_events_per_file=config.get(
                "max_events_per_file",
                DEFAULT_MAX_EVENTS_PER_FILE,
            ),
            max_bytes_per_file=config.get(
                "max_bytes_per_file",
                DEFAULT_MAX_BYTES_PER_FILE,
            ),
        )

    def _failure_report(
        self,
        exc: Exception,
        *,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        error_code = _safe_error_code(exc)
        if error_code == "GENERATION_RECONCILE_REQUIRED":
            details = exc.details if isinstance(exc, StateError) else {}
            return self._reconcile_blocked_report(
                mode="apply",
                details=details,
            )
        return {
            "schema_version": 1,
            "operation": "generate",
            "generation_id": self.generation_id,
            "generated_at": utc_now(),
            "status": "failed",
            "mode": "apply",
            "target": self.target,
            "workspace_mutated": bool(steps),
            "remote_mutated": any(
                step["name"] == "lightrag"
                and step.get("remote_mutated") is True
                for step in steps
            ),
            "steps": steps,
            "artifacts": {
                "report": str(self.paths.generation_report),
            },
            "error_code": error_code,
            "next_command": "fix the reported error and rerun generate",
        }

    def _write_report(self, report: dict[str, Any]) -> None:
        write_json_atomic(self.paths.generation_report, report)

    @staticmethod
    def _step(
        name: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "status": status,
            **(details or {}),
        }

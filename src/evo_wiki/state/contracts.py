from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RemoteStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    MISSING = "MISSING"


class ActionGate(str, Enum):
    OPEN = "OPEN"
    BLOCKED = "BLOCKED"


class StateError(RuntimeError):
    """State failure with a stable machine-readable code."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        committed: bool = False,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.committed = committed
        self.details = details or {}


class MigrationResult(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["ready", "applied", "already_applied", "failed"]
    mode: Literal["dry_run", "apply", "abort_candidate"]
    workspace_mutated: bool
    migratable: bool
    legacy_input_fingerprint: str
    database: str | None = None
    state_commit_seq: int | None = None
    imported_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str | None = None


class SchemaMigrationResult(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["ready", "applied", "already_applied", "failed"]
    mode: Literal["dry_run", "apply"]
    workspace_mutated: bool
    database_schema_version: int
    target_database_schema_version: int
    pending_migrations: list[str] = Field(default_factory=list)
    backup_id: str | None = None
    backup_sha256: str | None = None
    state_commit_seq: int | None = None
    error_code: str | None = None


class VerificationCheck(StrictModel):
    name: str
    status: Literal["PASS", "WARN", "FAIL"]
    code: str
    detail: str | None = None


class VerificationResult(StrictModel):
    schema_version: Literal[1] = 1
    overall_status: Literal["PASS", "WARN", "FAIL"]
    database: str
    database_schema_version: int | None
    state_commit_seq: int | None
    last_exported_state_commit_seq: int | None
    checks: list[VerificationCheck]


class BackupResult(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["success", "failed"]
    backup_id: str
    backup_path: str | None = None
    created_at: str
    database_schema_version: int | None = None
    state_commit_seq: int | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    verification_status: Literal["PASS", "WARN", "FAIL"] | None = None
    error_code: str | None = None


class ExportResult(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["success", "failed"]
    state_committed: bool
    state_commit_seq: int
    export_succeeded: bool
    exported_paths: list[str] = Field(default_factory=list)
    error_code: str | None = None


class ReconcileObservation(StrictModel):
    binding_id: str
    track_id: str | None
    before_remote_status: RemoteStatus
    observed_remote_status: RemoteStatus
    before_action_gate: ActionGate
    resulting_action_gate: ActionGate
    gate_reason: str | None = None
    total_chunks: int | None = None
    error_code: str | None = None


class ReconcileResult(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["ready", "applied", "failed"]
    mode: Literal["dry_run", "apply"]
    workspace_mutated: bool
    observations: list[ReconcileObservation]
    error_code: str | None = None


class ReplacementRemoteDocument(StrictModel):
    doc_id: str
    file_path: str
    status: str
    track_id: str | None = None
    chunks_count: int | None = None


class ReplacementImpact(StrictModel):
    chunk_count: int | None = None
    graph_cleanup_scope: Literal["service_managed_unknown"] = (
        "service_managed_unknown"
    )
    query_availability_gap: Literal[True] = True


class ReplacementRollback(StrictModel):
    required: Literal[True] = True
    owner_binding_id: str | None = None
    owner_revision_id: str | None = None
    snapshot_status: str | None = None
    available: bool


class ReplacementEffectEnvelope(StrictModel):
    max_delete_requests: Literal[2] = 2
    max_submission_requests: Literal[2] = 2
    auto_compensation_planned: Literal[True] = True


class ReplacementPlan(StrictModel):
    plan_id: str
    plan_digest: str
    binding_id: str
    source_path: str
    target_revision_id: str
    target_sha256: str
    review_status: Literal["ready", "blocked"]
    blockers: list[str] = Field(default_factory=list)
    remote_document: ReplacementRemoteDocument | None = None
    impact: ReplacementImpact
    rollback: ReplacementRollback
    required_approvals: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    effect_envelope: ReplacementEffectEnvelope = Field(
        default_factory=ReplacementEffectEnvelope
    )
    execution_authorized: Literal[False] = False


class ReplacementPlanResult(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["ready", "blocked", "no_conflicts", "failed"]
    mode: Literal["dry_run"] = "dry_run"
    workspace_mutated: Literal[False] = False
    delete_attempted: Literal[False] = False
    plans: list[ReplacementPlan] = Field(default_factory=list)
    error_code: str | None = None


class ReplacementBackupSummary(StrictModel):
    backup_id: str
    backup_sha256: str
    state_commit_seq: int


class ReplacementMaintenance(StrictModel):
    active: bool
    started_at: str | None = None
    elapsed_seconds: float | None = None
    limit_seconds: float


class ReplacementOperationSummary(StrictModel):
    operation_id: str
    plan_id: str
    plan_digest: str
    source_path: str
    phase: str
    status: str
    effect_certainty: Literal["NONE", "KNOWN", "UNKNOWN"]
    delete_attempts: int
    submit_attempts: int
    backup: ReplacementBackupSummary
    maintenance: ReplacementMaintenance
    compensation_active: bool
    next_action: str | None = None
    error_code: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class ReplacementExecutionResult(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal[
        "completed",
        "rolled_back",
        "blocked",
        "needs_audit",
        "failed",
    ]
    mode: Literal["apply"] = "apply"
    workspace_mutated: bool
    delete_attempted: bool
    operation: ReplacementOperationSummary
    state_commit_seq: int
    error_code: str | None = None


class ReplacementStatusResult(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal[
        "ready",
        "in_progress",
        "completed",
        "rolled_back",
        "blocked",
        "needs_audit",
        "failed",
        "no_operations",
    ]
    mode: Literal["read_only"] = "read_only"
    workspace_mutated: Literal[False] = False
    delete_attempted: Literal[False] = False
    operations: list[ReplacementOperationSummary] = Field(
        default_factory=list
    )
    database_schema_version: int
    error_code: str | None = None

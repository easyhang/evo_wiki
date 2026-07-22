from __future__ import annotations

import hashlib
from dataclasses import dataclass


INITIAL_SCHEMA_VERSION = 1
REPLACEMENT_SCHEMA_VERSION = 2
QUERY_GOVERNANCE_SCHEMA_VERSION = 3
NOTIFICATION_SCHEMA_VERSION = 4
QUERY_DELIVERY_SCHEMA_VERSION = 5
SCHEMA_VERSION = QUERY_DELIVERY_SCHEMA_VERSION
MIGRATION_NAME = "0001_state_core"

MIGRATION_SQL = r"""
CREATE TABLE schema_meta (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL,
  application_version TEXT NOT NULL
);

CREATE TABLE state_clock (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  state_commit_seq INTEGER NOT NULL CHECK (state_commit_seq >= 0),
  updated_at TEXT NOT NULL
);

CREATE TABLE migration_record (
  id TEXT PRIMARY KEY,
  source_fingerprint TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (
    status IN ('CANDIDATE_VERIFIED', 'DB_INSTALLED_CONFIG_LEGACY', 'SQLITE_ACTIVE')
  ),
  run_origin TEXT NOT NULL CHECK (run_origin = 'LEGACY_MIGRATION'),
  verification_status TEXT NOT NULL CHECK (verification_status = 'UNVERIFIED'),
  side_effects_executed INTEGER NOT NULL CHECK (side_effects_executed = 0),
  imported_at TEXT NOT NULL,
  legacy_observed_at TEXT,
  backup_manifest_path TEXT,
  imported_counts_json TEXT NOT NULL
);

CREATE TABLE compatibility_export (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  last_exported_state_commit_seq INTEGER,
  generated_at TEXT,
  export_manifest_sha256 TEXT
);

CREATE TABLE security_domain (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  classification TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED')),
  created_at TEXT NOT NULL
);

CREATE TABLE retrieval_partition (
  id TEXT PRIMARY KEY,
  security_domain_id TEXT NOT NULL REFERENCES security_domain(id),
  backend_kind TEXT NOT NULL,
  backend_alias TEXT NOT NULL,
  namespace TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED')),
  config_json TEXT NOT NULL,
  backend_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (backend_alias, namespace, backend_fingerprint)
);

CREATE TABLE source_document (
  id TEXT PRIMARY KEY,
  logical_key TEXT NOT NULL,
  canonical_path TEXT NOT NULL,
  security_domain_id TEXT NOT NULL REFERENCES security_domain(id),
  media_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  retired_at TEXT,
  UNIQUE (security_domain_id, logical_key)
);

CREATE TABLE source_revision (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES source_document(id),
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
  suffix TEXT NOT NULL,
  text_like INTEGER NOT NULL CHECK (text_like IN (0, 1)),
  snapshot_path TEXT,
  snapshot_status TEXT NOT NULL CHECK (
    snapshot_status IN ('AVAILABLE', 'UNAVAILABLE_LEGACY')
  ),
  status TEXT NOT NULL CHECK (
    status IN ('STAGED', 'ACTIVE', 'SUPERSEDED', 'REJECTED', 'DELETED')
  ),
  provenance TEXT NOT NULL CHECK (
    provenance IN ('native', 'legacy_unverified')
  ),
  created_at TEXT NOT NULL,
  CHECK (
    (snapshot_status = 'AVAILABLE' AND snapshot_path IS NOT NULL) OR
    (snapshot_status = 'UNAVAILABLE_LEGACY' AND snapshot_path IS NULL)
  ),
  UNIQUE (source_id, sha256)
);

CREATE TABLE lane_run (
  id TEXT PRIMARY KEY,
  journal_run_id TEXT,
  lane TEXT NOT NULL,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (
    status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED',
               'NEEDS_AUDIT', 'CANCELLED')
  ),
  run_origin TEXT NOT NULL CHECK (
    run_origin IN ('NATIVE', 'LEGACY_MIGRATION')
  ),
  verification_status TEXT NOT NULL CHECK (
    verification_status IN ('VERIFIED', 'UNVERIFIED')
  ),
  side_effects_executed INTEGER NOT NULL CHECK (side_effects_executed IN (0, 1)),
  imported_at TEXT,
  legacy_observed_at TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);

CREATE TABLE lane_run_revision (
  run_id TEXT NOT NULL REFERENCES lane_run(id),
  revision_id TEXT NOT NULL REFERENCES source_revision(id),
  role TEXT NOT NULL CHECK (role IN ('INPUT', 'TARGET', 'OUTPUT')),
  PRIMARY KEY (run_id, revision_id, role)
);

CREATE TABLE lightrag_binding (
  id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL REFERENCES source_revision(id),
  retrieval_partition_id TEXT NOT NULL REFERENCES retrieval_partition(id),
  backend_fingerprint TEXT NOT NULL,
  file_source TEXT NOT NULL,
  remote_doc_id TEXT,
  track_id TEXT,
  remote_status TEXT NOT NULL CHECK (
    remote_status IN ('UNKNOWN', 'PENDING', 'PROCESSING',
                      'PROCESSED', 'FAILED', 'MISSING')
  ),
  action_gate TEXT NOT NULL CHECK (action_gate IN ('OPEN', 'BLOCKED')),
  gate_reason TEXT,
  chunk_count INTEGER CHECK (chunk_count IS NULL OR chunk_count >= 0),
  submitted_at TEXT,
  processed_at TEXT,
  last_observed_at TEXT,
  error_code TEXT,
  CHECK (
    (action_gate = 'OPEN' AND remote_status = 'PROCESSED' AND gate_reason IS NULL)
    OR
    (action_gate = 'BLOCKED' AND gate_reason IS NOT NULL)
  ),
  UNIQUE (revision_id, retrieval_partition_id, backend_fingerprint)
);

CREATE INDEX idx_source_revision_source
  ON source_revision(source_id, created_at);
CREATE INDEX idx_lane_run_latest
  ON lane_run(lane, status, finished_at);
CREATE INDEX idx_lane_run_revision_revision
  ON lane_run_revision(revision_id, run_id);
CREATE INDEX idx_lightrag_binding_status
  ON lightrag_binding(remote_status, action_gate);
CREATE INDEX idx_lightrag_binding_track
  ON lightrag_binding(track_id);

INSERT INTO state_clock(singleton, state_commit_seq, updated_at)
VALUES (1, 0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

INSERT INTO compatibility_export(singleton, last_exported_state_commit_seq)
VALUES (1, NULL);
"""


MIGRATION_0002_NAME = "0002_replacement_operation"
MIGRATION_0002_SQL = r"""
CREATE TABLE replacement_operation (
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  plan_digest TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES source_document(id),
  source_path TEXT NOT NULL,
  target_revision_id TEXT NOT NULL REFERENCES source_revision(id),
  target_binding_id TEXT NOT NULL REFERENCES lightrag_binding(id),
  owner_revision_id TEXT NOT NULL REFERENCES source_revision(id),
  owner_binding_id TEXT NOT NULL REFERENCES lightrag_binding(id),
  retrieval_partition_id TEXT NOT NULL REFERENCES retrieval_partition(id),
  backend_fingerprint TEXT NOT NULL,
  owner_remote_doc_id TEXT NOT NULL,
  owner_remote_track_id TEXT NOT NULL,
  target_remote_doc_id TEXT,
  target_remote_track_id TEXT,
  phase TEXT NOT NULL CHECK (
    phase IN (
      'PREPARED',
      'DELETE_INTENT',
      'DELETE_ACCEPTED',
      'DELETE_CONFIRMED',
      'SUBMIT_INTENT',
      'SUBMIT_ACCEPTED',
      'TARGET_PROCESSED',
      'VALIDATED',
      'COMPENSATION_REQUIRED',
      'TARGET_DELETE_INTENT',
      'TARGET_DELETE_ACCEPTED',
      'TARGET_DELETE_CONFIRMED',
      'OWNER_SUBMIT_INTENT',
      'OWNER_SUBMIT_ACCEPTED',
      'OWNER_PROCESSED',
      'COMPLETED',
      'ROLLED_BACK',
      'NEEDS_AUDIT',
      'FAILED'
    )
  ),
  status TEXT NOT NULL CHECK (
    status IN (
      'IN_PROGRESS', 'COMPLETED', 'ROLLED_BACK',
      'BLOCKED', 'NEEDS_AUDIT', 'FAILED'
    )
  ),
  effect_certainty TEXT NOT NULL CHECK (
    effect_certainty IN ('NONE', 'KNOWN', 'UNKNOWN')
  ),
  last_effect TEXT,
  backup_id TEXT NOT NULL,
  backup_path TEXT NOT NULL,
  backup_sha256 TEXT NOT NULL,
  backup_state_commit_seq INTEGER NOT NULL CHECK (
    backup_state_commit_seq >= 0
  ),
  prepared_state_commit_seq INTEGER NOT NULL CHECK (
    prepared_state_commit_seq >= 0
  ),
  maintenance_window_seconds REAL NOT NULL CHECK (
    maintenance_window_seconds >= 10
    AND maintenance_window_seconds <= 86400
  ),
  absence_confirmations INTEGER NOT NULL CHECK (
    absence_confirmations >= 1 AND absence_confirmations <= 10
  ),
  auto_compensate INTEGER NOT NULL CHECK (auto_compensate IN (0, 1)),
  smoke_query_sha256 TEXT NOT NULL,
  delete_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
    delete_attempts >= 0 AND delete_attempts <= 2
  ),
  submit_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
    submit_attempts >= 0 AND submit_attempts <= 2
  ),
  confirmed_by TEXT NOT NULL,
  confirmed_host TEXT NOT NULL,
  maintenance_started_at TEXT,
  error_code TEXT,
  next_action TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE UNIQUE INDEX idx_replacement_operation_active_source
  ON replacement_operation(source_id, retrieval_partition_id)
  WHERE status IN ('IN_PROGRESS', 'BLOCKED', 'NEEDS_AUDIT');

CREATE INDEX idx_replacement_operation_status
  ON replacement_operation(status, updated_at);

CREATE UNIQUE INDEX idx_source_revision_one_active
  ON source_revision(source_id)
  WHERE status = 'ACTIVE';
"""

MIGRATION_0003_NAME = "0003_query_governance"
MIGRATION_0003_SQL = r"""
CREATE TABLE query_run (
  id TEXT PRIMARY KEY,
  retrieval_partition_id TEXT NOT NULL REFERENCES retrieval_partition(id),
  principal_hmac TEXT NOT NULL,
  query_hmac TEXT NOT NULL,
  request_mode TEXT NOT NULL,
  gateway_mode TEXT NOT NULL CHECK (
    gateway_mode IN ('shadow', 'enforce')
  ),
  status TEXT NOT NULL CHECK (
    status IN (
      'RETRIEVING', 'ANSWERED', 'REFUSED',
      'NEEDS_AUDIT', 'FAILED', 'ABANDONED'
    )
  ),
  verification_level TEXT NOT NULL,
  verdict_code TEXT,
  error_code TEXT,
  reference_count INTEGER NOT NULL DEFAULT 0 CHECK (
    reference_count >= 0
  ),
  active_reference_count INTEGER NOT NULL DEFAULT 0 CHECK (
    active_reference_count >= 0
  ),
  answer_sha256 TEXT,
  citation_set_sha256 TEXT,
  lease_expires_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE INDEX idx_query_run_active_partition
  ON query_run(retrieval_partition_id, status, lease_expires_at);

CREATE INDEX idx_query_run_created
  ON query_run(created_at);

CREATE TABLE maintenance_fence (
  id TEXT PRIMARY KEY,
  retrieval_partition_id TEXT NOT NULL REFERENCES retrieval_partition(id),
  replacement_operation_id TEXT REFERENCES replacement_operation(id),
  reason_code TEXT NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN ('DRAINING', 'ACTIVE', 'FAILED', 'CLOSED')
  ),
  pause_started_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE UNIQUE INDEX idx_maintenance_fence_active_partition
  ON maintenance_fence(retrieval_partition_id)
  WHERE state IN ('DRAINING', 'ACTIVE', 'FAILED');

CREATE INDEX idx_maintenance_fence_operation
  ON maintenance_fence(replacement_operation_id, state);

CREATE TABLE gateway_instance (
  id TEXT PRIMARY KEY,
  retrieval_partition_id TEXT NOT NULL REFERENCES retrieval_partition(id),
  gateway_mode TEXT NOT NULL CHECK (
    gateway_mode IN ('shadow', 'enforce')
  ),
  process_status TEXT NOT NULL CHECK (
    process_status IN ('STARTING', 'READY', 'STOPPING', 'FAILED')
  ),
  version TEXT NOT NULL,
  started_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  stopped_at TEXT
);

CREATE INDEX idx_gateway_instance_partition_heartbeat
  ON gateway_instance(retrieval_partition_id, heartbeat_at);

CREATE TABLE audit_item (
  id TEXT PRIMARY KEY,
  source_lane TEXT NOT NULL CHECK (source_lane IN ('query', 'operations')),
  trigger_code TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (
    severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')
  ),
  status TEXT NOT NULL CHECK (
    status IN ('OPEN', 'IN_REVIEW', 'RESOLVED', 'REJECTED', 'WAIVED')
  ),
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  assignee TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_audit_item_queue
  ON audit_item(status, severity, created_at);

CREATE TABLE audit_event (
  id TEXT PRIMARY KEY,
  audit_item_id TEXT NOT NULL REFERENCES audit_item(id),
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_audit_event_item
  ON audit_event(audit_item_id, created_at);
"""

MIGRATION_0004_NAME = "0004_notification_outbox"
MIGRATION_0004_SQL = r"""
CREATE TABLE notification_outbox (
  id TEXT PRIMARY KEY,
  dedupe_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (
    severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')
  ),
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN (
      'PENDING', 'DELIVERING', 'RETRY_WAIT', 'DELIVERED', 'FAILED'
    )
  ),
  delivery_required INTEGER NOT NULL CHECK (
    delivery_required IN (0, 1)
  ),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
    attempt_count >= 0
  ),
  max_attempts INTEGER NOT NULL CHECK (
    max_attempts >= 1 AND max_attempts <= 20
  ),
  claimed_by TEXT,
  claim_expires_at TEXT,
  next_attempt_at TEXT NOT NULL,
  last_error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  delivered_at TEXT
);

CREATE INDEX idx_notification_outbox_due
  ON notification_outbox(status, next_attempt_at, created_at);

CREATE INDEX idx_notification_outbox_subject
  ON notification_outbox(subject_type, subject_id, created_at);

CREATE TABLE notification_attempt (
  id TEXT PRIMARY KEY,
  notification_id TEXT NOT NULL REFERENCES notification_outbox(id),
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
  outcome TEXT NOT NULL CHECK (
    outcome IN ('DELIVERED', 'RETRYABLE', 'TERMINAL')
  ),
  http_status_class TEXT,
  error_code TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  UNIQUE(notification_id, attempt_number)
);

CREATE INDEX idx_notification_attempt_item
  ON notification_attempt(notification_id, attempt_number);
"""

MIGRATION_0005_NAME = "0005_query_delivery_status"
MIGRATION_0005_SQL = r"""
ALTER TABLE query_run ADD COLUMN generation_status TEXT CHECK (
  generation_status IN ('succeeded', 'failed')
);
ALTER TABLE query_run ADD COLUMN answer_origin TEXT CHECK (
  answer_origin IN ('knowledge_base', 'general_model')
);
ALTER TABLE query_run ADD COLUMN evidence_status TEXT CHECK (
  evidence_status IN ('grounded', 'partially_grounded', 'ungrounded')
);
ALTER TABLE query_run ADD COLUMN review_status TEXT CHECK (
  review_status IN (
    'not_required', 'pending', 'approved', 'rejected', 'unavailable'
  )
);
"""


@dataclass(frozen=True)
class SchemaMigration:
    version: int
    name: str
    sql: str


MIGRATIONS = (
    SchemaMigration(INITIAL_SCHEMA_VERSION, MIGRATION_NAME, MIGRATION_SQL),
    SchemaMigration(
        REPLACEMENT_SCHEMA_VERSION,
        MIGRATION_0002_NAME,
        MIGRATION_0002_SQL,
    ),
    SchemaMigration(
        QUERY_GOVERNANCE_SCHEMA_VERSION,
        MIGRATION_0003_NAME,
        MIGRATION_0003_SQL,
    ),
    SchemaMigration(
        NOTIFICATION_SCHEMA_VERSION,
        MIGRATION_0004_NAME,
        MIGRATION_0004_SQL,
    ),
    SchemaMigration(
        QUERY_DELIVERY_SCHEMA_VERSION,
        MIGRATION_0005_NAME,
        MIGRATION_0005_SQL,
    ),
)


def migration_checksum(version: int | None = None) -> str:
    requested = SCHEMA_VERSION if version is None else version
    migration = migration_for_version(requested)
    return "sha256:" + hashlib.sha256(
        migration.sql.encode("utf-8")
    ).hexdigest()


def migration_for_version(version: int) -> SchemaMigration:
    for migration in MIGRATIONS:
        if migration.version == version:
            return migration
    raise ValueError(f"unsupported schema migration version: {version}")


def migration_checksums() -> dict[int, str]:
    return {
        migration.version: "sha256:"
        + hashlib.sha256(migration.sql.encode("utf-8")).hexdigest()
        for migration in MIGRATIONS
    }


REQUIRED_TABLES = {
    "schema_meta",
    "state_clock",
    "migration_record",
    "compatibility_export",
    "security_domain",
    "retrieval_partition",
    "source_document",
    "source_revision",
    "lane_run",
    "lane_run_revision",
    "lightrag_binding",
}

REQUIRED_TABLES_V2 = REQUIRED_TABLES | {
    "replacement_operation",
}

REQUIRED_TABLES_V3 = REQUIRED_TABLES_V2 | {
    "query_run",
    "maintenance_fence",
    "gateway_instance",
    "audit_item",
    "audit_event",
}

REQUIRED_TABLES_V4 = REQUIRED_TABLES_V3 | {
    "notification_outbox",
    "notification_attempt",
}

REQUIRED_TABLES_V5 = REQUIRED_TABLES_V4

REQUIRED_INDEXES = {
    "idx_source_revision_source",
    "idx_lane_run_latest",
    "idx_lane_run_revision_revision",
    "idx_lightrag_binding_status",
    "idx_lightrag_binding_track",
}

REQUIRED_INDEXES_V2 = REQUIRED_INDEXES | {
    "idx_replacement_operation_active_source",
    "idx_replacement_operation_status",
    "idx_source_revision_one_active",
}

REQUIRED_INDEXES_V3 = REQUIRED_INDEXES_V2 | {
    "idx_query_run_active_partition",
    "idx_query_run_created",
    "idx_maintenance_fence_active_partition",
    "idx_maintenance_fence_operation",
    "idx_gateway_instance_partition_heartbeat",
    "idx_audit_item_queue",
    "idx_audit_event_item",
}

REQUIRED_INDEXES_V4 = REQUIRED_INDEXES_V3 | {
    "idx_notification_outbox_due",
    "idx_notification_outbox_subject",
    "idx_notification_attempt_item",
}

REQUIRED_INDEXES_V5 = REQUIRED_INDEXES_V4


def required_tables_for_version(version: int) -> set[str]:
    if version == INITIAL_SCHEMA_VERSION:
        return set(REQUIRED_TABLES)
    if version == REPLACEMENT_SCHEMA_VERSION:
        return set(REQUIRED_TABLES_V2)
    if version == QUERY_GOVERNANCE_SCHEMA_VERSION:
        return set(REQUIRED_TABLES_V3)
    if version == NOTIFICATION_SCHEMA_VERSION:
        return set(REQUIRED_TABLES_V4)
    if version == QUERY_DELIVERY_SCHEMA_VERSION:
        return set(REQUIRED_TABLES_V5)
    raise ValueError(f"unsupported schema version: {version}")


def required_indexes_for_version(version: int) -> set[str]:
    if version == INITIAL_SCHEMA_VERSION:
        return set(REQUIRED_INDEXES)
    if version == REPLACEMENT_SCHEMA_VERSION:
        return set(REQUIRED_INDEXES_V2)
    if version == QUERY_GOVERNANCE_SCHEMA_VERSION:
        return set(REQUIRED_INDEXES_V3)
    if version == NOTIFICATION_SCHEMA_VERSION:
        return set(REQUIRED_INDEXES_V4)
    if version == QUERY_DELIVERY_SCHEMA_VERSION:
        return set(REQUIRED_INDEXES_V5)
    raise ValueError(f"unsupported schema version: {version}")

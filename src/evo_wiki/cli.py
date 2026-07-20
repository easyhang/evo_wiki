from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
from enum import IntEnum
from pathlib import Path

from .config import EvoConfig
from .corpus import scan_corpus
from .generation import GenerationService
from .journal import (
    JournalError,
    verify_logs_root,
)
from .journal_legacy import migrate_legacy_journal
from .lightrag_lane import LightRAGBuildError, build_lightrag, doctor_lightrag, prepare_lightrag_input
from .orchestration import (
    current_scan,
    execute_lanes,
    lane_state_path,
    merge_change_sets,
)
from .state.notifications import (
    NotificationDispatcher,
    build_notification,
    notification_settings,
    should_notify,
)
from .paths import ProjectPaths
from .platform_export import export_platform
from .query_audit import (
    delete_query_audit_payload,
    read_query_audit_payload,
)
from .query_gateway import GatewayQueryRequest, TrustedQueryGateway
from .retrieval_skills.evidence_subgraph import (
    EvidenceSubgraphError,
    retrieve_evidence_subgraph,
)
from .state import (
    ReplacementOperationService,
    ReplacementPlanner,
    StateBackupService,
    StateError,
    StateExporter,
    StateMigrator,
    StateReconciler,
    StateSchemaMigrator,
    StateStore,
    StateVerifier,
)
from .utils import read_json, write_json
from .version import __version__
from .wiki import ensure_wiki_stub, render_wiki
from .wiki_health import lint_wiki_artifacts


class ExitCode(IntEnum):
    OK = 0
    INTERNAL_ERROR = 1
    USAGE_ERROR = 2
    WIKI_HEALTH_FAILED = 3
    CONFIG_ERROR = 4
    STATE_ERROR = 5
    REMOTE_ERROR = 6


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except JournalError as exc:
        if getattr(args, "json_output", False):
            _emit_cli_error(
                args,
                error_code=exc.error_code,
                state_committed=False,
            )
            return ExitCode.INTERNAL_ERROR
        print(f"EvoWiki journal failed [{exc.error_code}]", file=sys.stderr)
        return ExitCode.INTERNAL_ERROR
    except StateError as exc:
        state_exit = _state_error_exit_code(exc.error_code)
        if getattr(args, "json_output", False):
            _emit_cli_error(
                args,
                error_code=exc.error_code,
                state_committed=exc.committed,
                details=exc.details,
            )
            return state_exit
        print(
            f"EvoWiki state failed [{exc.error_code}]"
            f"{' (state committed)' if exc.committed else ''}",
            file=sys.stderr,
        )
        return state_exit
    except LightRAGBuildError as exc:
        if getattr(args, "json_output", False):
            _emit_cli_error(
                args,
                error_code=exc.failure_code or "LIGHTRAG_BUILD_FAILED",
                state_committed=False,
            )
            return ExitCode.REMOTE_ERROR
        print(f"LightRAG build failed: {exc}", file=sys.stderr)
        return ExitCode.REMOTE_ERROR
    except EvidenceSubgraphError as exc:
        suffix = f"; trace={exc.trace_path}" if exc.trace_path else ""
        print(
            f"Evidence subgraph query failed [{exc.failure_code}]{suffix}",
            file=sys.stderr,
        )
        return ExitCode.REMOTE_ERROR
    except Exception as exc:
        if getattr(args, "json_output", False):
            _emit_cli_error(
                args,
                error_code="INTERNAL_ERROR",
                state_committed=False,
            )
            return ExitCode.INTERNAL_ERROR
        print(f"evo-wiki error: {exc}", file=sys.stderr)
        return ExitCode.INTERNAL_ERROR


def _emit_cli_error(
    args: argparse.Namespace,
    *,
    error_code: str,
    state_committed: bool,
    details: dict | None = None,
) -> None:
    command = getattr(args, "command", "unknown")
    subcommand = getattr(args, "state_command", None)
    operation = (
        f"{command}.{subcommand}"
        if isinstance(subcommand, str)
        else str(command)
    )
    payload = {
        **(details or {}),
        "status": "failed",
        "operation": operation,
        "error_code": error_code,
        "state_committed": state_committed,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _state_error_exit_code(error_code: str) -> ExitCode:
    if (
        error_code.startswith("STATE_CONFIG")
        or error_code.startswith("PLATFORM_SERVE_CONFIG")
        or error_code.startswith("PLATFORM_SERVE_AUTH")
        or error_code.startswith("PLATFORM_SERVE_BIND")
        or error_code in {
            "GENERATION_NOT_INITIALIZED",
            "GENERATION_CORPUS_EMPTY",
            "PLATFORM_NOT_GENERATED",
        }
        or error_code.startswith("QUERY_GATEWAY_CONFIG")
        or error_code.startswith("OPS_NOTIFICATION_CONFIG")
        or error_code in {
            "STATE_PATH_INVALID",
            "STATE_SCHEMA_UNSUPPORTED",
            "REPLACEMENT_DISABLED",
            "QUERY_GATEWAY_DISABLED",
            "QUERY_GATEWAY_AUTH_UNSAFE",
            "QUERY_GATEWAY_AUDIT_REQUIRED",
            "QUERY_GATEWAY_BIND_UNSAFE",
            "QUERY_GATEWAY_DEPENDENCY_MISSING",
            "QUERY_GATEWAY_EVIDENCE_POLICY_UNSUPPORTED",
            "QUERY_GATEWAY_FAIL_CLOSED_REQUIRED",
            "QUERY_AUDIT_KEY_MISSING",
            "OPS_NOTIFICATION_DISABLED",
            "OPS_NOTIFICATION_WEBHOOK_MISSING",
            "OPS_NOTIFICATION_WEBHOOK_INVALID",
            "OPS_NOTIFICATION_WEBHOOK_INSECURE",
            "OPS_NOTIFICATION_SIGNING_KEY_MISSING",
            "QG_ACCEPTANCE_CONFIRMATION_MISMATCH",
            "QG_ACCEPTANCE_PROVIDER_CONFIG_MISSING",
        }
    ):
        return ExitCode.CONFIG_ERROR
    if error_code in {
        "GENERATION_WIKI_SOURCE_MISSING",
        "GENERATION_WIKI_STUB",
        "GENERATION_WIKI_FAILED",
    }:
        return ExitCode.WIKI_HEALTH_FAILED
    if error_code in {
        "GENERATION_REMOTE_FAILED",
        "GENERATION_REBUILD_REQUIRED",
    }:
        return ExitCode.REMOTE_ERROR
    if error_code in {
        "QUERY_LEASE_NOT_EXPIRED",
        "QUERY_RUN_NOT_FOUND",
        "QUERY_RUN_TERMINAL",
        "QUERY_LEASE_CONFIRMATION_MISMATCH",
        "OPS_NOTIFICATION_NOT_FOUND",
        "OPS_NOTIFICATION_NOT_RETRYABLE",
        "OPS_NOTIFICATION_RETRY_INVALID",
        "QG_ACCEPTANCE_RUN_ID_INVALID",
        "QG_ACCEPTANCE_CLEANUP_CONFIRMATION_MISMATCH",
    }:
        return ExitCode.STATE_ERROR
    if (
        error_code.startswith("REMOTE_")
        or error_code.startswith("REPLACE_REMOTE_")
        or error_code.startswith("QUERY_")
        or error_code.startswith("QG_ACCEPTANCE_")
        or error_code in {
            "OPS_NOTIFICATION_DELIVERY_FAILED",
            "OPS_NOTIFICATION_REQUEST_FAILED",
            "OPS_NOTIFICATION_REMOTE_RETRYABLE",
            "OPS_NOTIFICATION_REMOTE_REJECTED",
        }
        or error_code in {
            "REPLACE_PLAN_STALE",
            "REPLACE_OWNER_DRIFT",
        }
    ):
        return ExitCode.REMOTE_ERROR
    return ExitCode.STATE_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evo-wiki",
        description="AI-driven knowledge platform generator for developers",
    )
    parser.add_argument("--version", action="version", version=f"evo-wiki {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_root(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default="workspace", help="Runtime workspace root containing corpus/ and artifacts/; defaults to ./workspace")

    p = sub.add_parser("init", help="Initialize a new Evo wiki project")
    add_root(p)
    p.add_argument(
        "--profile",
        choices=[
            "local-platform",
            "production-export",
            "wiki-only",
        ],
        default="local-platform",
    )
    p.set_defaults(func=cmd_init)

    p = sub.add_parser(
        "generate",
        help="Generate a Wiki or complete governed Web platform",
    )
    add_root(p)
    p.add_argument(
        "--target",
        choices=["platform", "wiki"],
        default="platform",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan generation without workspace or remote writes",
    )
    p.add_argument("--smoke-query", default=None)
    p.add_argument("--json", action="store_true", dest="json_output")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser(
        "serve",
        help="Serve the generated platform and query gateway locally",
    )
    add_root(p)
    p.add_argument("--listen", default="127.0.0.1:8080")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("scan", help="Scan corpus and write change set")
    add_root(p); p.set_defaults(func=cmd_scan)

    p = sub.add_parser("render-wiki", help="Render artifacts/wiki/wiki-src Markdown into static HTML")
    add_root(p); p.set_defaults(func=cmd_render_wiki)

    p = sub.add_parser("lint-wiki", help="Run llm-wiki-style health checks for wiki-src/audit/log")
    add_root(p); p.set_defaults(func=cmd_lint_wiki)

    p = sub.add_parser("prepare-lightrag", help="Prepare LightRAG input package from corpus")
    add_root(p); p.set_defaults(func=cmd_prepare_lightrag)

    p = sub.add_parser("build-lightrag", help="Submit prepared LightRAG input to an existing LightRAG service")
    add_root(p)
    p.add_argument("--smoke-query", default=None, help="Optional hybrid query after import submission")
    p.add_argument("--dry-run", action="store_true", help="Do not call LightRAG service; only report import delta")
    p.set_defaults(func=cmd_build_lightrag)

    p = sub.add_parser("export-platform", help="Export the read-only Web platform directory (static site + SPA + nginx.conf)")
    add_root(p); p.set_defaults(func=cmd_export_platform)

    p = sub.add_parser("inspect", help="Print top-level manifest and reports")
    add_root(p); p.set_defaults(func=cmd_inspect)

    p = sub.add_parser(
        "migrate-state",
        help="Plan or apply the explicit legacy JSON to SQLite state cutover",
    )
    add_root(p)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration; default is a zero-workspace-write dry-run",
    )
    p.add_argument(
        "--abort-candidate",
        action="store_true",
        help="Move an inactive installed candidate aside; requires --apply",
    )
    p.add_argument("--json", action="store_true", dest="json_output")
    p.set_defaults(func=cmd_migrate_state)

    p = sub.add_parser("state", help="Operate and verify SQLite state")
    state_sub = p.add_subparsers(dest="state_command", required=True)

    state_verify = state_sub.add_parser(
        "verify",
        help="Verify SQLite facts and report derived-artifact warnings",
    )
    add_root(state_verify)
    state_verify.add_argument("--json", action="store_true", dest="json_output")
    state_verify.set_defaults(func=cmd_state_verify)

    state_export = state_sub.add_parser(
        "export",
        help="Regenerate compatibility JSON from SQLite facts",
    )
    add_root(state_export)
    state_export.add_argument("--json", action="store_true", dest="json_output")
    state_export.set_defaults(func=cmd_state_export)

    state_backup = state_sub.add_parser(
        "backup",
        help="Create and verify a consistent online SQLite backup",
    )
    add_root(state_backup)
    state_backup.add_argument("--json", action="store_true", dest="json_output")
    state_backup.set_defaults(func=cmd_state_backup)

    state_migrate_schema = state_sub.add_parser(
        "migrate-schema",
        help="Plan or apply an explicit SQLite schema upgrade",
    )
    add_root(state_migrate_schema)
    state_migrate_schema.add_argument("--apply", action="store_true")
    state_migrate_schema.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    state_migrate_schema.set_defaults(func=cmd_state_migrate_schema)

    state_reconcile = state_sub.add_parser(
        "reconcile",
        help="Observe blocked LightRAG bindings without replaying side effects",
    )
    add_root(state_reconcile)
    state_reconcile.add_argument("--apply", action="store_true")
    state_reconcile.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    state_reconcile.set_defaults(func=cmd_state_reconcile)

    state_replace_plan = state_sub.add_parser(
        "replace-plan",
        help="Build a zero-write review plan for blocked HTTP 409 replacements",
    )
    add_root(state_replace_plan)
    state_replace_plan.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    state_replace_plan.set_defaults(func=cmd_state_replace_plan)

    state_replace_execute = state_sub.add_parser(
        "replace-execute",
        help="Execute or safely resume one reviewed HTTP 409 replacement",
    )
    add_root(state_replace_execute)
    state_replace_execute.add_argument("--plan-id", required=True)
    state_replace_execute.add_argument(
        "--confirm-digest",
        required=True,
    )
    state_replace_execute.add_argument("--smoke-query", required=True)
    state_replace_execute.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    state_replace_execute.set_defaults(func=cmd_state_replace_execute)

    state_replace_status = state_sub.add_parser(
        "replace-status",
        help="Read durable replacement operation status",
    )
    add_root(state_replace_status)
    state_replace_status.add_argument("--operation-id", default=None)
    state_replace_status.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    state_replace_status.set_defaults(func=cmd_state_replace_status)

    state_replace_recover = state_sub.add_parser(
        "replace-recover",
        help="Run an explicitly confirmed compensation rollback",
    )
    add_root(state_replace_recover)
    state_replace_recover.add_argument("--operation-id", required=True)
    state_replace_recover.add_argument(
        "--action",
        choices=["rollback"],
        required=True,
    )
    state_replace_recover.add_argument("--confirm", required=True)
    state_replace_recover.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    state_replace_recover.set_defaults(func=cmd_state_replace_recover)

    p = sub.add_parser("logs", help="Verify or migrate EvoWiki operational journals")
    logs_sub = p.add_subparsers(required=True)

    logs_verify = logs_sub.add_parser(
        "verify",
        help="Verify journal contracts, sequence numbers, and hash chains",
    )
    add_root(logs_verify)
    logs_verify.add_argument(
        "--run",
        default=None,
        help="Verify one run ID instead of all discovered run journals",
    )
    logs_verify.set_defaults(func=cmd_logs_verify)

    logs_migrate = logs_sub.add_parser(
        "migrate-legacy",
        help="Dry-run or apply the one-time legacy JSONL migration",
    )
    add_root(logs_migrate)
    logs_migrate.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration; default is a read-only dry-run",
    )
    logs_migrate.set_defaults(func=cmd_logs_migrate_legacy)

    p = sub.add_parser(
        "gateway",
        help="Check, serve, or inspect the trusted query gateway",
    )
    gateway_sub = p.add_subparsers(dest="gateway_command", required=True)
    gateway_check = gateway_sub.add_parser(
        "check",
        help="Read-only gateway/backend readiness check",
    )
    add_root(gateway_check)
    gateway_check.add_argument("--json", action="store_true", dest="json_output")
    gateway_check.set_defaults(func=cmd_gateway_check)
    gateway_serve = gateway_sub.add_parser(
        "serve",
        help="Serve the trusted query gateway",
    )
    add_root(gateway_serve)
    gateway_serve.set_defaults(func=cmd_gateway_serve)
    gateway_status = gateway_sub.add_parser(
        "status",
        help="Read gateway instances, query counts, fences, and audit counts",
    )
    add_root(gateway_status)
    gateway_status.add_argument("--json", action="store_true", dest="json_output")
    gateway_status.set_defaults(func=cmd_gateway_status)
    gateway_lease_recover = gateway_sub.add_parser(
        "lease-recover",
        help="Explicitly abandon one expired query lease",
    )
    add_root(gateway_lease_recover)
    gateway_lease_recover.add_argument("--request-id", required=True)
    gateway_lease_recover.add_argument(
        "--action",
        choices=["abandon"],
        required=True,
    )
    gateway_lease_recover.add_argument("--confirm", required=True)
    gateway_lease_recover.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    gateway_lease_recover.set_defaults(func=cmd_gateway_lease_recover)
    gateway_acceptance = gateway_sub.add_parser(
        "acceptance",
        help="Run the QG-001 zero-write preflight or isolated acceptance",
    )
    add_root(gateway_acceptance)
    gateway_acceptance.add_argument("--report", required=True)
    gateway_acceptance.add_argument(
        "--apply",
        action="store_true",
        help="Run the destructive cases only inside a temporary environment",
    )
    gateway_acceptance.add_argument("--confirm", default=None)
    gateway_acceptance.add_argument(
        "--provider-env-file",
        default=None,
    )
    gateway_acceptance.add_argument(
        "--allow-image-pull",
        action="store_true",
    )
    gateway_acceptance.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    gateway_acceptance.set_defaults(func=cmd_gateway_acceptance)
    gateway_cleanup = gateway_sub.add_parser(
        "acceptance-cleanup",
        help="Remove only resources labelled for one QG-001 run",
    )
    gateway_cleanup.add_argument("--run-id", required=True)
    gateway_cleanup.add_argument("--confirm", required=True)
    gateway_cleanup.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    gateway_cleanup.set_defaults(func=cmd_gateway_acceptance_cleanup)

    p = sub.add_parser("audit", help="Operate the sanitized query audit queue")
    audit_sub = p.add_subparsers(dest="audit_command", required=True)
    audit_list = audit_sub.add_parser("list", help="List audit items")
    add_root(audit_list)
    audit_list.add_argument(
        "--status",
        choices=["OPEN", "IN_REVIEW", "RESOLVED", "REJECTED", "WAIVED"],
        default=None,
    )
    audit_list.add_argument("--json", action="store_true", dest="json_output")
    audit_list.set_defaults(func=cmd_audit_list)
    audit_show = audit_sub.add_parser("show", help="Show one audit item")
    add_root(audit_show)
    audit_show.add_argument("--audit-id", required=True)
    audit_show.add_argument(
        "--include-content",
        action="store_true",
        help="Read the protected question, answer, and citation snapshot",
    )
    audit_show.add_argument("--json", action="store_true", dest="json_output")
    audit_show.set_defaults(func=cmd_audit_show)
    audit_resolve = audit_sub.add_parser("resolve", help="Resolve one audit item")
    add_root(audit_resolve)
    audit_resolve.add_argument("--audit-id", required=True)
    audit_resolve.add_argument("--confirm", required=True)
    audit_resolve.add_argument(
        "--resolution",
        choices=["APPROVED", "REJECTED"],
        default="APPROVED",
    )
    audit_resolve.add_argument("--json", action="store_true", dest="json_output")
    audit_resolve.set_defaults(func=cmd_audit_resolve)

    p = sub.add_parser(
        "alerts",
        help="Inspect and deliver the durable operations notification outbox",
    )
    alerts_sub = p.add_subparsers(dest="alerts_command", required=True)
    alerts_status = alerts_sub.add_parser(
        "status",
        help="Read notification outbox state",
    )
    add_root(alerts_status)
    alerts_status.add_argument("--notification-id", default=None)
    alerts_status.add_argument("--json", action="store_true", dest="json_output")
    alerts_status.set_defaults(func=cmd_alerts_status)
    alerts_dispatch = alerts_sub.add_parser(
        "dispatch",
        help="Dispatch currently due notification events",
    )
    add_root(alerts_dispatch)
    alerts_dispatch.add_argument("--limit", type=int, default=20)
    alerts_dispatch.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    alerts_dispatch.set_defaults(func=cmd_alerts_dispatch)
    alerts_retry = alerts_sub.add_parser(
        "retry",
        help="Explicitly retry a failed notification",
    )
    add_root(alerts_retry)
    alerts_retry.add_argument("--notification-id", required=True)
    alerts_retry.add_argument("--confirm", required=True)
    alerts_retry.add_argument("--json", action="store_true", dest="json_output")
    alerts_retry.set_defaults(func=cmd_alerts_retry)

    p = sub.add_parser("run", help="Run selected lanes")
    add_root(p)
    p.add_argument("--lane", choices=["wiki", "lightrag", "both"], default="wiki")
    p.add_argument("--lightrag-dry-run", action="store_true")
    p.add_argument("--smoke-query", default=None)
    p.add_argument("--json", action="store_true", dest="json_output")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("doctor", help="Check EvoWiki and LightRAG readiness")
    add_root(p)
    p.add_argument(
        "--check-service",
        action="store_true",
        help="Read LightRAG /health and /openapi.json (bounded, no writes)",
    )
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "query",
        help="Run a governed gateway query or retrieval-only evidence-subgraph query",
    )
    add_root(p)
    p.add_argument("--skill", choices=["evidence-subgraph"], default=None)
    p.add_argument(
        "--gateway",
        "--require-evidence",
        dest="require_evidence",
        action="store_true",
        help=(
            "Use the schema-v2 query gateway; --require-evidence is a "
            "compatibility alias and evidence warnings do not hide answers"
        ),
    )
    p.add_argument(
        "--only-context",
        action="store_true",
        help="Required for retrieval-only mode; return scoped evidence without generation",
    )
    p.add_argument("--query", required=True, help="Question used only for scoped evidence ranking")
    p.add_argument(
        "--seed",
        action="append",
        default=[],
        help="Explicit graph seed; repeat for multiple seeds",
    )
    p.add_argument(
        "--principal",
        default="local-cli",
        help="Local CLI principal label; only its HMAC is persisted",
    )
    p.add_argument(
        "--mode",
        choices=["naive", "local", "global", "hybrid", "mix"],
        default="mix",
    )
    p.add_argument("--json", action="store_true", dest="json_output")
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--max-nodes", type=int, default=None)
    p.add_argument("--max-edges", type=int, default=None)
    p.add_argument("--max-content-units", type=int, default=None)
    p.add_argument("--top-k", type=int, default=None)
    p.add_argument("--timeout-seconds", type=float, default=None)
    p.add_argument("--explain-retrieval", action="store_true")
    p.set_defaults(func=cmd_query)
    return parser


def load(root: str) -> tuple[ProjectPaths, EvoConfig]:
    paths = ProjectPaths.from_root(root)
    config = EvoConfig.load(paths.root)
    return paths, config


def state_backend(config: EvoConfig) -> str:
    state = config.project.get("state")
    if not isinstance(state, dict):
        return "legacy_json"
    backend = state.get("backend", "legacy_json")
    if backend not in {"legacy_json", "sqlite"}:
        raise StateError(
            "state.backend must be legacy_json or sqlite",
            error_code="STATE_CONFIG_INVALID",
        )
    return backend


def sqlite_store(
    paths: ProjectPaths,
    config: EvoConfig,
    *,
    initialize: bool = False,
) -> StateStore:
    raw_state = config.project.get("state")
    state_config = raw_state if isinstance(raw_state, dict) else {}
    store = StateStore(paths.root, state_config)
    if initialize:
        store.initialize()
    elif not store.exists:
        raise StateError(
            "SQLite state database is missing",
            error_code="STATE_DATABASE_MISSING",
        )
    return store


def active_sqlite_store(
    paths: ProjectPaths,
    config: EvoConfig,
) -> StateStore:
    store = active_store(paths, config)
    if store is None:
        raise StateError(
            "workspace must complete migrate-state --apply first",
            error_code="STATE_MIGRATION_REQUIRED",
        )
    return store


def active_store(
    paths: ProjectPaths,
    config: EvoConfig,
) -> StateStore | None:
    backend = state_backend(config)
    raw_state = config.project.get("state")
    state_config = raw_state if isinstance(raw_state, dict) else {}
    candidate = StateStore(paths.root, state_config)
    if backend == "legacy_json":
        if candidate.exists:
            raise StateError(
                "an installed SQLite candidate awaits migrate-state --apply or "
                "--apply --abort-candidate",
                error_code="STATE_CUTOVER_INCOMPLETE",
            )
        return None
    candidate.initialize()
    return candidate


def _emit_model(
    value: object,
    *,
    json_output: bool,
    human_lines: list[str],
) -> None:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    elif isinstance(value, dict):
        payload = value
    else:
        raise TypeError("unsupported CLI result")
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("\n".join(human_lines))


def cmd_init(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    EvoConfig.write_defaults(paths.root, profile=args.profile)
    config = EvoConfig.load(paths.root)
    if state_backend(config) == "sqlite":
        store = sqlite_store(paths, config, initialize=True)
        StateExporter(store).export()
    else:
        active_store(paths, config)
    ensure_wiki_stub(paths, config)
    write_json(paths.artifacts / "manifest.json", {"project": config.project["project"], "status": "initialized"})
    print(
        f"Initialized Evo wiki project at {paths.root} "
        f"(profile: {config.project.get('profile', args.profile)})"
    )
    if state_backend(config) == "legacy_json":
        print(
            "Existing legacy state was preserved; generate will back it up "
            "and cut over to SQLite automatically."
        )
    print(
        "Next: add source files under corpus/raw/, let the Evo Wiki Skill "
        "compile wiki-src/{concepts,entities,sources}, then run generate."
    )
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    service = GenerationService(
        paths,
        config,
        target=args.target,
        smoke_query=args.smoke_query,
    )
    result = service.plan() if args.dry_run else service.generate()
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Generation: {result['status']}")
        print(f"Target: {result['target']}")
        for step in result["steps"]:
            print(f"- {step['name']}: {step['status']}")
        print(f"Next: {result['next_command']}")
    return (
        ExitCode.STATE_ERROR
        if result["status"] == "blocked"
        else ExitCode.OK
    )


def cmd_serve(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    config.validate(paths.root, target="platform")
    store = active_sqlite_store(paths, config)
    gateway = TrustedQueryGateway(store, config.project)
    from .gateway_http import serve_platform

    serve_platform(
        gateway,
        paths.platform,
        listen=args.listen,
    )
    return ExitCode.OK


def cmd_scan(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    store = active_store(paths, config)
    files, change_set = current_scan(paths, store)
    # L1：scan 只是预览，不应覆盖 run 写入的 delta-plan.json，改写独立的 scan.json。
    write_json(paths.agent / "scan.json", {"selected_lanes": [], "change_set": change_set})
    print(json.dumps({"file_count": len(files), "change_set": change_set}, ensure_ascii=False, indent=2))
    return 0


def cmd_render_wiki(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    report = render_wiki(paths, config)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_lint_wiki(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    report = lint_wiki_artifacts(paths.root, paths.wiki_src, paths.wiki_audit, paths.wiki_log)
    write_json(paths.wiki_reports / "wiki-health.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    has_error = any(issue.get("severity") == "error" for issue in report.get("issues", []))
    return ExitCode.WIKI_HEALTH_FAILED if has_error else ExitCode.OK


def cmd_prepare_lightrag(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    files, _ = current_scan(paths, active_store(paths, config))
    report = prepare_lightrag_input(paths, files)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_build_lightrag(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    store = active_store(paths, config)
    if store is not None:
        store.stage_files(scan_corpus(paths.root, paths.corpus))
    report = build_lightrag(
        paths,
        smoke_query=args.smoke_query,
        dry_run=args.dry_run,
        config=config.project.get("lightrag", {}),
        state_store=store,
    )
    if store is not None:
        StateExporter(store).export()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_export_platform(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    result = export_platform(paths, config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_store(paths, config)
    bundle = {
        "state": (
            store.inspect()
            if store is not None
            else {
                "backend": "legacy_json",
                "migration_required": True,
            }
        ),
        "manifest": read_json(paths.artifacts / "manifest.json", {}),
        "wiki_report": read_json(paths.wiki_reports / "wiki-report.json", {}),
        "wiki_health": read_json(paths.wiki_reports / "wiki-health.json", {}),
        "lightrag_report": read_json(paths.lightrag_reports / "lightrag-report.json", {}),
    }
    print(json.dumps(bundle, ensure_ascii=False, indent=2))
    return 0


def cmd_migrate_state(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    raw_state = config.project.get("state")
    state_config = raw_state if isinstance(raw_state, dict) else {}
    migrator = StateMigrator(
        paths.root,
        state_config,
        lightrag_config=config.project.get("lightrag", {}),
        journal_config=config.project.get("journal", {}),
    )
    if args.abort_candidate:
        if not args.apply:
            raise StateError(
                "--abort-candidate requires --apply",
                error_code="STATE_CONFIG_INVALID",
            )
        result = migrator.abort_candidate()
    elif args.apply:
        result = migrator.apply()
    else:
        result = migrator.dry_run()
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"State migration: {result.status}",
            f"Mode: {result.mode}",
            f"Workspace mutated: {str(result.workspace_mutated).lower()}",
            f"Legacy fingerprint: {result.legacy_input_fingerprint}",
            (
                f"Database: {result.database}"
                if result.database
                else "Database: not installed"
            ),
            f"Warnings: {len(result.warnings)}",
        ],
    )
    return (
        ExitCode.OK
        if result.status != "failed"
        else ExitCode.STATE_ERROR
    )


def cmd_state_verify(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = sqlite_store(paths, config)
    result = StateVerifier(store).verify()
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"SQLite state verification: {result.overall_status}",
            f"Database: {result.database}",
            f"Schema version: {result.database_schema_version}",
            f"State commit sequence: {result.state_commit_seq}",
            (
                "Compatibility export sequence: "
                f"{result.last_exported_state_commit_seq}"
            ),
            *[
                f"- [{check.status}] {check.name}: {check.code}"
                for check in result.checks
            ],
        ],
    )
    return (
        ExitCode.STATE_ERROR
        if result.overall_status == "FAIL"
        else ExitCode.OK
    )


def cmd_state_export(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    result = StateExporter(store).export()
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            "Compatibility export: success",
            f"State commit sequence: {result.state_commit_seq}",
            f"Exported files: {len(result.exported_paths)}",
        ],
    )
    return ExitCode.OK


def cmd_state_backup(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = sqlite_store(paths, config)
    result = StateBackupService(
        store,
        config.project.get("journal", {}),
    ).backup()
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"SQLite backup: {result.status}",
            f"Backup ID: {result.backup_id}",
            f"Path: {result.backup_path}",
            f"State commit sequence: {result.state_commit_seq}",
            f"Size: {result.size_bytes} bytes",
            f"Verification: {result.verification_status}",
        ],
    )
    return (
        ExitCode.OK
        if result.status == "success"
        else ExitCode.STATE_ERROR
    )


def cmd_state_migrate_schema(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = sqlite_store(paths, config)
    migrator = StateSchemaMigrator(
        store,
        config.project.get("journal", {}),
    )
    result = migrator.apply() if args.apply else migrator.plan()
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"SQLite schema migration: {result.status}",
            f"Mode: {result.mode}",
            f"Workspace mutated: {str(result.workspace_mutated).lower()}",
            "Database schema: "
            f"{result.database_schema_version}"
            f" -> {result.target_database_schema_version}",
            f"Pending migrations: {len(result.pending_migrations)}",
        ],
    )
    return ExitCode.OK


def cmd_state_reconcile(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = (
        active_sqlite_store(paths, config)
        if args.apply
        else sqlite_store(paths, config)
    )
    result = StateReconciler(
        store,
        config.project.get("lightrag", {}),
        config.project.get("journal", {}),
    ).reconcile(apply=args.apply)
    if result.workspace_mutated:
        StateExporter(store).export()
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"LightRAG reconcile: {result.status}",
            f"Mode: {result.mode}",
            f"Workspace mutated: {str(result.workspace_mutated).lower()}",
            f"Bindings observed: {len(result.observations)}",
        ],
    )
    return (
        ExitCode.REMOTE_ERROR
        if result.status == "failed"
        else ExitCode.OK
    )


def cmd_state_replace_plan(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    if state_backend(config) != "sqlite":
        raise StateError(
            "workspace must complete migrate-state --apply first",
            error_code="STATE_MIGRATION_REQUIRED",
        )
    store = sqlite_store(paths, config)
    result = ReplacementPlanner(
        store,
        config.project.get("lightrag", {}),
    ).plan()
    human_lines = [
        f"LightRAG replacement plan: {result.status}",
        "Mode: dry_run",
        "Workspace mutated: false",
        "Delete attempted: false",
        f"Plans: {len(result.plans)}",
    ]
    for plan in result.plans:
        remote_status = (
            plan.remote_document.status
            if plan.remote_document is not None
            else "unresolved"
        )
        human_lines.append(
            f"- [{plan.review_status}] {plan.source_path}: "
            f"remote={remote_status}, "
            f"chunks={plan.impact.chunk_count}"
        )
        if plan.blockers:
            human_lines.append(
                f"  blockers={','.join(plan.blockers)}"
            )
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=human_lines,
    )
    return (
        ExitCode.OK
        if result.status in {"ready", "no_conflicts"}
        else ExitCode.REMOTE_ERROR
    )


def cmd_state_replace_execute(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    result = ReplacementOperationService(
        store,
        config.project.get("lightrag", {}),
        config.project.get("journal", {}),
        config.project.get("query_gateway", {}),
        config.project,
    ).execute(
        plan_id=args.plan_id,
        confirm_digest=args.confirm_digest,
        smoke_query=args.smoke_query,
    )
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"LightRAG replacement execution: {result.status}",
            f"Operation: {result.operation.operation_id}",
            f"Phase: {result.operation.phase}",
            "Workspace mutated: "
            f"{str(result.workspace_mutated).lower()}",
            f"Delete attempted: {str(result.delete_attempted).lower()}",
            "Maintenance active: "
            f"{str(result.operation.maintenance.active).lower()}",
            "Next action: "
            f"{result.operation.next_action or 'none'}",
            f"Error code: {result.error_code or 'none'}",
        ],
    )
    return (
        ExitCode.OK
        if result.status in {"completed", "rolled_back"}
        else ExitCode.REMOTE_ERROR
    )


def cmd_state_replace_status(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    result = ReplacementOperationService(
        store,
        config.project.get("lightrag", {}),
        config.project.get("journal", {}),
        config.project.get("query_gateway", {}),
        config.project,
    ).status(operation_id=args.operation_id)
    human_lines = [
        f"LightRAG replacement status: {result.status}",
        "Mode: read_only",
        "Workspace mutated: false",
        f"Operations: {len(result.operations)}",
    ]
    for operation in result.operations:
        human_lines.append(
            f"- {operation.operation_id}: "
            f"{operation.phase}/{operation.status}, "
            f"maintenance={str(operation.maintenance.active).lower()}"
        )
        if operation.error_code:
            human_lines.append(
                f"  error_code={operation.error_code}"
            )
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=human_lines,
    )
    return (
        ExitCode.REMOTE_ERROR
        if result.status in {"blocked", "needs_audit", "failed"}
        else ExitCode.OK
    )


def cmd_state_replace_recover(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    result = ReplacementOperationService(
        store,
        config.project.get("lightrag", {}),
        config.project.get("journal", {}),
        config.project.get("query_gateway", {}),
        config.project,
    ).recover_rollback(
        operation_id=args.operation_id,
        confirm=args.confirm,
    )
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"LightRAG replacement recovery: {result.status}",
            f"Operation: {result.operation.operation_id}",
            f"Phase: {result.operation.phase}",
            "Workspace mutated: "
            f"{str(result.workspace_mutated).lower()}",
            "Next action: "
            f"{result.operation.next_action or 'none'}",
            f"Error code: {result.error_code or 'none'}",
        ],
    )
    return (
        ExitCode.OK
        if result.status in {"completed", "rolled_back"}
        else ExitCode.REMOTE_ERROR
    )


def cmd_logs_verify(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    report = verify_logs_root(paths.artifacts / "logs", run_id=args.run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "failed" else 0


def cmd_logs_migrate_legacy(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    report = migrate_legacy_journal(
        paths.artifacts / "logs",
        apply=args.apply,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "failed" else 0


def cmd_gateway_check(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    result = TrustedQueryGateway(store, config.project).check()
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"Query gateway: {result['status']}",
            f"Mode: {result['mode']}",
            f"Schema version: {result['schema_version']}",
            f"Security domain: {result['security_domain']}",
            "Workspace mutated: false",
        ],
    )
    return ExitCode.OK


def cmd_gateway_serve(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    gateway = TrustedQueryGateway(store, config.project)
    from .gateway_http import serve_gateway

    serve_gateway(gateway)
    return ExitCode.OK


def cmd_gateway_status(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    status = store.gateway_status()
    result = {
        "schema_version": 1,
        "status": "ready",
        "mode": "read_only",
        "workspace_mutated": False,
        **status,
        "error_code": None,
    }
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            "Query gateway status: ready",
            f"Instances: {len(status['instances'])}",
            f"Active maintenance fences: {len(status['maintenance_fences'])}",
            f"Open audits: {status['audit_counts'].get('OPEN', 0)}",
            f"Retrieving queries: {status['query_counts'].get('RETRIEVING', 0)}",
            f"Failed notifications: "
            f"{status['notification_counts'].get('FAILED', 0)}",
        ],
    )
    return ExitCode.OK


def cmd_gateway_lease_recover(args: argparse.Namespace) -> int:
    if args.confirm != args.request_id:
        raise StateError(
            "query lease confirmation does not match",
            error_code="QUERY_LEASE_CONFIRMATION_MISMATCH",
        )
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    run = store.abandon_expired_query_lease(args.request_id)
    result = {
        "schema_version": 1,
        "status": "abandoned",
        "mode": "apply",
        "workspace_mutated": True,
        "request_id": str(run["id"]),
        "query_status": str(run["status"]),
        "error_code": None,
    }
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"Query lease: {run['id']}",
            "Status: ABANDONED",
        ],
    )
    return ExitCode.OK


def cmd_gateway_acceptance(args: argparse.Namespace) -> int:
    from .ops_acceptance import QG001AcceptanceService

    service = QG001AcceptanceService(
        source_root=Path(args.root),
        report_path=Path(args.report),
        provider_env_file=(
            Path(args.provider_env_file)
            if args.provider_env_file
            else None
        ),
        allow_image_pull=bool(args.allow_image_pull),
    )
    if args.apply:
        result = service.apply(confirm=str(args.confirm or ""))
    else:
        result = service.plan()
        from .utils import write_json_atomic

        write_json_atomic(Path(args.report).expanduser().resolve(), result)
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"QG-001 acceptance: {result['status']}",
            f"Mode: {result['mode']}",
            "Source workspace mutated: "
            f"{str(result['source_workspace_mutated']).lower()}",
            f"Blockers: {len(result.get('blockers', []))}",
            f"Error code: {result.get('error_code') or 'none'}",
        ],
    )
    return (
        ExitCode.OK
        if result["status"] in {"ready", "passed"}
        else ExitCode.REMOTE_ERROR
    )


def cmd_gateway_acceptance_cleanup(args: argparse.Namespace) -> int:
    if args.confirm != args.run_id:
        raise StateError(
            "acceptance cleanup confirmation does not match",
            error_code="QG_ACCEPTANCE_CLEANUP_CONFIRMATION_MISMATCH",
        )
    from .ops_acceptance import cleanup_acceptance_run

    result = cleanup_acceptance_run(args.run_id)
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"QG-001 cleanup: {result['status']}",
            f"Run ID: {result['run_id']}",
            f"Containers removed: {result['containers_removed']}",
            f"Networks removed: {result['networks_removed']}",
        ],
    )
    return ExitCode.OK


def cmd_alerts_status(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    items = store.list_notifications(
        notification_id=args.notification_id,
    )
    counts: dict[str, int] = {}
    for item in items:
        status = str(item["status"])
        counts[status] = counts.get(status, 0) + 1
    result = {
        "schema_version": 1,
        "status": "ready",
        "mode": "read_only",
        "workspace_mutated": False,
        "counts": counts,
        "items": items,
        "error_code": None,
    }
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            "Notification outbox: ready",
            f"Items: {len(items)}",
            f"Pending: {counts.get('PENDING', 0)}",
            f"Retry wait: {counts.get('RETRY_WAIT', 0)}",
            f"Failed: {counts.get('FAILED', 0)}",
        ],
    )
    return ExitCode.OK


def cmd_alerts_dispatch(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    settings = notification_settings(config.project)
    result = NotificationDispatcher(store, settings).dispatch_due(
        limit=args.limit,
    )
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"Notification dispatch: {result['status']}",
            f"Claimed: {result['claimed']}",
            f"Delivered: {result['delivered']}",
            f"Retry wait: {result['retry_wait']}",
            f"Failed: {result['failed']}",
        ],
    )
    return (
        ExitCode.OK
        if result["status"] in {"delivered", "no_pending"}
        else ExitCode.REMOTE_ERROR
    )


def cmd_alerts_retry(args: argparse.Namespace) -> int:
    if args.confirm != args.notification_id:
        raise StateError(
            "notification confirmation does not match",
            error_code="OPS_NOTIFICATION_CONFIRMATION_MISMATCH",
        )
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    settings = notification_settings(config.project)
    item = store.retry_notification(
        args.notification_id,
        additional_attempts=settings.max_attempts,
    )
    result = {
        "schema_version": 1,
        "status": "pending",
        "mode": "apply",
        "workspace_mutated": True,
        "notification_id": str(item["id"]),
        "attempt_count": int(item["attempt_count"]),
        "max_attempts": int(item["max_attempts"]),
        "error_code": None,
    }
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"Notification: {item['id']}",
            "Status: PENDING",
            f"Attempt count: {item['attempt_count']}",
        ],
    )
    return ExitCode.OK


def cmd_audit_list(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    items = store.list_audit_items(status=args.status)
    for item in items:
        item["review_status"] = _audit_review_status(
            str(item["status"])
        )
    result = {
        "schema_version": 1,
        "status": "ready",
        "mode": "read_only",
        "workspace_mutated": False,
        "items": items,
        "error_code": None,
    }
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            "Query audit queue: ready",
            f"Items: {len(items)}",
            *[
                f"- {item['id']}: {item['severity']}/"
                f"{item['status']} {item['trigger_code']}"
                for item in items
            ],
        ],
    )
    return ExitCode.OK


def cmd_audit_show(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    item = store.audit_item(args.audit_id)
    item["review_status"] = _audit_review_status(str(item["status"]))
    if args.include_content:
        item["content"] = read_query_audit_payload(
            paths.root,
            item["evidence"],
        )
    result = {
        "schema_version": 1,
        "status": "ready",
        "mode": "read_only",
        "workspace_mutated": False,
        "item": item,
        "error_code": None,
    }
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"Audit item: {item['id']}",
            f"Status: {item['status']}",
            f"Severity: {item['severity']}",
            f"Trigger: {item['trigger_code']}",
            f"Events: {len(item['events'])}",
        ],
    )
    return ExitCode.OK


def cmd_audit_resolve(args: argparse.Namespace) -> int:
    if args.confirm != args.audit_id:
        raise StateError(
            "audit confirmation does not match",
            error_code="QUERY_AUDIT_CONFIRMATION_MISMATCH",
        )
    paths, config = load(args.root)
    store = active_sqlite_store(paths, config)
    existing = store.audit_item(args.audit_id)
    if existing["evidence"].get("payload_path"):
        # Validate the protected snapshot before committing the review fact so
        # a missing or tampered file cannot leave a silently retained payload.
        read_query_audit_payload(paths.root, existing["evidence"])
    stored_resolution = (
        "RESOLVED" if args.resolution == "APPROVED" else args.resolution
    )
    settings = notification_settings(config.project)
    notification = (
        build_notification(
            root=paths.root,
            event_type="AUDIT_RESOLVED",
            severity=str(existing["severity"]),
            subject_type="audit_item",
            subject_id=args.audit_id,
            dedupe_key=(
                f"AUDIT_RESOLVED:{args.audit_id}:{args.resolution}"
            ),
            security_domain=str(
                (config.project.get("security") or {}).get(
                    "default_domain",
                    "default",
                )
            ),
            state=stored_resolution,
            max_attempts=settings.max_attempts,
        )
        if should_notify(settings, str(existing["severity"]))
        else None
    )
    item = store.resolve_audit_item(
        audit_id=args.audit_id,
        actor=f"{getpass.getuser()}@{socket.gethostname()}",
        resolution=stored_resolution,
        notification=notification,
    )
    payload_deleted = delete_query_audit_payload(
        paths.root,
        existing["evidence"],
    )
    item["review_status"] = _audit_review_status(str(item["status"]))
    result = {
        "schema_version": 1,
        "status": "resolved",
        "mode": "apply",
        "workspace_mutated": True,
        "item": item,
        "payload_deleted": payload_deleted,
        "state_commit_seq": store.state_commit_seq(),
        "error_code": None,
    }
    _emit_model(
        result,
        json_output=args.json_output,
        human_lines=[
            f"Audit item: {item['id']}",
            f"Status: {item['status']}",
            f"State commit sequence: {result['state_commit_seq']}",
        ],
    )
    return ExitCode.OK


def _audit_review_status(status: str) -> str:
    return {
        "OPEN": "pending",
        "IN_REVIEW": "pending",
        "RESOLVED": "approved",
        "REJECTED": "rejected",
        "WAIVED": "not_required",
    }.get(status, "unavailable")


def cmd_run(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    store = active_store(paths, config)
    lanes = ["wiki", "lightrag"] if args.lane == "both" else [args.lane]
    result = execute_lanes(
        paths,
        config,
        store=store,
        lanes=lanes,
        lightrag_dry_run=args.lightrag_dry_run,
        smoke_query=args.smoke_query,
        command_name="run",
        reason=f"cli_run_{args.lane}",
        wiki_renderer=render_wiki,
    )
    if getattr(args, "json_output", False):
        print(json.dumps(result.payload, ensure_ascii=False, indent=2))
    else:
        print("\n".join(result.summary))
    return result.exit_code


def cmd_doctor(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    report = doctor_lightrag(
        paths,
        config.project.get("lightrag", {}),
        check_service=args.check_service,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "failed":
        return ExitCode.OK
    failed_names = {
        check.get("name")
        for check in report.get("checks", [])
        if check.get("status") == "failed"
    }
    if args.check_service and failed_names == {"lightrag_service"}:
        return ExitCode.REMOTE_ERROR
    return ExitCode.CONFIG_ERROR


def cmd_query(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    if args.require_evidence:
        if args.skill is not None or args.only_context or args.seed:
            raise StateError(
                "--gateway cannot be combined with evidence-subgraph options",
                error_code="QUERY_REQUEST_INVALID",
            )
        store = active_sqlite_store(paths, config)
        result = TrustedQueryGateway(store, config.project).query(
            GatewayQueryRequest(
                query=args.query,
                mode=args.mode,
                top_k=args.top_k or 20,
            ),
            principal=args.principal,
        )
        print(
            json.dumps(
                result.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
        return (
            ExitCode.OK
            if result.generation_status == "succeeded"
            else ExitCode.REMOTE_ERROR
        )
    if args.skill != "evidence-subgraph" or not args.only_context:
        raise StateError(
            "evidence-subgraph queries require --skill evidence-subgraph "
            "and --only-context",
            error_code="QUERY_REQUEST_INVALID",
        )
    if not args.seed:
        raise StateError(
            "evidence-subgraph queries require at least one --seed",
            error_code="QUERY_REQUEST_INVALID",
        )
    result = retrieve_evidence_subgraph(
        paths,
        config.project,
        query=args.query,
        seeds=args.seed,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
        max_edges=args.max_edges,
        max_content_units=args.max_content_units,
        top_k=args.top_k,
        timeout_seconds=args.timeout_seconds,
        explain_retrieval=args.explain_retrieval,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .corpus import CorpusFile, TEXT_SUFFIXES
from .evidence import gate_lightrag_references
from .lightrag_sync import RemoteTrackState, TrackSnapshot, parse_track_status
from .paths import ProjectPaths
from .state.contracts import ActionGate, RemoteStatus, StateError
from .state.store import StateStore
from .utils import read_json, relpath, utc_now, write_json


DEFAULT_EMBEDDING_BATCH_SIZE = 8
MAX_EMBEDDING_BATCH_SIZE = 10
WORKSPACE_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_POLL_TIMEOUT_SECONDS = 600.0
MIN_POLL_INTERVAL_SECONDS = 0.1
MAX_POLL_INTERVAL_SECONDS = 60.0
MIN_POLL_TIMEOUT_SECONDS = 1.0
MAX_POLL_TIMEOUT_SECONDS = 3600.0


class LightRAGBuildError(RuntimeError):
    """A build failure with an optional safe, machine-readable failure code."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str | None = None,
        track_status: list[dict[str, Any]] | None = None,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.failure_code = failure_code
        self.track_status = track_status
        self.http_status = http_status


@dataclass(frozen=True)
class RagCapabilities:
    """Sanitized capabilities discovered from LightRAG health and OpenAPI."""

    core_version: str | None
    api_version: str | None
    authenticated_health: bool
    openapi_available: bool
    expected_workspace: str
    workspace: str | None
    workspace_matches: bool | None
    storage_workspaces: dict[str, str | None] | None
    storage_workspaces_available: bool | None
    storage_workspaces_match: bool | None
    requested_embedding_batch_size: int
    remote_embedding_batch_size: int | None
    embedding_batch_matches: bool | None
    rerank_enabled: bool | None
    parser_routing_available: bool | None
    supports_chunk_content: bool | None
    supports_conversation_history: bool | None
    supports_bypass: bool | None
    supports_graph_subgraph: bool | None
    supports_track_status: bool | None
    supports_document_delete: bool | None
    supports_document_inventory: bool | None
    supports_pipeline_status: bool | None


def prepare_lightrag_input(paths: ProjectPaths, files: list[CorpusFile]) -> dict:
    paths.lightrag_input.mkdir(parents=True, exist_ok=True)
    files_dir = paths.lightrag_input / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    docs = []
    for item in files:
        source = paths.root / item.path
        if not source.exists():
            continue
        # L2：item.path 通常形如 "corpus/raw/a.md"；若文件不在 corpus/ 下（异常布局），
        # 退化为按原相对路径放置，避免 relative_to 抛 ValueError。
        item_path = Path(item.path)
        try:
            rel = item_path.relative_to("corpus")
        except ValueError:
            rel = item_path
        copied = files_dir / rel
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copied)
        text = read_text_for_lightrag(source) if item.suffix in TEXT_SUFFIXES else ""
        docs.append(
            {
                "id": stable_doc_id(item.path),
                "source_path": item.path,
                "input_path": relpath(copied, paths.root),
                "sha256": item.sha256,
                "size": item.size,
                "text_like": item.text_like,
                "text": text,
            }
        )
    documents_path = paths.lightrag_input / "documents.jsonl"
    documents_path.write_text("".join(json.dumps(doc, ensure_ascii=False) + "\n" for doc in docs), encoding="utf-8")
    manifest = {
        "status": "prepared",
        "generated_at": utc_now(),
        "document_count": len(docs),
        "documents_jsonl": relpath(documents_path, paths.root),
    }
    write_json(paths.lightrag_input / "manifest.json", manifest)
    return manifest


def build_lightrag(
    paths: ProjectPaths,
    *,
    smoke_query: str | None = None,
    dry_run: bool = False,
    config: dict[str, Any] | None = None,
    state_store: StateStore | None = None,
) -> dict:
    input_manifest = read_json(paths.lightrag_input / "manifest.json", {})
    documents_path = paths.lightrag_input / "documents.jsonl"
    if not documents_path.exists():
        raise LightRAGBuildError("LightRAG input is missing. Run prepare-lightrag first.")

    docs = [json.loads(line) for line in documents_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    text_docs = [doc for doc in docs if doc.get("text")]
    ledger = (
        {"documents": state_store.list_lightrag_documents()}
        if state_store is not None
        else read_json(
            paths.lightrag_state / "lightrag-import-ledger.json",
            {"documents": {}},
        )
    )
    previous = ledger.get("documents", {})
    imported = []
    skipped = []
    track_ids = []
    pending_ledger_entries: dict[str, dict[str, Any]] = {}
    binding_ids: dict[str, str] = {}
    submission_preserved_ids: set[str] = set()
    # H1：检测「曾经导入过、但当前 corpus 已不再包含」的文档。LightRAG 无法保证从
    # 已有图谱/向量中彻底删除旧知识，因此一旦发现删除，就必须诚实标记 requires_rebuild。
    current_ids = {doc["id"] for doc in docs}
    deleted = deleted_lightrag_sources(current_ids, previous)

    if dry_run:
        for doc in text_docs:
            if previous.get(doc["id"], {}).get("sha256") == doc["sha256"]:
                skipped.append(doc["source_path"])
            else:
                imported.append(doc["source_path"])
        report = base_report("dry_run", input_manifest, imported, skipped, None, deleted=deleted)
        write_json(paths.lightrag_reports / "lightrag-report.json", report)
        return report

    service = resolve_lightrag_service_config(config)
    partition_id = None
    backend_fingerprint = None
    if state_store is not None:
        partition_id, backend_fingerprint = state_store.ensure_partition(
            config
        )
    # [MULTI-WS] 将 workspace 传递给客户端，用于 LIGHTRAG-WORKSPACE 请求头。
    client = LightRAGServiceClient(service["base_url"], headers=service["headers"], timeout=service["timeout_seconds"], workspace=service.get("workspace"))
    print(
        f"Embedding batch expectation: requested={service['embedding_batch_size']} "
        f"documents={len(text_docs)}; verify the remote value with `doctor --check-service`",
        file=sys.stderr,
    )

    try:
        preflight_lightrag_build(client, service)
        for doc in text_docs:
            previous_entry = previous.get(doc["id"], {})
            if (
                previous_entry.get("sha256") == doc["sha256"]
                and (
                    state_store is None
                    or previous_entry.get("remote_status") == "PROCESSED"
                )
            ):
                if (
                    state_store is not None
                    and previous_entry.get("revision_status")
                    == "STAGED"
                ):
                    existing_binding_id = previous_entry.get("binding_id")
                    if isinstance(existing_binding_id, str):
                        state_store.activate_processed_binding(
                            existing_binding_id
                        )
                skipped.append(doc["source_path"])
                continue
            binding_id = None
            if state_store is not None:
                if partition_id is None or backend_fingerprint is None:
                    raise StateError(
                        "LightRAG retrieval partition is unavailable",
                        error_code="STATE_PARTITION_MISSING",
                    )
                binding_id = state_store.mark_submission_started(
                    source_path=doc["source_path"],
                    sha256=doc["sha256"],
                    partition_id=partition_id,
                    backend_fingerprint=backend_fingerprint,
                )
                binding_ids[doc["source_path"]] = binding_id
            try:
                response = client.post_json(
                    "/documents/text",
                    {
                        "text": doc["text"],
                        "file_source": doc["source_path"],
                    },
                )
            except Exception as exc:
                if state_store is not None and binding_id is not None:
                    if (
                        isinstance(exc, LightRAGBuildError)
                        and exc.http_status == 409
                    ):
                        state_store.mark_submission_conflict(binding_id)
                    else:
                        state_store.mark_submission_unknown(binding_id)
                    submission_preserved_ids.add(binding_id)
                raise
            track_id = response.get("track_id")
            if (
                state_store is not None
                and binding_id is not None
                and isinstance(track_id, str)
                and track_id
            ):
                state_store.mark_submission_acknowledged(
                    binding_id,
                    track_id=track_id,
                )
            imported.append(doc["source_path"])
            track_ids.append(
                {
                    "source_path": doc["source_path"],
                    "status": response.get("status"),
                    "track_id": track_id,
                }
            )
            pending_ledger_entries[doc["id"]] = {
                "source_path": doc["source_path"],
                "sha256": doc["sha256"],
                "submitted_at": utc_now(),
                "service_track_id": response.get("track_id"),
            }
        track_statuses = poll_lightrag_tracks(
            client,
            track_ids,
            poll_interval_seconds=service["poll_interval_seconds"],
            poll_timeout_seconds=service["poll_timeout_seconds"],
        )
        if state_store is not None:
            for snapshot in track_statuses:
                binding_id = binding_ids.get(snapshot["source_path"])
                if binding_id is None:
                    continue
                state_store.mark_binding_observation(
                    binding_id,
                    remote_status=RemoteStatus.PROCESSED,
                    action_gate=ActionGate.OPEN,
                    gate_reason=None,
                    chunk_count=snapshot.get("total_chunks"),
                )
                state_store.activate_processed_binding(binding_id)
        # 把已从 corpus 删除的条目在 ledger 中标注出来（保留记录、但不再视为"已同步"）。
        smoke = None
        if smoke_query:
            smoke = client.post_json(
                "/query",
                {
                    "query": smoke_query,
                    "mode": "hybrid",
                    "include_references": True,
                    "include_chunk_content": True,
                },
            )
            references = normalize_lightrag_references(
                smoke.get("references") or smoke.get("ref_results")
            )
            evidence = gate_lightrag_references(smoke_query, references)
            write_json(
                paths.lightrag_queries / "smoke-test.json",
                {
                    "query": smoke_query,
                    "answer": smoke.get("response"),
                    "references": evidence[0],
                    "evidence": evidence[1],
                    "raw_response": smoke,
                },
            )
    except Exception as exc:
        if state_store is not None:
            safe_snapshots = (
                exc.track_status
                if isinstance(exc, LightRAGBuildError)
                and exc.track_status is not None
                else []
            )
            snapshots_by_source = {
                item.get("source_path"): item
                for item in safe_snapshots
                if isinstance(item, dict)
            }
            for source_path, binding_id in binding_ids.items():
                if (
                    binding_id in submission_preserved_ids
                    and source_path not in snapshots_by_source
                ):
                    # Preserve the more precise response-loss or HTTP-conflict
                    # reason written at the POST boundary; the generic outer
                    # handler has no new remote observation with which to
                    # replace it.
                    continue
                snapshot = snapshots_by_source.get(source_path, {})
                observed = snapshot.get("state")
                if observed == "failed":
                    remote_status = RemoteStatus.FAILED
                    reason = "REMOTE_FAILED"
                elif observed == "processed":
                    remote_status = RemoteStatus.PROCESSED
                    reason = None
                elif observed in {"processing", "waiting"}:
                    remote_status = (
                        RemoteStatus.PROCESSING
                        if observed == "processing"
                        else RemoteStatus.PENDING
                    )
                    reason = "REMOTE_STATUS_UNCONFIRMED"
                else:
                    remote_status = RemoteStatus.UNKNOWN
                    reason = "REMOTE_STATUS_UNCONFIRMED"
                state_store.mark_binding_observation(
                    binding_id,
                    remote_status=remote_status,
                    action_gate=(
                        ActionGate.OPEN
                        if remote_status is RemoteStatus.PROCESSED
                        else ActionGate.BLOCKED
                    ),
                    gate_reason=reason,
                    chunk_count=snapshot.get("total_chunks"),
                    error_code=(
                        exc.failure_code
                        if isinstance(exc, LightRAGBuildError)
                        else "REMOTE_STATUS_REQUEST_FAILED"
                    ),
                )
        report = base_report("failed", input_manifest, imported, skipped, str(exc), deleted=deleted, service=service["public"])
        report["service_track_ids"] = track_ids
        if isinstance(exc, LightRAGBuildError) and exc.failure_code:
            report["failure_code"] = exc.failure_code
        if isinstance(exc, LightRAGBuildError) and exc.track_status is not None:
            report["track_status"] = exc.track_status
        write_json(paths.lightrag_reports / "lightrag-report.json", report)
        raise LightRAGBuildError(
            report["error"],
            failure_code=report.get("failure_code"),
            track_status=report.get("track_status"),
            http_status=(
                exc.http_status
                if isinstance(exc, LightRAGBuildError)
                else None
            ),
        ) from exc

    processed_at = utc_now()
    for entry in pending_ledger_entries.values():
        entry["processed_at"] = processed_at
    if state_store is None:
        previous.update(pending_ledger_entries)
        for doc_id, meta in previous.items():
            if doc_id not in current_ids:
                meta["removed_from_corpus"] = True
        ledger["documents"] = previous
        ledger["updated_at"] = utc_now()
        ledger["service"] = service["public"]
        write_json(
            paths.lightrag_state / "lightrag-import-ledger.json",
            ledger,
        )
    report = base_report("success", input_manifest, imported, skipped, None, deleted=deleted, service=service["public"])
    report["service_track_ids"] = track_ids
    report["track_status"] = track_statuses
    report["smoke_test"] = {"query": smoke_query, "ran": bool(smoke_query)}
    write_json(paths.lightrag_reports / "lightrag-report.json", report)
    write_json(
        paths.lightrag / "manifest.json",
        {
            "status": "success",
            "generated_at": report["generated_at"],
            "service": service["public"],
            "document_count": len(text_docs),
        },
    )
    return report


def deleted_lightrag_sources(
    current_ids: set[str],
    previous_documents: dict[str, Any],
) -> list[str]:
    """Return locally removed documents without mutating state or the service."""
    return sorted(
        (
            meta.get("source_path", doc_id)
            if isinstance(meta, dict)
            else doc_id
        )
        for doc_id, meta in previous_documents.items()
        if doc_id not in current_ids
    )


def detect_lightrag_deletions(
    paths: ProjectPaths,
    files: list[CorpusFile],
    *,
    state_store: StateStore | None,
) -> list[str]:
    """Read the current lane baseline and report rebuild-requiring deletions."""
    previous = (
        state_store.list_lightrag_documents()
        if state_store is not None
        else read_json(
            paths.lightrag_state / "lightrag-import-ledger.json",
            {"documents": {}},
        ).get("documents", {})
    )
    if not isinstance(previous, dict):
        previous = {}
    current_ids = {stable_doc_id(item.path) for item in files}
    return deleted_lightrag_sources(current_ids, previous)


def preflight_lightrag_build(client: "LightRAGServiceClient", service: dict[str, Any]) -> None:
    """Fail closed before writes when the remote namespace or polling API is uncertain."""
    health = client.request_json("GET", "/health")
    if not isinstance(health, dict) or health.get("status") != "healthy":
        raise LightRAGBuildError(
            "LightRAG service did not report healthy status before import",
            failure_code="SERVICE_UNHEALTHY",
        )
    health_capabilities = parse_lightrag_capabilities(
        health,
        None,
        expected_workspace=service["workspace"],
        requested_embedding_batch_size=service["embedding_batch_size"],
    )
    if health_capabilities.workspace is None:
        raise LightRAGBuildError(
            "LightRAG workspace cannot be confirmed before import",
            failure_code="WORKSPACE_UNCONFIRMED",
        )
    if health_capabilities.workspace_matches is False:
        raise LightRAGBuildError(
            "LightRAG workspace does not match the configured EvoWiki workspace",
            failure_code="WORKSPACE_MISMATCH",
        )
    if health_capabilities.storage_workspaces_match is False:
        raise LightRAGBuildError(
            "LightRAG storage workspace does not match the configured EvoWiki workspace",
            failure_code="STORAGE_WORKSPACE_MISMATCH",
        )
    try:
        openapi = client.request_json("GET", "/openapi.json")
    except LightRAGBuildError as exc:
        raise LightRAGBuildError(
            "LightRAG track-status capability cannot be confirmed before import",
            failure_code="TRACK_STATUS_UNCONFIRMED",
        ) from exc

    capabilities = parse_lightrag_capabilities(
        health,
        openapi,
        expected_workspace=service["workspace"],
        requested_embedding_batch_size=service["embedding_batch_size"],
    )
    if capabilities.supports_track_status is not True:
        raise LightRAGBuildError(
            "LightRAG does not expose a confirmed track-status endpoint",
            failure_code="TRACK_STATUS_UNSUPPORTED",
        )


def poll_lightrag_tracks(
    client: "LightRAGServiceClient",
    track_ids: list[dict[str, Any]],
    *,
    poll_interval_seconds: float,
    poll_timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Wait for all submissions to reach processed, without retaining remote content."""
    if not track_ids:
        return []
    expected_tracks: list[tuple[str, str]] = []
    for entry in track_ids:
        source_path = entry.get("source_path")
        track_id = entry.get("track_id")
        if not isinstance(source_path, str) or not source_path or not isinstance(track_id, str) or not track_id:
            raise LightRAGBuildError(
                "LightRAG import response did not contain a valid track ID",
                failure_code="TRACK_ID_MISSING",
            )
        expected_tracks.append((source_path, track_id))

    deadline = time.monotonic() + poll_timeout_seconds
    while True:
        snapshots: list[tuple[str, TrackSnapshot]] = []
        for source_path, track_id in expected_tracks:
            payload = client.request_json("GET", f"/documents/track_status/{track_id}")
            snapshot = parse_track_status(payload, track_id)
            snapshots.append((source_path, snapshot))

        failed = [snapshot for _, snapshot in snapshots if snapshot.state is RemoteTrackState.FAILED]
        invalid = [snapshot for _, snapshot in snapshots if snapshot.state is RemoteTrackState.INVALID]
        public_snapshots = [
            _public_track_snapshot(source_path, snapshot)
            for source_path, snapshot in snapshots
        ]
        if failed:
            raise LightRAGBuildError(
                "One or more LightRAG tracks reported failed status",
                failure_code="TRACK_FAILED",
                track_status=public_snapshots,
            )
        if invalid:
            raise LightRAGBuildError(
                "One or more LightRAG track-status responses were invalid",
                failure_code="TRACK_STATUS_INVALID",
                track_status=public_snapshots,
            )
        if all(snapshot.state is RemoteTrackState.PROCESSED for _, snapshot in snapshots):
            return public_snapshots

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LightRAGBuildError(
                "Timed out while waiting for LightRAG tracks to finish processing",
                failure_code="TRACK_POLL_TIMEOUT",
                track_status=public_snapshots,
            )
        time.sleep(min(poll_interval_seconds, remaining))


def _public_track_snapshot(source_path: str, snapshot: TrackSnapshot) -> dict[str, Any]:
    """Return only safe aggregate polling facts for reports; never persist remote text/errors."""
    return {
        "source_path": source_path,
        "track_id": snapshot.track_id,
        "state": snapshot.state.value,
        "document_count": snapshot.document_count,
        "status_counts": dict(snapshot.status_counts),
        "total_chunks": snapshot.total_chunks,
        "unknown_statuses": list(snapshot.unknown_statuses),
        "error_code": snapshot.error_code,
    }


class LightRAGServiceClient:
    # [MULTI-WS] 新增 workspace 参数：若提供则每个请求携带 LIGHTRAG-WORKSPACE 头。
    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0, workspace: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.workspace = workspace
        self.timeout = timeout

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request_json("POST", path, payload)

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", **self.headers}
        # [MULTI-WS] 若配置了 workspace，每次请求携带 LIGHTRAG-WORKSPACE 头。
        if self.workspace:
            headers["LIGHTRAG-WORKSPACE"] = self.workspace
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - user-configured service URL
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 409:
                raise LightRAGBuildError(
                    "LightRAG service rejected the request with HTTP 409",
                    failure_code="REMOTE_HTTP_409",
                    http_status=409,
                ) from exc
            raise LightRAGBuildError(
                f"LightRAG service {method} {path} failed with HTTP {exc.code}: {detail}",
                http_status=exc.code,
            ) from exc
        except URLError as exc:
            raise LightRAGBuildError(f"Cannot reach LightRAG service at {self.base_url}: {exc.reason}") from exc
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LightRAGBuildError(f"LightRAG service {method} {path} returned non-JSON response: {body[:200]}") from exc


def resolve_lightrag_service_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    base_url = os.environ.get("LIGHTRAG_BASE_URL") or cfg.get("base_url")
    if not base_url or "YOUR_LIGHTRAG_SERVER" in str(base_url):
        raise LightRAGBuildError(
            "LightRAG base_url is required. Create `lightrag-config.json` from "
            "`lightrag-config.example.json` and set `base_url`, or set LIGHTRAG_BASE_URL."
        )
    # [MULTI-WS] 优先从环境变量读取 workspace，否则从配置文件取，并校验格式。
    workspace = os.environ.get("LIGHTRAG_WORKSPACE") or cfg.get("workspace") or ""
    if not isinstance(workspace, str) or not WORKSPACE_PATTERN.fullmatch(workspace):
        raise LightRAGBuildError(
            "lightrag.workspace is required and must contain only letters, numbers, and underscores"
        )
    api_key_env = cfg.get("api_key_env", "LIGHTRAG_API_KEY")
    bearer_token_env = cfg.get("bearer_token_env", "LIGHTRAG_BEARER_TOKEN")
    api_key = os.environ.get(api_key_env) or cfg.get("api_key")
    bearer_token = os.environ.get(bearer_token_env) or cfg.get("bearer_token")
    timeout_seconds = float(cfg.get("timeout_seconds", 30))
    sync_config = cfg.get("sync") or {}
    if not isinstance(sync_config, dict):
        raise LightRAGBuildError("lightrag.sync must be an object")
    poll_interval_seconds = _bounded_float(
        sync_config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS),
        name="sync.poll_interval_seconds",
        minimum=MIN_POLL_INTERVAL_SECONDS,
        maximum=MAX_POLL_INTERVAL_SECONDS,
    )
    poll_timeout_seconds = _bounded_float(
        sync_config.get("poll_timeout_seconds", DEFAULT_POLL_TIMEOUT_SECONDS),
        name="sync.poll_timeout_seconds",
        minimum=MIN_POLL_TIMEOUT_SECONDS,
        maximum=MAX_POLL_TIMEOUT_SECONDS,
    )
    embedding_config = cfg.get("embedding") or {}
    raw_batch_size = os.environ.get("LIGHTRAG_EMBEDDING_BATCH_SIZE")
    if raw_batch_size is None:
        raw_batch_size = embedding_config.get("batch_size", DEFAULT_EMBEDDING_BATCH_SIZE)
    try:
        embedding_batch_size = int(raw_batch_size)
    except (TypeError, ValueError) as exc:
        raise LightRAGBuildError("embedding.batch_size must be an integer") from exc
    if not 1 <= embedding_batch_size <= MAX_EMBEDDING_BATCH_SIZE:
        raise LightRAGBuildError(
            f"embedding.batch_size must be between 1 and {MAX_EMBEDDING_BATCH_SIZE}"
        )

    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    public = {
        "mode": "service",
        "base_url": base_url.rstrip("/"),
        # [MULTI-WS] 将 workspace 信息写入 public 摘要，便于调试和报告。
        "workspace": workspace,
        "api_key_env": api_key_env,
        "bearer_token_env": bearer_token_env,
        "auth": {
            "api_key_configured": bool(api_key),
            "bearer_token_configured": bool(bearer_token),
        },
        "embedding": {
            "batch_size": embedding_batch_size,
            "max_supported_batch_size": MAX_EMBEDDING_BATCH_SIZE,
            "scope": "client_expectation_only",
        },
        "sync": {
            "poll_interval_seconds": poll_interval_seconds,
            "poll_timeout_seconds": poll_timeout_seconds,
        },
    }
    return {
        "base_url": public["base_url"],
        "workspace": workspace or None,
        "headers": headers,
        "timeout_seconds": timeout_seconds,
        "embedding_batch_size": embedding_batch_size,
        "poll_interval_seconds": poll_interval_seconds,
        "poll_timeout_seconds": poll_timeout_seconds,
        "workspace": workspace,
        "public": public,
    }


def _bounded_float(value: Any, *, name: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise LightRAGBuildError(f"{name} must be a number") from exc
    if not minimum <= result <= maximum:
        raise LightRAGBuildError(f"{name} must be between {minimum:g} and {maximum:g}")
    return result


def base_report(
    status: str,
    input_manifest: dict,
    imported: list[str],
    skipped: list[str],
    error: str | None,
    *,
    deleted: list[str] | None = None,
    service: dict[str, Any] | None = None,
) -> dict:
    deleted = deleted or []
    report = {
        "status": status,
        "generated_at": utc_now(),
        "input": input_manifest,
        "imported": imported,
        "skipped_unchanged": skipped,
        # H1：删除无法被 LightRAG 增量安全清除，发现删除即要求全量重建。
        "requires_rebuild": bool(deleted),
        "deleted_pending_rebuild": deleted,
        "error": error,
    }
    if service is not None:
        report["service"] = service
    return report


def read_text_for_lightrag(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def stable_doc_id(path: str) -> str:
    return path.replace("/", "__").replace(" ", "_")


def normalize_lightrag_references(value: Any) -> list[dict[str, Any]]:
    """Normalize LightRAG reference content to a predictable list of strings."""
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        reference = dict(item)
        content = reference.get("content")
        if isinstance(content, str):
            reference["content"] = [content] if content else []
        elif isinstance(content, list):
            reference["content"] = [part for part in content if isinstance(part, str) and part]
        else:
            reference["content"] = []
        normalized.append(reference)
    return normalized


def parse_lightrag_capabilities(
    health: dict[str, Any],
    openapi: dict[str, Any] | None,
    *,
    expected_workspace: str,
    requested_embedding_batch_size: int,
) -> RagCapabilities:
    """Parse capability facts without inferring support from version numbers."""
    configuration = health.get("configuration")
    authenticated_health = isinstance(configuration, dict)
    config = configuration if authenticated_health else {}
    remote_workspace = _optional_string(config.get("workspace"))
    workspace_matches = (
        remote_workspace == expected_workspace
        if remote_workspace is not None
        else None
    )
    storage_workspaces = _parse_storage_workspaces(config.get("storage_workspaces"))
    storage_workspace_values = (
        [value for value in storage_workspaces.values() if value is not None]
        if storage_workspaces is not None
        else []
    )
    storage_workspaces_match = (
        all(value == expected_workspace for value in storage_workspace_values)
        if storage_workspace_values
        else None
    )

    remote_batch = config.get("embedding_batch_num")
    if not isinstance(remote_batch, int):
        remote_batch = None
    batch_matches = (
        remote_batch == requested_embedding_batch_size
        if remote_batch is not None
        else None
    )

    openapi_available = isinstance(openapi, dict)
    schemas = openapi.get("components", {}).get("schemas", {}) if openapi_available else {}
    query_schema = schemas.get("QueryRequest", {}) if isinstance(schemas, dict) else {}
    query_properties = query_schema.get("properties", {}) if isinstance(query_schema, dict) else {}
    paths = openapi.get("paths", {}) if openapi_available else {}
    if not isinstance(paths, dict):
        paths = {}

    supports_chunk_content = (
        "include_chunk_content" in query_properties
        if openapi_available and isinstance(query_properties, dict)
        else None
    )
    supports_conversation_history = (
        "conversation_history" in query_properties
        if openapi_available and isinstance(query_properties, dict)
        else None
    )
    mode_schema = (
        query_properties.get("mode", {})
        if isinstance(query_properties, dict)
        else {}
    )
    mode_values = _openapi_enum_values(mode_schema, schemas)
    supports_bypass = (
        "bypass" in mode_values
        if isinstance(mode_values, list)
        else None
    )
    supports_graph_subgraph = (
        any(
            path.rstrip("/") == "/graphs"
            and isinstance(methods, dict)
            and "get" in methods
            for path, methods in paths.items()
        )
        if openapi_available
        else None
    )
    supports_track_status = (
        any(path.rstrip("/").endswith("/documents/track_status/{track_id}") for path in paths)
        if openapi_available
        else None
    )
    supports_document_delete = (
        any(
            path.rstrip("/").endswith("/documents/delete_document")
            and isinstance(methods, dict)
            and "delete" in methods
            for path, methods in paths.items()
        )
        if openapi_available
        else None
    )
    supports_document_inventory = (
        any(
            path.rstrip("/").endswith("/documents/paginated")
            and isinstance(methods, dict)
            and "post" in methods
            for path, methods in paths.items()
        )
        if openapi_available
        else None
    )
    supports_pipeline_status = (
        any(
            path.rstrip("/").endswith("/documents/pipeline_status")
            and isinstance(methods, dict)
            and "get" in methods
            for path, methods in paths.items()
        )
        if openapi_available
        else None
    )

    return RagCapabilities(
        core_version=_optional_string(health.get("core_version")),
        api_version=_optional_string(health.get("api_version")),
        authenticated_health=authenticated_health,
        openapi_available=openapi_available,
        expected_workspace=expected_workspace,
        workspace=remote_workspace,
        workspace_matches=workspace_matches,
        storage_workspaces=storage_workspaces,
        storage_workspaces_available=(
            bool(storage_workspace_values)
            if storage_workspaces is not None
            else None
        ),
        storage_workspaces_match=storage_workspaces_match,
        requested_embedding_batch_size=requested_embedding_batch_size,
        remote_embedding_batch_size=remote_batch,
        embedding_batch_matches=batch_matches,
        rerank_enabled=config.get("enable_rerank") if isinstance(config.get("enable_rerank"), bool) else None,
        parser_routing_available=(
            bool(config.get("parser_routing"))
            if "parser_routing" in config
            else None
        ),
        supports_chunk_content=supports_chunk_content,
        supports_conversation_history=supports_conversation_history,
        supports_bypass=supports_bypass,
        supports_graph_subgraph=supports_graph_subgraph,
        supports_track_status=supports_track_status,
        supports_document_delete=supports_document_delete,
        supports_document_inventory=supports_document_inventory,
        supports_pipeline_status=supports_pipeline_status,
    )


def probe_lightrag_service(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Perform bounded, read-only health and OpenAPI capability discovery."""
    try:
        service = resolve_lightrag_service_config(config)
        timeout = min(float(service["timeout_seconds"]), 5.0)
        client = LightRAGServiceClient(
            service["base_url"],
            headers=service["headers"],
            timeout=timeout,
        )
        health = client.request_json("GET", "/health")
    except (LightRAGBuildError, TypeError, ValueError):
        return {
            "name": "lightrag_service",
            "status": "failed",
            "detail": "LightRAG health probe failed; verify base_url, service availability, and authentication",
        }
    if not isinstance(health, dict):
        return {
            "name": "lightrag_service",
            "status": "failed",
            "detail": "LightRAG health probe returned an invalid JSON shape",
        }

    openapi = None
    try:
        openapi = client.request_json("GET", "/openapi.json")
    except LightRAGBuildError:
        pass

    capabilities = parse_lightrag_capabilities(
        health,
        openapi,
        expected_workspace=service["workspace"],
        requested_embedding_batch_size=service["embedding_batch_size"],
    )
    facts = asdict(capabilities)
    warnings = []
    if health.get("status") != "healthy":
        return {
            "name": "lightrag_service",
            "status": "failed",
            "detail": "LightRAG responded but did not report healthy status",
            "capabilities": facts,
        }
    if capabilities.workspace is None:
        return {
            "name": "lightrag_service",
            "status": "failed",
            "failure_code": "WORKSPACE_UNCONFIRMED",
            "detail": "LightRAG is healthy but its configured workspace cannot be confirmed",
            "capabilities": facts,
        }
    if capabilities.workspace_matches is False:
        return {
            "name": "lightrag_service",
            "status": "failed",
            "failure_code": "WORKSPACE_MISMATCH",
            "detail": "LightRAG workspace does not match the configured EvoWiki workspace",
            "capabilities": facts,
        }
    if capabilities.storage_workspaces_match is False:
        return {
            "name": "lightrag_service",
            "status": "failed",
            "failure_code": "STORAGE_WORKSPACE_MISMATCH",
            "detail": "One or more LightRAG storage workspaces do not match the configured EvoWiki workspace",
            "capabilities": facts,
        }
    if not capabilities.authenticated_health:
        warnings.append("authenticated_health_unavailable")
    if not capabilities.openapi_available:
        warnings.append("openapi_unavailable")
    if capabilities.storage_workspaces_available is None:
        warnings.append("storage_workspaces_unknown")
    elif capabilities.storage_workspaces_match is None:
        warnings.append("storage_workspaces_unconfirmed")
    if capabilities.remote_embedding_batch_size is None:
        warnings.append("remote_embedding_batch_unknown")
    elif capabilities.embedding_batch_matches is False:
        warnings.append("embedding_batch_mismatch")
    for field in (
        "supports_chunk_content",
        "supports_conversation_history",
        "supports_bypass",
        "supports_graph_subgraph",
        "supports_track_status",
        "supports_document_delete",
        "supports_document_inventory",
        "supports_pipeline_status",
    ):
        if getattr(capabilities, field) is not True:
            warnings.append(field)

    return {
        "name": "lightrag_service",
        "status": "warning" if warnings else "ok",
        "detail": "service healthy" if not warnings else "service healthy; capabilities need attention",
        "warnings": warnings,
        "capabilities": facts,
    }


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _openapi_enum_values(
    schema: Any,
    schemas: Any,
    *,
    seen: set[str] | None = None,
) -> list[Any] | None:
    """Resolve the small OpenAPI subset used for Pydantic enum/Literal fields."""
    if not isinstance(schema, dict):
        return None
    values = schema.get("enum")
    if isinstance(values, list):
        return values
    reference = schema.get("$ref")
    if (
        isinstance(reference, str)
        and reference.startswith("#/components/schemas/")
        and isinstance(schemas, dict)
    ):
        name = reference.rsplit("/", 1)[-1]
        visited = set() if seen is None else set(seen)
        if name in visited:
            return None
        visited.add(name)
        return _openapi_enum_values(
            schemas.get(name),
            schemas,
            seen=visited,
        )
    for combinator in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(combinator)
        if not isinstance(branches, list):
            continue
        combined: list[Any] = []
        found = False
        for branch in branches:
            branch_values = _openapi_enum_values(
                branch,
                schemas,
                seen=seen,
            )
            if branch_values is not None:
                found = True
                combined.extend(branch_values)
        if found:
            return list(dict.fromkeys(combined))
    return None


def _parse_storage_workspaces(value: Any) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: _optional_string(workspace)
        for key, workspace in value.items()
        if isinstance(key, str)
    }


def doctor_lightrag(
    paths: ProjectPaths,
    config: dict[str, Any] | None = None,
    *,
    check_service: bool = False,
) -> dict:
    """检查本地项目；按需对 LightRAG 执行有时限的只读探测。

    不打印 secret，只输出是否配置；默认不请求远端服务。
    """
    checks: list[dict[str, Any]] = []

    # 1. project_root
    if paths.root.is_dir():
        checks.append({"name": "project_root", "status": "ok", "detail": "root exists"})
    else:
        checks.append({"name": "project_root", "status": "failed", "detail": f"root not found: {paths.root}"})

    # 2. corpus/raw —— 缺失只警告，不失败
    corpus_raw = paths.corpus / "raw"
    if corpus_raw.is_dir():
        checks.append({"name": "corpus_raw", "status": "ok", "detail": "corpus/raw exists"})
    else:
        checks.append({"name": "corpus_raw", "status": "warning", "detail": "corpus/raw missing; add source files before running lanes"})

    # 3. artifacts/lightrag/* 目录（paths.ensure_base_dirs 已在调用前执行）
    artifacts_dirs = [
        ("artifacts/lightrag/input", paths.lightrag_input),
        ("artifacts/lightrag/reports", paths.lightrag_reports),
        ("artifacts/lightrag/state", paths.lightrag_state),
        ("artifacts/lightrag/queries", paths.lightrag_queries),
    ]
    missing_dirs: list[str] = []
    for label, dir_path in artifacts_dirs:
        if not dir_path.is_dir():
            missing_dirs.append(label)
    if missing_dirs:
        checks.append({"name": "artifacts_dirs", "status": "failed", "detail": f"missing: {', '.join(missing_dirs)}"})
    else:
        checks.append({"name": "artifacts_dirs", "status": "ok", "detail": "all lightrag artifact dirs present"})

    # 4. lightrag_config —— 复用 resolve_lightrag_service_config
    cfg = config or {}
    base_url = os.environ.get("LIGHTRAG_BASE_URL") or cfg.get("base_url")
    api_key_env = cfg.get("api_key_env", "LIGHTRAG_API_KEY")
    bearer_token_env = cfg.get("bearer_token_env", "LIGHTRAG_BEARER_TOKEN")
    api_key = os.environ.get(api_key_env) or cfg.get("api_key")
    bearer_token = os.environ.get(bearer_token_env) or cfg.get("bearer_token")
    auth = {
        "api_key_configured": bool(api_key),
        "bearer_token_configured": bool(bearer_token),
    }
    if not base_url or "YOUR_LIGHTRAG_SERVER" in str(base_url):
        checks.append(
            {
                "name": "lightrag_config",
                "status": "failed",
                "detail": "LightRAG base_url is required. Create lightrag-config.json from lightrag-config.example.json and set base_url, or set LIGHTRAG_BASE_URL.",
                "auth": auth,
            }
        )
    else:
        try:
            resolved = resolve_lightrag_service_config(config)
        except (LightRAGBuildError, TypeError, ValueError):
            checks.append(
                {
                    "name": "lightrag_config",
                    "status": "failed",
                    "detail": "LightRAG configuration is invalid; check workspace, timeout, and embedding.batch_size",
                    "auth": auth,
                }
            )
        else:
            checks.append(
                {
                    "name": "lightrag_config",
                    "status": "ok",
                    "detail": "base_url and local constraints are valid",
                    "auth": auth,
                    "workspace": resolved["workspace"],
                    "embedding": resolved["public"]["embedding"],
                }
            )

    # 5. lightrag_input —— documents.jsonl 不存在只警告
    documents_path = paths.lightrag_input / "documents.jsonl"
    if documents_path.is_file():
        checks.append({"name": "lightrag_input", "status": "ok", "detail": "documents.jsonl present"})
    else:
        checks.append(
            {
                "name": "lightrag_input",
                "status": "warning",
                "detail": "documents.jsonl missing; run `evo-wiki prepare-lightrag --root <root>` first",
            }
        )

    if check_service:
        checks.append(probe_lightrag_service(config))

    status = "failed" if any(c["status"] == "failed" for c in checks) else "ok"
    return {"status": status, "checks": checks}

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from pydantic import ValidationError

from ...lightrag_lane import (
    LightRAGBuildError,
    LightRAGServiceClient,
    parse_lightrag_capabilities,
    resolve_lightrag_service_config,
)
from ...paths import ProjectPaths
from ...utils import read_json, relpath, write_json_atomic
from .contracts import (
    ContentUnit,
    EvidenceChunk,
    EvidenceSubgraph,
    EvidenceSubgraphSettings,
    GraphEdge,
    GraphNode,
    GraphResponse,
    MAX_CONTENT_UNITS_HARD_LIMIT,
    MAX_EDGES_HARD_LIMIT,
    MAX_NODES_HARD_LIMIT,
    MAX_TIMEOUT_SECONDS_HARD_LIMIT,
    MAX_TOP_K_HARD_LIMIT,
    RetrievalPlan,
    RetrievalTrace,
    SKILL_VERSION,
)


SKILL_ID = "evidence-subgraph"
GRAPH_FIELD_SEPARATOR = "<SEP>"
_LATIN_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_CJK_SPAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])")
_UNKNOWN_FILE_VALUES = {"", "unknown_source", "unknown", "-", "#"}


class EvidenceSubgraphError(RuntimeError):
    """Fail-closed retrieval error with a safe code and trace location."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str,
        trace_path: str | None = None,
    ):
        super().__init__(message)
        self.failure_code = failure_code
        self.trace_path = trace_path


def retrieve_evidence_subgraph(
    paths: ProjectPaths,
    project_config: dict[str, Any],
    *,
    query: str,
    seeds: list[str],
    max_depth: int | None = None,
    max_nodes: int | None = None,
    max_edges: int | None = None,
    max_content_units: int | None = None,
    top_k: int | None = None,
    timeout_seconds: float | None = None,
    explain_retrieval: bool = False,
) -> dict[str, Any]:
    """Run retrieval-only evidence-subgraph mode and persist a redacted trace."""
    run_id = _new_run_id()
    trace_path = (
        paths.lightrag_queries
        / "evidence-subgraph-traces"
        / f"{run_id}.json"
    )
    started = time.monotonic()
    query_sha256 = _sha256(query)
    safe_seeds = tuple(seed.strip() for seed in seeds if isinstance(seed, str) and seed.strip())
    trace_state: dict[str, Any] = {
        "workspace": None,
        "max_depth": None,
        "max_nodes_budget": None,
        "max_edges_budget": None,
        "max_content_units_budget": None,
        "timeout_seconds_budget": None,
        "subgraph_sha256": None,
        "subgraph_nodes": 0,
        "subgraph_edges": 0,
        "corpus_content_units": 0,
        "allowed_content_units": 0,
        "candidate_reduction_ratio": None,
        "evidence_ids": (),
        "evidence_scores": (),
        "evidence_hashes": (),
    }

    try:
        if not isinstance(query, str) or len(query.strip()) < 3:
            raise EvidenceSubgraphError(
                "query must contain at least three non-whitespace characters",
                failure_code="QUERY_INVALID",
            )
        if not safe_seeds:
            raise EvidenceSubgraphError(
                "at least one explicit seed is required",
                failure_code="SEED_REQUIRED",
            )
        if len(set(safe_seeds)) != len(safe_seeds):
            safe_seeds = tuple(dict.fromkeys(safe_seeds))

        retrieval_config = project_config.get("retrieval", {})
        if not isinstance(retrieval_config, dict):
            raise ValueError("retrieval configuration must be an object")
        lightrag_config = project_config.get("lightrag", {})
        if not isinstance(lightrag_config, dict):
            raise ValueError("lightrag configuration must be an object")
        settings = _load_settings(
            retrieval_config.get("evidence_subgraph", {}),
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_content_units=max_content_units,
            top_k=top_k,
            timeout_seconds=timeout_seconds,
        )
        deadline = started + settings.timeout_seconds
        trace_state.update(
            {
                "max_depth": settings.max_depth,
                "max_nodes_budget": settings.max_nodes,
                "max_edges_budget": settings.max_edges,
                "max_content_units_budget": settings.max_content_units,
                "timeout_seconds_budget": settings.timeout_seconds,
            }
        )
        _validate_skill_manifest()
        service = resolve_lightrag_service_config(lightrag_config)
        trace_state["workspace"] = service["workspace"]
        plan = RetrievalPlan(
            workspace=service["workspace"],
            seeds=safe_seeds,
            max_depth=settings.max_depth,
            max_nodes=settings.max_nodes,
            max_edges=settings.max_edges,
            max_content_units=settings.max_content_units,
            top_k=settings.top_k,
            timeout_seconds=settings.timeout_seconds,
        )
        client = LightRAGServiceClient(
            service["base_url"],
            headers=service["headers"],
            timeout=min(service["timeout_seconds"], settings.timeout_seconds),
        )
        _preflight_query(client, service, deadline=deadline)

        all_units, units_by_basename = _load_projection(
            paths,
            expected_workspace=service["workspace"],
            settings=settings,
            deadline=deadline,
        )
        trace_state["corpus_content_units"] = len(all_units)
        subgraph = _fetch_and_merge_subgraphs(client, plan, deadline=deadline)
        trace_state.update(
            {
                "subgraph_sha256": subgraph.subgraph_sha256,
                "subgraph_nodes": len(subgraph.nodes),
                "subgraph_edges": len(subgraph.edges),
            }
        )
        allowed_units, mapped_sources = _resolve_allowed_units(
            subgraph,
            units_by_basename,
            max_content_units=settings.max_content_units,
            deadline=deadline,
        )
        trace_state["allowed_content_units"] = len(allowed_units)
        reduction_ratio = 1.0 - (len(allowed_units) / len(all_units))
        trace_state["candidate_reduction_ratio"] = reduction_ratio

        evidence = _bm25_retrieve(
            query.strip(),
            allowed_units,
            settings.top_k,
            deadline=deadline,
        )
        allowed_ids = {unit.content_unit_id for unit in allowed_units}
        if any(item.content_unit_id not in allowed_ids for item in evidence):
            raise EvidenceSubgraphError(
                "retriever returned evidence outside the planned allow-list",
                failure_code="SCOPE_VIOLATION",
            )
        if not evidence:
            raise EvidenceSubgraphError(
                "no positive-scoring evidence was found inside the planned scope",
                failure_code="NO_EVIDENCE",
            )
        trace_state.update(
            {
                "evidence_ids": tuple(item.content_unit_id for item in evidence),
                "evidence_scores": tuple(round(item.score, 8) for item in evidence),
                "evidence_hashes": tuple(item.content_sha256 for item in evidence),
            }
        )
        trace = _write_trace(
            trace_path,
            run_id=run_id,
            status="success",
            query_sha256=query_sha256,
            seeds=safe_seeds,
            trace_state=trace_state,
            failure_code=None,
            started=started,
        )
        output: dict[str, Any] = {
            "schema_version": 1,
            "status": "success",
            "mode": "retrieval_only",
            "skill": {"id": SKILL_ID, "version": SKILL_VERSION},
            "generation_enabled": False,
            "scope_granularity": plan.scope_granularity,
            "scope": {
                "corpus_content_units": len(all_units),
                "allowed_content_units": len(allowed_units),
                "candidate_reduction_ratio": round(reduction_ratio, 8),
                "scope_reduced": reduction_ratio > 0,
                "mapped_sources": sorted(mapped_sources),
                "out_of_scope_evidence": 0,
            },
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "trace_path": relpath(trace_path, paths.root),
        }
        if explain_retrieval:
            output["retrieval_plan"] = plan.model_dump(mode="json")
            output["subgraph"] = {
                "sha256": subgraph.subgraph_sha256,
                "node_count": len(subgraph.nodes),
                "edge_count": len(subgraph.edges),
                "is_truncated": subgraph.is_truncated,
            }
            output["trace"] = trace.model_dump(mode="json")
        return output
    except EvidenceSubgraphError as exc:
        _write_trace(
            trace_path,
            run_id=run_id,
            status="failed",
            query_sha256=query_sha256,
            seeds=safe_seeds,
            trace_state=trace_state,
            failure_code=exc.failure_code,
            started=started,
        )
        exc.trace_path = relpath(trace_path, paths.root)
        raise
    except (LightRAGBuildError, ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        code = (
            "CONFIG_INVALID"
            if isinstance(exc, (ValidationError, ValueError, TypeError))
            else "SERVICE_OR_DATA_INVALID"
        )
        _write_trace(
            trace_path,
            run_id=run_id,
            status="failed",
            query_sha256=query_sha256,
            seeds=safe_seeds,
            trace_state=trace_state,
            failure_code=code,
            started=started,
        )
        raise EvidenceSubgraphError(
            "evidence-subgraph retrieval failed closed",
            failure_code=code,
            trace_path=relpath(trace_path, paths.root),
        ) from exc
    except Exception as exc:
        _write_trace(
            trace_path,
            run_id=run_id,
            status="failed",
            query_sha256=query_sha256,
            seeds=safe_seeds,
            trace_state=trace_state,
            failure_code="INTERNAL_ERROR",
            started=started,
        )
        raise EvidenceSubgraphError(
            "evidence-subgraph retrieval failed closed",
            failure_code="INTERNAL_ERROR",
            trace_path=relpath(trace_path, paths.root),
        ) from exc


def _load_settings(
    raw: object,
    *,
    max_depth: int | None,
    max_nodes: int | None,
    max_edges: int | None,
    max_content_units: int | None,
    top_k: int | None,
    timeout_seconds: float | None,
) -> EvidenceSubgraphSettings:
    if not isinstance(raw, dict):
        raise ValueError("retrieval.evidence_subgraph must be an object")
    values = dict(raw)
    defaults = EvidenceSubgraphSettings()
    integer_fields = (
        "max_depth",
        "max_nodes",
        "max_edges",
        "max_content_units",
        "top_k",
        "target_chars",
        "overlap_chars",
    )
    for key in integer_fields:
        configured = values.get(key, getattr(defaults, key))
        if isinstance(configured, bool) or not isinstance(configured, int):
            raise ValueError(f"{key} must be an integer")
    configured_timeout = values.get("timeout_seconds", defaults.timeout_seconds)
    if isinstance(configured_timeout, bool) or not isinstance(
        configured_timeout,
        (int, float),
    ):
        raise ValueError("timeout_seconds must be a number")
    overrides = {
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "max_edges": max_edges,
        "max_content_units": max_content_units,
        "top_k": top_k,
        "timeout_seconds": timeout_seconds,
    }
    for key, value in overrides.items():
        if value is not None:
            if key == "timeout_seconds":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("CLI timeout_seconds must be a number")
            elif isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"CLI {key} must be an integer")
            configured = values.get(key, getattr(defaults, key))
            if key != "max_depth" and value > configured:
                raise ValueError(f"CLI {key} may lower but not raise the configured limit")
            values[key] = value
    return EvidenceSubgraphSettings.model_validate(values)


def _validate_skill_manifest() -> None:
    resource = files(__package__).joinpath("skill.json")
    manifest = json.loads(resource.read_text(encoding="utf-8"))
    if manifest.get("id") != SKILL_ID or manifest.get("version") != SKILL_VERSION:
        raise ValueError("evidence-subgraph skill manifest identity is invalid")
    if manifest.get("mode") != "retrieval_only":
        raise ValueError("evidence-subgraph skill must remain retrieval-only")
    if manifest.get("depth_policy") != {"minimum": 1, "maximum": None}:
        raise ValueError("evidence-subgraph depth policy must have no fixed upper bound")
    if manifest.get("hard_limits") != {
        "max_nodes": MAX_NODES_HARD_LIMIT,
        "max_edges": MAX_EDGES_HARD_LIMIT,
        "max_content_units": MAX_CONTENT_UNITS_HARD_LIMIT,
        "top_k": MAX_TOP_K_HARD_LIMIT,
        "timeout_seconds": int(MAX_TIMEOUT_SECONDS_HARD_LIMIT),
    }:
        raise ValueError("evidence-subgraph resource limits are invalid")
    if manifest.get("fallback") != []:
        raise ValueError("evidence-subgraph skill cannot declare a fallback")
    security = manifest.get("security")
    if not isinstance(security, dict):
        raise ValueError("evidence-subgraph security manifest is missing")
    if security.get("deny_unbounded_global_search") is not True:
        raise ValueError("unbounded global search must remain disabled")
    if security.get("generation_enabled") is not False:
        raise ValueError("generation must remain disabled")


def _preflight_query(
    client: LightRAGServiceClient,
    service: dict[str, Any],
    *,
    deadline: float | None = None,
) -> None:
    health = _request_json_with_budget(client, "GET", "/health", deadline=deadline)
    if not isinstance(health, dict) or health.get("status") != "healthy":
        raise EvidenceSubgraphError(
            "LightRAG service did not report healthy status",
            failure_code="SERVICE_UNHEALTHY",
        )
    capabilities = parse_lightrag_capabilities(
        health,
        None,
        expected_workspace=service["workspace"],
        requested_embedding_batch_size=service["embedding_batch_size"],
    )
    if capabilities.workspace is None:
        raise EvidenceSubgraphError(
            "LightRAG workspace cannot be confirmed",
            failure_code="WORKSPACE_UNCONFIRMED",
        )
    if capabilities.workspace_matches is False:
        raise EvidenceSubgraphError(
            "LightRAG workspace does not match EvoWiki configuration",
            failure_code="WORKSPACE_MISMATCH",
        )
    if capabilities.storage_workspaces_match is False:
        raise EvidenceSubgraphError(
            "LightRAG storage workspace does not match EvoWiki configuration",
            failure_code="STORAGE_WORKSPACE_MISMATCH",
        )
    openapi = _request_json_with_budget(
        client,
        "GET",
        "/openapi.json",
        deadline=deadline,
    )
    paths = openapi.get("paths") if isinstance(openapi, dict) else None
    graph_methods = paths.get("/graphs") if isinstance(paths, dict) else None
    if not isinstance(graph_methods, dict) or "get" not in graph_methods:
        raise EvidenceSubgraphError(
            "LightRAG graph-subgraph capability is unavailable",
            failure_code="GRAPH_SUBGRAPH_UNSUPPORTED",
        )


def _fetch_and_merge_subgraphs(
    client: LightRAGServiceClient,
    plan: RetrievalPlan,
    *,
    deadline: float | None = None,
) -> EvidenceSubgraph:
    node_candidates: dict[str, tuple[GraphNode, int, int]] = {}
    edge_candidates: dict[tuple[str, str, str | None], GraphEdge] = {}
    for seed_index, seed in enumerate(plan.seeds):
        query_string = urlencode(
            {
                "label": seed,
                "max_depth": plan.max_depth,
                "max_nodes": plan.max_nodes,
            }
        )
        payload = _request_json_with_budget(
            client,
            "GET",
            f"/graphs?{query_string}",
            deadline=deadline,
        )
        try:
            response = GraphResponse.model_validate(payload)
        except ValidationError as exc:
            raise EvidenceSubgraphError(
                "LightRAG returned an invalid graph response",
                failure_code="SUBGRAPH_RESPONSE_INVALID",
            ) from exc
        if response.is_truncated:
            raise EvidenceSubgraphError(
                "LightRAG subgraph reached a configured resource budget",
                failure_code="GRAPH_BUDGET_EXCEEDED",
            )
        if len(response.nodes) > plan.max_nodes:
            raise EvidenceSubgraphError(
                "LightRAG subgraph exceeds the configured node budget",
                failure_code="GRAPH_BUDGET_EXCEEDED",
            )
        if len(response.edges) > plan.max_edges:
            raise EvidenceSubgraphError(
                "LightRAG subgraph exceeds the configured edge budget",
                failure_code="GRAPH_BUDGET_EXCEEDED",
            )
        if not response.nodes:
            raise EvidenceSubgraphError(
                "LightRAG returned an empty subgraph",
                failure_code="SUBGRAPH_EMPTY",
            )
        distances = _graph_distances(response, seed)
        if not distances:
            raise EvidenceSubgraphError(
                "the requested seed was not present in the returned subgraph",
                failure_code="SEED_NOT_FOUND",
            )
        nodes_by_id = {node.id: node for node in response.nodes}
        for node_id, distance in distances.items():
            if distance > plan.max_depth:
                continue
            candidate = (nodes_by_id[node_id], distance, seed_index)
            existing = node_candidates.get(node_id)
            if existing is None or (distance, seed_index, node_id) < (
                existing[1],
                existing[2],
                node_id,
            ):
                node_candidates[node_id] = candidate
        if len(node_candidates) > plan.max_nodes:
            raise EvidenceSubgraphError(
                "merged subgraph exceeds the configured node budget",
                failure_code="GRAPH_BUDGET_EXCEEDED",
            )
        for edge in response.edges:
            edge_candidates[(edge.source, edge.target, edge.type)] = edge
        if len(edge_candidates) > plan.max_edges:
            raise EvidenceSubgraphError(
                "merged subgraph exceeds the configured edge budget",
                failure_code="GRAPH_BUDGET_EXCEEDED",
            )

    ordered = sorted(
        node_candidates.values(),
        key=lambda item: (item[1], item[2], item[0].id),
    )
    selected = ordered
    selected_ids = {node.id for node, _, _ in selected}
    nodes = tuple(node for node, _, _ in selected)
    distances = {node.id: distance for node, distance, _ in selected}
    edges = tuple(
        sorted(
            (
                edge
                for edge in edge_candidates.values()
                if edge.source in selected_ids and edge.target in selected_ids
            ),
            key=lambda edge: (edge.source, edge.target, edge.type or "", edge.id),
        )
    )
    if not nodes:
        raise EvidenceSubgraphError(
            "bounded subgraph selection produced no nodes",
            failure_code="SUBGRAPH_EMPTY",
        )
    try:
        digest_payload = {
            "seeds": plan.seeds,
            "nodes": [
                {
                    "id": node.id,
                    "labels": node.labels,
                    "file_path": _split_graph_field(node.properties.get("file_path")),
                    "source_id": _split_graph_field(node.properties.get("source_id")),
                }
                for node in nodes
            ],
            "edges": [
                {
                    "id": edge.id,
                    "type": edge.type,
                    "source": edge.source,
                    "target": edge.target,
                    "file_path": _split_graph_field(edge.properties.get("file_path")),
                    "source_id": _split_graph_field(edge.properties.get("source_id")),
                }
                for edge in edges
            ],
        }
    except ValueError as exc:
        raise EvidenceSubgraphError(
            "LightRAG graph source fields are invalid",
            failure_code="SUBGRAPH_RESPONSE_INVALID",
        ) from exc
    subgraph_sha256 = _sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return EvidenceSubgraph(
        seeds=plan.seeds,
        nodes=nodes,
        edges=edges,
        distances=distances,
        is_truncated=False,
        subgraph_sha256=subgraph_sha256,
    )


def _graph_distances(response: GraphResponse, seed: str) -> dict[str, int]:
    normalized_seed = seed.casefold()
    starts = [
        node.id
        for node in response.nodes
        if normalized_seed == node.id.casefold()
        or any(normalized_seed == label.casefold() for label in node.labels)
    ]
    if not starts:
        starts = [
            node.id
            for node in response.nodes
            if normalized_seed in node.id.casefold()
            or any(normalized_seed in label.casefold() for label in node.labels)
        ]
    if not starts:
        return {}
    adjacency: dict[str, set[str]] = defaultdict(set)
    node_ids = {node.id for node in response.nodes}
    for edge in response.edges:
        if edge.source in node_ids and edge.target in node_ids:
            adjacency[edge.source].add(edge.target)
            adjacency[edge.target].add(edge.source)
    distances = {node_id: 0 for node_id in starts}
    queue = deque(sorted(starts))
    while queue:
        current = queue.popleft()
        for neighbor in sorted(adjacency[current]):
            if neighbor not in distances:
                distances[neighbor] = distances[current] + 1
                queue.append(neighbor)
    return distances


def _load_projection(
    paths: ProjectPaths,
    *,
    expected_workspace: str,
    settings: EvidenceSubgraphSettings,
    deadline: float | None = None,
) -> tuple[list[ContentUnit], dict[str, list[ContentUnit]]]:
    _check_budget(deadline)
    documents_path = paths.lightrag_input / "documents.jsonl"
    ledger_path = paths.lightrag_state / "lightrag-import-ledger.json"
    if not documents_path.exists() or not ledger_path.exists():
        raise EvidenceSubgraphError(
            "prepared documents and a successful import ledger are required",
            failure_code="PROJECTION_INPUT_MISSING",
        )
    ledger = read_json(ledger_path, {})
    service = ledger.get("service")
    if not isinstance(service, dict) or service.get("workspace") != expected_workspace:
        raise EvidenceSubgraphError(
            "ledger workspace does not match the active LightRAG workspace",
            failure_code="LEDGER_WORKSPACE_MISMATCH",
        )
    ledger_documents = ledger.get("documents")
    if not isinstance(ledger_documents, dict):
        raise EvidenceSubgraphError(
            "import ledger has an invalid document mapping",
            failure_code="LEDGER_INVALID",
        )
    documents: list[dict[str, Any]] = []
    for line in documents_path.read_text(encoding="utf-8").splitlines():
        _check_budget(deadline)
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise EvidenceSubgraphError(
                    "documents.jsonl contains a non-object record",
                    failure_code="PROJECTION_INPUT_INVALID",
                )
            documents.append(value)

    basename_to_document: dict[str, dict[str, Any]] = {}
    all_units: list[ContentUnit] = []
    units_by_basename: dict[str, list[ContentUnit]] = {}
    for document in documents:
        _check_budget(deadline)
        text = document.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        document_id = document.get("id")
        source_path = document.get("source_path")
        source_sha256 = document.get("sha256")
        if not all(isinstance(value, str) and value for value in (document_id, source_path, source_sha256)):
            raise EvidenceSubgraphError(
                "prepared document identity is invalid",
                failure_code="PROJECTION_INPUT_INVALID",
            )
        ledger_entry = ledger_documents.get(document_id)
        if (
            not isinstance(ledger_entry, dict)
            or ledger_entry.get("sha256") != source_sha256
            or not isinstance(ledger_entry.get("service_track_id"), str)
            or ledger_entry.get("removed_from_corpus") is True
        ):
            raise EvidenceSubgraphError(
                "prepared document is not confirmed by the successful import ledger",
                failure_code="PROJECTION_LEDGER_INCOMPLETE",
            )
        basename = Path(source_path).name
        if basename in basename_to_document:
            raise EvidenceSubgraphError(
                "multiple active documents share the same basename",
                failure_code="DUPLICATE_SOURCE_BASENAME",
            )
        basename_to_document[basename] = document
        units = _chunk_document(
            text,
            source_path=source_path,
            source_sha256=source_sha256,
            target_chars=settings.target_chars,
            overlap_chars=settings.overlap_chars,
            deadline=deadline,
        )
        if not units:
            raise EvidenceSubgraphError(
                "active document produced no local content units",
                failure_code="PROJECTION_EMPTY_DOCUMENT",
            )
        units_by_basename[basename] = units
        all_units.extend(units)
    if not all_units:
        raise EvidenceSubgraphError(
            "local evidence projection is empty",
            failure_code="PROJECTION_EMPTY",
        )
    return all_units, units_by_basename


def _chunk_document(
    text: str,
    *,
    source_path: str,
    source_sha256: str,
    target_chars: int,
    overlap_chars: int,
    deadline: float | None = None,
) -> list[ContentUnit]:
    _check_budget(deadline)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = [
        re.sub(r"[ \t]+", " ", part.strip())
        for part in re.split(r"\n\s*\n+", normalized)
        if part.strip()
    ]
    segments: list[str] = []
    for paragraph in paragraphs:
        _check_budget(deadline)
        if len(paragraph) <= target_chars:
            segments.append(paragraph)
            continue
        sentences = [part.strip() for part in _SENTENCE_BOUNDARY.split(paragraph) if part.strip()]
        for sentence in sentences:
            if len(sentence) <= target_chars:
                segments.append(sentence)
                continue
            step = max(1, target_chars - overlap_chars)
            segments.extend(
                sentence[start : start + target_chars]
                for start in range(0, len(sentence), step)
            )

    chunks: list[str] = []
    current = ""
    for segment in segments:
        _check_budget(deadline)
        if not current:
            current = segment
        elif len(current) + 2 + len(segment) <= target_chars:
            current = f"{current}\n\n{segment}"
        else:
            chunks.append(current)
            prefix_budget = max(0, target_chars - len(segment) - 2)
            prefix = current[-min(overlap_chars, prefix_budget) :] if prefix_budget else ""
            current = f"{prefix}\n\n{segment}".strip() if prefix else segment
    if current:
        chunks.append(current)

    units: list[ContentUnit] = []
    for ordinal, content in enumerate(chunk for chunk in chunks if chunk.strip()):
        _check_budget(deadline)
        content_sha256 = _sha256(content)
        unit_key = _sha256(f"{source_sha256}:{ordinal}:{content_sha256}")[:24]
        units.append(
            ContentUnit(
                content_unit_id=f"unit-{unit_key}",
                source_path=source_path,
                source_sha256=source_sha256,
                ordinal=ordinal,
                text=content,
                content_sha256=content_sha256,
            )
        )
    return units


def _resolve_allowed_units(
    subgraph: EvidenceSubgraph,
    units_by_basename: dict[str, list[ContentUnit]],
    *,
    max_content_units: int,
    deadline: float | None = None,
) -> tuple[list[ContentUnit], set[str]]:
    _check_budget(deadline)
    declared_files: set[str] = set()
    for item in (*subgraph.nodes, *subgraph.edges):
        for value in _split_graph_field(item.properties.get("file_path")):
            basename = Path(value).name
            if basename.casefold() not in _UNKNOWN_FILE_VALUES:
                declared_files.add(basename)
    if not declared_files:
        raise EvidenceSubgraphError(
            "subgraph contains no mappable source declarations",
            failure_code="SUBGRAPH_SCOPE_EMPTY",
        )
    unknown = sorted(declared_files - units_by_basename.keys())
    if unknown:
        raise EvidenceSubgraphError(
            "one or more subgraph sources cannot be mapped to active documents",
            failure_code="UNMAPPED_GRAPH_SOURCE",
        )
    allowed_units: list[ContentUnit] = []
    for basename in sorted(declared_files):
        for unit in units_by_basename[basename]:
            _check_budget(deadline)
            allowed_units.append(unit)
            if len(allowed_units) > max_content_units:
                raise EvidenceSubgraphError(
                    "subgraph scope exceeds the configured content-unit limit",
                    failure_code="GRAPH_BUDGET_EXCEEDED",
                )
    if not allowed_units:
        raise EvidenceSubgraphError(
            "subgraph source mapping produced an empty allow-list",
            failure_code="SUBGRAPH_SCOPE_EMPTY",
        )
    return allowed_units, declared_files


def _bm25_retrieve(
    query: str,
    allowed_units: list[ContentUnit],
    top_k: int,
    *,
    deadline: float | None = None,
) -> list[EvidenceChunk]:
    _check_budget(deadline)
    query_terms = _tokenize(query)
    if not query_terms:
        return []
    tokenized: list[list[str]] = []
    for unit in allowed_units:
        _check_budget(deadline)
        tokenized.append(_tokenize(unit.text))
    document_frequency: Counter[str] = Counter()
    for terms in tokenized:
        _check_budget(deadline)
        document_frequency.update(set(terms))
    average_length = sum(len(terms) for terms in tokenized) / max(1, len(tokenized))
    query_frequency = Counter(query_terms)
    scored: list[tuple[float, ContentUnit]] = []
    k1 = 1.5
    b = 0.75
    corpus_size = len(allowed_units)
    for unit, terms in zip(allowed_units, tokenized):
        _check_budget(deadline)
        term_frequency = Counter(terms)
        length = max(1, len(terms))
        score = 0.0
        for term, query_count in query_frequency.items():
            frequency = term_frequency.get(term, 0)
            if not frequency:
                continue
            frequency_in_docs = document_frequency[term]
            inverse_document_frequency = math.log(
                1 + (corpus_size - frequency_in_docs + 0.5) / (frequency_in_docs + 0.5)
            )
            denominator = frequency + k1 * (
                1 - b + b * length / max(1.0, average_length)
            )
            score += query_count * inverse_document_frequency * (
                frequency * (k1 + 1) / denominator
            )
        if score > 0:
            scored.append((score, unit))
    scored.sort(key=lambda item: (-item[0], item[1].source_path, item[1].ordinal))
    return [
        EvidenceChunk(
            content_unit_id=unit.content_unit_id,
            source_path=unit.source_path,
            content=unit.text,
            content_sha256=unit.content_sha256,
            score=round(score, 8),
        )
        for score, unit in scored[:top_k]
    ]


def _tokenize(text: str) -> list[str]:
    lowered = text.casefold()
    terms = _LATIN_TOKEN.findall(lowered)
    for span in _CJK_SPAN.findall(lowered):
        if len(span) == 1:
            terms.append(span)
            continue
        for size in (2, 3):
            if len(span) >= size:
                terms.extend(span[index : index + size] for index in range(len(span) - size + 1))
    return terms


def _request_json_with_budget(
    client: LightRAGServiceClient,
    method: str,
    path: str,
    *,
    deadline: float | None,
) -> dict[str, Any]:
    remaining = _check_budget(deadline)
    original_timeout = getattr(client, "timeout", None)
    if remaining is not None and isinstance(original_timeout, (int, float)):
        client.timeout = min(float(original_timeout), remaining)
    try:
        result = client.request_json(method, path)
    except LightRAGBuildError:
        _check_budget(deadline)
        raise
    finally:
        if isinstance(original_timeout, (int, float)):
            client.timeout = original_timeout
    _check_budget(deadline)
    return result


def _check_budget(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EvidenceSubgraphError(
            "evidence-subgraph exceeded its configured resource budget",
            failure_code="GRAPH_BUDGET_EXCEEDED",
        )
    return remaining


def _split_graph_field(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        raise ValueError("graph source fields must be strings")
    return sorted({part.strip() for part in value.split(GRAPH_FIELD_SEPARATOR) if part.strip()})


def _write_trace(
    path: Path,
    *,
    run_id: str,
    status: str,
    query_sha256: str,
    seeds: tuple[str, ...],
    trace_state: dict[str, Any],
    failure_code: str | None,
    started: float,
) -> RetrievalTrace:
    trace = RetrievalTrace(
        run_id=run_id,
        status=status,
        query_sha256=query_sha256,
        seeds=seeds,
        failure_code=failure_code,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        **trace_state,
    )
    write_json_atomic(path, trace.model_dump(mode="json"))
    return trace


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"esg-{timestamp}-{uuid.uuid4().hex[:8]}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

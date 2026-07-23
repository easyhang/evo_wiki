"""Trusted query delivery and evidence assessment for EvoWiki.

Generation, evidence quality, and human review are independent dimensions.
Every non-empty final answer is delivered; evidence warnings are recorded for
review instead of suppressing the answer.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .evidence import gate_lightrag_references
from .lightrag_lane import (
    LightRAGBuildError,
    LightRAGServiceClient,
    normalize_lightrag_references,
    parse_lightrag_capabilities,
    resolve_lightrag_service_config,
)
from .query_audit import (
    delete_query_audit_payload,
    write_query_audit_payload,
)
from .state.notifications import (
    build_notification,
    notification_settings,
    should_notify,
)
from .state.contracts import StateError
from .state.schema import (
    NOTIFICATION_SCHEMA_VERSION,
    QUERY_DELIVERY_SCHEMA_VERSION,
)
from .state.store import StateStore, _canonical_json
from .utils import utc_now


VERIFICATION_LEVEL = "provenance_critical_fact_v1"
READ_PROXY_VERIFICATION_LEVEL = "gateway_read_proxy_v1"
MAX_CITATION_EXCERPTS = 5
MAX_CITATION_EXCERPT_CHARS = 500
logger = logging.getLogger(__name__)
_CRITICAL_LITERAL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?"
    r"|\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?"
    r"|\d+(?:\.\d+)?%|\d+(?:\.\d+)?(?:元|万元|亿元)"
    r"|[A-Za-z]{1,8}[-_]?\d{2,}|第\d+(?:条|款|项|章))"
    r"(?![A-Za-z0-9])"
)
_REFUSAL_SIGNALS = (
    "无法回答",
    "无法确定",
    "没有足够",
    "未找到相关",
    "insufficient evidence",
    "cannot answer",
    "not enough information",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GatewayConversationTurn(StrictModel):
    role: Literal["user", "assistant"]
    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 4_000:
            raise ValueError(
                "conversation content must be non-empty and at most "
                "4000 characters"
            )
        return normalized


class GatewayQueryRequest(StrictModel):
    schema_version: Literal[2] = 2
    query: str
    mode: Literal["naive", "local", "global", "hybrid", "mix"] = "mix"
    top_k: int = Field(default=20, ge=1, le=100)
    conversation_history: list[GatewayConversationTurn] = Field(
        default_factory=list,
        max_length=6,
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 10_000:
            raise ValueError("query must be non-empty and at most 10000 characters")
        return normalized

    @model_validator(mode="after")
    def validate_conversation_history(self) -> GatewayQueryRequest:
        history = self.conversation_history
        if len(history) % 2:
            raise ValueError(
                "conversation_history must contain complete user/assistant pairs"
            )
        expected_roles = ("user", "assistant")
        if any(
            turn.role != expected_roles[index % 2]
            for index, turn in enumerate(history)
        ):
            raise ValueError(
                "conversation_history roles must strictly alternate from user"
            )
        if sum(len(turn.content) for turn in history) > 12_000:
            raise ValueError(
                "conversation_history content must total at most 12000 characters"
            )
        return self


class GatewayCitation(StrictModel):
    citation_id: str
    marker: str
    source_label: str
    revision_id: str
    excerpts: list[str]


class GatewayReviewHistory(StrictModel):
    previous_rejection_count: int = Field(default=0, ge=0)
    exact_rejected_answer_repeat: bool = False


class GatewayQueryResult(StrictModel):
    schema_version: Literal[2] = 2
    request_id: str
    generation_status: Literal["succeeded", "failed"]
    answer_origin: Literal["knowledge_base", "general_model"] | None = None
    evidence_status: Literal[
        "grounded",
        "partially_grounded",
        "ungrounded",
    ] | None = None
    review_status: Literal[
        "not_required",
        "pending",
        "approved",
        "rejected",
        "unavailable",
    ] = "not_required"
    answer: str | None = None
    citations: list[GatewayCitation] = Field(default_factory=list)
    audit_id: str | None = None
    review_history: GatewayReviewHistory = Field(
        default_factory=GatewayReviewHistory
    )
    error_code: str | None = None
    context_turns_used: int = Field(default=0, ge=0, le=3)

    @model_validator(mode="after")
    def validate_delivery_shape(self) -> GatewayQueryResult:
        if self.generation_status == "succeeded":
            if (
                not self.answer
                or self.answer_origin is None
                or self.evidence_status is None
                or self.error_code is not None
            ):
                raise ValueError("successful query result is incomplete")
        elif (
            self.answer is not None
            or self.answer_origin is not None
            or self.evidence_status is not None
            or self.citations
        ):
            raise ValueError("failed query result cannot contain an answer")
        return self


@dataclass(frozen=True)
class GatewaySettings:
    mode: Literal["disabled", "shadow", "enforce"]
    listen_host: str
    listen_port: int
    max_body_bytes: int
    max_response_bytes: int
    max_in_flight: int
    request_timeout_seconds: float
    drain_timeout_seconds: float
    audit_required: bool
    audit_hmac_key_env: str
    auth_mode: Literal["trusted_proxy", "local_single_user"]
    principal_header: str
    default_domain: str


def gateway_settings(project: dict[str, Any]) -> GatewaySettings:
    raw_gateway = project.get("query_gateway") or {}
    raw_security = project.get("security") or {}
    if not isinstance(raw_gateway, dict) or not isinstance(raw_security, dict):
        raise StateError(
            "query gateway and security configuration must be objects",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    mode = raw_gateway.get("mode", "disabled")
    if mode not in {"disabled", "shadow", "enforce"}:
        raise StateError(
            "query_gateway.mode must be disabled, shadow, or enforce",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    listen = raw_gateway.get("listen", "127.0.0.1:8765")
    if not isinstance(listen, str) or ":" not in listen:
        raise StateError(
            "query_gateway.listen must be host:port",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    host, raw_port = listen.rsplit(":", 1)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise StateError(
            "query gateway port is invalid",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        ) from exc
    auth_mode = raw_security.get("auth_mode", "trusted_proxy")
    if auth_mode not in {"trusted_proxy", "local_single_user"}:
        raise StateError(
            "security.auth_mode is invalid",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    audit_required = raw_gateway.get("audit_required", True)
    fail_closed = raw_security.get("fail_closed", True)
    if not isinstance(audit_required, bool) or not isinstance(
        fail_closed,
        bool,
    ):
        raise StateError(
            "query gateway safety flags must be booleans",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    evidence_policy = raw_gateway.get(
        "evidence_policy",
        VERIFICATION_LEVEL,
    )
    if evidence_policy != VERIFICATION_LEVEL:
        raise StateError(
            "query gateway evidence policy is unsupported",
            error_code="QUERY_GATEWAY_EVIDENCE_POLICY_UNSUPPORTED",
        )
    settings = GatewaySettings(
        mode=mode,
        listen_host=host,
        listen_port=port,
        max_body_bytes=_bounded_int(
            raw_gateway.get("max_body_bytes", 32768),
            name="query_gateway.max_body_bytes",
            minimum=1024,
            maximum=1_048_576,
        ),
        max_response_bytes=_bounded_int(
            raw_gateway.get("max_response_bytes", 4_194_304),
            name="query_gateway.max_response_bytes",
            minimum=65_536,
            maximum=16_777_216,
        ),
        max_in_flight=_bounded_int(
            raw_gateway.get("max_in_flight", 16),
            name="query_gateway.max_in_flight",
            minimum=1,
            maximum=256,
        ),
        request_timeout_seconds=_bounded_float(
            raw_gateway.get("request_timeout_seconds", 45),
            name="query_gateway.request_timeout_seconds",
            minimum=1,
            maximum=300,
        ),
        drain_timeout_seconds=_bounded_float(
            raw_gateway.get("drain_timeout_seconds", 30),
            name="query_gateway.drain_timeout_seconds",
            minimum=1,
            maximum=300,
        ),
        audit_required=audit_required,
        audit_hmac_key_env=str(
            raw_gateway.get(
                "audit_hmac_key_env",
                "EVO_WIKI_QUERY_AUDIT_KEY",
            )
        ),
        auth_mode=auth_mode,
        principal_header=str(
            raw_security.get("principal_header", "X-Evo-Principal")
        ),
        default_domain=str(raw_security.get("default_domain", "default")),
    )
    if not host or not 1 <= port <= 65535:
        raise StateError(
            "query gateway listen address is invalid",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    if settings.mode == "enforce" and settings.auth_mode == "local_single_user":
        raise StateError(
            "enforce mode requires trusted proxy authentication",
            error_code="QUERY_GATEWAY_AUTH_UNSAFE",
        )
    if settings.mode != "disabled" and not settings.audit_required:
        raise StateError(
            "enabled query gateway requires durable audit",
            error_code="QUERY_GATEWAY_AUDIT_REQUIRED",
        )
    if settings.mode != "disabled" and not fail_closed:
        raise StateError(
            "enabled query gateway requires fail-closed security",
            error_code="QUERY_GATEWAY_FAIL_CLOSED_REQUIRED",
        )
    if settings.mode == "enforce" and not _is_loopback_host(host):
        raise StateError(
            "enforce mode must listen on a loopback address",
            error_code="QUERY_GATEWAY_BIND_UNSAFE",
        )
    return settings


class TrustedQueryGateway:
    """Synchronous core used by both CLI and the optional ASGI delivery layer."""

    def __init__(
        self,
        store: StateStore,
        project_config: dict[str, Any],
        *,
        client: LightRAGServiceClient | None = None,
        audit_key: bytes | None = None,
    ):
        self.store = store
        self.project_config = project_config
        self.settings = gateway_settings(project_config)
        if self.settings.mode == "disabled":
            raise StateError(
                "query gateway is disabled",
                error_code="QUERY_GATEWAY_DISABLED",
            )
        store.require_schema_version(QUERY_DELIVERY_SCHEMA_VERSION)
        self.lightrag_config = project_config.get("lightrag") or {}
        if not isinstance(self.lightrag_config, dict):
            raise StateError(
                "lightrag configuration must be an object",
                error_code="QUERY_GATEWAY_CONFIG_INVALID",
            )
        self.partition = store.query_partition(self.lightrag_config)
        self.notification_settings = notification_settings(project_config)
        if self.notification_settings.enabled:
            store.require_schema_version(NOTIFICATION_SCHEMA_VERSION)
        configured_domain = str(self.partition["security_domain_name"])
        if configured_domain != self.settings.default_domain:
            raise StateError(
                "query gateway security domain does not match its partition",
                error_code="QUERY_DOMAIN_MISMATCH",
            )
        key = audit_key
        if key is None:
            raw_key = os.environ.get(self.settings.audit_hmac_key_env)
            key = raw_key.encode("utf-8") if raw_key else None
        if not key or len(key) < 16:
            raise StateError(
                "query audit HMAC key is missing or too short",
                error_code="QUERY_AUDIT_KEY_MISSING",
            )
        self.audit_key = key
        if client is None:
            service = resolve_lightrag_service_config(self.lightrag_config)
            client = LightRAGServiceClient(
                service["base_url"],
                headers=service["headers"],
                timeout=min(
                    float(service["timeout_seconds"]),
                    self.settings.request_timeout_seconds,
                ),
                workspace=service["workspace"],
            )
            self.service = service
        else:
            self.service = {
                "workspace": str(self.partition["namespace"]),
                "embedding_batch_size": int(
                    (self.lightrag_config.get("embedding") or {}).get(
                        "batch_size",
                        8,
                    )
                ),
            }
        self.client = client
        self._preflight_complete = False

    def check(self) -> dict[str, Any]:
        """Perform a sanitized, read-only backend and state readiness check."""
        try:
            health = self.client.request_json("GET", "/health")
            openapi = self.client.request_json("GET", "/openapi.json")
        except Exception as exc:
            raise StateError(
                "query backend readiness probe failed",
                error_code="QUERY_BACKEND_UNAVAILABLE",
            ) from exc
        if not isinstance(health, dict) or not isinstance(openapi, dict):
            raise StateError(
                "query backend readiness response is invalid",
                error_code="QUERY_BACKEND_INVALID",
            )
        capabilities = parse_lightrag_capabilities(
            health,
            openapi,
            expected_workspace=str(self.service["workspace"]),
            requested_embedding_batch_size=int(
                self.service["embedding_batch_size"]
            ),
        )
        if health.get("status") != "healthy":
            raise StateError(
                "query backend did not report healthy",
                error_code="QUERY_BACKEND_UNHEALTHY",
            )
        if capabilities.workspace_matches is not True:
            raise StateError(
                "query backend workspace cannot be confirmed",
                error_code=(
                    "QUERY_WORKSPACE_MISMATCH"
                    if capabilities.workspace_matches is False
                    else "QUERY_WORKSPACE_UNCONFIRMED"
                ),
            )
        if capabilities.storage_workspaces_match is not True:
            raise StateError(
                "query backend storage workspace cannot be confirmed",
                error_code=(
                    "QUERY_STORAGE_WORKSPACE_MISMATCH"
                    if capabilities.storage_workspaces_match is False
                    else "QUERY_STORAGE_WORKSPACE_UNCONFIRMED"
                ),
            )
        if capabilities.supports_chunk_content is not True:
            raise StateError(
                "query backend chunk-content capability is unavailable",
                error_code="QUERY_CHUNK_CONTENT_UNSUPPORTED",
            )
        if capabilities.supports_conversation_history is not True:
            raise StateError(
                "query backend conversation-history capability is unavailable",
                error_code="QUERY_CONVERSATION_HISTORY_UNSUPPORTED",
            )
        if capabilities.supports_bypass is not True:
            raise StateError(
                "query backend bypass capability is unavailable",
                error_code="QUERY_BYPASS_UNSUPPORTED",
            )
        self._preflight_complete = True
        return {
            "status": "ready",
            "mode": self.settings.mode,
            "schema_version": self.store.schema_version(),
            "partition_id": str(self.partition["id"]),
            "security_domain": str(
                self.partition["security_domain_name"]
            ),
            "capabilities": {
                "chunk_content": True,
                "conversation_history": True,
                "bypass": True,
                "workspace_confirmed": True,
                "storage_workspace_mismatch": False,
            },
            "workspace_mutated": False,
            "error_code": None,
        }

    def query(
        self,
        request: GatewayQueryRequest,
        *,
        principal: str,
    ) -> GatewayQueryResult:
        if not self._preflight_complete:
            self.check()
        request_id = f"qry-{uuid.uuid4().hex}"
        context_turns_used = len(request.conversation_history) // 2
        principal_hmac = _hmac_value(self.audit_key, principal)
        query_hmac = _hmac_value(
            self.audit_key,
            _canonical_json(
                request.model_dump(mode="json", exclude_none=True)
            ),
        )
        try:
            self.store.begin_query_run(
                request_id=request_id,
                retrieval_partition_id=str(self.partition["id"]),
                principal_hmac=principal_hmac,
                query_hmac=query_hmac,
                request_mode=request.mode,
                gateway_mode=self.settings.mode,
                verification_level=VERIFICATION_LEVEL,
                lease_seconds=self.settings.request_timeout_seconds + 15,
            )
        except StateError as exc:
            if exc.error_code == "QUERY_MAINTENANCE_ACTIVE":
                return _terminal_result(
                    request_id,
                    code=exc.error_code,
                    context_turns_used=context_turns_used,
                )
            raise

        try:
            rag_answer, references = self._call_backend(
                request,
                mode=request.mode,
                include_references=True,
            )
            effective_query = "\n".join(
                [
                    *[
                        turn.content
                        for turn in request.conversation_history
                        if turn.role == "user"
                    ],
                    request.query,
                ]
            )
            assessment = self._assess_evidence(
                query=effective_query,
                answer=rag_answer,
                references=references,
            )
            fallback_codes = list(assessment["codes"])
            needs_fallback = not assessment["citations"]
            if not rag_answer:
                needs_fallback = True
                fallback_codes.append("QUERY_ANSWER_EMPTY")
            elif _looks_like_refusal(rag_answer):
                needs_fallback = True
                fallback_codes.append("QUERY_BACKEND_REFUSED")

            if needs_fallback:
                answer, _ = self._call_backend(
                    request,
                    mode="bypass",
                    include_references=False,
                )
                if not answer:
                    raise StateError(
                        "query backend returned an empty final answer",
                        error_code="QUERY_ANSWER_EMPTY",
                    )
                answer_origin = "general_model"
                evidence_status = "ungrounded"
                citations: list[GatewayCitation] = []
                codes = _ordered_codes(
                    [*fallback_codes, "QUERY_GENERAL_MODEL_FALLBACK"]
                )
            else:
                answer = rag_answer
                answer_origin = "knowledge_base"
                evidence_status = str(assessment["evidence_status"])
                citations = list(assessment["citations"])
                codes = list(assessment["codes"])

            return self._commit_delivery(
                request_id=request_id,
                request=request,
                query_hmac=query_hmac,
                answer=answer,
                answer_origin=answer_origin,
                evidence_status=evidence_status,
                citations=citations,
                reference_count=len(references),
                active_count=len(assessment["citations"]),
                codes=codes,
                context_turns_used=context_turns_used,
            )
        except StateError as exc:
            self._finish_failed_run(
                request_id,
                code=exc.error_code,
            )
            return _terminal_result(
                request_id,
                code=exc.error_code,
                context_turns_used=context_turns_used,
            )
        except (LightRAGBuildError, TimeoutError, OSError):
            self._finish_failed_run(
                request_id,
                code="QUERY_BACKEND_REQUEST_FAILED",
            )
            return _terminal_result(
                request_id,
                code="QUERY_BACKEND_REQUEST_FAILED",
                context_turns_used=context_turns_used,
            )

    def _call_backend(
        self,
        request: GatewayQueryRequest,
        *,
        mode: str,
        include_references: bool,
    ) -> tuple[str, list[dict[str, Any]]]:
        payload: dict[str, Any] = {
            "query": request.query,
            "mode": mode,
            "include_references": include_references,
            "include_chunk_content": include_references,
        }
        if mode != "bypass":
            payload["top_k"] = request.top_k
        if request.conversation_history:
            payload["conversation_history"] = [
                turn.model_dump(mode="json")
                for turn in request.conversation_history
            ]
        raw = self.client.post_json("/query", payload)
        if not isinstance(raw, dict):
            raise StateError(
                "query backend response is invalid",
                error_code="QUERY_BACKEND_INVALID",
            )
        if (
            len(
                json.dumps(
                    raw,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > self.settings.max_response_bytes
        ):
            raise StateError(
                "query backend response exceeds the configured limit",
                error_code="QUERY_BACKEND_RESPONSE_TOO_LARGE",
            )
        answer = raw.get("response")
        if not isinstance(answer, str):
            answer = raw.get("answer")
        normalized_answer = answer.strip() if isinstance(answer, str) else ""
        references = (
            normalize_lightrag_references(
                raw.get("references") or raw.get("ref_results")
            )
            if include_references
            else []
        )
        if len(references) > 100:
            raise StateError(
                "query backend returned too many references",
                error_code="QUERY_REFERENCE_LIMIT_EXCEEDED",
            )
        return normalized_answer, references

    def _finish_failed_run(self, request_id: str, *, code: str) -> None:
        try:
            self.store.finish_query_run(
                request_id,
                status="FAILED",
                verdict_code=code,
                error_code=code,
                reference_count=0,
                active_reference_count=0,
                answer_sha256=None,
                citation_set_sha256=None,
                generation_status="failed",
                answer_origin=None,
                evidence_status=None,
                review_status="not_required",
            )
        except StateError:
            logger.exception(
                "failed to persist terminal query failure",
                extra={"request_id": request_id, "error_code": code},
            )

    def begin_reader_lease(
        self,
        *,
        principal: str,
        request_fingerprint: str,
        request_mode: str = "graph",
    ) -> str:
        """Start a maintenance-drain-aware lease for a non-query reader."""
        if not self._preflight_complete:
            self.check()
        request_id = f"read-{uuid.uuid4().hex}"
        self.store.begin_query_run(
            request_id=request_id,
            retrieval_partition_id=str(self.partition["id"]),
            principal_hmac=_hmac_value(self.audit_key, principal),
            query_hmac=_hmac_value(
                self.audit_key,
                request_fingerprint,
            ),
            request_mode=request_mode,
            gateway_mode=self.settings.mode,
            verification_level=READ_PROXY_VERIFICATION_LEVEL,
            lease_seconds=self.settings.request_timeout_seconds + 15,
        )
        return request_id

    def finish_reader_lease(
        self,
        request_id: str,
        *,
        success: bool,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        """Finish a reader lease and re-check the fence in the same write."""
        return self.store.finish_query_run(
            request_id,
            status="ANSWERED" if success else "FAILED",
            verdict_code=None if success else error_code,
            error_code=None if success else error_code,
            reference_count=0,
            active_reference_count=0,
            answer_sha256=None,
            citation_set_sha256=None,
        )

    def _assess_evidence(
        self,
        *,
        query: str,
        answer: str,
        references: list[dict[str, Any]],
    ) -> dict[str, Any]:
        codes: list[str] = []
        if not references:
            codes.append("QUERY_REFERENCES_EMPTY")

        # Re-read current ownership for every assessment so a replacement
        # cannot leave a stale citation mapped to a superseded revision.
        ownership = _candidate_index(
            self.store.query_reference_candidates(
                retrieval_partition_id=str(self.partition["id"]),
                backend_fingerprint=str(
                    self.partition["backend_fingerprint"]
                ),
            )
        )
        citations: list[GatewayCitation] = []
        accepted_references: list[dict[str, Any]] = []
        used_markers: set[str] = set()
        for index, reference in enumerate(references, start=1):
            content = reference.get("content")
            if not isinstance(content, list) or not any(
                isinstance(part, str) and part.strip()
                for part in content
            ):
                codes.append("QUERY_CHUNK_CONTENT_EMPTY")
                continue
            source = _reference_source(reference)
            if source is None:
                codes.append("QUERY_REFERENCE_SOURCE_MISSING")
                continue
            candidates = ownership.get(_normalized_basename(source), [])
            if not candidates:
                codes.append("QUERY_REFERENCE_UNMAPPED")
                continue
            active = [
                candidate
                for candidate in candidates
                if candidate["revision_status"] == "ACTIVE"
                and candidate["remote_status"] == "PROCESSED"
                and candidate["action_gate"] == "OPEN"
            ]
            if len(active) != 1:
                codes.append(
                    "QUERY_REFERENCE_NOT_ACTIVE"
                    if not active
                    else "QUERY_REFERENCE_AMBIGUOUS"
                )
                continue
            owner = active[0]
            excerpts = [
                part.strip()[:MAX_CITATION_EXCERPT_CHARS]
                for part in content
                if isinstance(part, str) and part.strip()
            ][:MAX_CITATION_EXCERPTS]
            citation_hash = hashlib.sha256(
                (
                    str(owner["revision_id"])
                    + "\0"
                    + "\0".join(excerpts)
                ).encode("utf-8")
            ).hexdigest()
            raw_marker = str(reference.get("reference_id") or "").strip()
            marker = (
                raw_marker
                if raw_marker.isdigit()
                and 1 <= len(raw_marker) <= 3
                and raw_marker not in used_markers
                else str(index)
            )
            while marker in used_markers:
                marker = str(int(marker) + 1)
            used_markers.add(marker)
            citations.append(
                GatewayCitation(
                    citation_id=f"cit-{citation_hash[:24]}",
                    marker=marker,
                    source_label=_normalized_basename(source),
                    revision_id=str(owner["revision_id"]),
                    excerpts=excerpts,
                )
            )
            accepted_references.append(reference)

        if citations:
            relevant_citations: list[GatewayCitation] = []
            relevant_references: list[dict[str, Any]] = []
            for citation, reference in zip(
                citations,
                accepted_references,
                strict=True,
            ):
                _, reference_evidence = gate_lightrag_references(
                    query,
                    [reference],
                )
                if reference_evidence.get("status") != "passed":
                    codes.append(
                        _reference_evidence_code(
                            reference_evidence.get("code")
                        )
                    )
                    continue
                relevant_citations.append(citation)
                relevant_references.append(reference)
            citations = relevant_citations
            accepted_references = relevant_references

        if citations:
            combined = "\n".join(
                part
                for citation in citations
                for part in citation.excerpts
            )
            missing_literals = [
                literal
                for literal in sorted(
                    set(_CRITICAL_LITERAL_RE.findall(answer))
                )
                if literal not in combined
            ]
            if missing_literals:
                codes.append("QUERY_CRITICAL_FACT_UNSUPPORTED")

        return {
            "evidence_status": (
                "partially_grounded"
                if citations and codes
                else "grounded"
                if citations
                else "ungrounded"
            ),
            "codes": _ordered_codes(codes),
            "citations": citations,
            "accepted_references": accepted_references,
        }

    def _commit_delivery(
        self,
        *,
        request_id: str,
        request: GatewayQueryRequest,
        query_hmac: str,
        answer: str,
        answer_origin: str,
        evidence_status: str,
        citations: list[GatewayCitation],
        reference_count: int,
        active_count: int,
        codes: list[str],
        context_turns_used: int,
    ) -> GatewayQueryResult:
        code = codes[0] if codes else None
        audit_id = None
        review_status = "not_required"
        payload_metadata: dict[str, str] | None = None
        pending_audit: dict[str, Any] | None = None
        pending_notification: dict[str, Any] | None = None
        if evidence_status in {"partially_grounded", "ungrounded"}:
            candidate_audit_id = f"audit-{uuid.uuid4().hex}"
            severity = (
                "HIGH"
                if any(
                    item in {
                    "QUERY_REFERENCE_UNMAPPED",
                    "QUERY_REFERENCE_AMBIGUOUS",
                    "QUERY_REFERENCE_NOT_ACTIVE",
                    }
                    for item in codes
                )
                else "MEDIUM"
            )
            try:
                payload_metadata = write_query_audit_payload(
                    self.store.root,
                    audit_id=candidate_audit_id,
                    payload={
                        "created_at": utc_now(),
                        "question": request.query,
                        "conversation_history": [
                            turn.model_dump(mode="json")
                            for turn in request.conversation_history
                        ],
                        "request_mode": request.mode,
                        "top_k": request.top_k,
                        "answer": answer,
                        "answer_origin": answer_origin,
                        "evidence_status": evidence_status,
                        "evidence_codes": codes,
                        "citations": [
                            citation.model_dump(mode="json")
                            for citation in citations
                        ],
                    },
                )
                pending_notification = (
                    build_notification(
                        root=self.store.root,
                        event_type="AUDIT_OPENED",
                        severity=severity,
                        subject_type="audit_item",
                        subject_id=candidate_audit_id,
                        dedupe_key=(
                            f"AUDIT_OPENED:{candidate_audit_id}"
                        ),
                        security_domain=str(
                            self.partition["security_domain_name"]
                        ),
                        state="OPEN",
                        error_code=str(
                            code or "QUERY_REVIEW_REQUIRED"
                        ),
                        counts={
                            "reference_count": reference_count,
                            "active_reference_count": active_count,
                        },
                        max_attempts=(
                            self.notification_settings.max_attempts
                        ),
                    )
                    if should_notify(
                        self.notification_settings,
                        severity,
                    )
                    else None
                )
                pending_audit = {
                    "audit_id": candidate_audit_id,
                    "trigger_code": str(
                        code or "QUERY_REVIEW_REQUIRED"
                    ),
                    "severity": severity,
                    "subject_type": "query_run",
                    "subject_id": request_id,
                    "evidence": {
                        "schema_version": 2,
                        "verification_level": VERIFICATION_LEVEL,
                        "evidence_status": evidence_status,
                        "reference_count": reference_count,
                        "active_reference_count": active_count,
                        "codes": codes,
                        **payload_metadata,
                    },
                }
                audit_id = candidate_audit_id
                review_status = "pending"
            except Exception:
                logger.exception(
                    "query answer delivered without durable review item",
                    extra={
                        "request_id": request_id,
                        "error_code": "QUERY_AUDIT_PERSIST_FAILED",
                    },
                )
                if payload_metadata is not None:
                    try:
                        delete_query_audit_payload(
                            self.store.root,
                            payload_metadata,
                        )
                    except Exception:
                        logger.exception(
                            "failed to clean orphaned query audit payload",
                            extra={"request_id": request_id},
                        )
                review_status = "unavailable"

        citation_payload = [
            citation.model_dump(mode="json")
            for citation in citations
        ]
        answer_sha256 = _sha256_text(answer)
        review_history = self.store.rejected_query_history(
            query_hmac=query_hmac,
            answer_sha256=answer_sha256,
        )
        finish_arguments = {
            "status": "ANSWERED",
            "verdict_code": str(code) if code else None,
            "error_code": None,
            "reference_count": reference_count,
            "active_reference_count": active_count,
            "answer_sha256": answer_sha256,
            "citation_set_sha256": (
                _sha256_text(_canonical_json(citation_payload))
                if citation_payload
                else None
            ),
            "generation_status": "succeeded",
            "answer_origin": answer_origin,
            "evidence_status": evidence_status,
            "review_status": review_status,
        }
        try:
            persisted = self.store.finish_query_run(
                request_id,
                **finish_arguments,
                audit_item=pending_audit,
                audit_notification=pending_notification,
            )
        except Exception:
            if pending_audit is None:
                raise
            logger.exception(
                "query answer delivered without durable review item",
                extra={
                    "request_id": request_id,
                    "error_code": "QUERY_AUDIT_PERSIST_FAILED",
                },
            )
            if payload_metadata is not None:
                try:
                    delete_query_audit_payload(
                        self.store.root,
                        payload_metadata,
                    )
                except Exception:
                    logger.exception(
                        "failed to clean orphaned query audit payload",
                        extra={"request_id": request_id},
                    )
            audit_id = None
            review_status = "unavailable"
            finish_arguments["review_status"] = review_status
            persisted = self.store.finish_query_run(
                request_id,
                **finish_arguments,
            )
        if persisted["status"] != "ANSWERED":
            if payload_metadata is not None:
                try:
                    delete_query_audit_payload(
                        self.store.root,
                        payload_metadata,
                    )
                except Exception:
                    logger.exception(
                        "failed to clean blocked query audit payload",
                        extra={"request_id": request_id},
                    )
            return _terminal_result(
                request_id,
                code=str(
                    persisted.get("error_code")
                    or "QUERY_DELIVERY_BLOCKED"
                ),
                context_turns_used=context_turns_used,
            )
        return GatewayQueryResult(
            request_id=request_id,
            generation_status="succeeded",
            answer_origin=answer_origin,
            evidence_status=evidence_status,
            review_status=review_status,
            answer=answer,
            citations=citations,
            audit_id=audit_id,
            review_history=GatewayReviewHistory(**review_history),
            error_code=None,
            context_turns_used=context_turns_used,
        )


def _terminal_result(
    request_id: str,
    *,
    code: str | None,
    context_turns_used: int = 0,
) -> GatewayQueryResult:
    return GatewayQueryResult(
        request_id=request_id,
        generation_status="failed",
        answer_origin=None,
        evidence_status=None,
        review_status="not_required",
        answer=None,
        citations=[],
        audit_id=None,
        error_code=code,
        context_turns_used=context_turns_used,
    )


def _ordered_codes(codes: list[str]) -> list[str]:
    return list(dict.fromkeys(code for code in codes if code))


def _reference_evidence_code(code: object) -> str:
    """Map low-level lexical outcomes to stable human-review triggers."""
    return {
        "IRRELEVANT_REFERENCES": "QUERY_REFERENCES_IRRELEVANT",
        "INSUFFICIENT_QUERY_SIGNAL_OVERLAP": (
            "QUERY_REFERENCE_RELEVANCE_INSUFFICIENT"
        ),
        "QUERY_SIGNAL_TOO_SHORT": "QUERY_REFERENCE_RELEVANCE_UNVERIFIED",
        "STATUTORY_SUPPORT_MISSING": "QUERY_LOCAL_LAW_SUPPORT_MISSING",
        "CHUNK_CONTENT_EMPTY": "QUERY_CHUNK_CONTENT_EMPTY",
    }.get(str(code), "QUERY_REFERENCE_RELEVANCE_UNVERIFIED")


def _candidate_index(
    candidates: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        labels = {
            _normalized_basename(str(candidate["file_source"])),
            _normalized_basename(str(candidate["canonical_path"])),
        }
        for label in labels:
            result.setdefault(label, []).append(candidate)
    return result


def _reference_source(reference: dict[str, Any]) -> str | None:
    for key in ("file_path", "source", "path", "file"):
        value = reference.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalized_basename(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.replace("\\", "/"))
    return PurePosixPath(normalized).name.casefold()


def _looks_like_refusal(answer: str) -> bool:
    lowered = answer.casefold()
    return any(signal in lowered for signal in _REFUSAL_SIGNALS)


def _hmac_value(key: bytes, value: str) -> str:
    return "hmac-sha256:" + hmac.new(
        key,
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_int(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise StateError(
            f"{name} must be an integer",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise StateError(
            f"{name} must be an integer",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        ) from exc
    if not minimum <= result <= maximum:
        raise StateError(
            f"{name} is outside its supported range",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    return result


def _bounded_float(
    value: Any,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool):
        raise StateError(
            f"{name} must be a number",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StateError(
            f"{name} must be a number",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        ) from exc
    if not minimum <= result <= maximum:
        raise StateError(
            f"{name} is outside its supported range",
            error_code="QUERY_GATEWAY_CONFIG_INVALID",
        )
    return result

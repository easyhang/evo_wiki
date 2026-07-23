"""Shared query-audit review operations for CLI and local Web delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .query_audit import (
    delete_query_audit_payload,
    read_query_audit_payload,
)
from .state.contracts import StateError
from .state.notifications import (
    build_notification,
    notification_settings,
    should_notify,
)
from .state.store import StateStore


def audit_review_status(status: str) -> str:
    return {
        "OPEN": "pending",
        "IN_REVIEW": "pending",
        "RESOLVED": "approved",
        "REJECTED": "rejected",
        "WAIVED": "not_required",
    }.get(status, "unavailable")


def list_review_items(
    store: StateStore,
    root: Path,
    *,
    status: str | None = None,
    include_question: bool = False,
) -> list[dict[str, Any]]:
    items = store.list_audit_items(status=status)
    for item in items:
        item["review_status"] = audit_review_status(str(item["status"]))
        if include_question:
            _attach_question_summary(root, item)
    return items


def load_review_item(
    store: StateStore,
    root: Path,
    audit_id: str,
    *,
    include_content: bool,
    tolerate_content_error: bool = False,
) -> dict[str, Any]:
    item = store.audit_item(audit_id)
    item["review_status"] = audit_review_status(str(item["status"]))
    evidence = item["evidence"]
    if not evidence.get("payload_path"):
        item["content_available"] = False
        item["content_error_code"] = "QUERY_AUDIT_PAYLOAD_MISSING"
        return item
    if not include_content:
        item["content_available"] = True
        return item
    try:
        item["content"] = read_query_audit_payload(root, evidence)
    except StateError as exc:
        if not tolerate_content_error:
            raise
        item["content_available"] = False
        item["content_error_code"] = exc.error_code
        return item
    item["content_available"] = True
    return item


def resolve_review_item(
    store: StateStore,
    root: Path,
    project: dict[str, Any],
    *,
    audit_id: str,
    resolution: str,
    actor: str,
) -> dict[str, Any]:
    if resolution not in {"APPROVED", "REJECTED"}:
        raise StateError(
            "audit resolution is invalid",
            error_code="QUERY_AUDIT_RESOLUTION_INVALID",
        )
    existing = store.audit_item(audit_id)
    evidence = existing["evidence"]
    has_payload = bool(evidence.get("payload_path"))
    if has_payload:
        # Resolve only content whose protected snapshot still matches its
        # recorded checksum.  Rejected content is deliberately retained so a
        # reviewer can compare a later answer with the rejected one.
        read_query_audit_payload(root, evidence)
    stored_resolution = "RESOLVED" if resolution == "APPROVED" else resolution
    settings = notification_settings(project)
    notification = (
        build_notification(
            root=root,
            event_type="AUDIT_RESOLVED",
            severity=str(existing["severity"]),
            subject_type="audit_item",
            subject_id=audit_id,
            dedupe_key=f"AUDIT_RESOLVED:{audit_id}:{resolution}",
            security_domain=str(
                (project.get("security") or {}).get(
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
        audit_id=audit_id,
        actor=actor,
        resolution=stored_resolution,
        notification=notification,
    )
    payload_deleted = False
    if resolution == "APPROVED" and has_payload:
        payload_deleted = delete_query_audit_payload(root, evidence)
    item["review_status"] = audit_review_status(str(item["status"]))
    return {
        "item": item,
        "payload_deleted": payload_deleted,
        "payload_retained": resolution == "REJECTED" and has_payload,
        "state_commit_seq": store.state_commit_seq(),
    }


def _attach_question_summary(root: Path, item: dict[str, Any]) -> None:
    evidence = item["evidence"]
    if not evidence.get("payload_path"):
        item["content_available"] = False
        item["content_error_code"] = "QUERY_AUDIT_PAYLOAD_MISSING"
        item["question_summary"] = None
        return
    try:
        content = read_query_audit_payload(root, evidence)
    except StateError as exc:
        item["content_available"] = False
        item["content_error_code"] = exc.error_code
        item["question_summary"] = None
        return
    question = content.get("question")
    if not isinstance(question, str) or not question.strip():
        item["content_available"] = False
        item["content_error_code"] = "QUERY_AUDIT_PAYLOAD_INVALID"
        item["question_summary"] = None
        return
    item["content_available"] = True
    item["question_summary"] = " ".join(question.split())[:180]

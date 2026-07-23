from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .state.contracts import StateError
from .utils import write_json_atomic


AUDIT_PAYLOAD_SCHEMA_VERSION = 1
_AUDIT_ID_RE = re.compile(r"audit-[a-f0-9]{32}")


def write_query_audit_payload(
    root: Path,
    *,
    audit_id: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    """Write reviewable query content outside SQLite with local-only access."""
    if _AUDIT_ID_RE.fullmatch(audit_id) is None:
        raise StateError(
            "query audit payload id is invalid",
            error_code="QUERY_AUDIT_PAYLOAD_INVALID",
        )
    directory = _review_directory(root, create=True)
    path = directory / f"{audit_id}.json"
    document = {
        "schema_version": AUDIT_PAYLOAD_SCHEMA_VERSION,
        "audit_id": audit_id,
        **payload,
    }
    try:
        write_json_atomic(path, document)
        os.chmod(path, 0o600)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {
        "payload_path": path.relative_to(root).as_posix(),
        "payload_sha256": _file_sha256(path),
    }


def read_query_audit_payload(
    root: Path,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    path = _payload_path(root, evidence)
    expected = evidence.get("payload_sha256")
    actual = _file_sha256(path)
    if not isinstance(expected, str) or actual != expected:
        raise StateError(
            "query audit payload checksum does not match",
            error_code="QUERY_AUDIT_PAYLOAD_CHECKSUM_MISMATCH",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(
            "query audit payload cannot be read",
            error_code="QUERY_AUDIT_PAYLOAD_INVALID",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != AUDIT_PAYLOAD_SCHEMA_VERSION
    ):
        raise StateError(
            "query audit payload schema is invalid",
            error_code="QUERY_AUDIT_PAYLOAD_INVALID",
        )
    return payload


def delete_query_audit_payload(
    root: Path,
    evidence: dict[str, Any],
) -> bool:
    raw_path = evidence.get("payload_path")
    if raw_path is None:
        return False
    path = _payload_path(root, evidence)
    read_query_audit_payload(root, evidence)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _payload_path(root: Path, evidence: dict[str, Any]) -> Path:
    raw_path = evidence.get("payload_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise StateError(
            "query audit payload path is missing",
            error_code="QUERY_AUDIT_PAYLOAD_MISSING",
        )
    root_resolved = root.resolve()
    expected_parent = _review_directory(root_resolved, create=False)
    path = (root_resolved / raw_path).resolve()
    if path.parent != expected_parent or path.suffix != ".json":
        raise StateError(
            "query audit payload path is outside the review directory",
            error_code="QUERY_AUDIT_PAYLOAD_INVALID",
        )
    if not path.is_file():
        raise StateError(
            "query audit payload does not exist",
            error_code="QUERY_AUDIT_PAYLOAD_MISSING",
        )
    return path


def _review_directory(root: Path, *, create: bool) -> Path:
    root_resolved = root.resolve()
    expected = root_resolved / "artifacts" / "query-audit" / "open"
    if create:
        expected.mkdir(parents=True, exist_ok=True)
    if not expected.is_dir() or expected.resolve() != expected:
        raise StateError(
            "query audit review directory is invalid",
            error_code="QUERY_AUDIT_PAYLOAD_INVALID",
        )
    if create:
        os.chmod(expected, 0o700)
    return expected


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()

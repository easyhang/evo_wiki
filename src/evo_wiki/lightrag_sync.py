"""Pure contracts for interpreting asynchronous LightRAG track status.

This module intentionally contains no HTTP, retry, sleep, or persistence logic.
It converts an untrusted service payload into a small, immutable snapshot that a
future polling workflow can consume without retaining document content or remote
error messages.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class RemoteTrackState(str, Enum):
    """Aggregate state of one LightRAG asynchronous submission."""

    WAITING = "waiting"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    INVALID = "invalid"


@dataclass(frozen=True)
class TrackSnapshot:
    """Sanitized, immutable view of a LightRAG track-status response."""

    track_id: str | None
    state: RemoteTrackState
    document_count: int
    status_counts: tuple[tuple[str, int], ...]
    total_chunks: int
    unknown_statuses: tuple[str, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_counts", tuple(self.status_counts))
        object.__setattr__(self, "unknown_statuses", tuple(self.unknown_statuses))


_PROCESSING_STATUSES = {
    "pending",
    "parsing",
    "analyzing",
    "processing",
    "preprocessed",
}
_KNOWN_STATUSES = _PROCESSING_STATUSES | {"processed", "failed"}
_MAX_UNKNOWN_STATUS_LENGTH = 64


def parse_track_status(payload: object, expected_track_id: str) -> TrackSnapshot:
    """Parse one LightRAG track response using fail-closed aggregation rules.

    ``documents[*].status`` is the only status source. The service's
    ``status_summary`` is deliberately ignored because its key format has varied
    between observed LightRAG responses.
    """

    safe_track_id = expected_track_id if _is_non_empty_string(expected_track_id) else None
    if safe_track_id is None:
        return _invalid_snapshot(None, "EXPECTED_TRACK_ID_INVALID")
    if not isinstance(payload, Mapping):
        return _invalid_snapshot(safe_track_id, "PAYLOAD_NOT_OBJECT")

    payload_track_id = payload.get("track_id")
    if not _is_non_empty_string(payload_track_id):
        return _invalid_snapshot(safe_track_id, "TRACK_ID_INVALID")
    if payload_track_id != safe_track_id:
        return _invalid_snapshot(safe_track_id, "TRACK_ID_MISMATCH")

    documents = payload.get("documents")
    if not isinstance(documents, list):
        return _invalid_snapshot(safe_track_id, "DOCUMENTS_INVALID")

    total_count = payload.get("total_count")
    if not _is_non_negative_int(total_count):
        return _invalid_snapshot(
            safe_track_id,
            "TOTAL_COUNT_INVALID",
            document_count=len(documents),
        )
    if total_count != len(documents):
        return _invalid_snapshot(
            safe_track_id,
            "TOTAL_COUNT_MISMATCH",
            document_count=len(documents),
        )

    if not documents:
        return TrackSnapshot(
            track_id=safe_track_id,
            state=RemoteTrackState.WAITING,
            document_count=0,
            status_counts=(),
            total_chunks=0,
        )

    statuses: list[str] = []
    total_chunks = 0
    for document in documents:
        if not isinstance(document, Mapping):
            return _invalid_snapshot(
                safe_track_id,
                "DOCUMENT_INVALID",
                document_count=len(documents),
                statuses=statuses,
                total_chunks=total_chunks,
            )

        document_track_id = document.get("track_id")
        if document_track_id is not None:
            if not _is_non_empty_string(document_track_id):
                return _invalid_snapshot(
                    safe_track_id,
                    "DOCUMENT_TRACK_ID_INVALID",
                    document_count=len(documents),
                    statuses=statuses,
                    total_chunks=total_chunks,
                )
            if document_track_id != safe_track_id:
                return _invalid_snapshot(
                    safe_track_id,
                    "DOCUMENT_TRACK_ID_MISMATCH",
                    document_count=len(documents),
                    statuses=statuses,
                    total_chunks=total_chunks,
                )

        raw_status = document.get("status")
        if not _is_non_empty_string(raw_status):
            return _invalid_snapshot(
                safe_track_id,
                "DOCUMENT_STATUS_INVALID",
                document_count=len(documents),
                statuses=statuses,
                total_chunks=total_chunks,
            )
        status = raw_status.strip().lower()
        safe_status = (
            status
            if status in _KNOWN_STATUSES
            else _sanitize_unknown_status(status)
        )
        statuses.append(safe_status)
        if status not in _KNOWN_STATUSES:
            return _invalid_snapshot(
                safe_track_id,
                "UNKNOWN_DOCUMENT_STATUS",
                document_count=len(documents),
                statuses=statuses,
                total_chunks=total_chunks,
                unknown_statuses=(safe_status,),
            )

        chunks_count = document.get("chunks_count")
        if chunks_count is not None:
            if not _is_non_negative_int(chunks_count):
                return _invalid_snapshot(
                    safe_track_id,
                    "CHUNK_COUNT_INVALID",
                    document_count=len(documents),
                    statuses=statuses,
                    total_chunks=total_chunks,
                )
            total_chunks += chunks_count

        if status == "processed" and not (
            _is_non_negative_int(chunks_count) and chunks_count > 0
        ):
            return _invalid_snapshot(
                safe_track_id,
                "PROCESSED_CHUNK_COUNT_INVALID",
                document_count=len(documents),
                statuses=statuses,
                total_chunks=total_chunks,
            )

    status_counts = _freeze_status_counts(statuses)
    if "failed" in statuses:
        state = RemoteTrackState.FAILED
    elif all(status == "processed" for status in statuses):
        state = RemoteTrackState.PROCESSED
    else:
        state = RemoteTrackState.PROCESSING

    return TrackSnapshot(
        track_id=safe_track_id,
        state=state,
        document_count=len(documents),
        status_counts=status_counts,
        total_chunks=total_chunks,
    )


def _invalid_snapshot(
    track_id: str | None,
    error_code: str,
    *,
    document_count: int = 0,
    statuses: list[str] | None = None,
    total_chunks: int = 0,
    unknown_statuses: tuple[str, ...] = (),
) -> TrackSnapshot:
    return TrackSnapshot(
        track_id=track_id,
        state=RemoteTrackState.INVALID,
        document_count=document_count,
        status_counts=_freeze_status_counts(statuses or []),
        total_chunks=total_chunks,
        unknown_statuses=tuple(sorted(set(unknown_statuses))),
        error_code=error_code,
    )


def _freeze_status_counts(statuses: list[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(Counter(statuses).items()))


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sanitize_unknown_status(status: str) -> str:
    printable = "".join(
        character if character.isascii() and (character.isalnum() or character in "_.:-") else "_"
        for character in status
    )
    return printable[:_MAX_UNKNOWN_STATUS_LENGTH]

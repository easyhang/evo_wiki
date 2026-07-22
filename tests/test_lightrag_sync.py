from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from evo_wiki.lightrag_sync import (
    RemoteTrackState,
    TrackSnapshot,
    parse_track_status,
)


TRACK_ID = "insert_20260715_083244_c6597d74"


def document(status: str, chunks_count: int | None = None, **extra: object) -> dict:
    return {
        "status": status,
        "chunks_count": chunks_count,
        "track_id": TRACK_ID,
        **extra,
    }


def payload(documents: list[object], **extra: object) -> dict:
    return {
        "track_id": TRACK_ID,
        "documents": documents,
        "total_count": len(documents),
        **extra,
    }


def test_parses_observed_pending_response_shape_without_retaining_content():
    response = payload(
        [
            document(
                "pending",
                content_summary="sensitive source text",
                error_msg="sensitive provider error",
            )
        ],
        status_summary={"DocStatus.PENDING": 1},
    )

    snapshot = parse_track_status(response, TRACK_ID)

    assert snapshot == TrackSnapshot(
        track_id=TRACK_ID,
        state=RemoteTrackState.PROCESSING,
        document_count=1,
        status_counts=(("pending", 1),),
        total_chunks=0,
    )
    assert "sensitive" not in repr(snapshot)


@pytest.mark.parametrize(
    "remote_status",
    ["pending", "parsing", "analyzing", "processing", "preprocessed"],
)
def test_transitional_document_statuses_are_processing(remote_status: str):
    snapshot = parse_track_status(payload([document(remote_status)]), TRACK_ID)

    assert snapshot.state is RemoteTrackState.PROCESSING
    assert snapshot.error_code is None


def test_all_processed_requires_chunks_and_sums_them():
    snapshot = parse_track_status(
        payload([document("processed", 3), document("processed", 4)]),
        TRACK_ID,
    )

    assert snapshot.state is RemoteTrackState.PROCESSED
    assert snapshot.document_count == 2
    assert snapshot.status_counts == (("processed", 2),)
    assert snapshot.total_chunks == 7


def test_mixed_processed_and_pending_is_processing():
    snapshot = parse_track_status(
        payload([document("processed", 3), document("pending")]),
        TRACK_ID,
    )

    assert snapshot.state is RemoteTrackState.PROCESSING
    assert dict(snapshot.status_counts) == {"pending": 1, "processed": 1}
    assert snapshot.total_chunks == 3


def test_any_well_formed_failed_document_makes_track_failed():
    snapshot = parse_track_status(
        payload(
            [
                document("processed", 3),
                document("failed", error_msg="do not retain this"),
            ]
        ),
        TRACK_ID,
    )

    assert snapshot.state is RemoteTrackState.FAILED
    assert snapshot.error_code is None
    assert "do not retain this" not in repr(snapshot)


def test_empty_document_list_is_waiting_not_processed():
    snapshot = parse_track_status(
        payload([], status_summary={"DocStatus.PROCESSED": 1}),
        TRACK_ID,
    )

    assert snapshot.state is RemoteTrackState.WAITING
    assert snapshot.document_count == 0
    assert snapshot.total_chunks == 0


def test_unknown_status_is_invalid_and_safely_reported():
    snapshot = parse_track_status(
        payload([document("future_remote_state\nforged-log", 1)]),
        TRACK_ID,
    )

    assert snapshot.state is RemoteTrackState.INVALID
    assert snapshot.error_code == "UNKNOWN_DOCUMENT_STATUS"
    assert snapshot.unknown_statuses == ("future_remote_state_forged-log",)
    assert snapshot.status_counts == (("future_remote_state_forged-log", 1),)


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (None, "PAYLOAD_NOT_OBJECT"),
        ([], "PAYLOAD_NOT_OBJECT"),
        ({"track_id": TRACK_ID, "documents": {}, "total_count": 0}, "DOCUMENTS_INVALID"),
        ({"track_id": TRACK_ID, "documents": [], "total_count": True}, "TOTAL_COUNT_INVALID"),
        (payload([document("pending")], total_count=2), "TOTAL_COUNT_MISMATCH"),
        (payload(["not-an-object"]), "DOCUMENT_INVALID"),
        (payload([document("")]), "DOCUMENT_STATUS_INVALID"),
        (payload([document("pending", chunks_count="1")]), "CHUNK_COUNT_INVALID"),
    ],
)
def test_malformed_payloads_fail_closed(response: object, error_code: str):
    snapshot = parse_track_status(response, TRACK_ID)

    assert snapshot.state is RemoteTrackState.INVALID
    assert snapshot.error_code == error_code
    assert snapshot.state is not RemoteTrackState.PROCESSED


def test_top_level_track_id_mismatch_is_invalid():
    response = payload([document("processed", 2)])
    response["track_id"] = "another-track"

    snapshot = parse_track_status(response, TRACK_ID)

    assert snapshot.state is RemoteTrackState.INVALID
    assert snapshot.error_code == "TRACK_ID_MISMATCH"
    assert snapshot.track_id == TRACK_ID


def test_document_track_id_mismatch_is_invalid():
    snapshot = parse_track_status(
        payload([document("processed", 2, track_id="another-track")]),
        TRACK_ID,
    )

    assert snapshot.state is RemoteTrackState.INVALID
    assert snapshot.error_code == "DOCUMENT_TRACK_ID_MISMATCH"


@pytest.mark.parametrize("chunks_count", [None, 0, -1, True, "2"])
def test_processed_document_requires_positive_integer_chunk_count(
    chunks_count: object,
):
    snapshot = parse_track_status(
        payload([document("processed", chunks_count)]),
        TRACK_ID,
    )

    assert snapshot.state is RemoteTrackState.INVALID
    assert snapshot.error_code in {
        "CHUNK_COUNT_INVALID",
        "PROCESSED_CHUNK_COUNT_INVALID",
    }


def test_document_status_wins_over_untrusted_status_summary():
    snapshot = parse_track_status(
        payload(
            [document("processed", 2)],
            status_summary={"DocStatus.FAILED": 100},
        ),
        TRACK_ID,
    )

    assert snapshot.state is RemoteTrackState.PROCESSED


def test_snapshot_is_immutable():
    snapshot = parse_track_status(payload([document("pending")]), TRACK_ID)

    with pytest.raises(FrozenInstanceError):
        snapshot.state = RemoteTrackState.PROCESSED  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.status_counts[0] = ("processed", 1)  # type: ignore[index]

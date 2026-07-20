from __future__ import annotations

import json
import stat
from argparse import Namespace
from pathlib import Path

import pytest

from evo_wiki.journal import (
    EVENTS_FILENAME,
    MAX_EVENT_BYTES,
    JournalError,
    RunJournalWriter,
    sha256_bytes,
    verify_journal,
    verify_logs_root,
    verify_run_journal,
)
from evo_wiki.journal_legacy import migrate_legacy_journal
from evo_wiki.paths import ProjectPaths
from evo_wiki.utils import write_json_atomic


def _complete_journal(logs_root: Path, *, run_id: str = "run-test-native"):
    writer = RunJournalWriter(logs_root, run_id=run_id)
    writer.append(
        event_type="orchestration.run_started",
        phase="start",
        status="RUNNING",
        lane="orchestration",
        safe_payload={"change_counts": {"added": 1}},
    )
    writer.append(
        event_type="orchestration.run_completed",
        phase="finish",
        status="SUCCEEDED",
        lane="orchestration",
        safe_payload={"exit_code": 0},
    )
    return writer


def _load_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_lines(path: Path, lines: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for line in lines
        ),
        encoding="utf-8",
    )


def _error_codes(report: dict) -> set[str]:
    return {error["code"] for error in report["errors"]}


def test_writer_creates_verifiable_private_hash_chain(tmp_path: Path):
    logs_root = tmp_path / "logs"
    writer = _complete_journal(logs_root)

    report = verify_logs_root(logs_root)

    assert report["status"] == "ok"
    assert report["run_count"] == 1
    run = report["runs"][0]
    assert run["event_count"] == 2
    assert run["terminal_event"] is True
    events = _load_lines(writer.events_path)
    assert [event["sequence_no"] for event in events] == [1, 2]
    assert events[0]["previous_event_hash"] is None
    assert events[1]["previous_event_hash"] == events[0]["event_hash"]
    assert stat.S_IMODE(writer.run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(writer.events_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(writer.lock_path.stat().st_mode) == 0o600


def test_rotation_preserves_one_cross_file_hash_chain(tmp_path: Path):
    writer = RunJournalWriter(
        tmp_path / "logs",
        run_id="run-rotation",
        max_events_per_file=2,
    )
    for sequence in range(1, 6):
        writer.append(
            event_type=(
                "orchestration.run_completed"
                if sequence == 5
                else "experiment.synthetic_event"
            ),
            phase="finish" if sequence == 5 else "probe",
            status="SUCCEEDED" if sequence == 5 else "RUNNING",
            lane="orchestration" if sequence == 5 else "experiment",
        )

    files = sorted(writer.run_dir.glob("events-*.jsonl"))
    assert [path.name for path in files] == [
        "events-000001.jsonl",
        "events-000002.jsonl",
        "events-000003.jsonl",
    ]
    assert [len(_load_lines(path)) for path in files] == [2, 2, 1]
    first = _load_lines(files[0])
    second = _load_lines(files[1])
    assert second[0]["sequence_no"] == 3
    assert second[0]["previous_event_hash"] == first[-1]["event_hash"]
    report = verify_run_journal(writer.run_dir, expected_run_id=writer.run_id)
    assert report["status"] == "ok"
    assert report["event_count"] == 5
    assert report["files"] == [path.name for path in files]


def test_rotation_file_gap_blocks_verification_and_further_append(tmp_path: Path):
    writer = RunJournalWriter(
        tmp_path / "logs",
        run_id="run-rotation-gap",
        max_events_per_file=1,
    )
    writer.append(
        event_type="experiment.synthetic_event",
        phase="probe",
        status="RUNNING",
        lane="experiment",
    )
    writer.append(
        event_type="orchestration.run_completed",
        phase="finish",
        status="SUCCEEDED",
        lane="orchestration",
    )
    writer.append(
        event_type="experiment.synthetic_event",
        phase="probe",
        status="RUNNING",
        lane="experiment",
    )
    second = writer.run_dir / "events-000002.jsonl"
    second.unlink()

    report = verify_run_journal(writer.run_dir, expected_run_id=writer.run_id)
    assert report["status"] == "failed"
    assert "JOURNAL_FILE_SEQUENCE_MISMATCH" in _error_codes(report)
    with pytest.raises(JournalError) as caught:
        writer.append(
            event_type="experiment.synthetic_event",
            phase="probe",
            status="RUNNING",
            lane="experiment",
        )
    assert caught.value.error_code == "JOURNAL_CORRUPT"


def test_rotation_limits_reject_unsafe_values(tmp_path: Path):
    with pytest.raises(JournalError) as caught:
        RunJournalWriter(tmp_path / "logs", max_events_per_file=0)
    assert caught.value.error_code == "JOURNAL_CONFIG_INVALID"
    with pytest.raises(JournalError) as caught:
        RunJournalWriter(tmp_path / "logs", max_bytes_per_file=1)
    assert caught.value.error_code == "JOURNAL_CONFIG_INVALID"


def test_rotation_starts_a_new_file_before_exceeding_byte_limit(tmp_path: Path):
    writer = RunJournalWriter(
        tmp_path / "logs",
        run_id="run-rotation-bytes",
        max_events_per_file=100,
        max_bytes_per_file=MAX_EVENT_BYTES,
    )
    writer.append(
        event_type="experiment.synthetic_event",
        phase="probe",
        status="RUNNING",
        lane="experiment",
        safe_payload={"padding": "x" * 64_000},
    )
    remaining_bytes = MAX_EVENT_BYTES - writer.events_path.stat().st_size
    writer.append(
        event_type="orchestration.run_completed",
        phase="finish",
        status="SUCCEEDED",
        lane="orchestration",
        safe_payload={"padding": "x" * remaining_bytes},
    )

    files = sorted(writer.run_dir.glob("events-*.jsonl"))
    assert [path.name for path in files] == [
        "events-000001.jsonl",
        "events-000002.jsonl",
    ]
    assert all(path.stat().st_size <= MAX_EVENT_BYTES for path in files)
    assert verify_run_journal(writer.run_dir, expected_run_id=writer.run_id)["status"] == "ok"


def test_incomplete_chain_is_warning_and_cli_safe(tmp_path: Path):
    writer = RunJournalWriter(tmp_path / "logs", run_id="run-incomplete")
    writer.append(
        event_type="orchestration.run_started",
        phase="start",
        status="RUNNING",
        lane="orchestration",
    )

    report = verify_journal(writer.events_path, expected_run_id=writer.run_id)

    assert report["status"] == "warning"
    assert report["terminal_event"] is False
    assert report["warnings"] == [{"code": "JOURNAL_TERMINAL_EVENT_MISSING"}]
    assert "safe_payload" not in json.dumps(report)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda lines: lines[0]["safe_payload"].update({"tampered": True}),
            "JOURNAL_EVENT_HASH_MISMATCH",
        ),
        (
            lambda lines: lines[1].update({"previous_event_hash": None}),
            "JOURNAL_PREVIOUS_HASH_MISMATCH",
        ),
        (
            lambda lines: lines[1].update({"sequence_no": 7}),
            "JOURNAL_SEQUENCE_MISMATCH",
        ),
        (
            lambda lines: lines[1].update({"run_id": "run-other"}),
            "JOURNAL_RUN_ID_MISMATCH",
        ),
        (
            lambda lines: lines[1].update({"event_id": lines[0]["event_id"]}),
            "JOURNAL_EVENT_ID_DUPLICATE",
        ),
    ],
)
def test_verifier_detects_contract_and_chain_tampering(
    tmp_path: Path,
    mutation,
    expected_code: str,
):
    writer = _complete_journal(tmp_path / "logs")
    lines = _load_lines(writer.events_path)
    mutation(lines)
    _write_lines(writer.events_path, lines)

    report = verify_journal(writer.events_path, expected_run_id=writer.run_id)

    assert report["status"] == "failed"
    assert expected_code in _error_codes(report)
    serialized = json.dumps(report)
    assert "tampered" not in serialized
    assert "safe_payload" not in serialized


def test_writer_refuses_to_extend_a_tampered_chain(tmp_path: Path):
    writer = _complete_journal(tmp_path / "logs")
    lines = _load_lines(writer.events_path)
    lines[0]["safe_payload"]["tampered"] = True
    _write_lines(writer.events_path, lines)

    with pytest.raises(JournalError) as caught:
        writer.append(
            event_type="orchestration.run_completed",
            phase="finish",
            status="SUCCEEDED",
            lane="orchestration",
        )

    assert caught.value.error_code == "JOURNAL_CORRUPT"


def test_verifier_detects_truncated_invalid_utf8_and_oversized_lines(tmp_path: Path):
    truncated = tmp_path / "run-truncated" / EVENTS_FILENAME
    truncated.parent.mkdir()
    truncated.write_bytes(b'{"incomplete":true}')
    truncated_report = verify_journal(
        truncated,
        expected_run_id="run-truncated",
    )
    assert "JOURNAL_TRUNCATED_LINE" in _error_codes(truncated_report)

    invalid_utf8 = tmp_path / "run-invalid" / EVENTS_FILENAME
    invalid_utf8.parent.mkdir()
    invalid_utf8.write_bytes(b"\xff\xfe\n")
    invalid_report = verify_journal(invalid_utf8, expected_run_id="run-invalid")
    assert "JOURNAL_EVENT_INVALID" in _error_codes(invalid_report)

    oversized = tmp_path / "run-oversized" / EVENTS_FILENAME
    oversized.parent.mkdir()
    oversized.write_bytes(b"x" * (MAX_EVENT_BYTES + 1) + b"\n")
    oversized_report = verify_journal(
        oversized,
        expected_run_id="run-oversized",
    )
    assert "JOURNAL_EVENT_TOO_LARGE" in _error_codes(oversized_report)


def test_writer_rejects_event_larger_than_64_kib(tmp_path: Path):
    writer = RunJournalWriter(tmp_path / "logs", run_id="run-too-large")

    with pytest.raises(JournalError) as caught:
        writer.append(
            event_type="orchestration.run_started",
            phase="start",
            status="RUNNING",
            lane="orchestration",
            safe_payload={"content": "x" * MAX_EVENT_BYTES},
        )

    assert caught.value.error_code == "JOURNAL_EVENT_TOO_LARGE"
    assert not writer.events_path.exists()


def test_unexpected_run_error_writes_sanitized_failed_terminal_event(
    tmp_path: Path,
    monkeypatch,
):
    from evo_wiki import cli
    from evo_wiki.config import EvoConfig

    paths = ProjectPaths.from_root(tmp_path / "project")
    paths.ensure_base_dirs()
    EvoConfig.write_defaults(paths.root)

    def explode(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_INTERNAL_DETAILS_MUST_NOT_BE_JOURNALED")

    monkeypatch.setattr(cli, "render_wiki", explode)
    with pytest.raises(RuntimeError):
        cli.cmd_run(
            Namespace(
                root=str(paths.root),
                lane="wiki",
                lightrag_dry_run=False,
                smoke_query=None,
            )
        )

    journals = sorted((paths.artifacts / "logs" / "runs").glob(f"*/{EVENTS_FILENAME}"))
    assert len(journals) == 1
    events = _load_lines(journals[0])
    assert [event["event_type"] for event in events] == [
        "orchestration.run_started",
        "orchestration.run_failed",
    ]
    assert events[-1]["safe_payload"]["error_code"] == "UNEXPECTED_RUN_ERROR"
    assert "SYNTHETIC_INTERNAL_DETAILS_MUST_NOT_BE_JOURNALED" not in json.dumps(events)
    assert verify_journal(
        journals[0],
        expected_run_id=journals[0].parent.name,
    )["status"] == "ok"


def _legacy_bytes(secret: str = "SYNTHETIC_SECRET_DO_NOT_COPY") -> bytes:
    records = [
        {
            "schema_version": 1,
            "event": "run_started",
            "status": "running",
            "selected_lanes": ["wiki"],
            "change_set": {
                "added": ["sensitive-file-name.md"],
                "modified": [],
                "deleted": [],
            },
            "root": "/private/legacy/root",
            "api_key": secret,
        },
        {
            "schema_version": 1,
            "event": "run_finished",
            "status": "success",
            "selected_lanes": ["wiki"],
            "change_set": {"added": [], "modified": [], "deleted": []},
            "exit_code": 0,
        },
    ]
    return "".join(json.dumps(record) + "\n" for record in records).encode()


def test_legacy_migration_dry_run_apply_redaction_archive_and_noop(tmp_path: Path):
    logs_root = tmp_path / "logs"
    logs_root.mkdir()
    source = logs_root / "evo-wiki-events.jsonl"
    original = _legacy_bytes()
    source.write_bytes(original)

    dry_run = migrate_legacy_journal(logs_root, apply=False)
    assert dry_run["status"] == "ok"
    assert dry_run["result"] == "ready"
    assert dry_run["writes_performed"] is False
    assert source.exists()
    assert not (logs_root / "runs").exists()

    applied = migrate_legacy_journal(logs_root, apply=True)
    assert applied["status"] == "ok"
    assert applied["result"] == "applied"
    assert not source.exists()
    archive = logs_root / applied["archive"]
    assert archive.read_bytes() == original
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    target = logs_root / "runs" / applied["target_run_id"] / EVENTS_FILENAME
    migrated_text = target.read_text(encoding="utf-8")
    assert "SYNTHETIC_SECRET_DO_NOT_COPY" not in migrated_text
    assert "sensitive-file-name.md" not in migrated_text
    assert "/private/legacy/root" not in migrated_text
    assert all(
        event["provenance"] == "legacy_unverified"
        for event in _load_lines(target)
    )

    repeated = migrate_legacy_journal(logs_root, apply=True)
    assert repeated["status"] == "ok"
    assert repeated["result"] == "already_applied"
    assert verify_logs_root(logs_root)["status"] == "ok"


def test_prepared_legacy_migration_reconciles_without_source(tmp_path: Path):
    logs_root = tmp_path / "logs"
    logs_root.mkdir()
    source = logs_root / "evo-wiki-events.jsonl"
    source.write_bytes(_legacy_bytes())
    applied = migrate_legacy_journal(logs_root, apply=True)
    manifest_path = logs_root / applied["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "prepared"
    manifest["completed_at"] = None
    write_json_atomic(manifest_path, manifest)

    recovered = migrate_legacy_journal(logs_root, apply=True)

    assert recovered["status"] == "ok"
    assert recovered["result"] == "reconciled"
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"


def test_conflicting_partial_legacy_migration_is_never_overwritten(tmp_path: Path):
    logs_root = tmp_path / "logs"
    logs_root.mkdir()
    source = logs_root / "evo-wiki-events.jsonl"
    original = _legacy_bytes()
    source.write_bytes(original)
    source_hash = sha256_bytes(original)
    run_id = f"legacy-{source_hash.removeprefix('sha256:')[:16]}"
    writer = RunJournalWriter(
        logs_root,
        run_id=run_id,
        provenance="legacy_unverified",
    )
    writer.append(
        event_type="legacy.event_imported",
        phase="legacy_import",
        status="IMPORTED",
        lane="migration",
        safe_payload={
            "source_line_no": 1,
            "source_event_sha256": f"sha256:{'0' * 64}",
        },
    )
    before = writer.events_path.read_bytes()

    with pytest.raises(JournalError) as caught:
        migrate_legacy_journal(logs_root, apply=True)

    assert caught.value.error_code == "LEGACY_MIGRATION_CONFLICT"
    assert source.read_bytes() == original
    assert writer.events_path.read_bytes() == before


def test_completed_manifest_path_escape_is_rejected(tmp_path: Path):
    logs_root = tmp_path / "logs"
    logs_root.mkdir()
    source = logs_root / "evo-wiki-events.jsonl"
    source.write_bytes(_legacy_bytes())
    applied = migrate_legacy_journal(logs_root, apply=True)
    manifest_path = logs_root / applied["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["archive"] = "../../outside.jsonl"
    write_json_atomic(manifest_path, manifest)

    with pytest.raises(JournalError) as caught:
        migrate_legacy_journal(logs_root, apply=True)

    assert caught.value.error_code == "LEGACY_MIGRATION_CONFLICT"


def test_empty_legacy_log_is_not_migrated(tmp_path: Path):
    logs_root = tmp_path / "logs"
    logs_root.mkdir()
    source = logs_root / "evo-wiki-events.jsonl"
    source.write_bytes(b"")

    report = migrate_legacy_journal(logs_root, apply=True)

    assert report["status"] == "failed"
    assert report["result"] == "invalid_legacy_log"
    assert source.exists()
    assert not (logs_root / "runs").exists()

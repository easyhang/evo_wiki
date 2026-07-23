from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .journal import (
    EVENTS_FILENAME,
    JournalError,
    JournalEvent,
    RunJournalWriter,
    exclusive_lock,
    sha256_bytes,
    verify_journal,
)
from .utils import read_json, utc_now, write_json_atomic


LEGACY_FILENAME = "evo-wiki-events.jsonl"
LEGACY_EVENT_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def migrate_legacy_journal(logs_root: Path, *, apply: bool = False) -> dict[str, Any]:
    """Dry-run or apply a deterministic, explicitly unverified legacy import."""
    source_path = logs_root / LEGACY_FILENAME
    if not source_path.exists():
        recovery = _recover_prepared_migration(logs_root, apply=apply)
        if recovery is not None:
            return recovery
        completed = _latest_completed_manifest(logs_root)
        if completed is not None:
            _, completed_manifest = completed
            _verify_manifest_artifacts(logs_root, completed_manifest)
            return {
                "status": "ok",
                "mode": "apply" if apply else "dry_run",
                "result": "already_applied",
                "migration": completed_manifest,
            }
        return {
            "status": "warning",
            "mode": "apply" if apply else "dry_run",
            "result": "legacy_log_not_found",
        }

    source_bytes = source_path.read_bytes()
    source_sha256 = sha256_bytes(source_bytes)
    sha16 = source_sha256.removeprefix("sha256:")[:16]
    run_id = f"legacy-{sha16}"
    records, parse_errors = _parse_legacy_lines(source_bytes)
    base_report = {
        "mode": "apply" if apply else "dry_run",
        "source": LEGACY_FILENAME,
        "source_sha256": source_sha256,
        "source_bytes": len(source_bytes),
        "line_count": len(records),
        "target_run_id": run_id,
        "provenance": "legacy_unverified",
    }
    if parse_errors:
        return {
            **base_report,
            "status": "failed",
            "result": "invalid_legacy_log",
            "errors": parse_errors,
        }
    if not apply:
        return {
            **base_report,
            "status": "ok",
            "result": "ready",
            "writes_performed": False,
        }

    _ensure_private_directory(logs_root)
    with exclusive_lock(logs_root / ".legacy-migration.lock"):
        current_bytes = source_path.read_bytes()
        if sha256_bytes(current_bytes) != source_sha256:
            raise JournalError(
                "legacy journal changed during migration",
                error_code="LEGACY_MIGRATION_CONFLICT",
            )
        return _apply_migration(
            logs_root,
            source_path=source_path,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            records=records,
            run_id=run_id,
            base_report=base_report,
        )


def _apply_migration(
    logs_root: Path,
    *,
    source_path: Path,
    source_bytes: bytes,
    source_sha256: str,
    records: list[tuple[int, bytes, dict[str, Any]]],
    run_id: str,
    base_report: dict[str, Any],
) -> dict[str, Any]:
    sha16 = source_sha256.removeprefix("sha256:")[:16]
    migrations_dir = logs_root / "migrations"
    legacy_dir = logs_root / "legacy"
    _ensure_private_directory(migrations_dir)
    _ensure_private_directory(legacy_dir)
    manifest_path = migrations_dir / f"{run_id}.json"
    archive_path = legacy_dir / f"evo-wiki-events.{sha16}.jsonl"
    writer = RunJournalWriter(
        logs_root,
        run_id=run_id,
        provenance="legacy_unverified",
    )

    existing_manifest = read_json(manifest_path, {})
    if existing_manifest:
        if existing_manifest.get("source_sha256") != source_sha256:
            raise JournalError(
                "legacy migration manifest conflicts with the source",
                error_code="LEGACY_MIGRATION_CONFLICT",
            )
        if existing_manifest.get("status") == "completed":
            _verify_completed_migration(
                writer.events_path,
                archive_path,
                source_sha256,
                run_id,
            )
            raise JournalError(
                "legacy source reappeared after a completed migration",
                error_code="LEGACY_MIGRATION_CONFLICT",
            )

    imported_count, terminal_present = _existing_import_prefix(
        writer.events_path,
        run_id=run_id,
        records=records,
    )
    if terminal_present and imported_count != len(records):
        raise JournalError(
            "legacy migration journal has a premature terminal event",
            error_code="LEGACY_MIGRATION_CONFLICT",
        )
    if not terminal_present:
        for line_no, raw_line, record in records[imported_count:]:
            writer.append(
                event_type="legacy.event_imported",
                phase="legacy_import",
                status="IMPORTED",
                lane="migration",
                safe_payload=_safe_legacy_payload(line_no, raw_line, record),
            )
        writer.append(
            event_type="legacy.import_completed",
            phase="legacy_import",
            status="COMPLETED",
            lane="migration",
            safe_payload={
                "imported_event_count": len(records),
                "source_sha256": source_sha256,
            },
        )

    verification = verify_journal(writer.events_path, expected_run_id=run_id)
    if verification["status"] != "ok":
        raise JournalError(
            "generated legacy migration journal did not verify",
            error_code="LEGACY_MIGRATION_CONFLICT",
        )

    manifest = {
        "schema_version": 1,
        "migration_id": run_id,
        "status": "prepared",
        "provenance": "legacy_unverified",
        "source_sha256": source_sha256,
        "source_bytes": len(source_bytes),
        "source_line_count": len(records),
        "target_run_id": run_id,
        "target_journal": _relative_to_logs(writer.events_path, logs_root),
        "archive": _relative_to_logs(archive_path, logs_root),
        "created_at": existing_manifest.get("created_at") or utc_now(),
        "completed_at": None,
    }
    write_json_atomic(manifest_path, manifest)
    manifest_path.chmod(0o600)

    if archive_path.exists():
        if sha256_bytes(archive_path.read_bytes()) != source_sha256:
            raise JournalError(
                "legacy archive path contains different bytes",
                error_code="LEGACY_MIGRATION_CONFLICT",
            )
        if source_path.exists():
            raise JournalError(
                "both source and completed archive exist",
                error_code="LEGACY_MIGRATION_CONFLICT",
            )
    else:
        os.replace(source_path, archive_path)
        archive_path.chmod(0o600)
        _fsync_directory(legacy_dir)
        _fsync_directory(logs_root)

    manifest["status"] = "completed"
    manifest["completed_at"] = utc_now()
    manifest["archive_sha256"] = sha256_bytes(archive_path.read_bytes())
    write_json_atomic(manifest_path, manifest)
    manifest_path.chmod(0o600)
    _fsync_directory(migrations_dir)
    return {
        **base_report,
        "status": "ok",
        "result": "applied",
        "writes_performed": True,
        "archive": _relative_to_logs(archive_path, logs_root),
        "manifest": _relative_to_logs(manifest_path, logs_root),
        "verification": verification,
    }


def _parse_legacy_lines(
    source_bytes: bytes,
) -> tuple[list[tuple[int, bytes, dict[str, Any]]], list[dict[str, Any]]]:
    records: list[tuple[int, bytes, dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(source_bytes.splitlines(keepends=True), start=1):
        content = raw_line.rstrip(b"\r\n")
        if not content:
            errors.append({"code": "LEGACY_EMPTY_LINE", "line_no": line_no})
            continue
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append({"code": "LEGACY_EVENT_INVALID", "line_no": line_no})
            continue
        if not isinstance(value, dict):
            errors.append({"code": "LEGACY_EVENT_INVALID", "line_no": line_no})
            continue
        records.append((line_no, raw_line, value))
    if source_bytes and not source_bytes.endswith((b"\n", b"\r")):
        errors.append({"code": "LEGACY_TRUNCATED_LINE", "line_no": len(records) + 1})
    if not records and not errors:
        errors.append({"code": "LEGACY_LOG_EMPTY", "line_no": None})
    return records, errors


def _safe_legacy_payload(
    line_no: int,
    raw_line: bytes,
    record: dict[str, Any],
) -> dict[str, Any]:
    raw_event_type = record.get("event")
    event_type = (
        raw_event_type
        if isinstance(raw_event_type, str)
        and LEGACY_EVENT_PATTERN.fullmatch(raw_event_type)
        else "unknown"
    )
    raw_status = record.get("status")
    status = raw_status[:32] if isinstance(raw_status, str) else "unknown"
    raw_lanes = record.get("selected_lanes")
    selected_lanes = (
        [
            lane[:64]
            for lane in raw_lanes
            if isinstance(lane, str) and lane in {"wiki", "lightrag"}
        ]
        if isinstance(raw_lanes, list)
        else []
    )
    change_set = record.get("change_set")
    change_counts = {}
    if isinstance(change_set, dict):
        for key in ("added", "modified", "deleted"):
            values = change_set.get(key)
            change_counts[key] = len(values) if isinstance(values, list) else 0
    payload: dict[str, Any] = {
        "source_line_no": line_no,
        "source_event_sha256": sha256_bytes(raw_line),
        "legacy_event_type": event_type,
        "legacy_status": status,
        "selected_lanes": selected_lanes,
        "change_counts": change_counts,
    }
    if isinstance(record.get("exit_code"), int) and not isinstance(
        record.get("exit_code"),
        bool,
    ):
        payload["exit_code"] = record["exit_code"]
    return payload


def _existing_import_prefix(
    events_path: Path,
    *,
    run_id: str,
    records: list[tuple[int, bytes, dict[str, Any]]],
) -> tuple[int, bool]:
    if not events_path.exists():
        return 0, False
    imported_count = 0
    terminal_present = False
    with events_path.open("rb") as handle:
        for raw_event in handle:
            try:
                event = JournalEvent.model_validate_json(raw_event)
            except ValidationError as exc:
                raise JournalError(
                    "partial migration journal is invalid",
                    error_code="LEGACY_MIGRATION_CONFLICT",
                ) from exc
            if event.run_id != run_id:
                raise JournalError(
                    "partial migration run ID is invalid",
                    error_code="LEGACY_MIGRATION_CONFLICT",
                )
            if event.event_type == "legacy.event_imported":
                if imported_count >= len(records):
                    raise JournalError(
                        "partial migration contains too many imported events",
                        error_code="LEGACY_MIGRATION_CONFLICT",
                    )
                line_no, raw_line, _ = records[imported_count]
                expected = {
                    "line": line_no,
                    "hash": sha256_bytes(raw_line),
                }
                actual = {
                    "line": event.safe_payload.get("source_line_no"),
                    "hash": event.safe_payload.get("source_event_sha256"),
                }
                if actual != expected:
                    raise JournalError(
                        "partial migration does not match the legacy source",
                        error_code="LEGACY_MIGRATION_CONFLICT",
                    )
                imported_count += 1
            elif event.event_type == "legacy.import_completed":
                terminal_present = True
            else:
                raise JournalError(
                    "partial migration contains an unexpected event",
                    error_code="LEGACY_MIGRATION_CONFLICT",
                )
    verification = verify_journal(events_path, expected_run_id=run_id)
    if verification["status"] == "failed":
        raise JournalError(
            "partial migration journal hash chain is invalid",
            error_code="LEGACY_MIGRATION_CONFLICT",
        )
    return imported_count, terminal_present


def _recover_prepared_migration(
    logs_root: Path,
    *,
    apply: bool,
) -> dict[str, Any] | None:
    migrations_dir = logs_root / "migrations"
    if not migrations_dir.exists():
        return None
    prepared = [
        (path, read_json(path, {}))
        for path in sorted(migrations_dir.glob("legacy-*.json"))
        if read_json(path, {}).get("status") == "prepared"
    ]
    if not prepared:
        return None
    if len(prepared) > 1:
        raise JournalError(
            "multiple prepared legacy migrations require manual audit",
            error_code="LEGACY_MIGRATION_CONFLICT",
        )
    manifest_path, manifest = prepared[0]
    if not apply:
        return {
            "status": "warning",
            "mode": "dry_run",
            "result": "prepared_migration_requires_apply",
            "migration": manifest,
        }
    archive_path = _resolve_under_logs(logs_root, manifest.get("archive"))
    events_path = _resolve_under_logs(logs_root, manifest.get("target_journal"))
    source_sha256 = manifest.get("source_sha256")
    run_id = manifest.get("target_run_id")
    if (
        not isinstance(source_sha256, str)
        or not isinstance(run_id, str)
        or not archive_path.exists()
        or sha256_bytes(archive_path.read_bytes()) != source_sha256
    ):
        raise JournalError(
            "prepared legacy migration cannot be reconciled",
            error_code="LEGACY_MIGRATION_CONFLICT",
        )
    verification = verify_journal(events_path, expected_run_id=run_id)
    if verification["status"] != "ok":
        raise JournalError(
            "prepared migration journal cannot be verified",
            error_code="LEGACY_MIGRATION_CONFLICT",
        )
    manifest["status"] = "completed"
    manifest["completed_at"] = utc_now()
    manifest["archive_sha256"] = source_sha256
    write_json_atomic(manifest_path, manifest)
    manifest_path.chmod(0o600)
    return {
        "status": "ok",
        "mode": "apply",
        "result": "reconciled",
        "migration": manifest,
        "verification": verification,
    }


def _latest_completed_manifest(
    logs_root: Path,
) -> tuple[Path, dict[str, Any]] | None:
    migrations_dir = logs_root / "migrations"
    if not migrations_dir.exists():
        return None
    completed = [
        (path, read_json(path, {}))
        for path in sorted(migrations_dir.glob("legacy-*.json"))
        if read_json(path, {}).get("status") == "completed"
    ]
    return completed[-1] if completed else None


def _verify_manifest_artifacts(
    logs_root: Path,
    manifest: dict[str, Any],
) -> None:
    run_id = manifest.get("target_run_id")
    source_sha256 = manifest.get("source_sha256")
    if not isinstance(run_id, str) or not isinstance(source_sha256, str):
        raise JournalError(
            "completed legacy migration manifest is invalid",
            error_code="LEGACY_MIGRATION_CONFLICT",
        )
    archive_path = _resolve_under_logs(logs_root, manifest.get("archive"))
    events_path = _resolve_under_logs(logs_root, manifest.get("target_journal"))
    _verify_completed_migration(
        events_path,
        archive_path,
        source_sha256,
        run_id,
    )


def _verify_completed_migration(
    events_path: Path,
    archive_path: Path,
    source_sha256: str,
    run_id: str,
) -> None:
    if (
        not archive_path.exists()
        or sha256_bytes(archive_path.read_bytes()) != source_sha256
        or verify_journal(events_path, expected_run_id=run_id)["status"] != "ok"
    ):
        raise JournalError(
            "completed legacy migration artifacts are inconsistent",
            error_code="LEGACY_MIGRATION_CONFLICT",
        )


def _relative_to_logs(path: Path, logs_root: Path) -> str:
    return path.resolve().relative_to(logs_root.resolve()).as_posix()


def _resolve_under_logs(logs_root: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise JournalError(
            "legacy migration manifest path is invalid",
            error_code="LEGACY_MIGRATION_CONFLICT",
        )
    candidate = (logs_root / raw_path).resolve()
    try:
        candidate.relative_to(logs_root.resolve())
    except ValueError as exc:
        raise JournalError(
            "legacy migration manifest path escapes the logs directory",
            error_code="LEGACY_MIGRATION_CONFLICT",
        ) from exc
    return candidate


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

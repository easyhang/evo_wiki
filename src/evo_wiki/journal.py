from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .utils import utc_now

try:  # Linux/macOS single-machine baseline.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on unsupported platforms
    fcntl = None  # type: ignore[assignment]


JOURNAL_SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 64 * 1024
EVENTS_FILENAME = "events-000001.jsonl"
EVENTS_FILENAME_PATTERN = re.compile(r"^events-(\d{6})\.jsonl$")
DEFAULT_MAX_EVENTS_PER_FILE = 5_000
DEFAULT_MAX_BYTES_PER_FILE = 64 * 1024 * 1024
LOCK_FILENAME = "journal.lock"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,127}")
HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
TERMINAL_EVENT_TYPES = {
    "orchestration.run_completed",
    "orchestration.run_failed",
    "legacy.import_completed",
    "state.migration_completed",
    "state.migration_failed",
    "state.migration_candidate_aborted",
    "state.schema_migration_completed",
    "state.schema_migration_failed",
    "state.backup_completed",
    "state.backup_failed",
    "state.reconcile_completed",
    "state.reconcile_failed",
    "state.replacement_completed",
    "state.replacement_rolled_back",
    "state.replacement_needs_audit",
    "state.replacement_blocked",
    "state.replacement_failed",
}


class JournalError(RuntimeError):
    """Safe journal failure suitable for CLI error reporting."""

    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = error_code


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JournalArtifactRef(StrictFrozenModel):
    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    media_type: str = Field(min_length=1, max_length=128)

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        candidate = PurePosixPath(normalized)
        if candidate.is_absolute() or ".." in candidate.parts or normalized != candidate.as_posix():
            raise ValueError("artifact path must be a normalized relative POSIX path")
        return normalized


class JournalEvent(StrictFrozenModel):
    schema_version: Literal[1] = JOURNAL_SCHEMA_VERSION
    event_id: str = Field(pattern=r"^evt-[0-9a-f]{32}$")
    sequence_no: int = Field(ge=1)
    run_id: str = Field(min_length=3, max_length=128)
    parent_run_id: str | None = Field(default=None, max_length=128)
    lane: str = Field(min_length=1, max_length=64)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    phase: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    safe_payload: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: tuple[JournalArtifactRef, ...] = ()
    previous_event_hash: str | None = None
    event_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    provenance: Literal["native", "legacy_unverified"] = "native"

    @field_validator("run_id", "parent_run_id")
    @classmethod
    def validate_run_id(cls, value: str | None) -> str | None:
        if value is not None and RUN_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("run ID contains unsupported characters")
        return value

    @field_validator("previous_event_hash")
    @classmethod
    def validate_previous_hash(cls, value: str | None) -> str | None:
        if value is not None and HASH_PATTERN.fullmatch(value) is None:
            raise ValueError("previous_event_hash is invalid")
        return value


def canonical_json(value: Any) -> str:
    """Return the version-1 canonical JSON representation used by the hash chain."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise JournalError(
            "journal payload is not canonical JSON",
            error_code="JOURNAL_PAYLOAD_INVALID",
        ) from exc


def sha256_value(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


def sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def compute_event_hash(event_data: dict[str, Any]) -> str:
    hash_input = dict(event_data)
    hash_input.pop("event_hash", None)
    return sha256_value(hash_input)


def new_run_id(prefix: str = "run") -> str:
    timestamp = utc_now().replace("-", "").replace(":", "")
    return f"{prefix}-{timestamp}-{uuid.uuid4().hex[:12]}"


class RunJournalWriter:
    """Single-journal append API with an OS file lock and fsync boundary."""

    def __init__(
        self,
        logs_root: Path,
        *,
        run_id: str | None = None,
        provenance: Literal["native", "legacy_unverified"] = "native",
        max_events_per_file: int = DEFAULT_MAX_EVENTS_PER_FILE,
        max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE,
    ):
        self.logs_root = logs_root
        self.run_id = run_id or new_run_id()
        if RUN_ID_PATTERN.fullmatch(self.run_id) is None:
            raise JournalError("run ID is invalid", error_code="JOURNAL_RUN_ID_INVALID")
        self.provenance = provenance
        self.max_events_per_file = _validate_rotation_limit(
            max_events_per_file,
            name="max_events_per_file",
            minimum=1,
        )
        self.max_bytes_per_file = _validate_rotation_limit(
            max_bytes_per_file,
            name="max_bytes_per_file",
            minimum=MAX_EVENT_BYTES,
        )
        self.run_dir = logs_root / "runs" / self.run_id
        self.events_path = self.run_dir / EVENTS_FILENAME
        self.lock_path = self.run_dir / LOCK_FILENAME

    def append(
        self,
        *,
        event_type: str,
        phase: str,
        status: str,
        lane: str,
        safe_payload: dict[str, Any] | None = None,
        artifact_refs: tuple[JournalArtifactRef, ...] = (),
        parent_run_id: str | None = None,
    ) -> JournalEvent:
        _ensure_private_directory(self.logs_root)
        _ensure_private_directory(self.logs_root / "runs")
        _ensure_private_directory(self.run_dir)

        with exclusive_lock(self.lock_path):
            if _event_paths(self.run_dir):
                verification = verify_run_journal(
                    self.run_dir,
                    expected_run_id=self.run_id,
                )
                if verification["status"] == "failed":
                    raise JournalError(
                        "journal hash chain is invalid; append refused",
                        error_code="JOURNAL_CORRUPT",
                    )
            current_path = _current_events_path(self.run_dir)
            previous = _read_last_event(current_path, expected_run_id=self.run_id)
            sequence_no = 1 if previous is None else previous.sequence_no + 1
            previous_hash = None if previous is None else previous.event_hash
            event_data: dict[str, Any] = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "event_id": f"evt-{uuid.uuid4().hex}",
                "sequence_no": sequence_no,
                "run_id": self.run_id,
                "parent_run_id": parent_run_id,
                "lane": lane,
                "event_type": event_type,
                "phase": phase,
                "status": status,
                "safe_payload": safe_payload or {},
                "artifact_refs": [
                    reference.model_dump(mode="json") for reference in artifact_refs
                ],
                "previous_event_hash": previous_hash,
                "created_at": utc_now(),
                "provenance": self.provenance,
            }
            event_data["event_hash"] = compute_event_hash(event_data)
            try:
                event = JournalEvent.model_validate(event_data)
            except ValidationError as exc:
                raise JournalError(
                    "journal event contract validation failed",
                    error_code="JOURNAL_EVENT_INVALID",
                ) from exc
            encoded = canonical_json(event.model_dump(mode="json")).encode("utf-8") + b"\n"
            if len(encoded) > MAX_EVENT_BYTES:
                raise JournalError(
                    "journal event exceeds the 64 KiB limit",
                    error_code="JOURNAL_EVENT_TOO_LARGE",
                )
            target_path = _rotation_target(
                current_path,
                encoded_size=len(encoded),
                current_event_count=_event_count_in_file(current_path),
                max_events_per_file=self.max_events_per_file,
                max_bytes_per_file=self.max_bytes_per_file,
            )
            _append_fsynced(target_path, encoded)
            return event


def verify_journal(
    events_path: Path,
    *,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    run_id = expected_run_id or events_path.parent.name
    if not events_path.exists():
        return _missing_journal_report(run_id, events_path.name)
    return _verify_event_paths(
        [events_path],
        run_id=run_id,
        journal_label=f"{run_id}/{events_path.name}",
    )


def verify_run_journal(
    run_dir: Path,
    *,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    run_id = expected_run_id or run_dir.name
    paths, layout_errors = _event_paths_with_errors(run_dir)
    if not paths:
        report = _missing_journal_report(run_id, EVENTS_FILENAME)
        report["errors"].extend(layout_errors)
        return report
    report = _verify_event_paths(
        paths,
        run_id=run_id,
        journal_label=f"{run_id}/",
    )
    report["files"] = [path.name for path in paths]
    if layout_errors:
        report["errors"].extend(layout_errors)
        report["status"] = "failed"
    return report


def _verify_event_paths(
    events_paths: list[Path],
    *,
    run_id: str,
    journal_label: str,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    previous_hash: str | None = None
    expected_sequence = 1
    event_count = 0
    event_ids: set[str] = set()
    last_event_type: str | None = None

    for events_path in events_paths:
        with events_path.open("rb") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                location = {"journal_file": events_path.name, "line_no": line_no}
                if len(raw_line) > MAX_EVENT_BYTES:
                    errors.append({"code": "JOURNAL_EVENT_TOO_LARGE", **location})
                    continue
                if not raw_line.endswith(b"\n"):
                    errors.append({"code": "JOURNAL_TRUNCATED_LINE", **location})
                content = raw_line.rstrip(b"\n")
                if not content:
                    errors.append({"code": "JOURNAL_EMPTY_LINE", **location})
                    continue
                try:
                    raw_event = json.loads(content.decode("utf-8"))
                    event = JournalEvent.model_validate(raw_event)
                except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
                    errors.append({"code": "JOURNAL_EVENT_INVALID", **location})
                    continue

                event_count += 1
                last_event_type = event.event_type
                if event.run_id != run_id:
                    errors.append(
                        {
                            "code": "JOURNAL_RUN_ID_MISMATCH",
                            **location,
                            "sequence_no": event.sequence_no,
                        }
                    )
                if event.sequence_no != expected_sequence:
                    errors.append(
                        {
                            "code": "JOURNAL_SEQUENCE_MISMATCH",
                            **location,
                            "sequence_no": event.sequence_no,
                        }
                    )
                if event.previous_event_hash != previous_hash:
                    errors.append(
                        {
                            "code": "JOURNAL_PREVIOUS_HASH_MISMATCH",
                            **location,
                            "sequence_no": event.sequence_no,
                        }
                    )
                calculated_hash = compute_event_hash(event.model_dump(mode="json"))
                if event.event_hash != calculated_hash:
                    errors.append(
                        {
                            "code": "JOURNAL_EVENT_HASH_MISMATCH",
                            **location,
                            "sequence_no": event.sequence_no,
                        }
                    )
                if event.event_id in event_ids:
                    errors.append(
                        {
                            "code": "JOURNAL_EVENT_ID_DUPLICATE",
                            **location,
                            "sequence_no": event.sequence_no,
                        }
                    )
                event_ids.add(event.event_id)
                previous_hash = event.event_hash
                # Re-synchronize after a detected gap so one missing segment does
                # not flood the safe verifier report with one error per later row.
                expected_sequence = event.sequence_no + 1

    terminal_event = last_event_type in TERMINAL_EVENT_TYPES
    if not errors and not terminal_event:
        warnings.append({"code": "JOURNAL_TERMINAL_EVENT_MISSING"})
    status = "failed" if errors else ("warning" if warnings else "ok")
    return {
        "run_id": run_id,
        "journal": journal_label,
        "status": status,
        "event_count": event_count,
        "terminal_event": terminal_event,
        "last_event_hash": previous_hash,
        "errors": errors,
        "warnings": warnings,
    }


def verify_logs_root(logs_root: Path, *, run_id: str | None = None) -> dict[str, Any]:
    if run_id is not None and RUN_ID_PATTERN.fullmatch(run_id) is None:
        return {
            "status": "failed",
            "run_count": 0,
            "runs": [],
            "errors": [{"code": "JOURNAL_RUN_ID_INVALID"}],
            "warnings": [],
        }

    runs_root = logs_root / "runs"
    if run_id is not None:
        candidates = [runs_root / run_id]
    elif runs_root.exists():
        candidates = sorted(
            path
            for path in runs_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
    else:
        candidates = []

    results = [
        verify_run_journal(
            candidate,
            expected_run_id=candidate.name,
        )
        for candidate in candidates
    ]
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not results:
        warnings.append({"code": "JOURNAL_RUNS_NOT_FOUND"})
    if (logs_root / "evo-wiki-events.jsonl").exists():
        warnings.append({"code": "LEGACY_JOURNAL_REQUIRES_MIGRATION"})
    if any(result["status"] == "failed" for result in results):
        errors.append({"code": "JOURNAL_VERIFY_FAILED"})
    elif any(result["status"] == "warning" for result in results):
        warnings.append({"code": "JOURNAL_RUN_INCOMPLETE"})
    status = "failed" if errors else ("warning" if warnings else "ok")
    return {
        "status": status,
        "run_count": len(results),
        "runs": results,
        "errors": errors,
        "warnings": warnings,
    }


def _missing_journal_report(run_id: str, filename: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "journal": f"{run_id}/{filename}",
        "status": "failed",
        "event_count": 0,
        "terminal_event": False,
        "errors": [{"code": "JOURNAL_NOT_FOUND", "line_no": None}],
        "warnings": [],
    }


def _event_paths(run_dir: Path) -> list[Path]:
    paths, _ = _event_paths_with_errors(run_dir)
    return paths


def _event_paths_with_errors(run_dir: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    if not run_dir.exists():
        return [], []
    candidates = sorted(run_dir.glob("events-*.jsonl"))
    paths: list[Path] = []
    errors: list[dict[str, Any]] = []
    indexes: list[int] = []
    for candidate in candidates:
        matched = EVENTS_FILENAME_PATTERN.fullmatch(candidate.name)
        if matched is None:
            errors.append(
                {
                    "code": "JOURNAL_FILE_NAME_INVALID",
                    "journal_file": candidate.name,
                }
            )
            continue
        paths.append(candidate)
        indexes.append(int(matched.group(1)))
    if indexes and indexes != list(range(1, len(indexes) + 1)):
        errors.append({"code": "JOURNAL_FILE_SEQUENCE_MISMATCH"})
    return paths, errors


def _current_events_path(run_dir: Path) -> Path:
    paths, errors = _event_paths_with_errors(run_dir)
    if errors:
        raise JournalError(
            "journal event-file layout is invalid",
            error_code="JOURNAL_CORRUPT",
        )
    return paths[-1] if paths else run_dir / EVENTS_FILENAME


def _rotation_target(
    current_path: Path,
    *,
    encoded_size: int,
    current_event_count: int,
    max_events_per_file: int,
    max_bytes_per_file: int,
) -> Path:
    current_size = current_path.stat().st_size if current_path.exists() else 0
    should_rotate = current_event_count > 0 and (
        current_event_count >= max_events_per_file
        or current_size + encoded_size > max_bytes_per_file
    )
    if not should_rotate:
        return current_path
    matched = EVENTS_FILENAME_PATTERN.fullmatch(current_path.name)
    if matched is None:
        raise JournalError(
            "current journal event file name is invalid",
            error_code="JOURNAL_CORRUPT",
        )
    target = current_path.with_name(f"events-{int(matched.group(1)) + 1:06d}.jsonl")
    if target.exists():
        raise JournalError(
            "journal rotation target already exists",
            error_code="JOURNAL_CORRUPT",
        )
    return target


def _event_count_in_file(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _validate_rotation_limit(value: object, *, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise JournalError(
            f"journal {name} is invalid",
            error_code="JOURNAL_CONFIG_INVALID",
        )
    return value


def _read_last_event(
    path: Path,
    *,
    expected_run_id: str,
) -> JournalEvent | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell() - 1
        if position < 0:
            return None
        handle.seek(position)
        if handle.read(1) != b"\n":
            raise JournalError(
                "journal ends with a partial event",
                error_code="JOURNAL_CORRUPT",
            )
        position -= 1
        while position >= 0:
            handle.seek(position)
            if handle.read(1) == b"\n":
                position += 1
                break
            position -= 1
        if position < 0:
            position = 0
        handle.seek(position)
        raw_line = handle.readline()
    try:
        event = JournalEvent.model_validate_json(raw_line)
    except ValidationError as exc:
        raise JournalError(
            "journal last event is invalid",
            error_code="JOURNAL_CORRUPT",
        ) from exc
    if event.run_id != expected_run_id:
        raise JournalError(
            "journal run ID does not match its directory",
            error_code="JOURNAL_CORRUPT",
        )
    return event


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise JournalError(
            "safe journal locking is unavailable on this platform",
            error_code="JOURNAL_LOCK_UNSUPPORTED",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _append_fsynced(path: Path, content: bytes) -> None:
    existed = path.exists()
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise JournalError(
                    "journal append made no progress",
                    error_code="JOURNAL_WRITE_FAILED",
                )
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if not existed:
        _fsync_directory(path.parent)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

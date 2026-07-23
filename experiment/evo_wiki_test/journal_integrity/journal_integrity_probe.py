from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from evo_wiki.journal import (
    EVENTS_FILENAME,
    RunJournalWriter,
    verify_journal,
    verify_run_journal,
)
from evo_wiki.journal_legacy import migrate_legacy_journal
from evo_wiki.utils import utc_now, write_json_atomic


EXPERIMENT_ROOT = Path(__file__).resolve().parent
RAW_ROOT = EXPERIMENT_ROOT / "raw"
SYNTHETIC_EVENT_COUNT = 200
SYNTHETIC_EVENTS_PER_FILE = 50
SYNTHETIC_SECRET = "SYNTHETIC_SECRET_DO_NOT_COPY"
RAW_RUN_ROOT = RAW_ROOT / f"probe-{uuid.uuid4().hex[:12]}"


def _create_case(name: str) -> Path:
    target = RAW_RUN_ROOT / name
    target.mkdir(parents=True)
    return target


def _write_lines(path: Path, lines: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for line in lines
        ),
        encoding="utf-8",
    )


def _build_baseline() -> tuple[Path, dict, int]:
    case_root = _create_case("baseline")
    logs_root = case_root / "logs"
    writer = RunJournalWriter(
        logs_root,
        run_id="run-integrity-baseline",
        max_events_per_file=SYNTHETIC_EVENTS_PER_FILE,
    )
    started = time.monotonic()
    writer.append(
        event_type="orchestration.run_started",
        phase="start",
        status="RUNNING",
        lane="orchestration",
        safe_payload={"experiment": "journal_integrity"},
    )
    for sequence in range(2, SYNTHETIC_EVENT_COUNT):
        writer.append(
            event_type="experiment.synthetic_event",
            phase="probe",
            status="RUNNING",
            lane="experiment",
            safe_payload={"synthetic_sequence": sequence},
        )
    writer.append(
        event_type="orchestration.run_completed",
        phase="finish",
        status="SUCCEEDED",
        lane="orchestration",
        safe_payload={"synthetic_event_count": SYNTHETIC_EVENT_COUNT},
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    verification = verify_run_journal(writer.run_dir, expected_run_id=writer.run_id)
    return writer.run_dir, verification, duration_ms


def _tamper_case(baseline_run_dir: Path) -> dict:
    case_root = _create_case("tampered")
    copied_run = case_root / baseline_run_dir.name
    shutil.copytree(baseline_run_dir, copied_run)
    for events_path in sorted(copied_run.glob("events-*.jsonl")):
        lines = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]
        for line in lines:
            if line["sequence_no"] == 100:
                line["safe_payload"]["synthetic_sequence"] = -100
                _write_lines(events_path, lines)
                return verify_run_journal(copied_run, expected_run_id=copied_run.name)
    raise RuntimeError("synthetic event sequence 100 was not created")


def _truncated_case(baseline_run_dir: Path) -> dict:
    case_root = _create_case("truncated")
    copied_run = case_root / baseline_run_dir.name
    shutil.copytree(baseline_run_dir, copied_run)
    events_path = sorted(copied_run.glob("events-*.jsonl"))[-1]
    content = events_path.read_bytes()
    events_path.write_bytes(content[:-17])
    return verify_run_journal(copied_run, expected_run_id=copied_run.name)


def _missing_segment_case(baseline_run_dir: Path) -> dict:
    case_root = _create_case("missing_segment")
    copied_run = case_root / baseline_run_dir.name
    shutil.copytree(baseline_run_dir, copied_run)
    (copied_run / "events-000002.jsonl").unlink()
    return verify_run_journal(copied_run, expected_run_id=copied_run.name)


def _legacy_case() -> dict:
    case_root = _create_case("legacy")
    logs_root = case_root / "logs"
    logs_root.mkdir()
    source = logs_root / "evo-wiki-events.jsonl"
    legacy_records = [
        {
            "event": "run_started",
            "status": "running",
            "selected_lanes": ["wiki"],
            "change_set": {"added": ["private-source-a.md"]},
            "api_key": SYNTHETIC_SECRET,
        },
        {
            "event": "run_finished",
            "status": "success",
            "selected_lanes": ["wiki"],
            "change_set": {"added": []},
            "exit_code": 0,
        },
        {
            "event": "run_started",
            "status": "running",
            "selected_lanes": ["lightrag"],
            "change_set": {"added": ["private-source-b.md"]},
            "query": SYNTHETIC_SECRET,
        },
        {
            "event": "run_finished",
            "status": "failed",
            "selected_lanes": ["lightrag"],
            "change_set": {"added": []},
            "exit_code": 2,
        },
    ]
    source.write_text(
        "".join(json.dumps(record) + "\n" for record in legacy_records),
        encoding="utf-8",
    )
    dry_run = migrate_legacy_journal(logs_root, apply=False)
    applied = migrate_legacy_journal(logs_root, apply=True)
    repeated = migrate_legacy_journal(logs_root, apply=True)
    migrated_path = (
        logs_root
        / "runs"
        / applied["target_run_id"]
        / EVENTS_FILENAME
    )
    migrated_text = migrated_path.read_text(encoding="utf-8")
    return {
        "dry_run_result": dry_run["result"],
        "apply_result": applied["result"],
        "repeat_result": repeated["result"],
        "source_archived": not source.exists(),
        "synthetic_secret_absent": SYNTHETIC_SECRET not in migrated_text,
        "source_names_absent": (
            "private-source-a.md" not in migrated_text
            and "private-source-b.md" not in migrated_text
        ),
        "verification": verify_journal(
            migrated_path,
            expected_run_id=applied["target_run_id"],
        ),
    }


def main() -> int:
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    baseline_run_dir, baseline, append_duration_ms = _build_baseline()
    tampered = _tamper_case(baseline_run_dir)
    truncated = _truncated_case(baseline_run_dir)
    missing_segment = _missing_segment_case(baseline_run_dir)
    legacy = _legacy_case()

    tamper_codes = {
        error["code"]
        for error in tampered["errors"]
        if error.get("sequence_no") == 100
    }
    truncated_codes = {error["code"] for error in truncated["errors"]}
    missing_segment_codes = {error["code"] for error in missing_segment["errors"]}
    assertions = {
        "baseline_verified": (
            baseline["status"] == "ok"
            and baseline["event_count"] == SYNTHETIC_EVENT_COUNT
        ),
        "tamper_detected_at_sequence_100": (
            "JOURNAL_EVENT_HASH_MISMATCH" in tamper_codes
        ),
        "truncation_detected": "JOURNAL_TRUNCATED_LINE" in truncated_codes,
        "missing_segment_detected": (
            "JOURNAL_FILE_SEQUENCE_MISMATCH" in missing_segment_codes
        ),
        "legacy_redacted": (
            legacy["synthetic_secret_absent"]
            and legacy["source_names_absent"]
        ),
        "legacy_migration_idempotent": legacy["repeat_result"] == "already_applied",
    }
    overall_status = "passed" if all(assertions.values()) else "failed"
    results = {
        "experiment_id": "journal-integrity-log-001b-rotation",
        "generated_at": utc_now(),
        "scope": "synthetic_offline_integrity_only",
        "synthetic_event_count": SYNTHETIC_EVENT_COUNT,
        "synthetic_events_per_file": SYNTHETIC_EVENTS_PER_FILE,
        "raw_run": RAW_RUN_ROOT.relative_to(EXPERIMENT_ROOT).as_posix(),
        "append_duration_ms_informational": append_duration_ms,
        "cases": {
            "baseline": baseline,
            "tampered": tampered,
            "truncated": truncated,
            "missing_segment": missing_segment,
            "legacy": legacy,
        },
        "assertions": assertions,
        "overall_status": overall_status,
        "claim_boundary": (
            "This probe validates cross-file integrity detection, redaction, and "
            "migration idempotency only; it is not a production throughput benchmark."
        ),
    }
    write_json_atomic(EXPERIMENT_ROOT / "journal_integrity_results.json", results)
    report = f"""# LOG-001B Journal 分段完整性实验

- 运行状态：`{overall_status}`
- 合成事件数：`{SYNTHETIC_EVENT_COUNT}`
- 每段事件上限：`{SYNTHETIC_EVENTS_PER_FILE}`
- 追加总耗时：`{append_duration_ms} ms`（仅环境记录，不作为性能门槛）
- 原始链校验：`{baseline["status"]}`
- 第 100 条篡改定位：`{assertions["tamper_detected_at_sequence_100"]}`
- 末行截断检测：`{assertions["truncation_detected"]}`
- 缺失中间分段检测：`{assertions["missing_segment_detected"]}`
- legacy 脱敏：`{assertions["legacy_redacted"]}`
- legacy 重复迁移 no-op：`{assertions["legacy_migration_idempotent"]}`

## 结论边界

本实验只证明合成数据上的跨分段 hash 链校验、篡改/截断/缺段检测、迁移脱敏和幂等机制。
它不证明生产吞吐、日志清理策略、崩溃恢复、SQLite 一致性或外部可信时间戳能力。
"""
    (EXPERIMENT_ROOT / "journal_integrity_report.md").write_text(
        report,
        encoding="utf-8",
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if overall_status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

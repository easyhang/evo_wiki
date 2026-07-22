from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .artifacts import (
    write_agent_plan,
    write_run_summary,
    write_top_manifest,
)
from .config import EvoConfig
from .corpus import (
    diff_against_previous,
    persist_corpus_state,
    scan_corpus,
)
from .journal import (
    DEFAULT_MAX_BYTES_PER_FILE,
    DEFAULT_MAX_EVENTS_PER_FILE,
    JournalError,
    RunJournalWriter,
    sha256_value,
)
from .lightrag_lane import (
    LightRAGBuildError,
    build_lightrag,
    prepare_lightrag_input,
)
from .paths import ProjectPaths
from .state import StateError, StateExporter, StateStore
from .utils import read_json
from .wiki import render_wiki


@dataclass(frozen=True)
class LaneRunResult:
    exit_code: int
    error_code: str | None
    summary: list[str]
    payload: dict[str, Any]
    wiki_report: dict[str, Any] | None
    lightrag_report: dict[str, Any] | None


def diff_against_files(current: list, previous: list) -> dict:
    prev_by_path = {item.path: item for item in previous}
    curr_by_path = {item.path: item for item in current}
    added = sorted(set(curr_by_path) - set(prev_by_path))
    deleted = sorted(set(prev_by_path) - set(curr_by_path))
    modified = sorted(
        path
        for path in set(curr_by_path) & set(prev_by_path)
        if curr_by_path[path].sha256 != prev_by_path[path].sha256
    )
    return {"added": added, "modified": modified, "deleted": deleted}


def current_scan(
    paths: ProjectPaths,
    store: StateStore | None = None,
) -> tuple[list, dict]:
    files = scan_corpus(paths.root, paths.corpus)
    if store is not None:
        by_path = {}
        for lane in ("global", "wiki", "lightrag"):
            for item in store.latest_lane_files(lane):
                by_path[item.path] = item
        return files, diff_against_files(files, list(by_path.values()))
    state_path = paths.artifacts / "corpus-state.json"
    change_set = diff_against_previous(files, state_path)
    return files, change_set


def lane_state_path(paths: ProjectPaths, lane: str) -> Path:
    base = paths.wiki_state if lane == "wiki" else paths.lightrag_state
    return base / "corpus-state.json"


def merge_change_sets(change_sets: list[dict]) -> dict:
    merged = {"added": set(), "modified": set(), "deleted": set()}
    for change_set in change_sets:
        for key in merged:
            merged[key].update(change_set.get(key, []))
    return {key: sorted(value) for key, value in merged.items()}


def execute_lanes(
    paths: ProjectPaths,
    config: EvoConfig,
    *,
    store: StateStore | None,
    lanes: list[str],
    lightrag_dry_run: bool = False,
    smoke_query: str | None = None,
    command_name: str = "run",
    reason: str = "cli_run",
    stop_on_lane_failure: bool = False,
    wiki_renderer: Callable[
        [ProjectPaths, EvoConfig],
        dict[str, Any],
    ] = render_wiki,
) -> LaneRunResult:
    """Run Wiki/LightRAG lanes behind one reusable orchestration boundary."""
    files = scan_corpus(paths.root, paths.corpus)
    lane_changes = {
        lane: (
            diff_against_files(files, store.latest_lane_files(lane))
            if store is not None
            else diff_against_previous(
                files,
                lane_state_path(paths, lane),
            )
        )
        for lane in lanes
    }
    change_set = merge_change_sets(list(lane_changes.values()))
    change_counts = {
        key: len(change_set.get(key, []))
        for key in ("added", "modified", "deleted")
    }
    journal_config = config.project.get("journal", {})
    if not isinstance(journal_config, dict):
        raise JournalError(
            "project journal configuration must be an object",
            error_code="JOURNAL_CONFIG_INVALID",
        )
    journal = RunJournalWriter(
        paths.artifacts / "logs",
        max_events_per_file=journal_config.get(
            "max_events_per_file",
            DEFAULT_MAX_EVENTS_PER_FILE,
        ),
        max_bytes_per_file=journal_config.get(
            "max_bytes_per_file",
            DEFAULT_MAX_BYTES_PER_FILE,
        ),
    )
    revision_ids = store.stage_files(files) if store is not None else {}
    lane_run_ids = {
        lane: f"{journal.run_id}-{lane}"
        for lane in lanes
    }
    if store is not None:
        for lane in lanes:
            store.begin_lane_run(
                run_id=lane_run_ids[lane],
                journal_run_id=journal.run_id,
                lane=lane,
            )
    journal.append(
        event_type="orchestration.run_started",
        phase="start",
        status="RUNNING",
        lane="orchestration",
        safe_payload={
            "command": command_name,
            "selected_lanes": lanes,
            "change_counts": change_counts,
            "change_set_sha256": sha256_value(change_set),
            "state_commit_seq": (
                store.state_commit_seq()
                if store is not None
                else None
            ),
        },
    )

    wiki_report: dict[str, Any] | None = None
    lightrag_report: dict[str, Any] | None = None
    try:
        write_agent_plan(
            paths,
            selected_lanes=lanes,
            change_set=change_set,
            reason=reason,
            change_sets=lane_changes,
        )
        wiki_status = None
        lightrag_status = None
        wiki_has_error = False
        summary: list[str] = []
        if "wiki" in lanes:
            wiki_report = wiki_renderer(paths, config)
            wiki_status = {
                "status": wiki_report["status"],
                "output": "artifacts/wiki/dist/index.html",
                "page_count": wiki_report["page_count"],
            }
            wiki_has_error = any(
                issue.get("severity") == "error"
                for issue in wiki_report.get("health", {}).get("issues", [])
            )
            summary.append(
                f"Wiki lane: {wiki_report['status']} "
                f"({wiki_report['page_count']} pages)"
            )
            if store is not None:
                store.finish_lane_run(
                    run_id=lane_run_ids["wiki"],
                    status="FAILED" if wiki_has_error else "SUCCEEDED",
                    files=files,
                    revision_ids=revision_ids,
                    error_code=(
                        "WIKI_HEALTH_FAILED"
                        if wiki_has_error
                        else None
                    ),
                )
            elif not wiki_has_error:
                persist_corpus_state(
                    files,
                    lane_state_path(paths, "wiki"),
                )

        if "lightrag" in lanes and wiki_has_error and stop_on_lane_failure:
            lightrag_status = {
                "status": "not_run",
                "error_code": "WIKI_PREREQUISITE_FAILED",
            }
            summary.append(
                "LightRAG lane: not run (Wiki prerequisite failed)"
            )
            if store is not None:
                store.finish_lane_run(
                    run_id=lane_run_ids["lightrag"],
                    status="FAILED",
                    error_code="WIKI_PREREQUISITE_FAILED",
                    side_effects_executed=False,
                )
        elif "lightrag" in lanes:
            input_report = prepare_lightrag_input(paths, files)
            try:
                lightrag_report = build_lightrag(
                    paths,
                    smoke_query=smoke_query,
                    dry_run=lightrag_dry_run,
                    config=config.project.get("lightrag", {}),
                    state_store=store,
                )
                lightrag_status = {
                    "status": lightrag_report["status"],
                    "service": lightrag_report.get("service"),
                    "document_count": input_report["document_count"],
                }
                summary.append(
                    f"LightRAG lane: {lightrag_report['status']} "
                    f"({input_report['document_count']} docs)"
                )
                if store is not None:
                    store.finish_lane_run(
                        run_id=lane_run_ids["lightrag"],
                        status="SUCCEEDED",
                        files=files,
                        revision_ids=revision_ids,
                        side_effects_executed=not lightrag_dry_run,
                    )
                else:
                    persist_corpus_state(
                        files,
                        lane_state_path(paths, "lightrag"),
                    )
            except LightRAGBuildError as exc:
                lightrag_report = read_json(
                    paths.lightrag_reports / "lightrag-report.json",
                    {},
                )
                lightrag_status = {
                    "status": "failed",
                    "service": (
                        config.project.get("lightrag", {}).get("base_url")
                    ),
                    "error": str(exc),
                    "document_count": input_report["document_count"],
                    "remote_mutated": bool(
                        lightrag_report.get("imported")
                    ),
                }
                summary.append(f"LightRAG lane: failed ({exc})")
                if store is not None:
                    store.finish_lane_run(
                        run_id=lane_run_ids["lightrag"],
                        status="FAILED",
                        error_code=(
                            exc.failure_code or "LIGHTRAG_BUILD_FAILED"
                        ),
                        side_effects_executed=True,
                    )

        if store is None:
            persist_corpus_state(
                files,
                paths.artifacts / "corpus-state.json",
            )
        write_top_manifest(
            paths,
            config,
            selected_lanes=lanes,
            files=files,
            change_set=change_set,
            wiki_status=wiki_status,
            lightrag_status=lightrag_status,
        )
        write_run_summary(paths, summary)
        export_succeeded = None
        if store is not None:
            export_succeeded = StateExporter(store).export().export_succeeded

        event_summary = {
            "wiki": (
                wiki_status.get("status")
                if wiki_status
                else "not_requested"
            ),
            "lightrag": (
                lightrag_status.get("status")
                if lightrag_status
                else "not_requested"
            ),
        }
        if wiki_has_error:
            exit_code = 3
            error_code = "WIKI_HEALTH_FAILED"
        elif lightrag_status and lightrag_status.get("status") == "failed":
            exit_code = 6
            error_code = "LIGHTRAG_BUILD_FAILED"
        else:
            exit_code = 0
            error_code = None
        payload = {
            "operation": command_name,
            "selected_lanes": lanes,
            "lane_status": event_summary,
            "state_committed": store is not None,
            "state_commit_seq": (
                store.state_commit_seq()
                if store is not None
                else None
            ),
            "export_succeeded": export_succeeded,
        }
    except Exception as exc:
        if store is not None:
            for lane_run_id in lane_run_ids.values():
                try:
                    store.finish_lane_run(
                        run_id=lane_run_id,
                        status="FAILED",
                        error_code=(
                            exc.error_code
                            if isinstance(exc, StateError)
                            else "UNEXPECTED_RUN_ERROR"
                        ),
                    )
                except Exception:
                    pass
        try:
            journal.append(
                event_type="orchestration.run_failed",
                phase="finish",
                status="FAILED",
                lane="orchestration",
                safe_payload={
                    "command": command_name,
                    "selected_lanes": lanes,
                    "error_code": (
                        exc.error_code
                        if isinstance(exc, StateError)
                        else "UNEXPECTED_RUN_ERROR"
                    ),
                },
            )
        except JournalError as journal_error:
            raise journal_error from exc
        raise

    journal.append(
        event_type=(
            "orchestration.run_completed"
            if exit_code == 0
            else "orchestration.run_failed"
        ),
        phase="finish",
        status="SUCCEEDED" if exit_code == 0 else "FAILED",
        lane="orchestration",
        safe_payload={
            "command": command_name,
            "selected_lanes": lanes,
            "lane_status": event_summary,
            "exit_code": exit_code,
            "error_code": error_code,
            "state_commit_seq": (
                store.state_commit_seq()
                if store is not None
                else None
            ),
        },
    )
    return LaneRunResult(
        exit_code=exit_code,
        error_code=error_code,
        summary=summary,
        payload=payload,
        wiki_report=wiki_report,
        lightrag_report=lightrag_report,
    )

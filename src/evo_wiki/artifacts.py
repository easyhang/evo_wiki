from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .config import EvoConfig
from .corpus import CorpusFile, corpus_hash
from .paths import ProjectPaths
from .utils import read_json, utc_now, write_json


VALID_LANES = {"wiki", "lightrag"}


def write_agent_plan(paths: ProjectPaths, *, selected_lanes: list[str], change_set: dict, reason: str) -> None:
    paths.agent.mkdir(parents=True, exist_ok=True)
    plan = {
        "run_type": "incremental" if any(change_set.values()) else "full_or_no_change",
        "selected_lanes": selected_lanes,
        "reason": reason,
        "wiki": {"action": "run" if "wiki" in selected_lanes else "skip"},
        "lightrag": {"action": "run" if "lightrag" in selected_lanes else "skip"},
        "change_set": change_set,
    }
    write_json(paths.agent / "delta-plan.json", plan)
    (paths.agent / "evo-plan.md").write_text(
        "# Evo wiki Run Plan\n\n"
        f"- Selected lanes: {', '.join(selected_lanes) or 'none'}\n"
        f"- Reason: {reason}\n"
        f"- Added: {len(change_set.get('added', []))}\n"
        f"- Modified: {len(change_set.get('modified', []))}\n"
        f"- Deleted: {len(change_set.get('deleted', []))}\n",
        encoding="utf-8",
    )


def write_run_summary(paths: ProjectPaths, lines: Iterable[str]) -> None:
    paths.agent.mkdir(parents=True, exist_ok=True)
    body = "# Evo wiki Run Summary\n\n" + "\n".join(f"- {line}" for line in lines) + "\n"
    (paths.agent / "run-summary.md").write_text(body, encoding="utf-8")


def write_top_manifest(
    paths: ProjectPaths,
    config: EvoConfig,
    *,
    selected_lanes: list[str],
    files: list[CorpusFile],
    change_set: dict,
    wiki_status: dict | None = None,
    lightrag_status: dict | None = None,
) -> None:
    wiki = wiki_status or lane_status_from_manifest(
        paths.wiki / "manifest.json",
        fallback={"status": "not_requested", "can_run_later": True},
    )
    lightrag = lightrag_status or lane_status_from_manifest(
        paths.lightrag / "manifest.json",
        fallback={"status": "not_requested", "can_run_later": True, "input_source": "corpus/"},
    )
    manifest = {
        "project": config.project.get("project", "evo-wiki-project"),
        "generated_at": utc_now(),
        "selected_lanes": selected_lanes,
        "corpus_hash": corpus_hash(files),
        "change_set": change_set,
        "lanes": {
            "wiki": wiki,
            "lightrag": lightrag,
        },
        "next_action": next_action(selected_lanes, wiki, lightrag),
    }
    write_json(paths.artifacts / "manifest.json", manifest)


def lane_status_from_manifest(path: Path, *, fallback: dict) -> dict:
    manifest = read_json(path, {})
    if not manifest:
        return fallback
    status = dict(manifest)
    status.setdefault("status", "success")
    status["from_previous_run"] = True
    return status


def next_action(selected_lanes: list[str], wiki: dict, lightrag: dict) -> str:
    if selected_lanes == ["wiki"] and wiki.get("status") == "success":
        return "review_wiki_then_decide_whether_to_build_lightrag"
    if selected_lanes == ["lightrag"] and lightrag.get("status") == "success":
        return "connect_agent_to_lightrag_or_run_more_smoke_tests"
    if set(selected_lanes) == {"wiki", "lightrag"}:
        return "review_wiki_and_lightrag_reports"
    return "inspect_reports"

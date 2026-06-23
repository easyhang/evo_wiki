from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .artifacts import write_agent_plan, write_run_summary, write_top_manifest
from .config import EvoConfig
from .corpus import diff_against_previous, persist_corpus_state, scan_corpus
from .docker_export import export_docker
from .lightrag_lane import LightRAGBuildError, build_lightrag, prepare_lightrag_input
from .paths import ProjectPaths
from .utils import read_json, write_json
from .version import __version__
from .wiki import ensure_wiki_stub, render_wiki
from .wiki_health import lint_wiki_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LightRAGBuildError as exc:
        print(f"LightRAG build failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"evo-wiki error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evo-wiki", description="Claude Code-first Evo wiki MVP")
    parser.add_argument("--version", action="version", version=f"evo-wiki {__version__}")
    sub = parser.add_subparsers(required=True)

    def add_root(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default="workspace", help="Runtime workspace root containing corpus/ and artifacts/; defaults to ./workspace")

    p = sub.add_parser("init", help="Initialize a new Evo wiki project")
    add_root(p); p.set_defaults(func=cmd_init)

    p = sub.add_parser("scan", help="Scan corpus and write change set")
    add_root(p); p.set_defaults(func=cmd_scan)

    p = sub.add_parser("render-wiki", help="Render artifacts/wiki/wiki-src Markdown into static HTML")
    add_root(p); p.set_defaults(func=cmd_render_wiki)

    p = sub.add_parser("lint-wiki", help="Run llm-wiki-style health checks for wiki-src/audit/log")
    add_root(p); p.set_defaults(func=cmd_lint_wiki)

    p = sub.add_parser("prepare-lightrag", help="Prepare LightRAG input package from corpus")
    add_root(p); p.set_defaults(func=cmd_prepare_lightrag)

    p = sub.add_parser("build-lightrag", help="Submit prepared LightRAG input to an existing LightRAG service")
    add_root(p)
    p.add_argument("--smoke-query", default=None, help="Optional hybrid query after import submission")
    p.add_argument("--dry-run", action="store_true", help="Do not call LightRAG service; only report import delta")
    p.set_defaults(func=cmd_build_lightrag)

    p = sub.add_parser("export-docker", help="Export Dockerfiles and docker-compose")
    add_root(p); p.set_defaults(func=cmd_export_docker)

    p = sub.add_parser("inspect", help="Print top-level manifest and reports")
    add_root(p); p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("run", help="Run selected lanes")
    add_root(p)
    p.add_argument("--lane", choices=["wiki", "lightrag", "both"], default="wiki")
    p.add_argument("--lightrag-dry-run", action="store_true")
    p.add_argument("--smoke-query", default=None)
    p.set_defaults(func=cmd_run)
    return parser


def load(root: str) -> tuple[ProjectPaths, EvoConfig]:
    paths = ProjectPaths.from_root(root)
    config = EvoConfig.load(paths.root)
    return paths, config


def current_scan(paths: ProjectPaths) -> tuple[list, dict]:
    files = scan_corpus(paths.root, paths.corpus)
    state_path = paths.artifacts / "corpus-state.json"
    change_set = diff_against_previous(files, state_path)
    return files, change_set


def lane_state_path(paths: ProjectPaths, lane: str) -> Path:
    """Per-lane corpus baseline path (H2).

    每条 lane 维护各自的 corpus 基线，避免一条 lane 运行后把变更集"吃掉"，
    导致另一条 lane 误判为无变更。
    """
    base = paths.wiki_state if lane == "wiki" else paths.lightrag_state
    return base / "corpus-state.json"


def merge_change_sets(change_sets: list[dict]) -> dict:
    merged = {"added": set(), "modified": set(), "deleted": set()}
    for change_set in change_sets:
        for key in merged:
            merged[key].update(change_set.get(key, []))
    return {key: sorted(value) for key, value in merged.items()}


def cmd_init(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    EvoConfig.write_defaults(paths.root)
    config = EvoConfig.load(paths.root)
    ensure_wiki_stub(paths, config)
    write_json(paths.artifacts / "manifest.json", {"project": config.project["project"], "status": "initialized"})
    print(f"Initialized Evo wiki project at {paths.root}")
    print("Next: put source files under the runtime corpus/raw/ directory (default: workspace/corpus/raw/), let Claude Code compile wiki-src/{concepts,entities,sources}, then run render-wiki for HTML.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    files, change_set = current_scan(paths)
    persist_corpus_state(files, paths.artifacts / "corpus-state.json")
    # L1：scan 只是预览，不应覆盖 run 写入的 delta-plan.json，改写独立的 scan.json。
    write_json(paths.agent / "scan.json", {"selected_lanes": [], "change_set": change_set})
    print(json.dumps({"file_count": len(files), "change_set": change_set}, ensure_ascii=False, indent=2))
    return 0


def cmd_render_wiki(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    report = render_wiki(paths, config)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_lint_wiki(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    report = lint_wiki_artifacts(paths.root, paths.wiki_src, paths.wiki_audit, paths.wiki_log)
    write_json(paths.wiki_reports / "wiki-health.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    has_error = any(issue.get("severity") == "error" for issue in report.get("issues", []))
    return 1 if has_error else 0


def cmd_prepare_lightrag(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    files, _ = current_scan(paths)
    report = prepare_lightrag_input(paths, files)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_build_lightrag(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    report = build_lightrag(paths, smoke_query=args.smoke_query, dry_run=args.dry_run, config=config.project.get("lightrag", {}))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_export_docker(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    result = export_docker(paths)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    bundle = {
        "manifest": read_json(paths.artifacts / "manifest.json", {}),
        "wiki_report": read_json(paths.wiki_reports / "wiki-report.json", {}),
        "wiki_health": read_json(paths.wiki_reports / "wiki-health.json", {}),
        "lightrag_report": read_json(paths.lightrag_reports / "lightrag-report.json", {}),
    }
    print(json.dumps(bundle, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    paths, config = load(args.root)
    paths.ensure_base_dirs()
    files = scan_corpus(paths.root, paths.corpus)
    lanes = ["wiki", "lightrag"] if args.lane == "both" else [args.lane]
    # H2：按 lane 各自的基线计算变更，互不污染。
    lane_changes = {lane: diff_against_previous(files, lane_state_path(paths, lane)) for lane in lanes}
    change_set = merge_change_sets(list(lane_changes.values()))
    write_agent_plan(paths, selected_lanes=lanes, change_set=change_set, reason=f"cli_run_{args.lane}", change_sets=lane_changes)

    wiki_status = None
    lightrag_status = None
    wiki_has_error = False
    summary = []
    if "wiki" in lanes:
        wiki_report = render_wiki(paths, config)
        wiki_status = {"status": wiki_report["status"], "output": "artifacts/wiki/dist/index.html", "page_count": wiki_report["page_count"]}
        wiki_has_error = any(issue.get("severity") == "error" for issue in wiki_report.get("health", {}).get("issues", []))
        summary.append(f"Wiki lane: {wiki_report['status']} ({wiki_report['page_count']} pages)")
        # 仅在该 lane 运行后持久化自己的基线。
        persist_corpus_state(files, lane_state_path(paths, "wiki"))
    if "lightrag" in lanes:
        input_report = prepare_lightrag_input(paths, files)
        try:
            lr_report = build_lightrag(paths, smoke_query=args.smoke_query, dry_run=args.lightrag_dry_run, config=config.project.get("lightrag", {}))
            lightrag_status = {"status": lr_report["status"], "service": lr_report.get("service"), "document_count": input_report["document_count"]}
            summary.append(f"LightRAG lane: {lr_report['status']} ({input_report['document_count']} docs)")
            persist_corpus_state(files, lane_state_path(paths, "lightrag"))
        except LightRAGBuildError as exc:
            lightrag_status = {"status": "failed", "service": config.project.get("lightrag", {}).get("base_url"), "error": str(exc), "document_count": input_report["document_count"]}
            summary.append(f"LightRAG lane: failed ({exc})")
    # 全局 corpus-state 仅作为 scan/inspect 的并集预览。
    persist_corpus_state(files, paths.artifacts / "corpus-state.json")
    write_top_manifest(paths, config, selected_lanes=lanes, files=files, change_set=change_set, wiki_status=wiki_status, lightrag_status=lightrag_status)
    write_run_summary(paths, summary)
    print("\n".join(summary))
    # M3：退出码区分失败类型——lightrag 失败=2，wiki 存在 error 级健康问题=3，否则 0。
    if lightrag_status and lightrag_status.get("status") == "failed":
        return 2
    if wiki_has_error:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

    p = sub.add_parser("build-lightrag", help="Build LightRAG workspace through lightrag-hku")
    add_root(p)
    p.add_argument("--smoke-query", default=None, help="Optional hybrid query after import")
    p.add_argument("--dry-run", action="store_true", help="Do not call LightRAG; only report import delta")
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


def cmd_init(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    EvoConfig.write_defaults(paths.root)
    config = EvoConfig.load(paths.root)
    ensure_wiki_stub(paths, config)
    write_json(paths.artifacts / "manifest.json", {"project": config.project["project"], "status": "initialized"})
    print(f"Initialized Evo wiki project at {paths.root}")
    print("Next: put source files under the runtime corpus/raw/ directory (default: workspace/corpus/raw/), let Claude Code compile wiki-src/{concepts,entities,summaries}, then run render-wiki for HTML.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    files, change_set = current_scan(paths)
    persist_corpus_state(files, paths.artifacts / "corpus-state.json")
    write_json(paths.agent / "delta-plan.json", {"selected_lanes": [], "change_set": change_set})
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
    paths, _ = load(args.root)
    paths.ensure_base_dirs()
    report = build_lightrag(paths, smoke_query=args.smoke_query, dry_run=args.dry_run)
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
    files, change_set = current_scan(paths)
    lanes = ["wiki", "lightrag"] if args.lane == "both" else [args.lane]
    write_agent_plan(paths, selected_lanes=lanes, change_set=change_set, reason=f"cli_run_{args.lane}")

    wiki_status = None
    lightrag_status = None
    summary = []
    if "wiki" in lanes:
        wiki_report = render_wiki(paths, config)
        wiki_status = {"status": wiki_report["status"], "output": "artifacts/wiki/dist/index.html", "page_count": wiki_report["page_count"]}
        summary.append(f"Wiki lane: {wiki_report['status']} ({wiki_report['page_count']} pages)")
    if "lightrag" in lanes:
        input_report = prepare_lightrag_input(paths, files)
        try:
            lr_report = build_lightrag(paths, smoke_query=args.smoke_query, dry_run=args.lightrag_dry_run)
            lightrag_status = {"status": lr_report["status"], "workspace": "artifacts/lightrag/workspace", "document_count": input_report["document_count"]}
            summary.append(f"LightRAG lane: {lr_report['status']} ({input_report['document_count']} docs)")
        except LightRAGBuildError as exc:
            lightrag_status = {"status": "failed", "workspace": "artifacts/lightrag/workspace", "error": str(exc), "document_count": input_report["document_count"]}
            summary.append(f"LightRAG lane: failed ({exc})")
    persist_corpus_state(files, paths.artifacts / "corpus-state.json")
    write_top_manifest(paths, config, selected_lanes=lanes, files=files, change_set=change_set, wiki_status=wiki_status, lightrag_status=lightrag_status)
    write_run_summary(paths, summary)
    print("\n".join(summary))
    return 0 if not (lightrag_status and lightrag_status.get("status") == "failed") else 2


if __name__ == "__main__":
    raise SystemExit(main())

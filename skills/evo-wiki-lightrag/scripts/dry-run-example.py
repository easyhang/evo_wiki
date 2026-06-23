#!/usr/bin/env python3
"""运行 LightRAG 子 Skill 的最小 dry-run 样例。"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from evo_wiki.corpus import scan_corpus
from evo_wiki.lightrag_lane import build_lightrag, prepare_lightrag_input
from evo_wiki.paths import ProjectPaths


def main() -> int:
    repo = Path(__file__).resolve().parents[3]
    example = repo / "skills" / "evo-wiki-lightrag" / "examples" / "basic"
    tmp = Path(tempfile.mkdtemp())
    try:
        paths = ProjectPaths.from_root(tmp)
        paths.ensure_base_dirs()
        shutil.copytree(example / "corpus", paths.corpus, dirs_exist_ok=True)
        files = scan_corpus(paths.root, paths.corpus)
        input_report = prepare_lightrag_input(paths, files)
        lr_report = build_lightrag(paths, dry_run=True)
        print(json.dumps({"input": input_report, "lightrag": lr_report}, ensure_ascii=False, indent=2))
        return 0 if lr_report.get("status") == "dry_run" else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

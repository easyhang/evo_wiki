#!/usr/bin/env python3
"""重新渲染 Wiki 子 Skill 的 learnbuffett 风格 HTML 样例。"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from evo_wiki.config import EvoConfig
from evo_wiki.paths import ProjectPaths
from evo_wiki.wiki import render_wiki


def main() -> int:
    repo = Path(__file__).resolve().parents[3]
    example = repo / "skills" / "evo-wiki-wiki" / "examples" / "learnbuffett-style"
    src = example / "artifacts" / "wiki" / "wiki-src"
    out = example / "site"
    tmp = Path(tempfile.mkdtemp())
    try:
        paths = ProjectPaths.from_root(tmp)
        paths.ensure_base_dirs()
        for md in src.rglob("*.md"):
            target = paths.wiki_src / md.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(md, target)
        config = EvoConfig()
        config.wiki = dict(config.wiki)
        config.wiki["title"] = "巴菲特知识库（样例）"
        report = render_wiki(paths, config)
        print(json.dumps({"status": report["status"], "page_count": report["page_count"], "health": report["health"]}, ensure_ascii=False, indent=2))
        if report["health"].get("issue_count") != 0:
            return 1
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(paths.wiki_dist, out)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

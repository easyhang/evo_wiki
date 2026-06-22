from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from .utils import read_json, write_json


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base`` (M4).

    递归合并：仅当 base 和 override 中同一个键都是 dict 时才向下递归合并，
    否则用 override 的值整体覆盖（列表/标量不做合并）。base 不会被修改。
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


DEFAULT_PROJECT = {
    "project": "evo-wiki-project",
    "default_lane": "wiki_first",
    "corpus_dir": "corpus",
    "artifacts_dir": "artifacts",
    "lightrag": {
        "mode": "direct_dependency",
        "working_dir": "artifacts/lightrag/workspace",
        "input_file": "artifacts/lightrag/input/documents.jsonl",
    },
}

DEFAULT_WIKI = {
    "title": "Evo Wiki",
    "description": "Agent-generated LLM Wiki rendered to static HTML",
    "structure": {
        "index": "index.md",
        "concepts": "concepts/",
        "entities": "entities/",
        "summaries": "summaries/",
        "audit": "audit/",
        "log": "log/",
        "queries": "outputs/queries/",
    },
    "page_targets": {
        "concept": {"target_words": "400-1200", "hard_max_words": 1200},
        "entity": {"target_words": "200-500", "hard_max_words": 500},
        "summary": {"target_words": "150-400", "hard_max_words": 400},
    },
    "pages": [
        {
            "path": "index.md",
            "title": "首页",
            "type": "index",
            "description": "Wiki 入口页。由 Claude Code 生成内容，Python 负责渲染为 HTML。",
            "sources": [],
        }
    ],
    "protected_markers": {
        "start": "<!-- evo:user-edit:start -->",
        "end": "<!-- evo:user-edit:end -->",
    },
}


@dataclass
class EvoConfig:
    project: dict = field(default_factory=lambda: dict(DEFAULT_PROJECT))
    wiki: dict = field(default_factory=lambda: dict(DEFAULT_WIKI))

    @classmethod
    def load(cls, root: Path) -> "EvoConfig":
        project = deep_merge(DEFAULT_PROJECT, read_json(root / "project.json", {}))
        wiki = deep_merge(DEFAULT_WIKI, read_json(root / "wiki.json", {}))
        return cls(project=project, wiki=wiki)

    @staticmethod
    def write_defaults(root: Path, *, overwrite: bool = False) -> None:
        project_path = root / "project.json"
        wiki_path = root / "wiki.json"
        if overwrite or not project_path.exists():
            write_json(project_path, DEFAULT_PROJECT)
        if overwrite or not wiki_path.exists():
            write_json(wiki_path, DEFAULT_WIKI)

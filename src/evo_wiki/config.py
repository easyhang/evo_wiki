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
        "mode": "service",
        "base_url": "",
        "api_key_env": "LIGHTRAG_API_KEY",
        "bearer_token_env": "LIGHTRAG_BEARER_TOKEN",
        "input_file": "artifacts/lightrag/input/documents.jsonl",
        "timeout_seconds": 30,
    },
}

DEFAULT_WIKI = {
    "title": "Evo Wiki",
    "description": "Agent-generated LLM Wiki rendered to static HTML",
    "structure": {
        "index": "index.md",
        "concepts": "concepts/",
        "entities": "entities/",
        "sources": "sources/",
        "audit": "audit/",
        "log": "log/",
        "queries": "outputs/queries/",
    },
    "page_targets": {
        "concept": {"target_words": "400-1200", "hard_max_words": 1200},
        "entity": {"target_words": "200-500", "hard_max_words": 500},
        "source": {"target_words": "摘要 150-400 + 原文全文", "hard_max_words": None},
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


LIGHTRAG_CONFIG_EXAMPLE = {
    "_comment": "Copy this file to lightrag-config.json and fill in your LightRAG service details. lightrag-config.json is gitignored. base_url is required before running the LightRAG lane or export-platform.",
    "mode": "service",
    "base_url": "http://YOUR_LIGHTRAG_SERVER:9621",
    "api_key_env": "LIGHTRAG_API_KEY",
    "bearer_token_env": "LIGHTRAG_BEARER_TOKEN",
    "timeout_seconds": 30,
}


def load_lightrag_config(root: Path) -> dict:
    """Load lightrag-config.json from *root* and return the lightrag overrides.

    Returns an empty dict when the file doesn't exist, so callers can always
    deep-merge the result.
    """
    cfg_path = root / "lightrag-config.json"
    if cfg_path.exists():
        return read_json(cfg_path, {})
    return {}


@dataclass
class EvoConfig:
    project: dict = field(default_factory=lambda: dict(DEFAULT_PROJECT))
    wiki: dict = field(default_factory=lambda: dict(DEFAULT_WIKI))

    @classmethod
    def load(cls, root: Path) -> "EvoConfig":
        project = deep_merge(DEFAULT_PROJECT, read_json(root / "project.json", {}))
        # Separate lightrag-config.json overrides project.json's lightrag section
        lightrag_override = load_lightrag_config(root)
        if lightrag_override:
            project["lightrag"] = deep_merge(project["lightrag"], lightrag_override)
        wiki = deep_merge(DEFAULT_WIKI, read_json(root / "wiki.json", {}))
        return cls(project=project, wiki=wiki)

    @staticmethod
    def write_defaults(root: Path, *, overwrite: bool = False) -> None:
        project_path = root / "project.json"
        wiki_path = root / "wiki.json"
        lightrag_example_path = root / "lightrag-config.example.json"
        if overwrite or not project_path.exists():
            write_json(project_path, DEFAULT_PROJECT)
        if overwrite or not wiki_path.exists():
            write_json(wiki_path, DEFAULT_WIKI)
        if overwrite or not lightrag_example_path.exists():
            write_json(lightrag_example_path, LIGHTRAG_CONFIG_EXAMPLE)


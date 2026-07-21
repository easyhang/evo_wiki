from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .state.contracts import StateError
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
    "profile": "local-platform",
    "default_lane": "platform",
    "corpus_dir": "corpus",
    "artifacts_dir": "artifacts",
    "state": {
        "backend": "sqlite",
        "database": "artifacts/state/evo_wiki.sqlite3",
        "busy_timeout_seconds": 15,
    },
    "journal": {
        "max_events_per_file": 5000,
        "max_bytes_per_file": 67108864,
    },
    "operations": {
        "notifications": {
            "enabled": False,
            "webhook_url_env": "EVO_WIKI_OPS_WEBHOOK_URL",
            "signing_key_env": "EVO_WIKI_OPS_WEBHOOK_KEY",
            "min_severity": "HIGH",
            "max_attempts": 3,
            "request_timeout_seconds": 5,
            "initial_backoff_seconds": 1,
            "max_backoff_seconds": 30,
            "dispatch_interval_seconds": 2,
            "required_delivery_timeout_seconds": 15,
            "maintenance_delivery_required": True,
        },
    },
    "query_gateway": {
        "mode": "shadow",
        "listen": "127.0.0.1:8765",
        "max_body_bytes": 32768,
        "max_response_bytes": 4194304,
        "max_in_flight": 16,
        "request_timeout_seconds": 45,
        "drain_timeout_seconds": 30,
        "audit_required": True,
        "audit_hmac_key_env": "EVO_WIKI_QUERY_AUDIT_KEY",
        "evidence_policy": "provenance_critical_fact_v1",
    },
    "security": {
        "auth_mode": "local_single_user",
        "principal_header": "X-Evo-Principal",
        "default_domain": "default",
        "fail_closed": True,
    },
    "retrieval": {
        "evidence_subgraph": {
            "max_depth": 2,
            "max_nodes": 300,
            "max_edges": 3000,
            "max_content_units": 1000,
            "top_k": 5,
            "timeout_seconds": 30,
            "target_chars": 1200,
            "overlap_chars": 120,
            "require_scoped_retrieval": True,
            "deny_unbounded_global_search": True,
            "generation_enabled": False,
        },
    },
    "lightrag": {
        "mode": "service",
        "base_url": "",
        "workspace": "",
        "api_key_env": "LIGHTRAG_API_KEY",
        "bearer_token_env": "LIGHTRAG_BEARER_TOKEN",
        "input_file": "artifacts/lightrag/input/documents.jsonl",
        "timeout_seconds": 30,
        "sync": {
            "poll_interval_seconds": 2,
            "poll_timeout_seconds": 600,
        },
        "replacement": {
            "enabled": False,
            "maintenance_window_seconds": 600,
            "absence_confirmations": 2,
            "auto_compensate": True,
        },
        "embedding": {
            "batch_size": 8,
        },
    },
}

DEFAULT_WIKI = {
    # Direct EvoConfig() and existing wiki.json files retain the v1 content
    # contract. write_defaults() opts newly initialized workspaces into v2.
    "content_contract_version": 1,
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
    "brand": {
        "logo_path": None,
        "primary_color": "#2563eb",
    },
    "navigation": {
        "wiki": True,
        "qa": True,
        "graph": True,
        "entity_hub": True,
    },
    "query_defaults": {
        "mode": "mix",
        "top_k": 20,
        "history_turns": 3,
    },
    "graph_defaults": {
        "max_depth": 2,
        "max_nodes": 50,
        "popular_limit": 12,
    },
}


PROJECT_PROFILES = {
    "local-platform": {},
    "production-export": {
        "profile": "production-export",
        "query_gateway": {
            "mode": "enforce",
        },
        "security": {
            "auth_mode": "trusted_proxy",
        },
    },
    "wiki-only": {
        "profile": "wiki-only",
        "default_lane": "wiki",
        "query_gateway": {
            "mode": "disabled",
        },
        "security": {
            "auth_mode": "local_single_user",
        },
    },
}


WIKI_PROFILES = {
    "local-platform": {},
    "production-export": {},
    "wiki-only": {
        "navigation": {
            "wiki": True,
            "qa": False,
            "graph": False,
            "entity_hub": False,
        },
    },
}


VALID_PROFILES = frozenset(PROJECT_PROFILES)
VALID_QUERY_MODES = frozenset({"naive", "local", "global", "hybrid", "mix"})
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


LIGHTRAG_CONFIG_EXAMPLE = {
    "_comment": "复制为 lightrag-config.json，并填写真实 LightRAG 服务。base_url 和 workspace 必填；凭据只通过环境变量注入。sync 控制提交后的有界 track 轮询。replacement 具有破坏性，完成 replace-plan 审查前必须保持关闭。embedding.batch_size 只是客户端兼容性期望，不会修改远端配置；请使用 doctor --check-service 核对。",
    "mode": "service",
    "base_url": "http://YOUR_LIGHTRAG_SERVER:9621",
    "workspace": "evo_wiki",
    "api_key_env": "LIGHTRAG_API_KEY",
    "bearer_token_env": "LIGHTRAG_BEARER_TOKEN",
    "timeout_seconds": 30,
    "sync": {
        "poll_interval_seconds": 2,
        "poll_timeout_seconds": 600,
    },
    "replacement": {
        "enabled": False,
        "maintenance_window_seconds": 600,
        "absence_confirmations": 2,
        "auto_compensate": True,
    },
    "embedding": {
        "batch_size": 8,
    },
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
        project_path = root / "project.json"
        raw_project = read_json(project_path, {})
        project = deep_merge(DEFAULT_PROJECT, raw_project)
        # Existing workspaces predate the state contract. Keep them on the
        # legacy JSON backend until an explicit migrate-state --apply cutover.
        if project_path.exists() and (
            not isinstance(raw_project, dict)
            or "state" not in raw_project
        ):
            project["state"] = {
                **DEFAULT_PROJECT["state"],
                "backend": "legacy_json",
            }
        # Separate lightrag-config.json overrides project.json's lightrag section
        lightrag_override = load_lightrag_config(root)
        if lightrag_override:
            project["lightrag"] = deep_merge(project["lightrag"], lightrag_override)
        wiki = deep_merge(DEFAULT_WIKI, read_json(root / "wiki.json", {}))
        return cls(project=project, wiki=wiki)

    @staticmethod
    def write_defaults(
        root: Path,
        *,
        overwrite: bool = False,
        profile: str = "local-platform",
    ) -> None:
        if profile not in VALID_PROFILES:
            raise StateError(
                "unknown Evo Wiki initialization profile",
                error_code="STATE_CONFIG_INVALID",
                details={"profile": profile},
            )
        project_path = root / "project.json"
        wiki_path = root / "wiki.json"
        lightrag_example_path = root / "lightrag-config.example.json"
        if overwrite or not project_path.exists():
            write_json(
                project_path,
                deep_merge(DEFAULT_PROJECT, PROJECT_PROFILES[profile]),
            )
        if overwrite or not wiki_path.exists():
            wiki_defaults = deep_merge(
                DEFAULT_WIKI,
                WIKI_PROFILES[profile],
            )
            wiki_defaults["content_contract_version"] = 2
            write_json(
                wiki_path,
                wiki_defaults,
            )
        if overwrite or not lightrag_example_path.exists():
            write_json(lightrag_example_path, LIGHTRAG_CONFIG_EXAMPLE)

    def validate(
        self,
        root: Path,
        *,
        target: str | None = None,
        require_logo: bool = True,
    ) -> dict[str, Any]:
        """Validate and normalize the public project/presentation contract."""
        profile = self.project.get("profile", "local-platform")
        if profile not in VALID_PROFILES:
            raise StateError(
                "project.profile is invalid",
                error_code="STATE_CONFIG_INVALID",
                details={"profile": profile},
            )
        if target is not None and target not in {"wiki", "platform"}:
            raise StateError(
                "generation target must be wiki or platform",
                error_code="STATE_CONFIG_INVALID",
                details={"target": target},
            )

        title = self.wiki.get("title")
        description = self.wiki.get("description")
        content_contract_version = self.wiki.get(
            "content_contract_version",
            1,
        )
        if (
            isinstance(content_contract_version, bool)
            or content_contract_version not in {1, 2}
        ):
            raise StateError(
                "wiki.content_contract_version must be 1 or 2",
                error_code="WIKI_CONTENT_CONTRACT_INVALID",
                details={"content_contract_version": content_contract_version},
            )
        if not isinstance(title, str) or not title.strip():
            raise StateError(
                "wiki.title must be a non-empty string",
                error_code="STATE_CONFIG_INVALID",
            )
        if not isinstance(description, str):
            raise StateError(
                "wiki.description must be a string",
                error_code="STATE_CONFIG_INVALID",
            )

        brand = self.wiki.get("brand")
        if not isinstance(brand, dict):
            raise StateError(
                "wiki.brand must be an object",
                error_code="STATE_CONFIG_INVALID",
            )
        primary_color = brand.get("primary_color", "#2563eb")
        if not isinstance(primary_color, str) or not _HEX_COLOR.fullmatch(
            primary_color
        ):
            raise StateError(
                "wiki.brand.primary_color must use #RRGGBB",
                error_code="STATE_CONFIG_INVALID",
            )
        logo_path = brand.get("logo_path")
        logo_source: Path | None = None
        logo_public_path: str | None = None
        if logo_path is not None:
            if not isinstance(logo_path, str) or not logo_path.strip():
                raise StateError(
                    "wiki.brand.logo_path must be null or a workspace-relative path",
                    error_code="STATE_CONFIG_INVALID",
                )
            normalized = logo_path.replace("\\", "/")
            candidate = PurePosixPath(normalized)
            if (
                candidate.is_absolute()
                or ".." in candidate.parts
                or normalized != candidate.as_posix()
            ):
                raise StateError(
                    "wiki.brand.logo_path must stay inside the workspace",
                    error_code="STATE_PATH_INVALID",
                )
            logo_source = (root.resolve() / normalized).resolve()
            try:
                logo_source.relative_to(root.resolve())
            except ValueError as exc:
                raise StateError(
                    "wiki.brand.logo_path escapes the workspace",
                    error_code="STATE_PATH_INVALID",
                ) from exc
            if require_logo and not logo_source.is_file():
                raise StateError(
                    "wiki.brand.logo_path does not point to a file",
                    error_code="STATE_CONFIG_INVALID",
                )
            if logo_source.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".svg",
            }:
                raise StateError(
                    "wiki.brand.logo_path must be PNG, JPEG, WebP, or SVG",
                    error_code="STATE_CONFIG_INVALID",
                )
            logo_public_path = (
                "assets/shared/brand-logo" + logo_source.suffix.lower()
            )

        navigation = self.wiki.get("navigation")
        if not isinstance(navigation, dict):
            raise StateError(
                "wiki.navigation must be an object",
                error_code="STATE_CONFIG_INVALID",
            )
        normalized_navigation: dict[str, bool] = {}
        for key in ("wiki", "qa", "graph", "entity_hub"):
            value = navigation.get(key, DEFAULT_WIKI["navigation"][key])
            if not isinstance(value, bool):
                raise StateError(
                    f"wiki.navigation.{key} must be a boolean",
                    error_code="STATE_CONFIG_INVALID",
                )
            normalized_navigation[key] = value
        if normalized_navigation["entity_hub"] and not (
            normalized_navigation["qa"] and normalized_navigation["graph"]
        ):
            raise StateError(
                "wiki.navigation.entity_hub requires qa and graph",
                error_code="STATE_CONFIG_INVALID",
            )
        if target == "platform" and not (
            normalized_navigation["qa"] or normalized_navigation["graph"]
        ):
            raise StateError(
                "platform generation requires qa or graph navigation",
                error_code="STATE_CONFIG_INVALID",
            )

        query_defaults = self.wiki.get("query_defaults")
        if not isinstance(query_defaults, dict):
            raise StateError(
                "wiki.query_defaults must be an object",
                error_code="STATE_CONFIG_INVALID",
            )
        query_mode = query_defaults.get("mode", "mix")
        if query_mode not in VALID_QUERY_MODES:
            raise StateError(
                "wiki.query_defaults.mode is invalid",
                error_code="STATE_CONFIG_INVALID",
            )
        top_k = query_defaults.get("top_k", 20)
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise StateError(
                "wiki.query_defaults.top_k must be an integer",
                error_code="STATE_CONFIG_INVALID",
            )
        if not 1 <= top_k <= 100:
            raise StateError(
                "wiki.query_defaults.top_k must be between 1 and 100",
                error_code="STATE_CONFIG_INVALID",
            )
        history_turns = query_defaults.get("history_turns", 3)
        if (
            isinstance(history_turns, bool)
            or not isinstance(history_turns, int)
        ):
            raise StateError(
                "wiki.query_defaults.history_turns must be an integer",
                error_code="STATE_CONFIG_INVALID",
            )
        if not 0 <= history_turns <= 3:
            raise StateError(
                "wiki.query_defaults.history_turns must be between 0 and 3",
                error_code="STATE_CONFIG_INVALID",
            )

        graph_defaults = self.wiki.get("graph_defaults")
        if not isinstance(graph_defaults, dict):
            raise StateError(
                "wiki.graph_defaults must be an object",
                error_code="STATE_CONFIG_INVALID",
            )
        graph_ranges = {
            "max_depth": (1, 3),
            "max_nodes": (10, 200),
            "popular_limit": (1, 24),
        }
        normalized_graph: dict[str, int] = {}
        for key, (minimum, maximum) in graph_ranges.items():
            value = graph_defaults.get(key, DEFAULT_WIKI["graph_defaults"][key])
            if isinstance(value, bool) or not isinstance(value, int):
                raise StateError(
                    f"wiki.graph_defaults.{key} must be an integer",
                    error_code="STATE_CONFIG_INVALID",
                )
            if not minimum <= value <= maximum:
                raise StateError(
                    f"wiki.graph_defaults.{key} must be between "
                    f"{minimum} and {maximum}",
                    error_code="STATE_CONFIG_INVALID",
                )
            normalized_graph[key] = value

        return {
            "profile": profile,
            "content_contract_version": content_contract_version,
            "title": title.strip(),
            "description": description.strip(),
            "primary_color": primary_color.lower(),
            "logo_source": logo_source,
            "logo_public_path": logo_public_path,
            "navigation": normalized_navigation,
            "query_defaults": {
                "mode": query_mode,
                "top_k": top_k,
                "history_turns": history_turns,
            },
            "graph_defaults": normalized_graph,
        }

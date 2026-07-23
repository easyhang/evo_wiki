from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


SKILL_VERSION = "1.1.0"
MAX_NODES_HARD_LIMIT = 10_000
MAX_EDGES_HARD_LIMIT = 50_000
MAX_CONTENT_UNITS_HARD_LIMIT = 50_000
MAX_TOP_K_HARD_LIMIT = 20
MAX_TIMEOUT_SECONDS_HARD_LIMIT = 300.0


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceSubgraphSettings(StrictFrozenModel):
    # Depth is a traversal choice, not a resource budget. It has no fixed upper
    # bound; nodes, edges, projected units and elapsed time bound the work.
    max_depth: int = Field(default=2, ge=1)
    max_nodes: int = Field(default=300, ge=1, le=MAX_NODES_HARD_LIMIT)
    max_edges: int = Field(default=3000, ge=1, le=MAX_EDGES_HARD_LIMIT)
    max_content_units: int = Field(
        default=1000,
        ge=1,
        le=MAX_CONTENT_UNITS_HARD_LIMIT,
    )
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K_HARD_LIMIT)
    timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=MAX_TIMEOUT_SECONDS_HARD_LIMIT,
    )
    target_chars: int = Field(default=1200, ge=200, le=10000)
    overlap_chars: int = Field(default=120, ge=0, le=2000)
    require_scoped_retrieval: bool = True
    deny_unbounded_global_search: bool = True
    generation_enabled: bool = False

    @model_validator(mode="after")
    def validate_security_contract(self) -> "EvidenceSubgraphSettings":
        if self.overlap_chars >= self.target_chars:
            raise ValueError("overlap_chars must be smaller than target_chars")
        if not self.require_scoped_retrieval:
            raise ValueError("require_scoped_retrieval cannot be disabled")
        if not self.deny_unbounded_global_search:
            raise ValueError("deny_unbounded_global_search cannot be disabled")
        if self.generation_enabled:
            raise ValueError(
                "generation_enabled must remain false in retrieval-only mode"
            )
        return self


class RetrievalPlan(StrictFrozenModel):
    schema_version: int = 1
    skill_id: str = "evidence-subgraph"
    skill_version: str = SKILL_VERSION
    workspace: str
    seeds: tuple[str, ...]
    max_depth: int
    max_nodes: int
    max_edges: int
    max_content_units: int
    top_k: int
    timeout_seconds: float
    scope_granularity: str = "file_to_local_content_unit"
    fallback: tuple[str, ...] = ()
    generation_enabled: bool = False


class GraphNode(StrictFrozenModel):
    id: str = Field(min_length=1)
    labels: tuple[str, ...] = ()
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(StrictFrozenModel):
    id: str = Field(min_length=1)
    type: str | None = None
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphResponse(StrictFrozenModel):
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    is_truncated: bool = False


class EvidenceSubgraph(StrictFrozenModel):
    schema_version: int = 1
    seeds: tuple[str, ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    distances: dict[str, int]
    is_truncated: bool
    subgraph_sha256: str


class ContentUnit(StrictFrozenModel):
    content_unit_id: str
    source_path: str
    source_sha256: str
    ordinal: int = Field(ge=0)
    text: str
    content_sha256: str


class EvidenceChunk(StrictFrozenModel):
    content_unit_id: str
    source_path: str
    content: str
    content_sha256: str
    score: float = Field(ge=0)


class RetrievalTrace(StrictFrozenModel):
    schema_version: int = 1
    run_id: str
    status: str
    skill_id: str = "evidence-subgraph"
    skill_version: str = SKILL_VERSION
    query_sha256: str
    seeds: tuple[str, ...]
    workspace: str | None = None
    max_depth: int | None = None
    max_nodes_budget: int | None = None
    max_edges_budget: int | None = None
    max_content_units_budget: int | None = None
    timeout_seconds_budget: float | None = None
    scope_granularity: str = "file_to_local_content_unit"
    subgraph_sha256: str | None = None
    subgraph_nodes: int = 0
    subgraph_edges: int = 0
    corpus_content_units: int = 0
    allowed_content_units: int = 0
    candidate_reduction_ratio: float | None = None
    evidence_ids: tuple[str, ...] = ()
    evidence_scores: tuple[float, ...] = ()
    evidence_hashes: tuple[str, ...] = ()
    failure_code: str | None = None
    duration_ms: int = Field(ge=0)

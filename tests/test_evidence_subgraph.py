from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from evo_wiki.lightrag_lane import LightRAGServiceClient
from evo_wiki.paths import ProjectPaths
from evo_wiki.retrieval_skills.evidence_subgraph.contracts import (
    EvidenceChunk,
    EvidenceSubgraphSettings,
    RetrievalPlan,
)
from evo_wiki.retrieval_skills.evidence_subgraph.engine import (
    EvidenceSubgraphError,
    _bm25_retrieve,
    _chunk_document,
    _fetch_and_merge_subgraphs,
    retrieve_evidence_subgraph,
)
from evo_wiki.utils import write_json


QUERY = "韩永仁案中为什么认定自首？"


def _make_runtime(tmp_path: Path) -> tuple[ProjectPaths, dict]:
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure_base_dirs()
    documents = [
        {
            "id": "doc-a",
            "source_path": "corpus/raw/102_韩永仁故意伤害案.txt",
            "sha256": "sha-a",
            "text": "韩永仁明知他人已经报案，仍留在现场等待公安人员，到案后如实供述，因此认定为自首。",
        },
        {
            "id": "doc-b",
            "source_path": "corpus/raw/其他案件.txt",
            "sha256": "sha-b",
            "text": "这是与查询无关的其他案件材料，讨论盗窃数额以及财物返还。",
        },
    ]
    (paths.lightrag_input / "documents.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in documents),
        encoding="utf-8",
    )
    write_json(
        paths.lightrag_state / "lightrag-import-ledger.json",
        {
            "service": {"workspace": "evo_wiki"},
            "documents": {
                "doc-a": {
                    "sha256": "sha-a",
                    "service_track_id": "track-a",
                },
                "doc-b": {
                    "sha256": "sha-b",
                    "service_track_id": "track-b",
                },
            },
        },
    )
    config = {
        "retrieval": {
            "evidence_subgraph": {
                "max_depth": 2,
                "max_nodes": 30,
                "max_edges": 100,
                "max_content_units": 100,
                "top_k": 5,
                "timeout_seconds": 30,
                "target_chars": 200,
                "overlap_chars": 20,
                "require_scoped_retrieval": True,
                "deny_unbounded_global_search": True,
                "generation_enabled": False,
            }
        },
        "lightrag": {
            "base_url": "http://127.0.0.1:9621",
            "workspace": "evo_wiki",
        },
    }
    return paths, config


def _graph_payload(*, all_sources: bool = False, truncated: bool = False) -> dict:
    file_path = "102_韩永仁故意伤害案.txt"
    if all_sources:
        file_path += "<SEP>其他案件.txt"
    return {
        "nodes": [
            {
                "id": "韩永仁",
                "labels": ["韩永仁"],
                "properties": {
                    "description": "must never be stored as evidence",
                    "source_id": "remote-chunk-a",
                    "file_path": file_path,
                },
            },
            {
                "id": "自动投案",
                "labels": ["自动投案"],
                "properties": {
                    "description": "graph summary only",
                    "source_id": "remote-chunk-a",
                    "file_path": "102_韩永仁故意伤害案.txt",
                },
            },
        ],
        "edges": [
            {
                "id": "edge-1",
                "type": "RELATED",
                "source": "韩永仁",
                "target": "自动投案",
                "properties": {
                    "source_id": "remote-chunk-a",
                    "file_path": "102_韩永仁故意伤害案.txt",
                },
            }
        ],
        "is_truncated": truncated,
    }


def _replace_graph_source(graph: dict, value: str) -> dict:
    copied = json.loads(json.dumps(graph, ensure_ascii=False))
    for node in copied["nodes"]:
        node["properties"]["file_path"] = value
    for edge in copied["edges"]:
        edge["properties"]["file_path"] = value
    return copied


def _install_service_mock(
    monkeypatch,
    *,
    graph: dict | None = None,
    workspace: str = "evo_wiki",
    expected_depth: int = 2,
    expected_nodes: int = 30,
):
    calls: list[tuple[str, str]] = []

    def fake_request(self, method, path, payload=None):
        calls.append((method, path))
        if (method, path) == ("GET", "/health"):
            return {
                "status": "healthy",
                "configuration": {
                    "workspace": workspace,
                    "storage_workspaces": {"graph_storage": workspace},
                },
            }
        if (method, path) == ("GET", "/openapi.json"):
            return {"paths": {"/graphs": {"get": {}}}, "components": {"schemas": {}}}
        if method == "GET" and path.startswith("/graphs?"):
            parsed = parse_qs(urlparse(path).query)
            assert parsed["max_depth"] == [str(expected_depth)]
            assert parsed["max_nodes"] == [str(expected_nodes)]
            return graph if graph is not None else _graph_payload()
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(LightRAGServiceClient, "request_json", fake_request)
    return calls


def test_settings_reject_extra_fields_and_security_relaxation():
    with pytest.raises(ValidationError):
        EvidenceSubgraphSettings.model_validate({"unknown": 1})
    with pytest.raises(ValidationError):
        EvidenceSubgraphSettings.model_validate({"generation_enabled": True})
    with pytest.raises(ValidationError):
        EvidenceSubgraphSettings.model_validate({"deny_unbounded_global_search": False})
    with pytest.raises(ValidationError):
        EvidenceSubgraphSettings.model_validate({"overlap_chars": 1200, "target_chars": 1200})


def test_depth_has_no_fixed_upper_bound_and_resource_limits_remain_bounded():
    settings = EvidenceSubgraphSettings.model_validate({"max_depth": 10_000})
    assert settings.max_depth == 10_000
    with pytest.raises(ValidationError):
        EvidenceSubgraphSettings.model_validate({"max_nodes": 10_001})
    with pytest.raises(ValidationError):
        EvidenceSubgraphSettings.model_validate({"max_edges": 50_001})
    with pytest.raises(ValidationError):
        EvidenceSubgraphSettings.model_validate({"timeout_seconds": 301})


def test_runtime_config_can_raise_resource_budget_within_hard_limit(
    tmp_path: Path,
    monkeypatch,
):
    paths, config = _make_runtime(tmp_path)
    config["retrieval"]["evidence_subgraph"]["max_nodes"] = 301
    calls = _install_service_mock(monkeypatch, expected_nodes=301)

    result = retrieve_evidence_subgraph(paths, config, query=QUERY, seeds=["韩永仁"])

    assert result["status"] == "success"
    assert any(path.startswith("/graphs?") for _, path in calls)


def test_cli_depth_override_can_raise_configured_depth(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    _install_service_mock(monkeypatch, expected_depth=8)

    result = retrieve_evidence_subgraph(
        paths,
        config,
        query=QUERY,
        seeds=["韩永仁"],
        max_depth=8,
        explain_retrieval=True,
    )

    assert result["retrieval_plan"]["max_depth"] == 8


def test_content_units_are_deterministic_and_bounded():
    text = "第一段说明自动投案。" * 60 + "\n\n" + "第二段说明如实供述。" * 60
    first = _chunk_document(
        text,
        source_path="corpus/raw/a.txt",
        source_sha256="sha-a",
        target_chars=200,
        overlap_chars=20,
    )
    second = _chunk_document(
        text,
        source_path="corpus/raw/a.txt",
        source_sha256="sha-a",
        target_chars=200,
        overlap_chars=20,
    )
    assert [unit.content_unit_id for unit in first] == [
        unit.content_unit_id for unit in second
    ]
    assert first
    assert all(len(unit.text) <= 200 for unit in first)


def test_bm25_handles_chinese_and_never_adds_units():
    units = _chunk_document(
        "自动投案并如实供述构成自首。\n\n完全无关的财务材料。",
        source_path="corpus/raw/a.txt",
        source_sha256="sha-a",
        target_chars=200,
        overlap_chars=0,
    )
    evidence = _bm25_retrieve("为什么认定自动投案自首", units, top_k=5)
    assert evidence
    assert {item.content_unit_id for item in evidence} <= {
        unit.content_unit_id for unit in units
    }
    english_units = _chunk_document(
        "Automatic surrender requires a voluntary appearance.\n\nUnrelated accounting policy.",
        source_path="corpus/raw/english.txt",
        source_sha256="sha-english",
        target_chars=200,
        overlap_chars=0,
    )
    english_evidence = _bm25_retrieve(
        "voluntary automatic surrender",
        english_units,
        top_k=2,
    )
    assert english_evidence
    assert "Automatic surrender" in english_evidence[0].content


def test_multiple_seed_graphs_merge_deterministically():
    class Client:
        def request_json(self, method, path, payload=None):
            seed = parse_qs(urlparse(path).query)["label"][0]
            return {
                "nodes": [
                    {"id": seed, "labels": [seed], "properties": {"file_path": f"{seed}.txt"}},
                    {"id": "shared", "labels": ["shared"], "properties": {"file_path": "shared.txt"}},
                ],
                "edges": [
                    {
                        "id": f"edge-{seed}",
                        "type": "RELATED",
                        "source": seed,
                        "target": "shared",
                        "properties": {"file_path": f"{seed}.txt<SEP>shared.txt"},
                    }
                ],
                "is_truncated": False,
            }

    plan = RetrievalPlan(
        workspace="evo_wiki",
        seeds=("seed-a", "seed-b"),
        max_depth=2,
        max_nodes=10,
        max_edges=100,
        max_content_units=100,
        top_k=5,
        timeout_seconds=30,
    )
    graph = _fetch_and_merge_subgraphs(Client(), plan)
    assert [node.id for node in graph.nodes] == ["seed-a", "seed-b", "shared"]
    assert graph.distances == {"seed-a": 0, "seed-b": 0, "shared": 1}
    assert len(graph.edges) == 2
    assert graph.subgraph_sha256


def test_retrieval_returns_only_scoped_context_and_redacted_trace(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    calls = _install_service_mock(monkeypatch)

    result = retrieve_evidence_subgraph(
        paths,
        config,
        query=QUERY,
        seeds=["韩永仁"],
        explain_retrieval=True,
    )

    assert result["status"] == "success"
    assert result["generation_enabled"] is False
    assert result["scope"]["candidate_reduction_ratio"] > 0
    assert result["scope"]["out_of_scope_evidence"] == 0
    assert result["evidence"]
    assert {item["source_path"] for item in result["evidence"]} == {
        "corpus/raw/102_韩永仁故意伤害案.txt"
    }
    assert not any(path == "/query" for _, path in calls)
    trace = json.loads((paths.root / result["trace_path"]).read_text(encoding="utf-8"))
    serialized = json.dumps(trace, ensure_ascii=False)
    assert QUERY not in serialized
    assert "must never be stored as evidence" not in serialized
    assert "韩永仁明知他人" not in serialized
    assert trace["query_sha256"]
    assert trace["evidence_ids"]


def test_workspace_mismatch_fails_before_graph_and_writes_trace(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    calls = _install_service_mock(monkeypatch, workspace="other")

    with pytest.raises(EvidenceSubgraphError) as caught:
        retrieve_evidence_subgraph(paths, config, query=QUERY, seeds=["韩永仁"])

    assert caught.value.failure_code == "WORKSPACE_MISMATCH"
    assert not any(path.startswith("/graphs?") for _, path in calls)
    trace = json.loads((paths.root / caught.value.trace_path).read_text(encoding="utf-8"))
    assert trace["failure_code"] == "WORKSPACE_MISMATCH"


def test_no_candidate_reduction_is_valid_but_never_uses_query_fallback(
    tmp_path: Path,
    monkeypatch,
):
    paths, config = _make_runtime(tmp_path)
    calls = _install_service_mock(monkeypatch, graph=_graph_payload(all_sources=True))

    result = retrieve_evidence_subgraph(
        paths,
        config,
        query=QUERY,
        seeds=["韩永仁"],
    )

    assert result["status"] == "success"
    assert result["scope"]["candidate_reduction_ratio"] == 0
    assert result["scope"]["scope_reduced"] is False
    assert not any(path == "/query" for _, path in calls)


def test_truncated_graph_fails_closed(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    _install_service_mock(monkeypatch, graph=_graph_payload(truncated=True))

    with pytest.raises(EvidenceSubgraphError) as caught:
        retrieve_evidence_subgraph(paths, config, query=QUERY, seeds=["韩永仁"])

    assert caught.value.failure_code == "GRAPH_BUDGET_EXCEEDED"


def test_edge_budget_exhaustion_fails_closed(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    config["retrieval"]["evidence_subgraph"]["max_edges"] = 1
    graph = _graph_payload()
    graph["edges"].append(
        {
            "id": "edge-2",
            "type": "ALSO_RELATED",
            "source": "韩永仁",
            "target": "自动投案",
            "properties": {"file_path": "102_韩永仁故意伤害案.txt"},
        }
    )
    calls = _install_service_mock(monkeypatch, graph=graph)

    with pytest.raises(EvidenceSubgraphError) as caught:
        retrieve_evidence_subgraph(paths, config, query=QUERY, seeds=["韩永仁"])

    assert caught.value.failure_code == "GRAPH_BUDGET_EXCEEDED"
    assert not any(path == "/query" for _, path in calls)


def test_expired_total_budget_fails_before_graph_request():
    class Client:
        def request_json(self, method, path, payload=None):
            raise AssertionError("expired budget must block the remote request")

    plan = RetrievalPlan(
        workspace="evo_wiki",
        seeds=("韩永仁",),
        max_depth=100,
        max_nodes=10,
        max_edges=100,
        max_content_units=100,
        top_k=5,
        timeout_seconds=30,
    )
    with pytest.raises(EvidenceSubgraphError) as caught:
        _fetch_and_merge_subgraphs(
            Client(),
            plan,
            deadline=time.monotonic() - 1,
        )

    assert caught.value.failure_code == "GRAPH_BUDGET_EXCEEDED"


def test_unmapped_graph_source_fails_closed(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    graph = _replace_graph_source(_graph_payload(), "not-in-active-corpus.txt")
    _install_service_mock(monkeypatch, graph=graph)

    with pytest.raises(EvidenceSubgraphError) as caught:
        retrieve_evidence_subgraph(paths, config, query=QUERY, seeds=["韩永仁"])

    assert caught.value.failure_code == "UNMAPPED_GRAPH_SOURCE"


def test_missing_graph_capability_fails_before_graph_call(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_request(self, method, path, payload=None):
        calls.append((method, path))
        if path == "/health":
            return {
                "status": "healthy",
                "configuration": {"workspace": "evo_wiki"},
            }
        if path == "/openapi.json":
            return {"paths": {}, "components": {"schemas": {}}}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(LightRAGServiceClient, "request_json", fake_request)
    with pytest.raises(EvidenceSubgraphError) as caught:
        retrieve_evidence_subgraph(paths, config, query=QUERY, seeds=["韩永仁"])

    assert caught.value.failure_code == "GRAPH_SUBGRAPH_UNSUPPORTED"
    assert not any(path.startswith("/graphs?") for _, path in calls)


def test_duplicate_active_basenames_fail_closed(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    document = {
        "id": "doc-c",
        "source_path": "corpus/raw/duplicate/102_韩永仁故意伤害案.txt",
        "sha256": "sha-c",
        "text": "重复 basename 的另一份活动文档。",
    }
    with (paths.lightrag_input / "documents.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(document, ensure_ascii=False) + "\n")
    ledger_path = paths.lightrag_state / "lightrag-import-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["documents"]["doc-c"] = {
        "sha256": "sha-c",
        "service_track_id": "track-c",
    }
    write_json(ledger_path, ledger)
    _install_service_mock(monkeypatch)

    with pytest.raises(EvidenceSubgraphError) as caught:
        retrieve_evidence_subgraph(paths, config, query=QUERY, seeds=["韩永仁"])

    assert caught.value.failure_code == "DUPLICATE_SOURCE_BASENAME"


def test_projection_rejects_sha_not_confirmed_by_ledger(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    ledger_path = paths.lightrag_state / "lightrag-import-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["documents"]["doc-a"]["sha256"] = "different-sha"
    write_json(ledger_path, ledger)
    _install_service_mock(monkeypatch)

    with pytest.raises(EvidenceSubgraphError) as caught:
        retrieve_evidence_subgraph(paths, config, query=QUERY, seeds=["韩永仁"])

    assert caught.value.failure_code == "PROJECTION_LEDGER_INCOMPLETE"


def test_runtime_scope_assertion_blocks_injected_evidence(tmp_path: Path, monkeypatch):
    paths, config = _make_runtime(tmp_path)
    _install_service_mock(monkeypatch)

    def fake_retrieve(query, allowed_units, top_k, *, deadline=None):
        return [
            EvidenceChunk(
                content_unit_id="unit-outside",
                source_path="corpus/raw/outside.txt",
                content="outside",
                content_sha256="hash-outside",
                score=1.0,
            )
        ]

    monkeypatch.setattr(
        "evo_wiki.retrieval_skills.evidence_subgraph.engine._bm25_retrieve",
        fake_retrieve,
    )

    with pytest.raises(EvidenceSubgraphError) as caught:
        retrieve_evidence_subgraph(paths, config, query=QUERY, seeds=["韩永仁"])

    assert caught.value.failure_code == "SCOPE_VIOLATION"

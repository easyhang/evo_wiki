from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from evo_wiki.corpus import scan_corpus
from evo_wiki.gateway_http import create_gateway_app
from evo_wiki.lightrag_lane import LightRAGBuildError
from evo_wiki.query_gateway import (
    GatewayQueryRequest,
    TrustedQueryGateway,
    gateway_settings,
)
from evo_wiki.state import ActionGate, RemoteStatus, StateError, StateStore

from test_cli_smoke import run_cli


class FakeQueryClient:
    def __init__(
        self,
        response: dict[str, Any],
        *,
        bypass_response: dict[str, Any] | None = None,
        after_query: Callable[[], None] | None = None,
        after_graph: Callable[[], None] | None = None,
        supports_history: bool = True,
        supports_bypass: bool = True,
    ):
        self.response = response
        self.bypass_response = bypass_response or {
            "response": "这是由模型通用知识生成的回答。"
        }
        self.after_query = after_query
        self.after_graph = after_graph
        self.supports_history = supports_history
        self.supports_bypass = supports_bypass
        self.calls: list[tuple[str, str]] = []
        self.query_payloads: list[dict[str, Any]] = []

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path))
        if path == "/health":
            return {
                "status": "healthy",
                "configuration": {
                    "workspace": "gateway",
                    "storage_workspaces": {
                        "kv_storage": "gateway",
                        "vector_storage": "gateway",
                        "graph_storage": "gateway",
                        "doc_status_storage": "gateway",
                    },
                },
            }
        if path == "/openapi.json":
            properties: dict[str, Any] = {
                "include_chunk_content": {"type": "boolean"},
                "mode": {
                    "type": "string",
                    "enum": (
                        ["mix", "hybrid", "bypass"]
                        if self.supports_bypass
                        else ["mix", "hybrid"]
                    ),
                },
            }
            if self.supports_history:
                properties["conversation_history"] = {"type": "array"}
            return {
                "components": {
                    "schemas": {
                        "QueryRequest": {
                            "properties": properties
                        }
                    }
                },
                "paths": {"/query": {"post": {}}},
            }
        if path.startswith("/graphs") or path.startswith("/graph/label/"):
            if self.after_graph is not None:
                self.after_graph()
            return {"nodes": [], "edges": []}
        raise AssertionError((method, path, payload))

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("POST", path))
        assert path == "/query"
        bypass = payload["mode"] == "bypass"
        assert payload["include_references"] is (not bypass)
        assert payload["include_chunk_content"] is (not bypass)
        self.query_payloads.append(payload)
        if self.after_query is not None and not bypass:
            self.after_query()
        return self.bypass_response if bypass else self.response


def _gateway_workspace(
    tmp_path: Path,
    *,
    mode: str = "enforce",
) -> tuple[Path, StateStore, dict[str, Any], str]:
    project = tmp_path / f"gateway-{mode}"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    source = project / "corpus" / "raw" / "case.md"
    source.write_text(
        "韩永仁案在2020年5月认定具有自首情节。",
        encoding="utf-8",
    )
    store = StateStore(project)
    item = scan_corpus(project, project / "corpus")[0]
    revisions = store.stage_files([item])
    lightrag = {
        "mode": "service",
        "base_url": "http://127.0.0.1:9621",
        "workspace": "gateway",
        "embedding": {"batch_size": 8},
    }
    partition_id, fingerprint = store.ensure_partition(lightrag)
    binding_id = store.mark_submission_started(
        source_path=item.path,
        sha256=item.sha256,
        partition_id=partition_id,
        backend_fingerprint=fingerprint,
    )
    store.mark_submission_acknowledged(
        binding_id,
        track_id="track-gateway",
    )
    store.mark_binding_observation(
        binding_id,
        remote_status=RemoteStatus.PROCESSED,
        action_gate=ActionGate.OPEN,
        gate_reason=None,
        chunk_count=1,
    )
    revision_id = revisions[(item.path, item.sha256)]
    with store.business_transaction() as connection:
        connection.execute(
            "UPDATE source_revision SET status = 'ACTIVE' WHERE id = ?",
            (revision_id,),
        )
    project_config = {
        "lightrag": lightrag,
        "query_gateway": {
            "mode": mode,
            "request_timeout_seconds": 10,
            "drain_timeout_seconds": 2,
        },
        "security": {
            "auth_mode": "trusted_proxy",
            "default_domain": "default",
        },
    }
    project_json = project / "project.json"
    persisted = json.loads(project_json.read_text(encoding="utf-8"))
    persisted["lightrag"] = lightrag
    persisted["query_gateway"] = project_config["query_gateway"]
    persisted["security"] = project_config["security"]
    project_json.write_text(
        json.dumps(persisted, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return project, store, project_config, partition_id


def _response(
    *,
    source: str = "case.md",
    content: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "response": "韩永仁案在2020年5月被认定具有自首情节。",
        "references": [
            {
                "file_path": source,
                "content": (
                    ["韩永仁案在2020年5月认定具有自首情节。"]
                    if content is None
                    else content
                ),
            }
        ],
    }


def test_gateway_answers_only_after_durable_active_evidence(
    tmp_path: Path,
):
    project, store, config, _ = _gateway_workspace(tmp_path)
    client = FakeQueryClient(_response())
    before_seq = store.state_commit_seq()
    gateway = TrustedQueryGateway(
        store,
        config,
        client=client,
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(
            query="韩永仁案为什么认定自首？",
            mode="mix",
        ),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer_origin == "knowledge_base"
    assert result.evidence_status == "grounded"
    assert result.review_status == "not_required"
    assert result.answer
    assert result.citations[0].source_label == "case.md"
    assert result.citations[0].marker == "1"
    assert store.query_run(result.request_id)["status"] == "ANSWERED"
    assert store.state_commit_seq() == before_seq
    database_bytes = (
        project / "artifacts" / "state" / "evo_wiki.sqlite3"
    ).read_bytes()
    assert "韩永仁案为什么认定自首".encode() not in database_bytes
    assert result.answer.encode() not in database_bytes
    assert result.context_turns_used == 0


@pytest.mark.parametrize("pair_count", [1, 2, 3])
def test_gateway_accepts_up_to_three_complete_context_pairs(
    tmp_path: Path,
    pair_count: int,
):
    project, store, config, _ = _gateway_workspace(tmp_path)
    client = FakeQueryClient(_response())
    gateway = TrustedQueryGateway(
        store,
        config,
        client=client,
        audit_key=b"0123456789abcdef0123456789abcdef",
    )
    history: list[dict[str, str]] = []
    for index in range(pair_count):
        history.extend(
            [
                {
                    "role": "user",
                    "content": f"历史问题-{index}-韩永仁案的自首依据是什么？",
                },
                {
                    "role": "assistant",
                    "content": f"历史回答-{index}-依据已验证材料。",
                },
            ]
        )

    result = gateway.query(
        GatewayQueryRequest(
            query="本轮继续说明为什么认定自首？",
            conversation_history=history,
        ),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.context_turns_used == pair_count
    assert client.query_payloads[0]["conversation_history"] == history
    database_bytes = (
        project / "artifacts" / "state" / "evo_wiki.sqlite3"
    ).read_bytes()
    assert "历史问题".encode() not in database_bytes
    assert "历史回答".encode() not in database_bytes


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "user", "content": "残缺问题"}],
        [
            {"role": "assistant", "content": "错误起始角色"},
            {"role": "user", "content": "错误结束角色"},
        ],
        [
            {"role": "user", "content": "问题一"},
            {"role": "user", "content": "重复用户角色"},
        ],
        [
            {"role": "user", "content": "问"} ,
            {"role": "assistant", "content": "答"},
        ]
        * 4,
        [
            {"role": "user", "content": "问" * 4_001},
            {"role": "assistant", "content": "答"},
        ],
        [
            {"role": "user", "content": "问" * 2_001},
            {"role": "assistant", "content": "答" * 2_000},
        ]
        * 3,
    ],
)
def test_gateway_rejects_invalid_context_history(
    history: list[dict[str, str]],
):
    with pytest.raises(ValueError):
        GatewayQueryRequest(
            query="继续追问",
            conversation_history=history,
        )


def test_gateway_uses_historical_user_questions_for_relevance(
    tmp_path: Path,
):
    _, store, config, _ = _gateway_workspace(tmp_path)
    client = FakeQueryClient(_response())
    gateway = TrustedQueryGateway(
        store,
        config,
        client=client,
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(
            query="为什么？",
            conversation_history=[
                {
                    "role": "user",
                    "content": "韩永仁案为什么认定自首？",
                },
                {
                    "role": "assistant",
                    "content": "上一轮已根据材料回答。",
                },
            ],
        ),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.evidence_status == "grounded"
    assert result.context_turns_used == 1


def test_gateway_fails_closed_without_history_capability(tmp_path: Path):
    _, store, config, _ = _gateway_workspace(tmp_path)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response(), supports_history=False),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    with pytest.raises(StateError) as caught:
        gateway.check()

    assert (
        caught.value.error_code
        == "QUERY_CONVERSATION_HISTORY_UNSUPPORTED"
    )


def test_gateway_fails_readiness_without_bypass_capability(tmp_path: Path):
    _, store, config, _ = _gateway_workspace(tmp_path)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response(), supports_bypass=False),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    with pytest.raises(StateError) as caught:
        gateway.check()

    assert caught.value.error_code == "QUERY_BYPASS_UNSUPPORTED"


def test_gateway_uses_general_model_for_empty_chunk_content(tmp_path: Path):
    project, store, config, _ = _gateway_workspace(tmp_path)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response(content=[])),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer_origin == "general_model"
    assert result.evidence_status == "ungrounded"
    assert result.review_status == "pending"
    assert result.answer == "这是由模型通用知识生成的回答。"
    assert result.citations == []
    assert result.error_code is None
    assert result.audit_id
    assert store.query_run(result.request_id)["status"] == "ANSWERED"
    payload = (
        project
        / "artifacts"
        / "query-audit"
        / "open"
        / f"{result.audit_id}.json"
    )
    assert payload.is_file()
    assert payload.stat().st_mode & 0o777 == 0o600
    assert payload.parent.stat().st_mode & 0o777 == 0o700


def test_gateway_unmapped_reference_falls_back_and_enters_audit(
    tmp_path: Path,
):
    _, store, config, _ = _gateway_workspace(tmp_path)
    before_seq = store.state_commit_seq()
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response(source="other.md")),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer_origin == "general_model"
    assert result.evidence_status == "ungrounded"
    assert result.review_status == "pending"
    assert result.answer
    assert result.audit_id
    audit = store.audit_item(result.audit_id)
    assert audit["trigger_code"] == "QUERY_REFERENCE_UNMAPPED"
    assert audit["evidence"]["reference_count"] == 1
    assert audit["evidence"]["evidence_status"] == "ungrounded"
    serialized = json.dumps(audit, ensure_ascii=False)
    assert "韩永仁" not in serialized
    assert store.state_commit_seq() == before_seq


def test_gateway_keeps_rag_answer_when_some_evidence_is_valid(
    tmp_path: Path,
):
    _, store, config, _ = _gateway_workspace(tmp_path)
    response = _response()
    response["references"].append(
        {
            "reference_id": "2",
            "file_path": "unmapped.md",
            "content": ["韩永仁案的其他材料。"],
        }
    )
    client = FakeQueryClient(response)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=client,
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer_origin == "knowledge_base"
    assert result.evidence_status == "partially_grounded"
    assert result.review_status == "pending"
    assert len(result.citations) == 1
    assert len(client.query_payloads) == 1
    assert result.audit_id


def test_gateway_filters_individually_irrelevant_mapped_citation(
    tmp_path: Path,
):
    _, store, config, _ = _gateway_workspace(tmp_path)
    response = _response()
    response["references"].append(
        {
            "reference_id": "2",
            "file_path": "case.md",
            "content": ["完全不同的气象主题和温度记录。"],
        }
    )
    client = FakeQueryClient(response)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=client,
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer_origin == "knowledge_base"
    assert result.evidence_status == "partially_grounded"
    assert [citation.marker for citation in result.citations] == ["1"]
    assert len(client.query_payloads) == 1
    assert result.audit_id


def test_gateway_marks_short_question_as_partially_grounded(
    tmp_path: Path,
):
    _, store, config, _ = _gateway_workspace(tmp_path)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response()),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="嗯？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer_origin == "knowledge_base"
    assert result.evidence_status == "partially_grounded"
    assert result.review_status == "pending"
    assert result.citations


def test_gateway_marks_unsupported_critical_number_as_partial(
    tmp_path: Path,
):
    _, store, config, _ = _gateway_workspace(tmp_path)
    response = _response(content=["韩永仁案被认定具有自首情节。"])
    response["response"] = "韩永仁案在2021年6月被认定具有自首情节。"
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(response),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer_origin == "knowledge_base"
    assert result.evidence_status == "partially_grounded"
    assert result.review_status == "pending"
    assert result.citations


@pytest.mark.parametrize(
    "response",
    [
        {
            "response": "这是首轮生成但没有引用的回答。",
            "references": [],
        },
        {
            "response": "未找到相关信息，无法回答。",
            "references": [
                {
                    "reference_id": "1",
                    "file_path": "case.md",
                    "content": ["韩永仁案在2020年5月认定具有自首情节。"],
                }
            ],
        },
        {
            "response": "这是引用内容与问题无关的首轮回答。",
            "references": [
                {
                    "reference_id": "1",
                    "file_path": "case.md",
                    "content": ["完全不同的主题，没有相关实体。"],
                }
            ],
        },
    ],
)
def test_gateway_uses_bypass_for_unusable_rag_answer(
    tmp_path: Path,
    response: dict[str, Any],
):
    _, store, config, _ = _gateway_workspace(tmp_path)
    client = FakeQueryClient(response)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=client,
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer_origin == "general_model"
    assert result.evidence_status == "ungrounded"
    assert result.review_status == "pending"
    assert result.citations == []
    assert [item["mode"] for item in client.query_payloads] == [
        "mix",
        "bypass",
    ]


def test_gateway_reports_failure_when_bypass_answer_is_empty(
    tmp_path: Path,
):
    _, store, config, _ = _gateway_workspace(tmp_path)
    client = FakeQueryClient(
        {"response": "", "references": []},
        bypass_response={"response": ""},
    )
    gateway = TrustedQueryGateway(
        store,
        config,
        client=client,
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "failed"
    assert result.answer is None
    assert result.evidence_status is None
    assert result.error_code == "QUERY_ANSWER_EMPTY"
    row = store.query_run(result.request_id)
    assert row["generation_status"] == "failed"
    assert row["status"] == "FAILED"


@pytest.mark.parametrize("failed_mode", ["mix", "bypass"])
def test_gateway_maps_backend_exception_to_generation_failure(
    tmp_path: Path,
    failed_mode: str,
):
    class FailingQueryClient(FakeQueryClient):
        def post_json(
            self,
            path: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            if payload["mode"] == failed_mode:
                raise LightRAGBuildError(
                    "private upstream failure body",
                    failure_code="REMOTE_HTTP_ERROR",
                )
            return super().post_json(path, payload)

    project, store, config, _ = _gateway_workspace(tmp_path)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FailingQueryClient(
            {"response": "没有引用。", "references": []}
        ),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="知识库之外的问题是什么？"),
        principal="reader-a",
    )

    assert result.generation_status == "failed"
    assert result.answer is None
    assert result.error_code == "QUERY_BACKEND_REQUEST_FAILED"
    database_bytes = (
        project / "artifacts" / "state" / "evo_wiki.sqlite3"
    ).read_bytes()
    assert b"private upstream failure body" not in database_bytes


def test_gateway_displays_nonempty_final_refusal_text(
    tmp_path: Path,
):
    _, store, config, _ = _gateway_workspace(tmp_path)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(
            {
                "response": "未找到相关信息，无法回答。",
                "references": [],
            },
            bypass_response={
                "response": "我仍然无法确定，但这是最终模型回答。"
            },
        ),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="知识库之外的问题是什么？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer == "我仍然无法确定，但这是最终模型回答。"
    assert result.answer_origin == "general_model"
    assert result.evidence_status == "ungrounded"
    assert result.review_status == "pending"


def test_query_audit_cli_requires_explicit_content_and_deletes_on_approval(
    tmp_path: Path,
):
    project, store, config, _ = _gateway_workspace(tmp_path)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(
            {"response": "没有知识库依据。", "references": []},
            bypass_response={"response": "通用知识回答正文。"},
        ),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )
    result = gateway.query(
        GatewayQueryRequest(query="知识库之外的问题是什么？"),
        principal="reader-a",
    )
    assert result.audit_id
    payload_path = (
        project
        / "artifacts"
        / "query-audit"
        / "open"
        / f"{result.audit_id}.json"
    )
    assert payload_path.is_file()

    hidden = run_cli(
        tmp_path,
        "audit",
        "show",
        "--root",
        str(project),
        "--audit-id",
        result.audit_id,
        "--json",
    )
    visible = run_cli(
        tmp_path,
        "audit",
        "show",
        "--root",
        str(project),
        "--audit-id",
        result.audit_id,
        "--include-content",
        "--json",
    )

    assert hidden.returncode == 0, hidden.stderr
    assert "通用知识回答正文" not in hidden.stdout
    assert visible.returncode == 0, visible.stderr
    assert "通用知识回答正文" in visible.stdout
    visible_payload = json.loads(visible.stdout)
    assert (
        visible_payload["item"]["content"]["question"]
        == "知识库之外的问题是什么？"
    )
    database_bytes = (
        project / "artifacts" / "state" / "evo_wiki.sqlite3"
    ).read_bytes()
    assert "知识库之外的问题".encode() not in database_bytes
    assert "通用知识回答正文".encode() not in database_bytes

    resolved = run_cli(
        tmp_path,
        "audit",
        "resolve",
        "--root",
        str(project),
        "--audit-id",
        result.audit_id,
        "--confirm",
        result.audit_id,
        "--resolution",
        "APPROVED",
        "--json",
    )
    assert resolved.returncode == 0, resolved.stderr
    resolved_payload = json.loads(resolved.stdout)
    assert resolved_payload["item"]["review_status"] == "approved"
    assert resolved_payload["payload_deleted"] is True
    assert not payload_path.exists()
    assert store.query_run(result.request_id)["review_status"] == "approved"


def test_audit_snapshot_failure_does_not_block_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import evo_wiki.query_gateway as query_gateway

    project, store, config, _ = _gateway_workspace(tmp_path)

    def fail_snapshot(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        raise OSError("simulated protected snapshot failure")

    monkeypatch.setattr(
        query_gateway,
        "write_query_audit_payload",
        fail_snapshot,
    )
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(
            {"response": "没有知识库依据。", "references": []},
            bypass_response={"response": "仍然交付的通用知识回答。"},
        ),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="知识库之外的问题是什么？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer == "仍然交付的通用知识回答。"
    assert result.review_status == "unavailable"
    assert result.audit_id is None
    assert store.query_run(result.request_id)["review_status"] == "unavailable"
    assert store.list_audit_items() == []
    assert not any(
        (project / "artifacts" / "query-audit" / "open").glob("*.json")
    )


def test_audit_database_failure_rolls_back_and_delivers_without_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project, store, config, _ = _gateway_workspace(tmp_path)

    def fail_audit_insert(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("simulated audit database failure")

    monkeypatch.setattr(
        store,
        "_insert_audit_item_in_connection",
        fail_audit_insert,
    )
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(
            {"response": "没有知识库依据。", "references": []},
            bypass_response={"response": "数据库失败后仍交付。"},
        ),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="知识库之外的问题是什么？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer == "数据库失败后仍交付。"
    assert result.review_status == "unavailable"
    assert store.query_run(result.request_id)["status"] == "ANSWERED"
    assert store.list_audit_items() == []
    assert not any(
        (project / "artifacts" / "query-audit" / "open").glob("*.json")
    )


def test_maintenance_fence_blocks_query_before_backend_call(
    tmp_path: Path,
):
    _, store, config, partition_id = _gateway_workspace(tmp_path)
    store.open_maintenance_fence(
        fence_id="fence-test",
        retrieval_partition_id=partition_id,
        reason_code="TEST",
        deadline_seconds=30,
    )
    client = FakeQueryClient(_response())
    gateway = TrustedQueryGateway(
        store,
        config,
        client=client,
        audit_key=b"0123456789abcdef0123456789abcdef",
    )
    gateway.check()

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "failed"
    assert result.error_code == "QUERY_MAINTENANCE_ACTIVE"
    assert ("POST", "/query") not in client.calls


def test_fence_opened_during_backend_call_prevents_answer_delivery(
    tmp_path: Path,
):
    _, store, config, partition_id = _gateway_workspace(tmp_path)

    def open_fence() -> None:
        store.open_maintenance_fence(
            fence_id="fence-race",
            retrieval_partition_id=partition_id,
            reason_code="TEST_RACE",
            deadline_seconds=30,
        )

    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response(), after_query=open_fence),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "failed"
    assert result.answer is None
    assert store.query_run(result.request_id)["status"] == "REFUSED"


def test_fence_race_does_not_leave_audit_for_undelivered_answer(
    tmp_path: Path,
):
    project, store, config, partition_id = _gateway_workspace(tmp_path)

    def open_fence() -> None:
        store.open_maintenance_fence(
            fence_id="fence-ungrounded-race",
            retrieval_partition_id=partition_id,
            reason_code="TEST_UNGROUNDED_RACE",
            deadline_seconds=30,
        )

    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(
            {"response": "首轮没有引用。", "references": []},
            bypass_response={"response": "本应交付的通用知识回答。"},
            after_query=open_fence,
        ),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="知识库之外的问题是什么？"),
        principal="reader-a",
    )

    assert result.generation_status == "failed"
    assert result.error_code == "QUERY_MAINTENANCE_ACTIVE"
    assert store.list_audit_items() == []
    assert not any(
        (project / "artifacts" / "query-audit" / "open").glob("*.json")
    )


def test_shadow_and_enforce_share_the_same_delivery_semantics(
    tmp_path: Path,
):
    _, store, config, _ = _gateway_workspace(tmp_path, mode="shadow")
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response(source="other.md")),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    result = gateway.query(
        GatewayQueryRequest(query="韩永仁案为什么认定自首？"),
        principal="reader-a",
    )

    assert result.generation_status == "succeeded"
    assert result.answer_origin == "general_model"
    assert result.evidence_status == "ungrounded"
    assert result.review_status == "pending"
    assert result.audit_id


def test_asgi_gateway_requires_identity_and_returns_strict_contract(
    tmp_path: Path,
):
    from starlette.testclient import TestClient

    _, store, config, _ = _gateway_workspace(tmp_path)
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response()),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    with TestClient(create_gateway_app(gateway)) as client:
        health = client.get("/internal/healthz")
        unauthorized = client.post(
            "/api/query",
            json={
                "schema_version": 2,
                "query": "韩永仁案为什么认定自首？",
                "mode": "mix",
                "top_k": 20,
            },
        )
        answered = client.post(
            "/api/query",
            headers={"X-Evo-Principal": "reader-a"},
            json={
                "schema_version": 2,
                "query": "韩永仁案为什么认定自首？",
                "mode": "mix",
                "top_k": 20,
            },
        )
        invalid = client.post(
            "/api/query",
            headers={"X-Evo-Principal": "reader-a"},
            json={
                "schema_version": 2,
                "query": "韩永仁案为什么认定自首？",
                "mode": "mix",
                "top_k": 20,
                "include_chunk_content": False,
            },
        )
        legacy = client.post(
            "/api/query",
            headers={"X-Evo-Principal": "reader-a"},
            json={
                "schema_version": 1,
                "query": "韩永仁案为什么认定自首？",
            },
        )

    assert health.status_code == 200
    assert unauthorized.status_code == 401
    assert answered.status_code == 200
    assert answered.json()["generation_status"] == "succeeded"
    assert answered.json()["evidence_status"] == "grounded"
    assert "response" not in answered.json()
    assert "status" not in answered.json()
    assert "evidence" not in answered.json()
    assert "refused" not in answered.json()
    assert "needs_audit" not in answered.json()
    assert "shadow_failed" not in answered.json()
    assert answered.json()["citations"][0]["source_label"] == "case.md"
    assert invalid.status_code == 400
    assert legacy.status_code == 400
    assert (
        invalid.json()["error_code"]
        == "QUERY_REQUEST_INVALID"
    )


def test_asgi_delivers_ungrounded_answer_and_maps_empty_final_to_502(
    tmp_path: Path,
):
    from starlette.testclient import TestClient

    _, delivered_store, delivered_config, _ = _gateway_workspace(
        tmp_path,
        mode="shadow",
    )
    delivered_gateway = TrustedQueryGateway(
        delivered_store,
        delivered_config,
        client=FakeQueryClient(
            {"response": "首轮没有引用。", "references": []},
            bypass_response={"response": "通用知识回答。"},
        ),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )
    with TestClient(create_gateway_app(delivered_gateway)) as client:
        delivered = client.post(
            "/api/query",
            headers={"X-Evo-Principal": "reader-a"},
            json={
                "schema_version": 2,
                "query": "知识库之外的问题是什么？",
            },
        )

    _, failed_store, failed_config, _ = _gateway_workspace(
        tmp_path,
        mode="enforce",
    )
    failed_gateway = TrustedQueryGateway(
        failed_store,
        failed_config,
        client=FakeQueryClient(
            {"response": "", "references": []},
            bypass_response={"response": ""},
        ),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )
    with TestClient(create_gateway_app(failed_gateway)) as client:
        failed = client.post(
            "/api/query",
            headers={"X-Evo-Principal": "reader-a"},
            json={
                "schema_version": 2,
                "query": "知识库之外的问题是什么？",
            },
        )

    assert delivered.status_code == 200
    assert delivered.json()["generation_status"] == "succeeded"
    assert delivered.json()["evidence_status"] == "ungrounded"
    assert delivered.json()["answer"] == "通用知识回答。"
    assert failed.status_code == 502
    assert failed.json()["generation_status"] == "failed"
    assert failed.json()["answer"] is None
    assert failed.json()["error_code"] == "QUERY_ANSWER_EMPTY"


def test_gateway_status_and_audit_cli_are_read_only(tmp_path: Path):
    project, store, _, _ = _gateway_workspace(tmp_path)
    store.create_audit_item(
        audit_id="audit-cli",
        trigger_code="QUERY_REFERENCE_UNMAPPED",
        severity="HIGH",
        subject_type="query_run",
        subject_id="qry-cli",
        evidence={
            "schema_version": 1,
            "reference_count": 1,
            "error_code": "QUERY_REFERENCE_UNMAPPED",
        },
    )
    before = store.state_commit_seq()

    gateway_status = run_cli(
        tmp_path,
        "gateway",
        "status",
        "--root",
        str(project),
        "--json",
    )
    audit_list = run_cli(
        tmp_path,
        "audit",
        "list",
        "--root",
        str(project),
        "--json",
    )

    assert gateway_status.returncode == 0, gateway_status.stderr
    assert json.loads(gateway_status.stdout)["workspace_mutated"] is False
    assert audit_list.returncode == 0, audit_list.stderr
    payload = json.loads(audit_list.stdout)
    assert payload["items"][0]["id"] == "audit-cli"
    assert payload["workspace_mutated"] is False
    assert StateStore(project).state_commit_seq() == before


def test_graph_reader_lease_closes_delivery_race(tmp_path: Path):
    from starlette.testclient import TestClient

    _, store, config, partition_id = _gateway_workspace(tmp_path)

    def open_fence() -> None:
        store.open_maintenance_fence(
            fence_id="fence-graph-race",
            retrieval_partition_id=partition_id,
            reason_code="TEST_GRAPH_RACE",
            deadline_seconds=30,
        )

    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response(), after_graph=open_fence),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )

    with TestClient(create_gateway_app(gateway)) as client:
        response = client.get(
            "/api/graphs",
            headers={"X-Evo-Principal": "reader-a"},
        )

    assert response.status_code == 503
    assert response.json()["error_code"] == "QUERY_MAINTENANCE_ACTIVE"
    connection = store.connect(read_only=True)
    try:
        row = connection.execute(
            """
            SELECT request_mode, status, error_code
            FROM query_run WHERE request_mode = 'graph'
            """
        ).fetchone()
    finally:
        connection.close()
    assert dict(row) == {
        "request_mode": "graph",
        "status": "REFUSED",
        "error_code": "QUERY_MAINTENANCE_ACTIVE",
    }


def test_combined_local_platform_serves_static_and_governed_api(
    tmp_path: Path,
):
    from starlette.testclient import TestClient

    project, store, config, _ = _gateway_workspace(
        tmp_path,
        mode="shadow",
    )
    config["security"]["auth_mode"] = "local_single_user"
    gateway = TrustedQueryGateway(
        store,
        config,
        client=FakeQueryClient(_response()),
        audit_key=b"0123456789abcdef0123456789abcdef",
    )
    platform = project / "artifacts" / "platform"
    (platform / "app").mkdir(parents=True, exist_ok=True)
    (platform / "index.html").write_text(
        "<!doctype html><h1>Wiki</h1>",
        encoding="utf-8",
    )
    (platform / "app" / "index.html").write_text(
        "<!doctype html><h1>App</h1>",
        encoding="utf-8",
    )
    (platform / "nginx.conf").write_text(
        "private",
        encoding="utf-8",
    )
    (platform / "README.md").write_text(
        "private",
        encoding="utf-8",
    )

    with TestClient(
        create_gateway_app(gateway, platform_dir=platform)
    ) as client:
        assert client.get("/").status_code == 200
        assert "Wiki" in client.get("/").text
        assert client.get("/app/").status_code == 200
        assert "App" in client.get("/app/").text
        assert client.get("/nginx.conf").status_code == 404
        assert client.get("/README.md").status_code == 404
        assert client.get("/status").status_code == 404
        assert client.get("/status/private.json").status_code == 404
        answer = client.post(
            "/api/query",
            json={
                "schema_version": 2,
                "query": "韩永仁案为什么认定自首？",
                "mode": "mix",
                "top_k": 20,
            },
        )

    assert answer.status_code == 200
    assert answer.json()["generation_status"] == "succeeded"


def test_enforce_gateway_rejects_unsafe_safety_configuration():
    base = {
        "query_gateway": {
            "mode": "enforce",
            "listen": "0.0.0.0:8765",
        },
        "security": {
            "auth_mode": "trusted_proxy",
            "fail_closed": True,
        },
    }
    with pytest.raises(StateError) as caught:
        gateway_settings(base)
    assert caught.value.error_code == "QUERY_GATEWAY_BIND_UNSAFE"

    base["query_gateway"]["listen"] = "127.0.0.1:8765"
    base["query_gateway"]["audit_required"] = False
    with pytest.raises(StateError) as caught:
        gateway_settings(base)
    assert caught.value.error_code == "QUERY_GATEWAY_AUDIT_REQUIRED"

#!/usr/bin/env python3
"""Read-only LightRAG query-flow probe for the existing nine-document corpus.

The probe deliberately uses only GET /health, GET /openapi.json and POST
/query. It never submits, deletes, reprocesses, or otherwise mutates documents.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
EXPERIMENT_ROOT = SCRIPT_DIR.parent
INPUT_DOCUMENTS = EXPERIMENT_ROOT / "artifacts" / "lightrag" / "input" / "documents.jsonl"
RESULTS_PATH = SCRIPT_DIR / "query_flow_results.json"
REPORT_PATH = SCRIPT_DIR / "query_flow_report.md"
RAW_DIR = SCRIPT_DIR / "raw"
DEFAULT_BASE_URL = "http://127.0.0.1:9621"
KNOWN_QUERY_MODES = {"local", "global", "hybrid", "naive", "mix", "bypass"}
NO_CONTEXT_MARKERS = ("[no-context]", "no context", "没有上下文")
REFUSAL_MARKERS = ("无法", "没有足够", "未找到", "不能确定", "不在当前语料", "没有相关依据", "未涉及", "不存在", "自然亦无")
URL_RE = re.compile(r"https?://[^\s\"']+")


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    duration_ms: int
    payload: Any
    error_code: str | None = None
    detail: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_detail(value: object, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = URL_RE.sub("<redacted-url>", str(value))
    text = re.sub(r"(?i)(bearer|api[-_ ]?key)\s+[^\s,;]+", r"\1 <redacted>", text)
    return text[:limit]


def auth_headers() -> dict[str, str]:
    """Read optional auth without ever returning it in an experiment result."""
    headers: dict[str, str] = {}
    api_key = os.environ.get("LIGHTRAG_API_KEY")
    bearer = os.environ.get("LIGHTRAG_BEARER_TOKEN")
    if api_key:
        headers["X-API-Key"] = api_key
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def request_json(base_url: str, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 60.0) -> HttpResult:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", **auth_headers()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured local service
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        detail = safe_detail(exc.read().decode("utf-8", errors="replace"))
        return HttpResult(exc.code, elapsed_ms(started), None, f"HTTP_{exc.code}", detail)
    except URLError as exc:
        return HttpResult(None, elapsed_ms(started), None, "SERVICE_UNREACHABLE", safe_detail(exc.reason))
    except TimeoutError as exc:
        return HttpResult(None, elapsed_ms(started), None, "TIMEOUT", safe_detail(exc))
    except OSError as exc:
        return HttpResult(None, elapsed_ms(started), None, "NETWORK_ERROR", safe_detail(exc))

    if not raw:
        return HttpResult(status, elapsed_ms(started), {}, "EMPTY_RESPONSE")
    try:
        return HttpResult(status, elapsed_ms(started), json.loads(raw))
    except json.JSONDecodeError:
        return HttpResult(status, elapsed_ms(started), None, "NON_JSON_RESPONSE", safe_detail(raw))


def elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def health_summary(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"shape": type(payload).__name__}
    configuration = payload.get("configuration")
    configuration = configuration if isinstance(configuration, dict) else {}
    queues = payload.get("llm_queue_status")
    query_queue = queues.get("query") if isinstance(queues, dict) else None
    return {
        "status": payload.get("status"),
        "pipeline_busy": payload.get("pipeline_busy"),
        "pipeline_active": payload.get("pipeline_active"),
        "pipeline_scanning": payload.get("pipeline_scanning"),
        "core_version": payload.get("core_version"),
        "api_version": payload.get("api_version"),
        "workspace_empty": configuration.get("workspace") in (None, ""),
        "embedding_batch_num": configuration.get("embedding_batch_num"),
        "query_queue": {
            "queued": query_queue.get("queued") if isinstance(query_queue, dict) else None,
            "running": query_queue.get("running") if isinstance(query_queue, dict) else None,
            "failed_total": query_queue.get("failed_total") if isinstance(query_queue, dict) else None,
        },
    }


def openapi_summary(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"shape": type(payload).__name__, "query_supported": False}
    schemas = payload.get("components", {}).get("schemas", {})
    query_schema = schemas.get("QueryRequest", {}) if isinstance(schemas, dict) else {}
    properties = query_schema.get("properties", {}) if isinstance(query_schema, dict) else {}
    mode_schema = properties.get("mode", {}) if isinstance(properties, dict) else {}
    modes = set(mode_schema.get("enum", [])) if isinstance(mode_schema, dict) else set()
    modes &= KNOWN_QUERY_MODES
    return {
        "query_supported": isinstance(query_schema, dict) and bool(properties),
        "query_modes": sorted(modes),
        "supports_mix": "mix" in modes,
        "supports_hybrid": "hybrid" in modes,
        "supports_include_references": "include_references" in properties,
        "supports_include_chunk_content": "include_chunk_content" in properties,
        "query_properties": sorted(properties) if isinstance(properties, dict) else [],
    }


def response_shape(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    references = payload.get("references")
    if not isinstance(references, list):
        references = payload.get("ref_results")
    references = references if isinstance(references, list) else []
    chunks = []
    files = []
    for item in references:
        if not isinstance(item, dict):
            continue
        file_path = item.get("file_path") or item.get("file_source")
        if isinstance(file_path, str) and file_path:
            files.append(file_path)
        content = item.get("content")
        if isinstance(content, str) and content:
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(part for part in content if isinstance(part, str) and part)
    response = payload.get("response")
    context = payload.get("context")
    return {
        "top_level_keys": sorted(payload),
        "response_chars": len(response) if isinstance(response, str) else None,
        "context_chars": len(context) if isinstance(context, str) else None,
        "reference_count": len(references),
        "non_empty_chunk_count": len(chunks),
        "reference_files": files,
        "has_entities": bool(payload.get("entities")),
        "has_relations": bool(payload.get("relations")),
        "has_chunks": bool(payload.get("chunks")),
        "contains_no_context": contains_no_context(payload),
    }


def contains_no_context(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in ("response", "context", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and any(marker in value.lower() for marker in NO_CONTEXT_MARKERS):
            return True
    return False


def known_sources() -> set[str]:
    sources: set[str] = set()
    if not INPUT_DOCUMENTS.exists():
        return sources
    for line in INPUT_DOCUMENTS.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        source = item.get("source_path")
        if isinstance(source, str):
            sources.add(source)
            sources.add(Path(source).name)
    return sources


def references(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    value = payload.get("references")
    if not isinstance(value, list):
        value = payload.get("ref_results")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def evaluate_context(result: HttpResult) -> tuple[str, str | None, dict[str, Any]]:
    shape = response_shape(result.payload)
    if result.error_code:
        return "failed", result.error_code, shape
    if result.status != 200:
        return "failed", f"HTTP_{result.status}", shape
    if not isinstance(result.payload, dict):
        return "failed", "RESPONSE_NOT_OBJECT", shape
    if shape.get("contains_no_context") or (
        shape.get("reference_count") == 0
        and not shape.get("context_chars")
        and not shape.get("has_entities")
        and not shape.get("has_relations")
        and not shape.get("has_chunks")
    ):
        return "warning", "EMPTY_CONTEXT", shape
    return "passed", None, shape


def evaluate_answer(result: HttpResult, sources: set[str]) -> tuple[str, str | None, dict[str, Any]]:
    shape = response_shape(result.payload)
    if result.error_code:
        return "failed", result.error_code, shape
    if result.status != 200:
        return "failed", f"HTTP_{result.status}", shape
    if not isinstance(result.payload, dict):
        return "failed", "RESPONSE_NOT_OBJECT", shape
    answer = result.payload.get("response")
    refs = references(result.payload)
    non_empty_content = shape.get("non_empty_chunk_count", 0) > 0
    invalid_sources = []
    for item in refs:
        source = item.get("file_path") or item.get("file_source")
        if isinstance(source, str) and sources and Path(source).name not in sources and source not in sources:
            invalid_sources.append(source)
    if not isinstance(answer, str) or not answer.strip():
        return "failed", "EMPTY_ANSWER", shape
    if shape.get("contains_no_context"):
        return "warning", "EMPTY_CONTEXT", shape
    if not refs:
        return "warning", "REFERENCES_EMPTY", shape
    if not non_empty_content:
        return "warning", "CHUNK_CONTENT_EMPTY", shape
    if invalid_sources:
        shape["unknown_reference_files"] = invalid_sources
        return "warning", "REFERENCE_SOURCE_UNKNOWN", shape
    return "passed", None, shape


def evaluate_negative(result: HttpResult) -> tuple[str, str | None, dict[str, Any]]:
    shape = response_shape(result.payload)
    if result.error_code:
        return "failed", result.error_code, shape
    if result.status != 200:
        return "failed", f"HTTP_{result.status}", shape
    if not isinstance(result.payload, dict):
        return "failed", "RESPONSE_NOT_OBJECT", shape
    answer = result.payload.get("response")
    refs = references(result.payload)
    if not isinstance(answer, str) or not answer.strip():
        return "warning", "EMPTY_ANSWER", shape
    has_refusal = any(marker in answer for marker in REFUSAL_MARKERS)
    if not refs and has_refusal:
        return "passed", None, shape
    if refs and has_refusal:
        return "warning", "REFUSAL_WITH_IRRELEVANT_REFERENCES", shape
    return "warning", "UNSUPPORTED_CLAIM", shape


def stage_record(name: str, status: str, result: HttpResult | None = None, *, error_code: str | None = None, shape: dict[str, Any] | None = None, detail: str | None = None, request_shape: dict[str, Any] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "status": status,
        "http_status": result.status if result else None,
        "duration_ms": result.duration_ms if result else None,
        "failure_code": error_code,
        "response_shape": shape or {},
    }
    if request_shape:
        record["request_shape"] = request_shape
    if detail:
        record["detail"] = safe_detail(detail)
    return record


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_raw(name: str, result: HttpResult) -> None:
    payload: object
    if result.error_code:
        payload = {"http_status": result.status, "error_code": result.error_code, "detail": safe_detail(result.detail)}
    else:
        payload = result.payload
    write_json(RAW_DIR / name, payload)


def render_report(results: dict[str, Any]) -> str:
    lines = [
        "# LightRAG 启动后查询流程实验报告",
        "",
        f"- 实验 ID：`{results['experiment_id']}`",
        f"- 服务：`{results['service']['base_url']}`",
        f"- 总体状态：**{results['overall_status']}**",
        "- 范围：只读 `/health`、`/openapi.json` 和 `/query`，未调用写端点。",
        "",
        "## 服务摘要",
        "",
        "```json",
        json.dumps(results["service"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 阶段结果",
        "",
        "| 阶段 | 状态 | HTTP | 耗时(ms) | 错误码 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for stage in results["stages"]:
        lines.append(
            f"| {stage['name']} | {stage['status']} | {stage.get('http_status') or '-'} | "
            f"{stage.get('duration_ms') or '-'} | {stage.get('failure_code') or '-'} |"
        )
    lines.extend(["", "## 断点判断", ""])
    failures = [stage for stage in results["stages"] if stage["status"] in {"failed", "warning"}]
    if not failures:
        lines.append("本次实验没有检测到传输、检索证据或回答生成断点。")
    else:
        for stage in failures:
            lines.append(
                f"- `{stage['name']}`：`{stage.get('failure_code') or 'UNSPECIFIED'}`；"
                f"建议结合该阶段的 `response_shape` 和 raw 响应检查。"
            )
    lines.extend([
        "",
        "## 修复建议",
        "",
        "- `EMPTY_CONTEXT`：查询前确认文档已 processed 且 chunks_count > 0；当前提交流程仍需后续接入 track polling。",
        "- `WORKSPACE_UNKNOWN`：服务虽然 healthy，但 workspace 为空；在配置 workspace/security-domain 映射前，不应宣称已完成细粒度 ACL 隔离。",
        "- `REFERENCES_EMPTY` 或 `CHUNK_CONTENT_EMPTY`：保持 evidence-required 请求，按 capability detection 拒绝不支持 chunk content 的服务。",
        "- `UNSUPPORTED_CLAIM`：增加 evidence verifier 和无证据拒答 gate；该码属于可信度问题，不等同于 HTTP 故障。",
        "- `REFUSAL_WITH_IRRELEVANT_REFERENCES`：拒答文本本身可能正确，但引用未证明该拒答；需要做引用相关性校验并在不相关时清空或标记 references。",
        "- HTTP/网络错误：先检查服务健康、query queue 和 provider，再决定是否采用有上限的重试。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    base_url = os.environ.get("LIGHTRAG_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    timeout = float(os.environ.get("LIGHTRAG_QUERY_PROBE_TIMEOUT", "60"))
    experiment_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stages: list[dict[str, Any]] = []

    health_before = request_json(base_url, "GET", "/health", timeout=timeout)
    health_before_shape = health_summary(health_before.payload)
    if health_before.error_code:
        stages.append(stage_record("preflight_health", "failed", health_before, error_code=health_before.error_code, detail=health_before.detail))
    elif health_before.status != 200 or not isinstance(health_before.payload, dict) or health_before.payload.get("status") != "healthy":
        stages.append(stage_record("preflight_health", "failed", health_before, error_code="SERVICE_NOT_HEALTHY", shape=health_before_shape))
    elif health_before_shape.get("workspace_empty"):
        stages.append(stage_record("preflight_health", "warning", health_before, error_code="WORKSPACE_UNKNOWN", shape=health_before_shape))
    else:
        stages.append(stage_record("preflight_health", "passed", health_before, shape=health_before_shape))

    openapi = request_json(base_url, "GET", "/openapi.json", timeout=timeout)
    if openapi.error_code:
        stages.append(stage_record("preflight_openapi", "failed", openapi, error_code=openapi.error_code, detail=openapi.detail))
    else:
        summary = openapi_summary(openapi.payload)
        status = "passed" if openapi.status == 200 and summary.get("query_supported") else "failed"
        error_code = None if status == "passed" else "OPENAPI_QUERY_UNAVAILABLE"
        stages.append(stage_record("preflight_openapi", status, openapi, error_code=error_code, shape=summary))

    context_payload = {
        "query": "韩永仁案中为什么认定自首？",
        "mode": "mix",
        "only_need_context": True,
        "include_references": True,
        "include_chunk_content": True,
        "top_k": 10,
        "chunk_top_k": 5,
    }
    context_result = request_json(base_url, "POST", "/query", context_payload, timeout=timeout)
    context_status, context_code, context_shape = evaluate_context(context_result)
    stages.append(stage_record("context_retrieval", context_status, context_result, error_code=context_code, shape=context_shape, detail=context_result.detail, request_shape={"method": "POST", "path": "/query", "mode": context_payload["mode"], "only_need_context": True, "include_references": True, "include_chunk_content": True, "query_chars": len(context_payload["query"])}))
    save_raw("context-response.json", context_result)

    answer_payload = {
        "query": "韩永仁案中为什么认定自首？",
        "mode": "hybrid",
        "include_references": True,
        "include_chunk_content": True,
        "response_type": "Multiple Paragraphs",
        "top_k": 10,
        "chunk_top_k": 5,
    }
    answer_result = request_json(base_url, "POST", "/query", answer_payload, timeout=timeout)
    answer_status, answer_code, answer_shape = evaluate_answer(answer_result, known_sources())
    stages.append(stage_record("answer_generation", answer_status, answer_result, error_code=answer_code, shape=answer_shape, detail=answer_result.detail, request_shape={"method": "POST", "path": "/query", "mode": answer_payload["mode"], "include_references": True, "include_chunk_content": True, "query_chars": len(answer_payload["query"])}))
    save_raw("hybrid-response.json", answer_result)

    negative_payload = {
        "query": "请给出当前语料没有涉及的虚构机构的成立年份，并说明依据。",
        "mode": "mix",
        "include_references": True,
        "include_chunk_content": True,
        "top_k": 10,
        "chunk_top_k": 5,
    }
    negative_result = request_json(base_url, "POST", "/query", negative_payload, timeout=timeout)
    negative_status, negative_code, negative_shape = evaluate_negative(negative_result)
    stages.append(stage_record("negative_control", negative_status, negative_result, error_code=negative_code, shape=negative_shape, detail=negative_result.detail, request_shape={"method": "POST", "path": "/query", "mode": negative_payload["mode"], "include_references": True, "include_chunk_content": True, "query_chars": len(negative_payload["query"])}))
    save_raw("negative-response.json", negative_result)

    health_after = request_json(base_url, "GET", "/health", timeout=timeout)
    health_after_shape = health_summary(health_after.payload)
    if health_after.error_code:
        stages.append(stage_record("postflight_health", "failed", health_after, error_code=health_after.error_code, detail=health_after.detail))
    elif health_after.status != 200 or not isinstance(health_after.payload, dict) or health_after.payload.get("status") != "healthy":
        stages.append(stage_record("postflight_health", "failed", health_after, error_code="SERVICE_NOT_HEALTHY", shape=health_after_shape))
    elif health_after_shape.get("workspace_empty"):
        stages.append(stage_record("postflight_health", "warning", health_after, error_code="WORKSPACE_UNKNOWN", shape=health_after_shape))
    else:
        stages.append(stage_record("postflight_health", "passed", health_after, shape=health_after_shape))

    statuses = {stage["status"] for stage in stages}
    overall = "failed" if "failed" in statuses else "warning" if "warning" in statuses else "passed"
    results = {
        "experiment_id": experiment_id,
        "service": {
            "base_url": base_url,
            "health_before": health_summary(health_before.payload),
            "health_after": health_summary(health_after.payload),
        },
        "stages": stages,
        "overall_status": overall,
        "generated_at": now_iso(),
    }
    write_json(RESULTS_PATH, results)
    REPORT_PATH.write_text(render_report(results), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if overall == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())

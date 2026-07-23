from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .corpus import CorpusFile, TEXT_SUFFIXES
from .paths import ProjectPaths
from .utils import read_json, relpath, utc_now, write_json


DEFAULT_LIGHTRAG_SERVICE_URL = "http://127.0.0.1:9621"


class LightRAGBuildError(RuntimeError):
    pass


def prepare_lightrag_input(paths: ProjectPaths, files: list[CorpusFile]) -> dict:
    paths.lightrag_input.mkdir(parents=True, exist_ok=True)
    files_dir = paths.lightrag_input / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    docs = []
    for item in files:
        source = paths.root / item.path
        if not source.exists():
            continue
        # L2：item.path 通常形如 "corpus/raw/a.md"；若文件不在 corpus/ 下（异常布局），
        # 退化为按原相对路径放置，避免 relative_to 抛 ValueError。
        item_path = Path(item.path)
        try:
            rel = item_path.relative_to("corpus")
        except ValueError:
            rel = item_path
        copied = files_dir / rel
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copied)
        text = read_text_for_lightrag(source) if item.suffix in TEXT_SUFFIXES else ""
        docs.append(
            {
                "id": stable_doc_id(item.path),
                "source_path": item.path,
                "input_path": relpath(copied, paths.root),
                "sha256": item.sha256,
                "size": item.size,
                "text_like": item.text_like,
                "text": text,
            }
        )
    documents_path = paths.lightrag_input / "documents.jsonl"
    documents_path.write_text("".join(json.dumps(doc, ensure_ascii=False) + "\n" for doc in docs), encoding="utf-8")
    manifest = {
        "status": "prepared",
        "generated_at": utc_now(),
        "document_count": len(docs),
        "documents_jsonl": relpath(documents_path, paths.root),
    }
    write_json(paths.lightrag_input / "manifest.json", manifest)
    return manifest


def build_lightrag(
    paths: ProjectPaths,
    *,
    smoke_query: str | None = None,
    dry_run: bool = False,
    config: dict[str, Any] | None = None,
) -> dict:
    input_manifest = read_json(paths.lightrag_input / "manifest.json", {})
    documents_path = paths.lightrag_input / "documents.jsonl"
    if not documents_path.exists():
        raise LightRAGBuildError("LightRAG input is missing. Run prepare-lightrag first.")

    docs = [json.loads(line) for line in documents_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    text_docs = [doc for doc in docs if doc.get("text")]
    ledger = read_json(paths.lightrag_state / "lightrag-import-ledger.json", {"documents": {}})
    previous = ledger.get("documents", {})
    imported = []
    skipped = []
    track_ids = []
    # H1：检测「曾经导入过、但当前 corpus 已不再包含」的文档。LightRAG 无法保证从
    # 已有图谱/向量中彻底删除旧知识，因此一旦发现删除，就必须诚实标记 requires_rebuild。
    current_ids = {doc["id"] for doc in docs}
    deleted = sorted(
        meta.get("source_path", doc_id)
        for doc_id, meta in previous.items()
        if doc_id not in current_ids
    )

    if dry_run:
        for doc in text_docs:
            if previous.get(doc["id"], {}).get("sha256") == doc["sha256"]:
                skipped.append(doc["source_path"])
            else:
                imported.append(doc["source_path"])
        report = base_report("dry_run", input_manifest, imported, skipped, None, deleted=deleted)
        write_json(paths.lightrag_reports / "lightrag-report.json", report)
        return report

    service = resolve_lightrag_service_config(config)
    client = LightRAGServiceClient(service["base_url"], headers=service["headers"], timeout=service["timeout_seconds"])

    try:
        for doc in text_docs:
            if previous.get(doc["id"], {}).get("sha256") == doc["sha256"]:
                skipped.append(doc["source_path"])
                continue
            response = client.post_json(
                "/documents/text",
                {
                    "text": doc["text"],
                    "file_source": doc["source_path"],
                },
            )
            imported.append(doc["source_path"])
            track_ids.append(
                {
                    "source_path": doc["source_path"],
                    "status": response.get("status"),
                    "track_id": response.get("track_id"),
                }
            )
            previous[doc["id"]] = {
                "source_path": doc["source_path"],
                "sha256": doc["sha256"],
                "submitted_at": utc_now(),
                "service_track_id": response.get("track_id"),
            }
        # 把已从 corpus 删除的条目在 ledger 中标注出来（保留记录、但不再视为"已同步"）。
        for doc_id, meta in previous.items():
            if doc_id not in current_ids:
                meta["removed_from_corpus"] = True
        smoke = None
        if smoke_query:
            smoke = client.post_json(
                "/query",
                {
                    "query": smoke_query,
                    "mode": "hybrid",
                    "include_references": True,
                },
            )
            write_json(
                paths.lightrag_queries / "smoke-test.json",
                {"query": smoke_query, "answer": smoke.get("response"), "raw_response": smoke},
            )
    except Exception as exc:
        report = base_report("failed", input_manifest, imported, skipped, str(exc), deleted=deleted, service=service["public"])
        report["service_track_ids"] = track_ids
        write_json(paths.lightrag_reports / "lightrag-report.json", report)
        raise LightRAGBuildError(report["error"]) from exc

    ledger["documents"] = previous
    ledger["updated_at"] = utc_now()
    ledger["service"] = service["public"]
    write_json(paths.lightrag_state / "lightrag-import-ledger.json", ledger)
    report = base_report("success", input_manifest, imported, skipped, None, deleted=deleted, service=service["public"])
    report["service_track_ids"] = track_ids
    report["smoke_test"] = {"query": smoke_query, "ran": bool(smoke_query)}
    write_json(paths.lightrag_reports / "lightrag-report.json", report)
    write_json(
        paths.lightrag / "manifest.json",
        {
            "status": "success",
            "generated_at": report["generated_at"],
            "service": service["public"],
            "document_count": len(text_docs),
        },
    )
    return report


class LightRAGServiceClient:
    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request_json("POST", path, payload)

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", **self.headers}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - user-configured service URL
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LightRAGBuildError(f"LightRAG service {method} {path} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise LightRAGBuildError(f"Cannot reach LightRAG service at {self.base_url}: {exc.reason}") from exc
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LightRAGBuildError(f"LightRAG service {method} {path} returned non-JSON response: {body[:200]}") from exc


def resolve_lightrag_service_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    base_url = os.environ.get("LIGHTRAG_BASE_URL") or cfg.get("base_url")
    if not base_url or "YOUR_LIGHTRAG_SERVER" in str(base_url):
        raise LightRAGBuildError(
            "LightRAG base_url is required. Create `lightrag-config.json` from "
            "`lightrag-config.example.json` and set `base_url`, or set LIGHTRAG_BASE_URL."
        )
    api_key_env = cfg.get("api_key_env", "LIGHTRAG_API_KEY")
    bearer_token_env = cfg.get("bearer_token_env", "LIGHTRAG_BEARER_TOKEN")
    api_key = os.environ.get(api_key_env) or cfg.get("api_key")
    bearer_token = os.environ.get(bearer_token_env) or cfg.get("bearer_token")
    timeout_seconds = float(cfg.get("timeout_seconds", 30))

    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    public = {
        "mode": "service",
        "base_url": base_url.rstrip("/"),
        "api_key_env": api_key_env,
        "bearer_token_env": bearer_token_env,
        "auth": {
            "api_key_configured": bool(api_key),
            "bearer_token_configured": bool(bearer_token),
        },
    }
    return {
        "base_url": public["base_url"],
        "headers": headers,
        "timeout_seconds": timeout_seconds,
        "public": public,
    }


def base_report(
    status: str,
    input_manifest: dict,
    imported: list[str],
    skipped: list[str],
    error: str | None,
    *,
    deleted: list[str] | None = None,
    service: dict[str, Any] | None = None,
) -> dict:
    deleted = deleted or []
    report = {
        "status": status,
        "generated_at": utc_now(),
        "input": input_manifest,
        "imported": imported,
        "skipped_unchanged": skipped,
        # H1：删除无法被 LightRAG 增量安全清除，发现删除即要求全量重建。
        "requires_rebuild": bool(deleted),
        "deleted_pending_rebuild": deleted,
        "error": error,
    }
    if service is not None:
        report["service"] = service
    return report


def read_text_for_lightrag(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def stable_doc_id(path: str) -> str:
    return path.replace("/", "__").replace(" ", "_")

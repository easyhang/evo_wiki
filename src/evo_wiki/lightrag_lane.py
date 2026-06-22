from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .corpus import CorpusFile, TEXT_SUFFIXES
from .paths import ProjectPaths
from .utils import read_json, relpath, utc_now, write_json


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


def build_lightrag(paths: ProjectPaths, *, smoke_query: str | None = None, dry_run: bool = False) -> dict:
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

    try:
        from lightrag import LightRAG, QueryParam  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        report = base_report("failed", input_manifest, imported, skipped, f"Cannot import LightRAG: {exc}", deleted=deleted)
        write_json(paths.lightrag_reports / "lightrag-report.json", report)
        raise LightRAGBuildError(report["error"]) from exc

    try:
        rag = LightRAG(working_dir=str(paths.lightrag_workspace))
        for doc in text_docs:
            if previous.get(doc["id"], {}).get("sha256") == doc["sha256"]:
                skipped.append(doc["source_path"])
                continue
            rag.insert(doc["text"])
            imported.append(doc["source_path"])
            previous[doc["id"]] = {
                "source_path": doc["source_path"],
                "sha256": doc["sha256"],
                "imported_at": utc_now(),
            }
        # 把已从 corpus 删除的条目在 ledger 中标注出来（保留记录、但不再视为"已同步"）。
        for doc_id, meta in previous.items():
            if doc_id not in current_ids:
                meta["removed_from_corpus"] = True
        smoke = None
        if smoke_query:
            smoke = rag.query(smoke_query, param=QueryParam(mode="hybrid"))
            write_json(paths.lightrag_queries / "smoke-test.json", {"query": smoke_query, "answer": smoke})
    except Exception as exc:  # pragma: no cover - depends on LLM/env config
        report = base_report("failed", input_manifest, imported, skipped, str(exc), deleted=deleted)
        write_json(paths.lightrag_reports / "lightrag-report.json", report)
        raise LightRAGBuildError(report["error"]) from exc

    ledger["documents"] = previous
    ledger["updated_at"] = utc_now()
    write_json(paths.lightrag_state / "lightrag-import-ledger.json", ledger)
    report = base_report("success", input_manifest, imported, skipped, None, deleted=deleted)
    report["smoke_test"] = {"query": smoke_query, "ran": bool(smoke_query)}
    write_json(paths.lightrag_reports / "lightrag-report.json", report)
    write_json(
        paths.lightrag / "manifest.json",
        {
            "status": "success",
            "generated_at": report["generated_at"],
            "workspace": relpath(paths.lightrag_workspace, paths.root),
            "document_count": len(text_docs),
        },
    )
    return report


def base_report(status: str, input_manifest: dict, imported: list[str], skipped: list[str], error: str | None, *, deleted: list[str] | None = None) -> dict:
    deleted = deleted or []
    return {
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


def read_text_for_lightrag(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def stable_doc_id(path: str) -> str:
    return path.replace("/", "__").replace(" ", "_")

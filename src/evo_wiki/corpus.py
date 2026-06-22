from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .utils import read_json, relpath, write_json

TEXT_SUFFIXES = {".md", ".txt", ".html", ".htm", ".csv", ".json", ".yaml", ".yml"}


@dataclass(frozen=True)
class CorpusFile:
    path: str
    sha256: str
    size: int
    suffix: str
    text_like: bool


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def scan_corpus(root: Path, corpus: Path) -> list[CorpusFile]:
    if not corpus.exists():
        return []
    files: list[CorpusFile] = []
    for path in sorted(corpus.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(corpus).parts):
            continue
        suffix = path.suffix.lower()
        files.append(
            CorpusFile(
                path=relpath(path, root),
                sha256=sha256_file(path),
                size=path.stat().st_size,
                suffix=suffix,
                text_like=suffix in TEXT_SUFFIXES,
            )
        )
    return files


def corpus_hash(files: list[CorpusFile]) -> str:
    h = hashlib.sha256()
    for item in files:
        h.update(item.path.encode())
        h.update(item.sha256.encode())
        h.update(str(item.size).encode())
    return "sha256:" + h.hexdigest()


def diff_against_previous(current: list[CorpusFile], state_path: Path) -> dict:
    previous = read_json(state_path, {"files": []})
    prev_by_path = {item["path"]: item for item in previous.get("files", [])}
    curr_by_path = {item.path: item for item in current}

    added = sorted(set(curr_by_path) - set(prev_by_path))
    deleted = sorted(set(prev_by_path) - set(curr_by_path))
    modified = sorted(
        path
        for path in set(curr_by_path) & set(prev_by_path)
        if curr_by_path[path].sha256 != prev_by_path[path].get("sha256")
    )
    return {"added": added, "modified": modified, "deleted": deleted}


def persist_corpus_state(files: list[CorpusFile], state_path: Path) -> None:
    write_json(
        state_path,
        {
            "files": [file.__dict__ for file in files],
            "corpus_hash": corpus_hash(files),
        },
    )

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .utils import relpath, slugify

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
YAML_FENCE_RE = re.compile(r"^---\s*$")
REQUIRED_AUDIT_FIELDS = {"id", "target", "quote", "severity", "author", "source", "created", "status"}
VALID_SEVERITIES = {"info", "suggest", "warn", "error"}
VALID_STATUSES = {"open", "resolved"}
VALID_SOURCES = {"agent", "manual"}
UNLINKED_CONCEPT_THRESHOLD = 3
# 结构性标题词不应被当作"潜在概念"：它们是约定俗成的章节标题，而非领域概念。
STOPWORDS = {
    "sources",
    "references",
    "background",
    "summary",
    "connections",
    "contributions",
    "key contributions",
    "key points",
    "key properties",
    "main argument",
    "open questions",
    "see also",
    "how it works",
}


@dataclass(frozen=True)
class HealthIssue:
    severity: str
    code: str
    message: str
    path: str | None = None


def lint_wiki_artifacts(
    root: Path,
    wiki_src: Path,
    audit_dir: Path,
    log_dir: Path,
    *,
    content_contract_version: int = 1,
    corpus_paths: list[str] | None = None,
) -> dict:
    """Run llm-wiki-demo-style checks, plus HTML-required source page structure."""
    stem_map = stem_to_path_map(wiki_src)
    issues: list[HealthIssue] = []
    # llm-wiki-demo style passes: link graph, index coverage, concept hints,
    # log shape, audit shape, and audit target validity.
    issues.extend(pass_dead_wikilinks(root, wiki_src, stem_map))
    issues.extend(pass_orphan_pages(root, wiki_src, stem_map))
    index_issues = pass_missing_index_entries(root, wiki_src, stem_map)
    if content_contract_version >= 2:
        index_issues = [
            HealthIssue(
                "error" if issue.code == "not_in_index" else issue.severity,
                issue.code,
                issue.message,
                issue.path,
            )
            for issue in index_issues
        ]
    issues.extend(index_issues)
    issues.extend(pass_unlinked_concepts(root, wiki_src, stem_map))
    issues.extend(pass_log_shape(root, log_dir))
    issues.extend(pass_audit_shape(root, audit_dir))
    issues.extend(pass_audit_targets(root, audit_dir))
    # Evo Wiki HTML source pages need these headings for the summary/original UX.
    issues.extend(pass_source_page_structure(root, wiki_src))
    contract_metrics, contract_issues = pass_content_contract(
        root,
        wiki_src,
        content_contract_version=content_contract_version,
        corpus_paths=corpus_paths or [],
    )
    issues.extend(contract_issues)
    by_severity = Counter(issue.severity for issue in issues)
    return {
        "content_contract_version": content_contract_version,
        "status": "clean" if not issues else "issues_found",
        "issue_count": len(issues),
        "by_severity": dict(sorted(by_severity.items())),
        "issues": [issue.__dict__ for issue in issues],
        "contract": contract_metrics,
    }


def _normalized_term(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _source_basename(value: object) -> str:
    normalized = unicodedata.normalize(
        "NFKC",
        str(value or "").replace("\\", "/"),
    )
    return normalized.rsplit("/", 1)[-1].casefold().strip()


def _frontmatter_list(fields: dict[str, object], key: str) -> list[str]:
    raw = fields.get(key)
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw and raw != "[]":
        return [raw.strip()]
    return []


def pass_content_contract(
    root: Path,
    wiki_src: Path,
    *,
    content_contract_version: int,
    corpus_paths: list[str],
) -> tuple[dict[str, object], list[HealthIssue]]:
    """Measure both contracts and enforce the opt-in v2 content contract."""
    issues: list[HealthIssue] = []
    source_owners: dict[str, list[Path]] = {}
    entity_owners: dict[str, set[str]] = {}
    graph_label_owners: dict[str, set[str]] = {}
    entity_count = 0
    source_pages = 0
    for md in collect_md_files(wiki_src):
        fields = parse_yaml_frontmatter(read_text_safe(md)) or {}
        page_type = str(fields.get("type") or expected_page_type(md, wiki_src))
        if expected_page_type(md, wiki_src) == "source":
            source_pages += 1
            sources = _frontmatter_list(fields, "sources")
            if content_contract_version >= 2:
                if fields.get("type") != "source":
                    issues.append(HealthIssue(
                        "error",
                        "source_frontmatter_type",
                        "Source page frontmatter must declare type: source",
                        relpath(md, root),
                    ))
                if not sources:
                    issues.append(HealthIssue(
                        "error",
                        "source_frontmatter_sources",
                        "Source page frontmatter must declare at least one corpus source",
                        relpath(md, root),
                    ))
            for source in sources:
                normalized_source = source.replace("\\", "/")
                source_path = PurePosixPath(normalized_source)
                if content_contract_version >= 2 and (
                    source_path.is_absolute()
                    or ".." in source_path.parts
                    or not source_path.parts
                    or source_path.parts[0] != "corpus"
                    or normalized_source != source_path.as_posix()
                ):
                    issues.append(HealthIssue(
                        "error",
                        "source_frontmatter_path",
                        "Source frontmatter paths must be canonical workspace-relative corpus paths",
                        relpath(md, root),
                    ))
                basename = _source_basename(source)
                if basename:
                    source_owners.setdefault(basename, []).append(md)
        if page_type == "entity":
            entity_count += 1
            title = str(fields.get("title") or md.stem).strip()
            graph_label = str(fields.get("graph_label") or title).strip()
            entity_key = md.relative_to(wiki_src).as_posix()
            normalized_graph_label = _normalized_term(graph_label)
            if normalized_graph_label:
                graph_label_owners.setdefault(
                    normalized_graph_label,
                    set(),
                ).add(entity_key)
            for term in [
                title,
                graph_label,
                *_frontmatter_list(fields, "aliases"),
            ]:
                normalized = _normalized_term(term)
                if normalized:
                    entity_owners.setdefault(normalized, set()).add(entity_key)

    duplicate_sources = {
        basename: owners
        for basename, owners in source_owners.items()
        if len(set(owners)) > 1
    }
    ambiguous_terms = {
        term: owners
        for term, owners in entity_owners.items()
        if len(owners) > 1
    }
    duplicate_graph_labels = {
        label: owners
        for label, owners in graph_label_owners.items()
        if len(owners) > 1
    }
    corpus_owners: dict[str, list[str]] = {}
    for path in corpus_paths:
        basename = _source_basename(path)
        if basename:
            corpus_owners.setdefault(basename, []).append(path)
    corpus_collisions = {
        basename: paths
        for basename, paths in corpus_owners.items()
        if len(paths) > 1
    }
    covered_paths = [
        path
        for paths in corpus_owners.values()
        for path in paths
        if _source_basename(path) in source_owners
        and _source_basename(path) not in corpus_collisions
        and _source_basename(path) not in duplicate_sources
    ]
    missing_paths = sorted(set(corpus_paths) - set(covered_paths))

    if content_contract_version >= 2:
        for basename, paths in sorted(corpus_collisions.items()):
            issues.append(HealthIssue(
                "error",
                "corpus_source_basename_collision",
                f'Corpus basename "{basename}" is not unique across {len(paths)} files',
            ))
        for basename, owners in sorted(duplicate_sources.items()):
            issues.append(HealthIssue(
                "error",
                "source_basename_mapping_duplicate",
                f'Source basename "{basename}" maps to {len(set(owners))} Wiki pages',
            ))
        for label, owners in sorted(duplicate_graph_labels.items()):
            issues.append(HealthIssue(
                "error",
                "duplicate_entity_graph_label",
                f'Entity graph label "{label}" belongs to {len(owners)} pages',
            ))
        for path in missing_paths:
            issues.append(HealthIssue(
                "error",
                "corpus_source_unmapped",
                "Corpus file has no unique source Wiki page",
                path,
            ))
        if corpus_owners:
            for basename, owners in sorted(source_owners.items()):
                if basename not in corpus_owners:
                    issues.append(HealthIssue(
                        "error",
                        "source_mapping_without_corpus",
                        f'Source mapping "{basename}" does not match a corpus file',
                        relpath(owners[0], root),
                    ))
        for term, owners in sorted(ambiguous_terms.items()):
            issues.append(HealthIssue(
                "warn",
                "ambiguous_entity_term",
                f'Entity term "{term}" belongs to {len(owners)} pages and will not be auto-linked',
            ))

    total = len(corpus_paths)
    coverage_ratio = 1.0 if total == 0 else len(covered_paths) / total
    return {
        "corpus_file_count": total,
        "mapped_corpus_file_count": len(covered_paths),
        "source_coverage_ratio": round(coverage_ratio, 6),
        "source_page_count": source_pages,
        "source_mapping_count": len(source_owners),
        "entity_mapping_count": entity_count,
        "ambiguous_entity_term_count": len(ambiguous_terms),
        "duplicate_graph_label_count": len(duplicate_graph_labels),
        "duplicate_source_mapping_count": len(duplicate_sources),
        "corpus_basename_collision_count": len(corpus_collisions),
    }, issues


def collect_md_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.md")) if directory.is_dir() else []


def read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def strip_html_comments(text: str) -> str:
    return HTML_COMMENT_RE.sub("", text)


def extract_wikilinks(text: str) -> list[str]:
    return WIKILINK_RE.findall(strip_html_comments(text))


def stem_to_path_map(wiki_src: Path) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for md in collect_md_files(wiki_src):
        mapping[md.stem.lower()] = md
        mapping[slugify(md.stem)] = md
        # 标题别名：与渲染器 build_link_map 对齐，让基于 frontmatter title 的中文
        # wikilink（如 [[护城河]]）能被正确识别，避免误报 dead_wikilink / orphan。
        fields = parse_yaml_frontmatter(read_text_safe(md))
        title = (fields or {}).get("title")
        if isinstance(title, str) and title:
            mapping[title.lower()] = md
            mapping[slugify(title)] = md
        if md.name == "index.md" and md.parent != wiki_src:
            mapping[md.parent.name.lower()] = md
            mapping[slugify(md.parent.name)] = md
    return mapping


def parse_yaml_frontmatter(text: str) -> dict[str, object] | None:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, object] = {}
    last_key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        stripped = line.strip()
        # 块状列表项（"  - item"）归属到最近一个值为空的标量键，聚合成 Python 列表。
        if stripped.startswith("- ") and last_key is not None:
            item = stripped[2:].strip().strip('"').strip("'")
            existing = fields.get(last_key)
            if isinstance(existing, list):
                existing.append(item)
            else:
                fields[last_key] = [item]
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            fields[key] = value
            last_key = key
    return None


def markdown_body(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[idx + 1 :]).lstrip("\n")
    return text


def expected_page_type(path: Path, wiki_src: Path) -> str:
    try:
        rel = path.relative_to(wiki_src)
    except ValueError:
        return "page"
    if rel.name == "index.md" and rel.parent == Path("."):
        return "index"
    if rel.parts and rel.parts[0] in {"concepts", "entities", "sources"}:
        return {"concepts": "concept", "entities": "entity", "sources": "source"}[rel.parts[0]]
    return "page"


def pass_dead_wikilinks(root: Path, wiki_src: Path, stem_map: dict[str, Path]) -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    for md in collect_md_files(wiki_src):
        for target in extract_wikilinks(read_text_safe(md)):
            if target.lower() not in stem_map and slugify(target) not in stem_map:
                issues.append(HealthIssue("error", "dead_wikilink", f"Dead wikilink [[{target}]]", relpath(md, root)))
    return issues


def pass_orphan_pages(root: Path, wiki_src: Path, stem_map: dict[str, Path]) -> list[HealthIssue]:
    linked_stems: set[str] = set()
    for md in collect_md_files(wiki_src):
        for target in extract_wikilinks(read_text_safe(md)):
            linked_stems.add(target.lower())
            linked_stems.add(slugify(target))
    aliases_by_path: dict[Path, set[str]] = {}
    for alias, path in stem_map.items():
        aliases_by_path.setdefault(path, set()).add(alias)
    issues: list[HealthIssue] = []
    for path, aliases in sorted(aliases_by_path.items()):
        if path.name == "index.md":
            continue
        # 只要任一别名（stem 或 title）被引用，就不算孤儿页。
        if not (aliases & linked_stems):
            issues.append(HealthIssue("warn", "orphan_page", "Page has no inbound wikilinks", relpath(path, root)))
    return issues


def pass_missing_index_entries(root: Path, wiki_src: Path, stem_map: dict[str, Path]) -> list[HealthIssue]:
    index_path = wiki_src / "index.md"
    if not index_path.exists():
        return [HealthIssue("error", "missing_index", "wiki-src/index.md does not exist", relpath(wiki_src, root))]
    index_text = read_text_safe(index_path).lower()
    aliases_by_path: dict[Path, set[str]] = {}
    for alias, path in stem_map.items():
        aliases_by_path.setdefault(path, set()).add(alias)
    issues: list[HealthIssue] = []
    for path, aliases in sorted(aliases_by_path.items()):
        if path == index_path or path.name == "index.md":
            continue
        # 任一别名（含 dash->空格 变体）出现在 index.md 即视为已收录。
        if not any(alias in index_text or alias.replace("-", " ") in index_text for alias in aliases):
            issues.append(HealthIssue("warn", "not_in_index", "Page is not mentioned in index.md", relpath(path, root)))
    return issues


def pass_unlinked_concepts(root: Path, wiki_src: Path, stem_map: dict[str, Path]) -> list[HealthIssue]:
    all_text = " ".join(read_text_safe(md) for md in collect_md_files(wiki_src))
    word_re = re.compile(r"\b([A-Z][a-z]{3,}(?:\s+[A-Z][a-z]{3,})*)\b")
    counts: Counter[str] = Counter(match.group(1) for match in word_re.finditer(all_text))
    linked = {target.lower() for md in collect_md_files(wiki_src) for target in extract_wikilinks(read_text_safe(md))}
    issues: list[HealthIssue] = []
    for term, count in counts.most_common(20):
        if count < UNLINKED_CONCEPT_THRESHOLD:
            break
        if term.lower() in STOPWORDS:
            continue
        if slugify(term) not in stem_map and term.lower() not in linked and slugify(term) not in linked:
            issues.append(HealthIssue("info", "unlinked_concept", f'Potential concept "{term}" appears {count} times without a page'))
    return issues


def pass_source_page_structure(root: Path, wiki_src: Path) -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    for md in collect_md_files(wiki_src):
        if expected_page_type(md, wiki_src) != "source":
            continue
        body = markdown_body(read_text_safe(md))
        if "## 摘要" not in body and "## Summary" not in body:
            issues.append(HealthIssue("error", "source_missing_summary", "Source page must include a summary section", relpath(md, root)))
        if "## 原文内容" not in body and "## Original" not in body:
            issues.append(HealthIssue("error", "source_missing_original", "Source page must include original content", relpath(md, root)))
    return issues


def pass_audit_shape(root: Path, audit_dir: Path) -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    if not audit_dir.is_dir():
        return [HealthIssue("info", "missing_audit_dir", "audit directory does not exist", relpath(audit_dir, root))]
    audit_files = list(audit_dir.glob("*.md"))
    resolved = audit_dir / "resolved"
    if resolved.is_dir():
        audit_files.extend(resolved.glob("*.md"))
    for md in sorted(audit_files):
        fields = parse_yaml_frontmatter(read_text_safe(md))
        if fields is None:
            issues.append(HealthIssue("error", "audit_frontmatter", "Audit file has no valid YAML frontmatter", relpath(md, root)))
            continue
        missing = REQUIRED_AUDIT_FIELDS - set(fields)
        if missing:
            issues.append(HealthIssue("error", "audit_missing_fields", f"Audit file missing fields: {', '.join(sorted(missing))}", relpath(md, root)))
        if fields.get("severity") and fields["severity"] not in VALID_SEVERITIES:
            issues.append(HealthIssue("error", "audit_bad_severity", f"Invalid severity: {fields['severity']}", relpath(md, root)))
        if fields.get("status") and fields["status"] not in VALID_STATUSES:
            issues.append(HealthIssue("error", "audit_bad_status", f"Invalid status: {fields['status']}", relpath(md, root)))
        if fields.get("source") and fields["source"] not in VALID_SOURCES:
            issues.append(HealthIssue("error", "audit_bad_source", f"Invalid source: {fields['source']}", relpath(md, root)))
    return issues


def pass_audit_targets(root: Path, audit_dir: Path) -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    if not audit_dir.is_dir():
        return issues
    for md in sorted(audit_dir.glob("*.md")):
        fields = parse_yaml_frontmatter(read_text_safe(md))
        if not fields or fields.get("status", "open") != "open":
            continue
        target = fields.get("target", "")
        if target and not (root / target).exists():
            issues.append(HealthIssue("error", "audit_missing_target", f"Audit targets missing file: {target}", relpath(md, root)))
    return issues


def pass_log_shape(root: Path, log_dir: Path) -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    if not log_dir.is_dir():
        return [HealthIssue("info", "missing_log_dir", "log directory does not exist", relpath(log_dir, root))]
    for path in sorted(log_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if not re.match(r"^\d{8}\.md$", path.name):
            issues.append(HealthIssue("warn", "bad_log_filename", "Log filename should be YYYYMMDD.md", relpath(path, root)))
        first = read_text_safe(path).split("\n", 1)[0]
        if first and not re.match(r"^# \d{4}-\d{2}-\d{2}", first):
            issues.append(HealthIssue("warn", "bad_log_h1", "Log H1 should start with # YYYY-MM-DD", relpath(path, root)))
    return issues

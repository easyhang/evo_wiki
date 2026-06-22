from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .utils import relpath, slugify

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
YAML_FENCE_RE = re.compile(r"^---\s*$")
REQUIRED_AUDIT_FIELDS = {"id", "target", "quote", "severity", "author", "source", "created", "status"}
VALID_SEVERITIES = {"info", "suggest", "warn", "error"}
VALID_STATUSES = {"open", "resolved"}
VALID_SOURCES = {"agent", "manual"}
UNLINKED_CONCEPT_THRESHOLD = 3


@dataclass(frozen=True)
class HealthIssue:
    severity: str
    code: str
    message: str
    path: str | None = None


def lint_wiki_artifacts(root: Path, wiki_src: Path, audit_dir: Path, log_dir: Path) -> dict:
    """Run llm-wiki-demo-inspired health checks against Evo wiki's HTML-bound wiki-src."""
    stem_map = stem_to_path_map(wiki_src)
    issues: list[HealthIssue] = []
    issues.extend(pass_dead_wikilinks(root, wiki_src, stem_map))
    issues.extend(pass_orphan_pages(root, wiki_src, stem_map))
    issues.extend(pass_missing_index_entries(root, wiki_src, stem_map))
    issues.extend(pass_unlinked_concepts(root, wiki_src, stem_map))
    issues.extend(pass_audit_shape(root, audit_dir))
    issues.extend(pass_audit_targets(root, audit_dir))
    issues.extend(pass_log_shape(root, log_dir))
    by_severity = Counter(issue.severity for issue in issues)
    return {
        "status": "clean" if not issues else "issues_found",
        "issue_count": len(issues),
        "by_severity": dict(sorted(by_severity.items())),
        "issues": [issue.__dict__ for issue in issues],
    }


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
        if md.name == "index.md" and md.parent != wiki_src:
            mapping[md.parent.name.lower()] = md
            mapping[slugify(md.parent.name)] = md
    return mapping


def parse_yaml_frontmatter(text: str) -> dict[str, str] | None:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return None


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
    issues: list[HealthIssue] = []
    for stem, path in sorted(stem_map.items()):
        if stem == "index" or path.name == "index.md":
            continue
        if stem not in linked_stems:
            issues.append(HealthIssue("warn", "orphan_page", "Page has no inbound wikilinks", relpath(path, root)))
    return issues


def pass_missing_index_entries(root: Path, wiki_src: Path, stem_map: dict[str, Path]) -> list[HealthIssue]:
    index_path = wiki_src / "index.md"
    if not index_path.exists():
        return [HealthIssue("error", "missing_index", "wiki-src/index.md does not exist", relpath(wiki_src, root))]
    index_text = read_text_safe(index_path).lower()
    issues: list[HealthIssue] = []
    seen: set[Path] = set()
    for stem, path in sorted(stem_map.items()):
        if path in seen or stem == "index" or path == index_path:
            continue
        seen.add(path)
        index_alias = stem.replace("-", " ")
        if stem not in index_text and index_alias not in index_text:
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
        if slugify(term) not in stem_map and term.lower() not in linked and slugify(term) not in linked:
            issues.append(HealthIssue("info", "unlinked_concept", f'Potential concept "{term}" appears {count} times without a page'))
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

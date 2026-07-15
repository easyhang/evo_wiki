from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    """Resolved project paths used by every lane."""

    root: Path
    corpus: Path
    artifacts: Path
    agent: Path
    wiki: Path
    wiki_src: Path
    wiki_dist: Path
    wiki_reports: Path
    wiki_state: Path
    wiki_audit: Path
    wiki_log: Path
    wiki_outputs: Path
    lightrag: Path
    lightrag_input: Path
    lightrag_workspace: Path
    lightrag_reports: Path
    lightrag_state: Path
    lightrag_queries: Path
    platform: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "ProjectPaths":
        root_path = Path(root).expanduser().resolve()
        artifacts = root_path / "artifacts"
        wiki = artifacts / "wiki"
        lightrag = artifacts / "lightrag"
        return cls(
            root=root_path,
            corpus=root_path / "corpus",
            artifacts=artifacts,
            agent=artifacts / "agent",
            wiki=wiki,
            wiki_src=wiki / "wiki-src",
            wiki_dist=wiki / "dist",
            wiki_reports=wiki / "reports",
            wiki_state=wiki / "state",
            wiki_audit=wiki / "audit",
            wiki_log=wiki / "log",
            wiki_outputs=wiki / "outputs",
            lightrag=lightrag,
            lightrag_input=lightrag / "input",
            lightrag_workspace=lightrag / "workspace",
            lightrag_reports=lightrag / "reports",
            lightrag_state=lightrag / "state",
            lightrag_queries=lightrag / "queries",
            platform=artifacts / "platform",
        )

    def ensure_base_dirs(self) -> None:
        for path in [
            self.corpus / "raw",
            self.corpus / "assets",
            self.agent,
            self.wiki_src,
            self.wiki_src / "concepts",
            self.wiki_src / "entities",
            self.wiki_src / "sources",
            self.wiki_dist,
            self.wiki_reports,
            self.wiki_state,
            self.wiki_audit / "resolved",
            self.wiki_log,
            self.wiki_outputs / "queries",
            self.lightrag_input / "files",
            self.lightrag_reports,
            self.lightrag_state,
            self.lightrag_queries,
            self.platform,
        ]:
            path.mkdir(parents=True, exist_ok=True)

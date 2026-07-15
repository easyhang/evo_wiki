# Evo wiki Architecture Notes

This MVP intentionally follows the v3 design:

- Claude Code-first, not CLI-first.
- Agent generates Wiki content; Python renders and validates artifacts.
- Wiki and LightRAG are separated lanes.
- All runtime durable state lives under the runtime workspace, which defaults to `./workspace` when the CLI is run from the tool repository.
- Tool code (`src/`, `tests/`, docs, `SKILL.md`) remains separate from runtime data (`workspace/corpus/`, `workspace/artifacts/`, `workspace/project.json`, `workspace/wiki.json`).

## Runtime boundary

`workspace/evo-wiki` is the tool repository. Its Python package, tests, documentation, and skill file are treated as the frozen tool surface.

By default, runtime/project data is written to:

```text
workspace/evo-wiki/workspace/
```

Inside that runtime root, Evo wiki expects:

```text
corpus/
artifacts/
project.json
wiki.json
```

Users can override this boundary with `--root /path/to/project-workspace` when Claude Code needs to operate on a different project workspace.

## Modules

- `evo_wiki.cli`: command entrypoint and lane orchestration.
- `evo_wiki.paths`: canonical artifact paths.
- `evo_wiki.config`: `project.json` and `wiki.json` defaults.
- `evo_wiki.corpus`: corpus scan, hash, change set.
- `evo_wiki.artifacts`: manifest, delta plan, run summary.
- `evo_wiki.wiki`: markdown-to-static-site renderer and search index.
- `evo_wiki.lightrag_lane`: LightRAG input package and HTTP submission to an existing LightRAG Server.
- `evo_wiki.spa_assets`: fixed read-only SPA shell (问答 / 图谱 / 实体枢纽) sharing the wiki's theme + topbar.
- `evo_wiki.platform_export`: read-only Web platform directory export (static site + SPA + nginx.conf + baked RAG state).

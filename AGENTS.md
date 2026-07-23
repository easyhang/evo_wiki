# EvoWiki repository instructions

- Work incrementally and preserve unrelated local changes.
- Run tests from this directory with `.venv/bin/python -m pytest -q`; do not run
  repository-wide discovery from the parent `Open_WIKI` workspace.
- Update `STATUS.md` after material implementation or operational changes.
- New runtime workspaces use SQLite. Existing workspaces without `state` remain
  legacy until an explicit `migrate-state --apply`.
- Once `project.json` selects SQLite, SQLite is the only business-state truth.
  Compatibility JSON is exporter-owned and must never be read as a fallback.
- Migration dry-run must make zero persistent workspace changes. Database
  installation and `project.json` cutover are separately atomic and must remain
  safe to resume at every interruption point.
- Never wait for LightRAG, a model, or any network service inside a SQLite
  transaction. Unknown remote side effects remain blocked and are never
  automatically replayed.
- Normal writers rely on `BEGIN IMMEDIATE` and SQLite busy handling. Use the
  operation lock only for migration/cutover; do not add a second cross-process
  database lock.
- `state_commit_seq` advances only for business facts. Verification, backup,
  export metadata, migration metadata, and journals do not advance it.
- Applied migration SQL is immutable. Add a new version and checksum rather
  than editing a released migration.
- Never hand-edit runtime SQLite files, migration records, binding gates, or
  generated compatibility JSON.
- Do not commit secrets, credentials, local service configuration, runtime
  databases, backups, journals, or generated workspace artifacts.

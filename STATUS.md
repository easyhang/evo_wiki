# Status

- Repository: `easyhang/evo_wiki`, cloned into this workspace.
- Local Python: `Python 3.13.2`.
- Virtual environment: `.venv/`.
- Install commands used: `.venv/bin/python -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -e '.[dev]'`, then the optional `.[gateway]` extra.
- Previous baseline: `PYTHONPATH=src pytest -q` passed, `189 passed`.
- CLI check: `.venv/bin/evo-wiki --help` works.
- On 2026-07-20, normalized the main and lane Skill documents for Codex
  frontmatter compatibility and Chinese-first developer-facing wording. The
  routing policy now defaults to a complete platform; Wiki-only is an explicit
  target. The Skills document the existing SQLite, gateway, replacement,
  audit, notification, and Evidence Subgraph safety contracts.
- On 2026-07-20, implemented the Evo Wiki 0.2.0 productization path:
  `init --profile`, `generate`, and local `serve`; strict brand/navigation/query
  configuration; automatic resumable legacy cutover and backup-first v1-v4
  schema migration; shared lane orchestration; Wiki stub quality gates;
  LightRAG/query-gateway preflight; sanitized generation reports; staging plus
  atomic platform activation; and an allow-listed developer-kit builder.
- Targeted generation tests cover zero-write dry-run, new/current state,
  legacy JSON cutover, one-time v1-v4 migration, stub blocking, platform
  personalization, combined local HTTP serving, atomic export rollback, and
  a Mock LightRAG protocol integration. The Mock validates orchestration only;
  it is not a real LightRAG installation or retrieval-quality acceptance.
- Final 0.2.0 verification on 2026-07-20: `200 passed`; `python -m build`
  produced the wheel and sdist; a temporary clean virtual environment verified
  both `evo-wiki` and `evo` entrypoints plus packaged Evidence Subgraph
  `SKILL.md`/`skill.json`; all five Skill frontmatters passed
  `quick_validate.py`; the allow-listed developer kit and every
  `SHA256SUMS` entry verified successfully.

## Current Optimization Progress

- On 2026-07-22, added the public `examples/WIKI_demo` reproducibility
  template for Evo Wiki 2.0.0. It documents separate static-Wiki and full
  LightRAG-backed platform paths and includes only sanitized configuration and
  neutral Wiki templates. The package intentionally excludes the nine-case
  corpus and Wiki content, generated HTML, SQLite and backups, audit/query
  records, logs, credentials, local service state, and absolute workstation
  paths; collaborators must supply their own corpus and LightRAG service.
- On 2026-07-21, tightened trusted-evidence delivery for local Q&A. A citation
  now needs at least two non-generic query signals in its own excerpt; a
  single weak overlap is discarded. Broad statutory questions additionally
  require local statutory text, so case-only material cannot be presented as
  a verified law citation. When no trusted citations remain, the gateway still
  delivers a `mode=bypass` answer as `ungrounded`, clears citations, and
  creates a pending audit item. The SPA defensively suppresses any accidental
  ungrounded citation card/inline link and shows “本地知识库未覆盖，已进入人工审核”
  plus “暂无可信本地依据”. Focused evidence, gateway and SPA tests passed
  (`91 passed`); the 9-source Wiki re-rendered cleanly and the platform was
  re-exported with JavaScript syntax checks passing. Restarting the local
  server requires its existing `EVO_WIKI_QUERY_AUDIT_KEY`, which is not
  available in this agent environment; no audit records were changed.
- On 2026-07-21, completed Evo Wiki 2.0.0 as a backward-compatible product
  contract release. New workspaces now opt into content contract v2, while
  unversioned 1.x workspaces remain read-only-compatible without file or
  database migration. V2 generation gates enforce one-to-one corpus/source
  mappings, canonical source pages, index discoverability, unique basenames
  and graph labels, and safe ambiguity warnings; health and render reports now
  expose contract version, coverage, mapping, and ambiguity metrics. Q&A uses
  one safe Markdown renderer across answers and audit details, trusts only
  structured citations for source/entity Wiki links, and restores pure session
  state without persisting HTML, failures, or audit content. Added the general
  development standard, upgrade guide, neutral Wiki templates, CHANGELOG, and
  updated root/Wiki Skills. All 241 tests passed; Skill validation, JavaScript
  syntax checks, 9/9 demo-source lint/render/export, browser draft restoration,
  and release checksums passed. Generated wheel, sdist, developer-kit ZIP, and
  SHA256SUMS locally; nothing was published, pushed, or submitted for review.
- On 2026-07-21, fixed front-end Q&A Markdown rendering. The safe renderer now
  supports headings, emphasis and strikethrough, block quotes, common ordered
  and unordered/task lists, pipe tables, horizontal rules, fenced code blocks,
  and soft line breaks while retaining citation anchors and citation-scoped
  Wiki entity links. Raw HTML remains escaped; Markdown images are rendered as
  text labels and never load remote content. The focused SPA regression and
  full test suite passed, generated bundles passed JavaScript syntax checks,
  and workspace/ui-demo was re-rendered/exported with asset hash
  51de4934bef6.
- On 2026-07-21, fixed Q&A loss after following a Wiki entity/source link.
  The main Q&A view now keeps a bounded, schema-v1 `sessionStorage`
  snapshot of successful displayed messages, compact citations, the last
  three conversation turns, the draft, query mode, and top-k. Returning from
  Wiki, switching SPA tabs, or reloading reconstructs the answer through the
  normal safe renderer; failed responses, loading state, audit details, and
  rendered HTML are not stored. “清空” clears both the visible and saved
  conversation. The demo was re-rendered/exported with asset hash
  `18d363c6c921`; generated JavaScript passed `node --check`, and the
  full suite passed (`239 passed`). An isolated browser flow verified
  ask → source Wiki → Back → reload → clear without touching LightRAG or audit
  records.
- On 2026-07-21, expanded `workspace/ui-demo` from the single-case sample to
  22 Wiki pages: nine full source pages, nine representative entity pages,
  three existing concepts, and one index. `wiki-registry.json` now contains
  nine unique source basename mappings and nine entity graph labels/aliases.
  Citation cards link directly to the mapped source Wiki. Answer prose links
  only globally unique entities covered by the current structured citations,
  once per answer; bare anonymized names, code, existing links, URLs, citation
  markers, and escaped HTML remain plain. Both evaluation-only corpus lines
  are retained as explicit corpus notes rather than case conclusions.
  Wiki lint is clean, both generated SPA bundles passed `node --check`,
  the 9-source runtime mapping check passed, and the full suite passed
  (`239 passed`). Desktop 1440x900 and mobile 390x844 browser acceptance
  covered the Wiki index, a full source page, Q&A layout, audit list, and
  independent graph controls with no horizontal overflow or audit mutation.
  The exported SPA asset hash is `ceda3a17e0f0`.
- On 2026-07-21, removed answer-attached knowledge subgraphs from both the
  main Q&A view and entity preset Q&A. Answers now stop after review warnings,
  body, and structured evidence cards; they do not issue `/api/graphs`
  requests. The independent graph view, entity neighborhood graph, graph API,
  public Wiki registry, and developer Evidence Subgraph Skill remain intact.
  This supersedes the answer-mini-graph behavior recorded in earlier entries.
  `workspace/ui-demo` was regenerated with a new asset hash; generated
  `app.js` passed `node --check`, the focused suite passed (`97 passed`), and
  the full suite passed (`238 passed`). Desktop and 390x844 browser acceptance
  used a non-persisting mock answer to verify body/evidence layout without an
  answer graph or audit mutation; the independent graph still loaded 33 nodes
  and 56 edges, and the entity neighborhood remained available.
- On 2026-07-21, added a local-only web audit center to the generated SPA.
  It provides pending/approved/rejected filters, lazy protected-content detail,
  one-click approve/reject actions, safe missing-content degradation, and
  historical rejection/exact-answer-repeat warnings. Shared CLI/HTTP review
  handling now deletes protected content after approval and retains it after
  rejection; trusted-proxy mode exposes neither the audit APIs nor the tab.
  `workspace/ui-demo` was regenerated, generated `app.js` passed
  `node --check`, all `238` tests passed, and desktop plus 390x844 browser
  acceptance covered the list, filters, expanded answer/evidence detail, and
  action controls without resolving an existing audit record.
- On 2026-07-20, positioned the completed product line as Evo Wiki 1.0.0.
  Grounded and partially grounded answers now render an asynchronous
  citation-linked mini subgraph after the answer and evidence cards.
  Version 1.0.1 tightens this path to entity `graph_label` seeds only, ranks
  multiple candidates from the current question and corresponding citation
  excerpts, and requires an exact normalized node ID or label match. It no
  longer uses source titles, citation filenames, partial matches, or
  `nodes[0]` as fallbacks. The display graph remains bounded to depth 1,
  24 nodes, and three seed attempts; no exact match means no mini graph and
  never changes answer delivery, evidence, or review status.
- Evo Wiki 1.0.0 verification: the focused registry/SPA/release tests passed;
  generated `app.js` passed `node --check`; the isolated local UI demo
  re-rendered cleanly and loaded in the browser with no console errors;
  `PYTHONPATH=src pytest -q` passed (`234 passed`); and the 1.0.0 wheel, sdist,
  developer kit, ZIP integrity, and every `SHA256SUMS` entry verified. No
  historical experiment, real model query, or live LightRAG retrieval/graph
  experiment was run.
- Prepared Evo Wiki 1.0.1 as the branch-ready patch release for the stricter
  citation-linked mini-graph selection contract.
- On 2026-07-20, implemented the approved query-delivery refactor without
  running historical or real LightRAG experiments. API schema v2 now separates
  `generation_status`, `answer_origin`, `evidence_status`, and
  `review_status`. A normal RAG response is retained when it has usable
  evidence; zero usable evidence, an empty first answer, or a refusal signal
  triggers `mode=bypass` on the same LightRAG service. Every final non-empty
  answer is delivered and included in the three-turn SPA history.
- SQLite schema v5 persists only the four delivery states plus existing HMAC,
  hashes, counts, and stable codes. `partially_grounded` and `ungrounded`
  responses create an atomic `0600`
  `artifacts/query-audit/open/<audit-id>.json` snapshot containing the
  question, displayed answer, and evidence. SQLite stores only the relative
  path and SHA-256. `audit show --include-content` explicitly reads it;
  `audit resolve --resolution APPROVED|REJECTED` closes the item. Approval
  deletes the snapshot while rejection retains it for later comparison.
  Snapshot failure does not hide the answer and returns
  `review_status=unavailable`.
- The SPA has no Shadow fold or audit-gated body. It renders grounded,
  partially grounded, and ungrounded notices, makes inline citation numbers
  target evidence cards, and uses `wiki-registry` for source links. Auth,
  capacity, maintenance, timeout, upstream failure, and final empty response
  remain generation failures. `query_gateway.mode=shadow|enforce` remains a
  deployment/authorization compatibility switch only.
- Verification for this refactor: `PYTHONPATH=src pytest -q` passed
  (`234 passed`, one upstream Starlette/httpx deprecation warning);
  `python -m build --no-isolation` produced both the 0.3.0 sdist and wheel in a
  temporary in-workspace directory, which was removed after verification.
  No Docker, real LightRAG, model, or historical experiment was run.
- The generated platform now publishes `wiki-registry.json`, resolves entity
  graph labels/aliases and source basenames without exposing workspace paths,
  links concepts/entities to source evidence, suppresses self-wikilinks, and
  promotes standalone Chinese legal section markers to semantic headings.
- Citation-linked mini graphs now accept entity `graph_label` seeds only.
  Candidates are ranked deterministically from the current question and the
  corresponding citation excerpts; a returned graph must contain an exact
  normalized node-ID or label match. There is no document-name seed or
  `nodes[0]` fallback, and no exact match means no mini graph.
  Search, the mobile navigation drawer, graph keyboard actions, deterministic
  bounded BFS layout, explicit entity actions, registry-backed Wiki links, and
  the depth-2/50-node mobile graph layout all passed browser acceptance.
- Generation now opens the SQLite state immutably before either preview or
  Wiki mutation. A current `UNKNOWN/BLOCKED` binding produces
  `GENERATION_RECONCILE_REQUIRED`, a safe count, and exact review/apply/retry
  commands; dry-run remains zero-write and apply preserves the previous
  platform. Processed OPEN bindings activate their staged revision
  idempotently, including reconcile and unchanged-document paths, while an
  HTTP 409 remains blocked.
- `experiment/evo_wiki_test` was backed up before activation as
  `backup-9b890620573f4e0f9004e72fc4a1a491`. The verified 282,624-byte schema-v4,
  state-sequence-19 backup is
  `artifacts/state/backups/evo_wiki-20260720T031654.394895Z-s00000019-c4a1a491.sqlite3`
  with SHA-256
  `a4552d57ad324d9d1c4ae3cef2a688191daa0b8af649d4b82900ea727cb59ca4`.
  Remote inventory and pipeline checks observed 9/9 processed documents,
  34 chunks, and an idle pipeline. Reconcile preview and apply reopened all
  nine bindings; the final immutable state is
  `ACTIVE/PROCESSED/OPEN = 9`, with 34 chunks and
  `state_commit_seq=49`.
- Final `state verify` is `WARN` only because historical operations predate
  complete journaling (`STATE_JOURNAL_INCOMPLETE`); integrity, foreign keys,
  schema, snapshots, bindings, and exports pass. The final generation dry-run
  was ready and apply succeeded with `remote_mutated=false`. The delivered
  `wiki.json` explicitly fixes history to three turns and graph defaults to
  depth 2, 50 nodes, and 12 popular nodes.
- Desktop and 390x844 browser acceptance covered the Wiki, Chinese
  title-priority keyboard search, source/legal headings, source provenance,
  mobile drawer focus/Escape behavior, graph caps/layout/actions, entity
  bidirectional mapping, and nodes without fake Wiki links. The live first
  question “韩永仁案为什么认定自首？” and contextual follow-up “为什么？”
  both returned the legacy `passed`/“已验证” contract with registry-backed
  structured citations. This is historical evidence and was not rerun for the
  schema v2 refactor.
- Final verification is `215 passed`. A clean virtual environment installed
  the 0.3.0 wheel and verified both `evo-wiki` and `evo` entrypoints plus the
  packaged Evidence Subgraph resources. The main, Wiki, LightRAG, Operations,
  and Evidence Subgraph Skills all pass `quick_validate.py`. Release artifacts
  are `evo_wiki-0.3.0-py3-none-any.whl`
  (`d9d5b69e51646427f07ebeee1db441fdac1ee586e5fbc0f36b011f50bc2ac4ba`),
  `evo_wiki-0.3.0.tar.gz`
  (`bac7eb955c547599d2db053e22f1652cccbd00eacf5c364ae6c20ecba746f1ed`),
  the allow-listed `evo-wiki-0.3.0/` developer kit, and
  `evo-wiki-0.3.0.zip`
  (`fb21241b8203ab812737b061db7d467714be177741682e73c508102b84469285`).
  Root and kit `SHA256SUMS` verification passed, and all 0.2.0 artifacts remain
  untouched. The older optimization notes below are retained as historical
  observations that this 0.3.0 closure supersedes.
- On 2026-07-20, reran the `experiment/evo_wiki_test` platform workflow and
  browser-tested the generated Wiki, governed Q&A, graph, entity hub, search,
  and mobile layouts. The zero-write generation preview was ready. The apply
  run regenerated the six-page Wiki staging output but safely stopped before
  platform activation because all nine imported LightRAG bindings were still
  `UNKNOWN/BLOCKED`; no remote write occurred and the previously activated
  platform stayed in place. A read-only reconcile preview observed all nine
  tracks as `PROCESSED` and would reopen their gates; no reconcile apply was
  performed.
- The earlier UI probe found follow-up work in evidence-state presentation,
  Wiki title/LightRAG-label entity mapping, graph density, mobile Wiki
  navigation, source-page structure, and Wiki corpus/page-depth coverage
  gates. The schema v2 refactor resolves the evidence-state delivery and
  conversation-history findings; the remaining content and graph findings are
  separate work.
- On 2026-07-18, `experiment/evo_wiki_test` was explicitly cut over from
  implicit legacy JSON state to SQLite. Post-cutover `state verify` returned
  `PASS`; the database uses WAL, has no integrity or foreign-key violations,
  and records `state_commit_seq=15`. Reapplying the migration returned
  `already_applied` without mutating the workspace.
- Completed the scoped SQLite P1-A1/A2 state foundation. New workspaces select
  `artifacts/state/evo_wiki.sqlite3`; existing workspaces without explicit
  state config remain legacy until `migrate-state --apply`. SQLite is the only
  business-state truth after cutover, and compatibility JSON is exporter-owned.
- Added migration checksum/schema metadata, WAL connection policy, short
  `BEGIN IMMEDIATE` business transactions, source/revision immutable snapshots,
  per-lane runs, retrieval partitions, and LightRAG bindings with separate
  remote status and action gate fields.
- `migrate-state` now defaults to a workspace-zero-write dry-run. Apply preserves
  original JSON and `project.json`, imports and verifies a candidate, then uses
  separately atomic database installation and config replacement. Tests inject
  crashes on both sides of the config switch and confirm the same apply command
  resumes safely.
- Added `state verify/export/backup/migrate-schema/reconcile/replace-*`. Verification reports
  PASS/WARN/FAIL; backup uses SQLite Backup API and collision-safe verified
  files; reconcile only observes existing tracks and never submits, deletes,
  replaces, or automatically retries. `replace-plan` is zero-write and has no
  apply mode.
- `state_commit_seq` advances only for business facts. Export metadata,
  verification, backups, migration metadata, and journals do not advance it.
  Compatibility export happens once at a stable command boundary; export
  failures preserve committed SQLite facts and expose structured machine state.
- Added the `evo-wiki-operations` sub-Skill, root `AGENTS.md`, architecture and
  README runbooks, and synchronized this technical report/status entry.
- SQLite acceptance covers zero-write migration preview, original-byte backup,
  unavailable legacy snapshots, synthetic legacy runs, blocked legacy bindings,
  both cutover crash windows, concurrent writers, repeated online backups, and
  existing CLI/LightRAG regression behavior.
- A copied 9-document experiment workspace passed dry-run and apply without
  remote writes: 9 global/wiki/LightRAG files and 9 legacy bindings imported,
  SQLite integrity/foreign keys/schema/snapshots/exports/WAL/permissions passed,
  all 9 bindings remained `UNKNOWN/BLOCKED`, and the verified online backup
  retained `state_commit_seq=15` across global/wiki/LightRAG baselines. Overall
  verify was `PASS`, including the
  migration operation journal terminal event and hash chain.
- Completed the safe subset of `LOG-001B`: each `run` now writes an independent journal with a strict Pydantic event contract, continuous sequence numbers, canonical SHA-256 chain, `flock`, flush/fsync, and private directory/file permissions. It rotates before exceeding configured event-count (default 5,000) or byte-size (default 64 MiB) limits; cross-file chains remain continuous and `logs verify` rejects gaps or disorder.
- Added `evo-wiki logs verify [--run <id>]` plus explicit dry-run/apply `logs migrate-legacy`. Legacy events are imported as `legacy_unverified`, their original bytes are archived, and repeated migration is a verified no-op.
- Journal safe payloads record only lane, change counts/hash, status, exit code and safe error codes; tests confirm they exclude root paths, source filenames, credentials, smoke queries and full exceptions.
- LOG-001B offline integrity experiment passed: 200 synthetic events were split into four files, the cross-file chain verified, tampering at sequence 100, final-line truncation, and a missing middle segment were detected; synthetic legacy secrets/source names were excluded, and repeated migration returned no-op. The observed 430 ms append duration is informational only, not a production benchmark.
- Completed `SEC-001`: platform exports no internal status snapshots; nginx denies `/status/`, `/nginx.conf`, and `/README.md` as reader-facing paths.
- Completed the low-risk portion of `EV-001`: smoke and SPA queries request chunk content, and reference content is normalized for evidence display.
- Completed the read-only portion of `LR-001`: `doctor --check-service` discovers health/OpenAPI capabilities with a five-second per-request ceiling and no version-only assumptions.
- Added the single-workspace configuration contract: `lightrag.workspace` is explicit and restricted to letters, numbers, and underscores; `doctor --check-service` fails closed when the remote workspace is empty, unavailable, mismatched, or when a reported storage workspace differs.
- Completed `LR-002`: `build-lightrag` now fails closed on remote workspace/storage mismatch or missing track-status capability before any document write. After submission it performs bounded track polling (2-second interval, 600-second default limit), and writes successful ledger entries only after every submitted track is `processed` with valid chunks. Failed, invalid, or timed-out tracks leave the ledger unchanged and produce sanitized status summaries plus a failure code in the report.
- Completed `LR-004A`: HTTP 409 is classified as `REMOTE_HTTP_409`; the target
  binding remains `UNKNOWN/BLOCKED`, no successful ledger fact is written, and
  the remote response body is excluded from durable reports. The new read-only
  `state replace-plan` command confirms health/OpenAPI, document inventory,
  pipeline state, unique basename ownership, backend identity, target snapshot,
  and rollback snapshot before producing a deterministic review plan. Every
  plan has `execution_authorized=false`; the planning command never deletes or
  automatically retries.
- Completed `LR-004B` for the bounded single-workspace/single-document scope.
  Immutable schema migration `0002_replacement_operation` adds a durable
  replacement state machine; existing v1 databases remain readable and require
  explicit backup-first `state migrate-schema --apply` before replacement
  writes. Production replacement remains disabled by default.
- `state replace-execute` binds a single-command confirmation to the complete
  dry-run `plan_digest`, creates and verifies a fresh SQLite backup, persists
  every DELETE/POST intent before the network call, confirms deletion through
  idle pipeline plus repeated full-inventory absence, and requires processed
  track, unique document/track, positive chunks and source-scoped smoke evidence
  before the atomic revision switch.
- Known target/evidence failures may delete the uniquely attributable failed
  target and restore the owner snapshot within the reviewed two-delete/two-submit
  envelope. Ambiguous responses or crashes in intent phases become
  `NEEDS_AUDIT`; rerunning never replays the uncertain side effect.
- Added read-only `state replace-status`, explicit safe
  `state replace-recover`, active-operation business-write gating, sanitized
  replacement journals, crash-boundary tests, automatic compensation tests,
  CLI contract tests and plan/config/backup gates. The confirmation is local
  operator intent, not RBAC or two-person approval.
- Completed the code and isolated-test scope of `QG-001`. Immutable schema
  migration `0003_query_governance` adds query leases, gateway heartbeats,
  maintenance fences and a sanitized audit queue without changing 0001/0002.
  Query bookkeeping is metadata and does not advance `state_commit_seq`;
  explicit audit resolution is a business fact and does.
- Added a strict trusted query gateway and optional Starlette/Uvicorn runtime.
  It accepts only the bounded public DTO, forces references and chunk content,
  maps usable returned basenames to exactly one ACTIVE + PROCESSED/OPEN
  binding, and classifies empty/unrelated evidence without suppressing a final
  non-empty answer. Raw queries, answers, chunk bodies, credentials and full
  upstream errors are excluded from SQLite and logs; pending review content is
  stored only in the protected, hash-addressed audit snapshot described above.
- The current verifier is explicitly `provenance_critical_fact_v1`: it checks
  ACTIVE ownership, non-empty chunks, conservative lexical relevance and
  deterministic critical literals. It is not semantic entailment, stable
  chunk anchoring, multi-domain ACL or a citation-precision guarantee.
- Added `gateway check/serve/status` and `audit list/show/resolve`; content
  reads now require `show --include-content`, and approval/rejection deletes
  the protected snapshot. Enforce mode requires loopback binding,
  trusted-proxy identity, durable audit and fail-closed security. Nginx exports
  route query and graph readers only to the gateway, apply
  auth/rate/body/timeout controls, and contain no LightRAG credentials or
  direct reader route.
- Query and graph readers both hold schema-v3 leases. LR-004 replacement opens
  a DRAINING fence before its first DELETE, requires a fresh READY gateway
  heartbeat, waits for all reader leases, fails before remote write on stale or
  timed-out leases, and closes the fence on COMPLETED/ROLLED_BACK. A fence
  opened while a response is in flight prevents final delivery.
- Completed `QG-001-OPS-ACCEPTANCE`. Immutable
  `0004_notification_outbox` adds durable notifications and attempt history
  without changing 0001-0003. Audit/maintenance event enqueue is atomic with
  its state transition; claim/retry/delivery and gateway/query bookkeeping do
  not advance `state_commit_seq`.
- Added environment-only HTTPS Webhook configuration, canonical HMAC-SHA256
  headers/body, no redirect following, retryable/terminal HTTP
  classification, expired-claim takeover, explicit failed-event retry, and
  privacy scanning. SQLite/reports exclude URL, key, response body, raw
  exception, query, answer, chunks, references, source paths and usernames.
- Added `alerts status/dispatch/retry`, expired-only
  `gateway lease-recover`, default-zero-write `gateway acceptance`, and
  label/run-ID-scoped `gateway acceptance-cleanup`. Crash cleanup also finds
  the exact run-ID temporary directory when `--rm` containers already exited
  and terminates a host Gateway PID only after validating its command/root.
  Required
  `MAINTENANCE_DRAINING` delivery now precedes DELETE; failed delivery changes
  the fence to `FAILED` with DELETE=0.
- Started `RAG-002` with Evidence Subgraph v0.1: the retrieval-only developer Skill performs explicit-seed resource-bounded graph expansion, successful-ledger/SHA projection, file-to-local-content-unit allow-list construction, deterministic BM25, runtime scope assertion, and redacted atomic traces. It never calls LightRAG `/query`, has no generation or global fallback, and is intentionally not exposed in the Web UI.
- Evidence Subgraph `1.1.0` removes the fixed depth-3 ceiling: any positive depth is accepted, while nodes, edges, content units, top-k and total elapsed time remain independently bounded. A subgraph that covers all ACTIVE candidates is now a valid scoped local retrieval with reduction ratio `0`, not `SCOPE_NOT_REDUCED`.
- Live Evidence Subgraph 1.1.0 probe: default depth 2 produced a 48-node/73-edge subgraph, covered 43/43 local content units, returned `candidate_reduction_ratio=0` and `scope_reduced=false`, and successfully returned 5 in-scope evidence chunks in 139 ms. Out-of-scope evidence remained 0 and `102_韩永仁故意伤害案.txt` ranked first.
- Earlier depth-1 probe remains the reduced-scope comparison point: it reduced the same projection from 43 to 18 content units (58.14%) and returned 5 in-scope evidence chunks.
- Live probe result: the running service reports core `1.5.4`, API `0313`,
  remote embedding batch `8`, and support for chunk content, track status,
  document inventory, pipeline status, and document delete.
- Completed the approved single-workspace restart: the Docker Compose LightRAG service now runs with `WORKSPACE=evo_wiki`, and `/health.configuration.workspace` plus all reported storage workspaces resolve to `evo_wiki`.
- Reindexed the 9-document experiment corpus under `experiment/evo_wiki_test` into the `evo_wiki` workspace. Remote document status reached `processed=9`, with `34` chunks.
- The previous import ledger was preserved as `experiment/evo_wiki_test/artifacts/lightrag/state/lightrag-import-ledger.before-evo_wiki-reindex-20260717T075842Z.json`; a new ledger was generated for the `evo_wiki` workspace.
- QG-001 copied/isolated operations acceptance is complete. SQLite restore,
  scheduled/remote backup, retention, context/resume, FTS, stable content-unit
  provenance, multi-domain ACL, OAuth/RBAC and multi-host operation remain out
  of scope.
- Added a low-risk evidence delivery gate: smoke artifacts record an evidence decision, and the generated SPA hides a reference set when no query signal appears in returned chunk content. This is a lexical safety gate, not a semantic verifier.
- Query-flow experiment result: health/OpenAPI/context/normal answer passed; negative control returned a refusal with unrelated references (`REFUSAL_WITH_IRRELEVANT_REFERENCES`).
- Post-reindex smoke query result: `韩永仁案中为什么认定自首？` returned a non-empty answer, 2 references, and 5 non-empty chunk-content items without `[no-context]`.
- LR-004A live read-only acceptance on `experiment/evo_wiki_test` returned
  `no_conflicts`, did not attempt delete, retained `state_commit_seq=15`, and
  left `state verify` at `PASS`. The 9-document remote workspace remains
  `processed=9` with 34 chunks.
- LR-004B implementation acceptance intentionally leaves
  `experiment/evo_wiki_test` unmodified: only `replace-plan`,
  `replace-status`, `state verify` and remote inventory/pipeline reads are
  permitted. The read-only check returned `no_conflicts`/`no_operations`, remote
  `processed=9`, 34 chunks and an idle pipeline; `state_commit_seq` remained
  `15` and database `user_version` remained `1`. `state verify` is now `WARN`
  solely because the valid v1 schema has explicit v2/v3 upgrades available; all
  integrity, foreign-key, object, snapshot, export, WAL, permission and journal
  checks remain `PASS`.
- QG-001 acceptance did not migrate or start the gateway against
  `experiment/evo_wiki_test`: its database remains user_version 1 and the
  9-document remote workspace remains read-only. The dry-run source guard and
  final apply guard both recorded the original SQLite SHA-256
  `c5c2d545…95b5c1`, 143360 bytes, unchanged mtime, `state_commit_seq=15`,
  user_version 1, remote 9/9 processed, 34 chunks and pipeline idle.
- The historical real QG-001 operations path ran only in a random labelled
  temporary Docker network/workspace. It passed copied v1→v4
  migration/no-op/verify, Docker Nginx Basic Auth and principal overwrite,
  direct-LightRAG denial, the former Shadow/Enforce delivery contract, HMAC
  retry/dedupe/redaction, human audit closure, heartbeat/stale
  lease/required-notification DELETE=0 blockers, operator lease abandonment,
  in-flight query maintenance 503, two bounded single-document replacements,
  final processed/evidence query, and complete cleanup. That redacted
  experiment remains unchanged and is superseded as a delivery contract by
  schema v2; the updated real acceptance was intentionally not run.
- Real destructive acceptance ran against a separate container, port, storage
  directory and `lr004b_acceptance` workspace. The successful path reached
  `COMPLETED` with 1 DELETE/1 submit and a 14.77 s maintenance interval. A
  second replacement intentionally failed smoke evidence and reached
  `ROLLED_BACK` with the bounded 2 DELETE/2 submit envelope in 17.45 s. Final
  SQLite verify was `PASS`; the remote inventory was one processed document,
  one chunk and an idle pipeline; local revision counts were one ACTIVE, one
  SUPERSEDED and one REJECTED. The temporary container/runtime was removed and
  only the redacted `experiment/lr004b_acceptance_summary.json` was retained.
- Verification: `PYTHONPATH=src pytest -q` passes, `189 passed`; SQLite
  v1/v2/v3/v4 migration/cutover/backup/concurrency, journal CLI/integrity,
  HTTP 409 sanitization, zero-write/no-DELETE planning, replacement
  crash/compensation, query evidence/audit/privacy, graph/query fence race,
  reader drain, Outbox/HMAC/retry/claim, alerts/lease/acceptance CLI,
  strict ASGI contract, platform routing, live
  `doctor --check-service`, and default-depth Evidence Subgraph retrieval pass.
  Retention, explicit close/root hash, context checkpoints, resume, and
  journal/SQLite cross-storage commit coordination remain deferred.

## Notes for Development

- Runtime project data is created under `workspace/` by default and is ignored by git.
- `lightrag-config.json` is ignored by git and must be created from `lightrag-config.example.json` before running LightRAG-backed platform export.
- Do not commit local secrets, API keys, bearer tokens, generated runtime data, or `.venv/`.

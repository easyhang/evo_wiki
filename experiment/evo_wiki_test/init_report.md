# evo_wiki 首次知识库生成记录

## CLI-only 原版流程

【实测结果】

1. `init` 成功创建 runtime workspace。
   - 日志：`logs/01_init.log`
   - 生成：`project.json`、`wiki.json`、`corpus/raw/`、`artifacts/wiki/wiki-src/index.md` 等目录。
2. 法律文本复制到 `corpus/raw/legal_docs/`。
   - 9 个 UTF-8 `.txt` 文件。
   - 总字节数 115345。
3. `scan` 成功扫描 corpus。
   - 日志：`logs/02_scan.log`
   - `file_count = 9`
   - `change_set.added = 9`，`modified = []`，`deleted = []`
   - 状态文件：`artifacts/corpus-state.json`
4. `run --lane wiki` 成功。
   - 日志：`logs/03_run_wiki.log`
   - 输出：`Wiki lane: success (1 pages)`
   - 只生成 1 个占位首页；`wiki-report.json` 有 `stub_content` warning。
5. `prepare-lightrag` 成功。
   - 日志：`logs/04_prepare_lightrag.log`
   - 输出：`document_count = 9`
   - 生成：`artifacts/lightrag/input/documents.jsonl`
6. `run --lane lightrag --smoke-query` 成功提交。
   - 日志：`logs/05_run_lightrag_smoke.log`
   - `lightrag-report.json` 记录 9 个 `imported` 文件和 9 个 `track_id`。
   - smoke query 返回：`Sorry, I'm not able to provide an answer to that question.[no-context]`
   - `references = []`

## 能力观察

【实测结果】

- 输入文件读取：`scan` 递归读取 `corpus/` 下文件，记录相对路径、SHA256、大小、后缀和 `text_like`。
- 切分：evo_wiki 自身没有在 CLI-only 流程中生成 chunk；LightRAG 后台可能切分，但本次 pipeline 卡在 pending/failed，未形成可用引用。
- 索引：Wiki lane 生成 `search-index.json`；LightRAG 侧索引未完成，`/query` 返回 no-context。
- Markdown/Wiki：CLI-only 只渲染默认 stub，不自动从法律文本生成概念页、实体页、来源页。
- 数据库文件：evo_wiki runtime 未生成 SQLite 或本地数据库；主要状态为 JSON、Markdown、HTML、JSONL。
- 元数据：保存了 `manifest.json`、`wiki-report.json`、`wiki-health.json`、`corpus-state.json`、`lightrag-import-ledger.json`。

## LightRAG 服务状态

【实测结果】

- API 可达：`/docs` 返回 200，`/query` 可调用，`/documents/text` 可提交并返回 `track_id`。
- API 不是无效；失败原因在 LightRAG 后台 pipeline。
- `logs/15_lightrag_pipeline_status.log` 显示 embedding 请求 400：`batch size is invalid, it should not be larger than 10`。
- 状态统计：`pending = 8`，`failed = 1`，`all = 9`。
- `logs/14_lightrag_track_status_102.log` 中，韩永仁案文档状态为 `pending`，`chunks_count = null`。

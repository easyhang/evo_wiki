# evo_wiki 基础能力评估报告

## 1. 执行摘要

【实测结果】

- 本次评估在 `experiment/evo_wiki_test/` 内完成，未修改 evo_wiki 源码、测试文件或原始测试数据。
- 原版 CLI 可以初始化 workspace、扫描文档、生成 corpus hash/change set、渲染 Wiki、准备 LightRAG JSONL、提交到已有 LightRAG API。
- CLI-only 不会自动把法律文本生成知识页；Wiki 内容生成依赖 Agent/Claude Code 维护 `wiki-src` Markdown。
- LightRAG API 可达且首次提交返回 9 个 `track_id`，但后台索引失败/挂起，问答返回 `[no-context]` 且无 references。
- 增量扫描能识别单文件修改；Wiki lane 会重新渲染所有现有 Markdown 页，但不会自动同步 corpus 变更到 Wiki 内容；LightRAG 修改重提遇到服务端 409。

## 2. 环境信息

【实测结果】

- Python：`3.13.2`
- Node：`v25.8.2`
- 测试数据：9 个 UTF-8 `.txt`，总字节数 115345。
- 单元测试：`23 passed in 1.22s`
- 详细环境与模块说明见 `environment.md`。

## 3. 架构分析

【架构推断】

- evo_wiki 是 Claude Code-first 工具，不是完整自动知识生成引擎。
- Python 侧核心职责是目录协议、扫描、状态记录、渲染、健康检查、LightRAG 输入准备和服务提交。
- Wiki lane 和 LightRAG lane 分离：状态、报告、baseline、输出目录均独立。
- 主要持久化介质是文件系统：Markdown、JSON、JSONL、HTML；没有本地 SQLite 或事务型数据库。
- LightRAG 是外部服务能力，evo_wiki 不内嵌索引、向量库或图数据库。

## 4. 实测流程

【实测结果】

- 初始化：`logs/01_init.log`，成功创建 workspace。
- 扫描：`logs/02_scan.log`，9 个 added。
- CLI-only Wiki：`logs/03_run_wiki.log`，成功渲染 1 页 stub，`wiki-report.json` 出现 `stub_content` warning。
- LightRAG 准备：`logs/04_prepare_lightrag.log`，生成 9 条 documents JSONL。
- LightRAG 首次提交：`logs/05_run_lightrag_smoke.log`，9 个文档提交成功并返回 track_id。
- Agent-assisted Wiki：创建 6 个样例页，`logs/08_run_wiki_agent_assisted_fixed.log` 显示成功渲染 6 页。
- audit 测试：`logs/06_lint_invalid_audit.log` 检出缺字段、非法 severity、非法 status。
- 增量：`logs/09_scan_incremental.log` 只识别 `102_韩永仁故意伤害案.txt` 为 modified；diff 见 `source_stats/version_diff_102.txt`。

## 5. 能力评估

【实测结果】

- 文档导入：支持文件扫描、SHA256、大小、后缀、text_like 标记；LightRAG 输入包包含 `source_path`、`input_path`、`sha256`、`size`、全文 `text`。
- 知识生成：CLI-only 不生成法律概念/实体/来源页；Agent-assisted Markdown 能被渲染、搜索和报告收录。
- wikilink/知识关联：支持 `[[wikilink]]` 转 HTML 链接，生成 `wiki-dependency-graph.json`，source 页右侧显示相关概念/实体和原文摘录，概念/实体页显示“链接到本页”。
- 来源追踪：Wiki frontmatter 支持 `sources` 文件路径；source 页能保留原文。LightRAG query 本次无可用引用，`references = []`。
- audit 防幻觉：存在 audit 文件形状检查和 target 检查；未发现自动把缺来源回答放入审核队列的运行时机制。
- 增量更新：corpus change set 能识别 added/modified/deleted；per-lane `corpus-state.json` 独立。Wiki lane 重新渲染全部现有页，不做内容级 diff。LightRAG 修改重提因同名文档 pending 返回 409。
- 状态持久化：核心状态包括 `artifacts/manifest.json`、`artifacts/corpus-state.json`、`artifacts/wiki/progress.json`、`wiki-report.json`、`wiki-health.json`、`wiki-dependency-graph.json`、`lightrag-import-ledger.json`。

【架构推断】

- 法律/政务需要的“章节级、条款级、chunk 级证据链”不是 evo_wiki CLI 原生保证；需要 Agent 写入更细粒度 metadata 或接入稳定 RAG 引用结果。
- 文件 JSON 状态适合小规模/单人开发验证；百万级 chunk、并发写入、事务恢复需要外部数据库或更严格锁机制。

## 6. 与政务知识库需求匹配分析

【实测结果】

- 可以作为本地化知识库底座的“静态 Wiki 构建与审阅层”：目录清晰、产物可读、可复制部署。
- 当前不能直接满足严格法律证据链：LightRAG references 为空；Wiki sources 只有文件级路径；章节/chunk 需要人工或二次开发补齐。
- 当前不能保证自动防幻觉：audit 是文件约定和 lint，不是回答生成时的强制拦截。
- 增量更新能发现文件级变化，但无法自动判断条款级影响范围。

## 7. 优势

【实测结果】

- 零运行依赖，安装和 CLI 启动简单。
- runtime 数据与工具源码分离，适合实验和审计。
- 报告、manifest、ledger 可读性强，便于 AI Agent 理解。
- Wiki 渲染器支持中文标题、wikilink、反向链接、搜索索引、source 原文页。
- lane 分离降低 Wiki 与 LightRAG 互相污染风险。

## 8. 不足

【实测结果】

- CLI-only 知识生成能力弱，只生成 stub。
- LightRAG 外部服务失败时，evo_wiki 只能记录失败，不能修复索引链路。
- 修改文档重新提交遇到 409；没有自动删除旧文档、重建或安全替换流程。
- source 追踪默认到文件级，未到章节、页码、chunk。
- audit 不自动入队，不参与问答运行时拦截。

【架构推断】

- 面向政府/企业落地需要补强权限、并发、审计闭环、证据粒度、批量导入格式和稳定 RAG 后端。

## 9. 后续改造建议

【架构推断】

- 增加结构化 ingest 层：为法律文本抽取案号、法院、裁判理由、法条、当事人、段落 offset。
- 为 Wiki frontmatter 增加证据字段：`source_path`、`section`、`quote`、`char_start`、`char_end`、`chunk_id`。
- 将 audit 从“lint 文件形状”升级为回答前/回答后的强制证据检查队列。
- 增量更新增加 content diff、影响页面列表、旧版本保留和回滚策略。
- LightRAG 接入增加状态轮询、失败重试、同名文档替换策略和 isolated namespace。
- 若新增“税务知识插件”，建议新增独立 skill/lane 约定和 schema，而不是直接耦合进 `wiki.py`；核心改动点为 skill 文档、domain schema、ingest 规则、source page 模板和可选 LightRAG metadata。

---
name: evo-wiki-lightrag
description: |
  从 workspace/corpus/ 原始语料准备 LightRAG 输入，并把输入提交到已有的 LightRAG Server。
  用户要构建 Agent 问答、GraphRAG、检查增量导入、执行预演，或只更新 LightRAG 而不改 Wiki
  时使用。本 Skill 不生成 Wiki，不把 Wiki 页面作为默认入库源；语料删除必须报告 rebuild 风险。
---

# Evo Wiki · LightRAG 子 Skill

LightRAG 工作流负责“原始语料 → 输入包 → 已有 LightRAG 服务”。它不负责 Wiki 写作，也不负责
启动或安装 LightRAG 服务。

## 1. 使用前提和边界

适用请求包括：

- 只基于原始语料更新 LightRAG；
- 为 Agent 提供问答或 GraphRAG；
- 检查本次新增、修改、删除会造成什么增量变化；
- 验证已有 LightRAG 服务和 workspace 映射；
- 只更新 LightRAG，不触碰 Wiki 产物。

默认输入是 `workspace/corpus/`，不是模型生成的 Wiki 页面。LightRAG 与 Wiki 不共享索引、状态、
报告或运行基线；LightRAG 失败不能破坏已有 Wiki 产物。

本工作流的产物位于：

```text
workspace/artifacts/lightrag/
  input/documents.jsonl
  input/manifest.json
  reports/lightrag-report.json
  state/lightrag-import-ledger.json
  state/corpus-state.json
  queries/
```

## 2. 配置服务

提交、服务检查和 platform 导出都要求明确的 LightRAG 地址与远端 workspace。先复制配置模板：

```bash
cp /path/to/workspace/lightrag-config.example.json \
  /path/to/workspace/lightrag-config.json
```

编辑 `lightrag-config.json`：

```json
{
  "base_url": "http://YOUR_LIGHTRAG_SERVER:9621",
  "workspace": "evo_wiki",
  "api_key_env": "LIGHTRAG_API_KEY",
  "bearer_token_env": "LIGHTRAG_BEARER_TOKEN"
}
```

`base_url` 和 `workspace` 必须换成真实值。也可以设置 `LIGHTRAG_BASE_URL` 覆盖地址；API key
和 Bearer token 只能通过环境变量注入。不要猜测 localhost，不要把真实凭据写入配置或报告。

服务检查是只读操作：

```bash
evo-wiki doctor --root /path/to/workspace --check-service
```

检查必须确认服务可访问、OpenAPI 明确声明 `include_chunk_content`、
`conversation_history` 和查询 `mode=bypass`，并且远端 workspace 与本地配置一致。`bypass`
只在首轮没有可用知识库证据、首轮回答为空或返回拒答文本时，由查询网关调用同一个 LightRAG
查询模型生成通用知识回答；它不引入第二套模型密钥。能力缺失、workspace 为空、无法确认或
不一致时 fail closed，不要提交文档或开放查询。

## 3. 标准流程

完整平台的标准入口是：

```bash
evo-wiki generate --root /path/to/workspace --dry-run --json
evo-wiki generate --root /path/to/workspace --json
```

`generate --dry-run` 是零 workspace 写入、零远端写入的完整预演。它会先检查 Wiki 正文和
当前 binding gate，再决定是否允许进入实际生成。发现当前 `UNKNOWN/BLOCKED` binding 时返回
`GENERATION_RECONCILE_REQUIRED`；按 Operations 子 Skill 执行 review → apply → retry。

以下分步命令用于只更新 LightRAG 或定位问题，不是完整平台的交付入口。

### 3.1 准备输入

```bash
evo-wiki prepare-lightrag --root /path/to/workspace
```

该命令从 `corpus/` 生成以下本地输入，因此属于 `[本地写入]`：

```text
artifacts/lightrag/input/documents.jsonl
artifacts/lightrag/input/manifest.json
```

### 3.2 先做预演

只调试 LightRAG 工作流时，可运行局部预演（dry-run）：

```bash
evo-wiki build-lightrag \
  --root /path/to/workspace \
  --dry-run
```

也可以通过工作流（lane）编排：

```bash
evo-wiki run --lane lightrag --lightrag-dry-run --root /path/to/workspace
```

这类局部预演会准备输入并写本地报告，但不提交远端。需要严格零 workspace 写入时，使用
`generate --dry-run`。重点阅读：

```text
artifacts/lightrag/reports/lightrag-report.json
```

确认新增、修改、删除和 `requires_rebuild` 后，再决定是否提交。

### 3.3 提交并等待处理

确认需要更新 Agent 问答且服务检查通过后运行：

```bash
evo-wiki build-lightrag --root /path/to/workspace
```

或运行工作流：

```bash
evo-wiki run --lane lightrag --root /path/to/workspace
```

提交后工具会轮询本次返回的 track。默认每 2 秒检查一次，最长等待 600 秒；只有所有提交文档都
达到 `processed` 且有有效 chunk，才写入成功的 import ledger。失败、无效或超时的 track 不得被
伪装成成功导入。

需要验证服务返回的答案时，可以追加冒烟查询：

```bash
evo-wiki build-lightrag \
  --root /path/to/workspace \
  --smoke-query "你的问题"
```

完整平台对应写法是：

```bash
evo-wiki generate \
  --root /path/to/workspace \
  --smoke-query "你的问题" \
  --json
```

## 4. 增量变化和删除安全

LightRAG 远端删除最容易产生“本地以为删了、远端仍然可检索”的假成功。必须按变化类型处理：

- 新增：准备输入并提交到 `/documents/text`。
- 修改：可以尝试重新提交；如果服务因同一 `file_source` 已存在而返回 HTTP 409，保留
  `REMOTE_HTTP_409`，不要静默覆盖或自动重试。
- 删除：不能假设旧知识已从图谱或向量中清除，必须标记 `requires_rebuild`。

报告至少要能表达：

```json
{
  "requires_rebuild": true,
  "deleted_pending_rebuild": ["corpus/raw/deleted.md"]
}
```

没有用户明确授权前，不调用远端删除接口，不清空服务，不执行 full rebuild。`generate` 遇到
删除会以 `GENERATION_REBUILD_REQUIRED` 停止，不会自动修复。HTTP 409 的后续处理属于
Operations 子 Skill 的单文档 `state replace-plan` / `replace-execute` 流程；不支持批量替换。

## 5. 读取报告并向用户汇报

回复前优先读取：

```text
artifacts/manifest.json
artifacts/lightrag/input/manifest.json
artifacts/lightrag/reports/lightrag-report.json
artifacts/lightrag/state/lightrag-import-ledger.json
```

汇报时说明：

- 本次是预演还是实际提交；
- 输入文档数；
- 将提交、跳过或触发 rebuild 的文档；
- 目标服务地址和远端 workspace；
- 是否配置鉴权环境变量，不输出凭据；
- 服务返回的 `track_id` 和最终状态；
- 下一步是配置服务、等待后台处理、运行冒烟查询，还是进行 full rebuild。

查询协议保持 schema v1 向后兼容：`conversation_history` 是可选字段，最多 3 个完整
user/assistant 对；单条最多 4,000 字符，总计最多 12,000 字符且角色严格交替。网关把完整请求
做 HMAC，只用历史 user 问题辅助相关性校验，不持久化历史原文；响应的
`context_turns_used` 表示实际使用的对话轮数。

## 6. 样例和开发者检索

样例只准备输入并做预演，不调用真实服务：

```bash
PYTHONPATH=src python3 skills/evo-wiki-lightrag/scripts/dry-run-example.py
```

自动测试中的 Mock LightRAG 只模拟 `/health`、`/openapi.json`、提交和 track 轮询协议，用于验证
Evo Wiki 编排，不代表真实 LightRAG 已安装、已入库或已经过效果验收。

如果需要按显式种子检索受限证据、但不生成最终答案，使用运行时 `evidence-subgraph@1`。它不接入
Web UI，不调用 LightRAG `/query`、`mix`、`hybrid` 或 `global`，详情见：

```text
src/evo_wiki/retrieval_skills/evidence_subgraph/SKILL.md
```

---
name: evo-wiki-lightrag
summary: 准备 LightRAG 输入并操作已有 LightRAG 服务（LightRAG lane）。
description: |
  Evo Wiki 的 LightRAG 子 Skill。用于从 corpus/ 原始语料准备 LightRAG 输入，并提交到一个已运行的
  LightRAG Server，供智能体问答 / GraphRAG 使用。必须与 Wiki lane 完全分离；不得默认使用 Wiki 页面作为入库源；
  删除语料时必须诚实标记 requires_rebuild，不得假装增量删除安全完成。
---

# Evo Wiki · LightRAG 子 Skill

## 1. 适用场景

当用户明确需要：

- “只基于原始语料更新 LightRAG 服务”
- “给 Agent 做问答知识库 / GraphRAG”
- “检查 LightRAG 增量是否安全”
- “这次只更新 LightRAG，不动 Wiki”

使用本 Skill。若用户只是想要人读静态知识站，请转到 `skills/evo-wiki-wiki/SKILL.md`。

## 2. LightRAG lane 边界

LightRAG lane 只负责：

```text
workspace/artifacts/lightrag/
  input/documents.jsonl
  input/manifest.json
  reports/lightrag-report.json
  state/lightrag-import-ledger.json
  state/corpus-state.json
  queries/
```

必须遵守：

- 默认输入是 `workspace/corpus/` 原始语料，不是 Wiki 生成页。
- 不强制生成 Wiki；不依赖 Wiki 成功。
- 不共享 Wiki 的索引、状态、报告或运行基线。
- 任一 LightRAG 服务提交失败，不破坏 Wiki artifacts。
- 所有操作以中文说明，命令名/API 名称可保留英文。

## 3. 标准流程

### 3.1 准备输入

```bash
evo-wiki prepare-lightrag --root /path/to/workspace
```

输出：

```text
artifacts/lightrag/input/documents.jsonl
artifacts/lightrag/input/manifest.json
```

### 3.2 Dry-run 检查增量

优先 dry-run，特别是第一次接入、语料删除、或环境变量未准备好时：

```bash
evo-wiki run --lane lightrag --lightrag-dry-run --root /path/to/workspace
```

检查：

```text
artifacts/lightrag/reports/lightrag-report.json
```

### 3.3 提交到已有 LightRAG 服务

仅在用户确认需要 Agent QA，且已有 LightRAG Server 可访问时运行。默认服务地址为 `http://127.0.0.1:9621`；可通过 `project.json` 的 `lightrag.base_url` 或环境变量覆盖：

```bash
export LIGHTRAG_BASE_URL=http://127.0.0.1:9621
# 如果服务启用 API key：
# export LIGHTRAG_API_KEY=...
# 如果服务使用 Bearer token：
# export LIGHTRAG_BEARER_TOKEN=...
```

提交：

```bash
evo-wiki run --lane lightrag --root /path/to/workspace
```

可选 smoke query：

```bash
evo-wiki run --lane lightrag --smoke-query "你的问题" --root /path/to/workspace
```

## 4. 删除与重建安全协议

LightRAG 增量删除风险最高。若 ledger 中曾导入的文档在当前 corpus 中已不存在，报告必须明确：

```json
{
  "requires_rebuild": true,
  "deleted_pending_rebuild": ["corpus/raw/deleted.md"]
}
```

处理原则：

- 新增：可准备并通过 `/documents/text` 提交到 LightRAG Server。
- 修改：可尝试重新提交对应文档；若服务因同一 `file_source` 已存在而返回冲突，以报告为准，不得静默覆盖。
- 删除：不能假装旧知识已从图谱/向量中彻底清除；必须标记 full rebuild 风险。
- 用户未确认前，不做破坏性删除或重建，也不擅自清空已有服务。

## 5. 报告解读

回复用户前优先读取：

```text
workspace/artifacts/manifest.json
workspace/artifacts/lightrag/input/manifest.json
workspace/artifacts/lightrag/reports/lightrag-report.json
workspace/artifacts/lightrag/state/lightrag-import-ledger.json
```

需要说明：

- 本次是否 dry-run。
- 输入文档数量。
- 哪些文档将提交、跳过或触发 rebuild。
- 目标 LightRAG 服务地址、是否配置鉴权环境变量，以及服务返回的 `track_id`。
- 下一步是配置/启动服务、等待服务后台索引完成、运行 smoke query，还是 full rebuild。

## 6. 脚本与样例

本 Skill 自带：

```text
skills/evo-wiki-lightrag/
  scripts/dry-run-example.py       # 使用样例语料准备输入并 dry-run
  examples/basic/                  # 只包含 LightRAG 所需原始语料
```

运行样例：

```bash
PYTHONPATH=src python3 skills/evo-wiki-lightrag/scripts/dry-run-example.py
```

该脚本不会调用真实 LightRAG 服务，不需要服务地址或鉴权环境变量；它只验证输入准备、ledger 对比和 dry-run 报告。

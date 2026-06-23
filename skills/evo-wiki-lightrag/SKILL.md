---
name: evo-wiki-lightrag
summary: 准备并构建独立 LightRAG GraphRAG 知识库（LightRAG lane）。
description: |
  Evo Wiki 的 LightRAG 子 Skill。用于从 corpus/ 原始语料准备 LightRAG 输入，并按需构建可供智能体问答的
  GraphRAG workspace。必须与 Wiki lane 完全分离；不得默认使用 Wiki 页面作为入库源；删除语料时必须诚实标记
  requires_rebuild，不得假装增量删除安全完成。
---

# Evo Wiki · LightRAG 子 Skill

## 1. 适用场景

当用户明确需要：

- “只基于原始语料生成 LightRAG 知识库”
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
  workspace/
  reports/lightrag-report.json
  state/lightrag-import-ledger.json
  state/corpus-state.json
  queries/
```

必须遵守：

- 默认输入是 `workspace/corpus/` 原始语料，不是 Wiki 生成页。
- 不强制生成 Wiki；不依赖 Wiki 成功。
- 不共享 Wiki 的索引、状态、报告或运行基线。
- 任一 LightRAG 构建失败，不破坏 Wiki artifacts。
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

### 3.3 真实构建

仅在用户确认需要 Agent QA，且 LLM / embedding 环境已配置时运行：

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

- 新增：可准备并导入。
- 修改：可重新导入对应文档，但需以报告为准。
- 删除：不能假装旧知识已从图谱/向量中彻底清除；必须标记 full rebuild 风险。
- 用户未确认前，不做破坏性删除或重建。

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
- 哪些文档将导入、跳过或触发 rebuild。
- 是否缺少 LightRAG / LLM / embedding 环境。
- 下一步是配置环境、真实构建、还是 full rebuild。

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

该脚本不会调用真实 LightRAG，不需要 LLM / embedding 环境；它只验证输入准备、ledger 对比和 dry-run 报告。

---
name: evo-wiki
summary: Evo Wiki 主 Skill：在 Wiki lane 与 LightRAG lane 之间做路由与边界约束。
description: |
  Evo Wiki 是面向 Claude Code 的 LLM Wiki / GraphRAG 知识平台工具。主 Skill 只负责判断用户目标、选择
  Wiki 子 Skill 或 LightRAG 子 Skill，并强调两条 lane 必须独立运行、独立状态、独立报告。具体写作、渲染、
  LightRAG 输入准备与构建规则分别见 skills/evo-wiki-wiki/SKILL.md 与 skills/evo-wiki-lightrag/SKILL.md。
---

# Evo Wiki 主 Skill

## 1. 定位

本文件是 **Evo Wiki 的主路由 Skill**，不再承载全部 Wiki 写作细节或 LightRAG 构建细节。

当用户提出需求时，先判断目标属于哪条 lane，再进入对应子 Skill：

| 用户目标 | 使用子 Skill | 目录 |
|---|---|---|
| 生成人读静态 Wiki、整理资料、改页面结构、生成 HTML 样例 | Wiki 子 Skill | `skills/evo-wiki-wiki/SKILL.md` |
| 构建 Agent 问答 / GraphRAG / LightRAG workspace | LightRAG 子 Skill | `skills/evo-wiki-lightrag/SKILL.md` |
| 先 Wiki 后问答知识库 | 先 Wiki 子 Skill，用户确认后再 LightRAG 子 Skill | 两者分阶段使用 |
| 用户明确同时要两套产物 | 分别执行两个子 Skill；状态与报告仍保持分离 | 两者都用 |

## 2. 核心边界

Wiki lane 与 LightRAG lane 必须完全分离：

- 不强制一起运行。
- 不共享索引。
- 不共享运行状态。
- 不要求一起部署。
- 不把模型生成的 Wiki 页面默认喂给 LightRAG。
- 任一 lane 失败，不破坏另一条 lane 已有产物。
- LightRAG 删除风险必须诚实报告，不得假装增量删除安全完成。

## 3. 子 Skill 职责

### 3.1 Wiki 子 Skill

使用：`skills/evo-wiki-wiki/SKILL.md`

负责：

- 从 `workspace/corpus/` 原始语料生成可读静态 Wiki。
- 维护 `index / concepts / entities / sources` 页面结构。
- 概念页与实体页必须基于语料做自然摘要，不编造；页面正文不需要单独标明来源。
- 原文页必须由「摘要」与「原文内容」组成，并保留完整原文。
- 生成 learnbuffett 风格的典雅书卷气 HTML 样例。
- 写入 `artifacts/wiki/progress.json`，用于断点续处理。
- 结束时运行 lint，检查页面一致性、概念冲突、死链、孤儿页与原文页结构。
- 运行 `evo-wiki run --lane wiki`、`render-wiki`、`lint-wiki`。

自带内容：

```text
skills/evo-wiki-wiki/
  SKILL.md
  scripts/render-example.py
  examples/learnbuffett-style/
```

### 3.2 LightRAG 子 Skill

使用：`skills/evo-wiki-lightrag/SKILL.md`

负责：

- 从 `workspace/corpus/` 原始语料准备 LightRAG 输入。
- 构建或 dry-run LightRAG GraphRAG workspace。
- 维护 `artifacts/lightrag/input`、`workspace`、`reports`、`state`、`queries`。
- 检测已删除语料并标记 `requires_rebuild`。
- 运行 `evo-wiki prepare-lightrag`、`build-lightrag`、`run --lane lightrag`。

自带内容：

```text
skills/evo-wiki-lightrag/
  SKILL.md
  scripts/dry-run-example.py
  examples/basic/
```

## 4. 选择流程

1. 先确认用户目标：人读 Wiki、Agent QA、还是两者都要。
2. 如果用户只是要阅读、审阅、部署文档站：使用 Wiki 子 Skill。
3. 如果用户明确要问答知识库 / GraphRAG：使用 LightRAG 子 Skill。
4. 如果用户说“先生成 Wiki，我确认后再做问答”：先 Wiki，停止等待确认，再 LightRAG。
5. 如果语料删除且用户要 LightRAG：先 dry-run，报告 rebuild 风险，再决定是否 full rebuild。

## 5. 共同运行数据目录

默认运行数据仍在：

```text
workspace/
  corpus/
  artifacts/
  project.json
  wiki.json
```

关键产物：

```text
workspace/artifacts/
  manifest.json
  agent/
  wiki/
  lightrag/
  docker/
```

## 6. 常用命令

```bash
# 初始化
evo-wiki init --root /path/to/workspace

# Wiki lane
evo-wiki run --lane wiki --root /path/to/workspace
evo-wiki lint-wiki --root /path/to/workspace
evo-wiki render-wiki --root /path/to/workspace

# LightRAG lane
evo-wiki prepare-lightrag --root /path/to/workspace
evo-wiki run --lane lightrag --lightrag-dry-run --root /path/to/workspace
evo-wiki run --lane lightrag --root /path/to/workspace

# 同时运行（仅当用户明确要求）
evo-wiki run --lane both --root /path/to/workspace
```

## 7. 语言与说明风格

所有面向 Claude Code / Agent 的 prompt、Skill 文档、操作说明、页面模板与示例默认使用中文。命令名、代码标识符、API 名称、库名等必要场景可保留英文。

---
name: evo-wiki-wiki
description: |
  把 workspace/corpus/ 中的原始语料整理成可读、可探索、可审计的静态 Wiki。用户要生成或维护
  Wiki、文档站、概念页、实体页、原文页，或审阅 Wiki 的链接、审计记录和 HTML 渲染结果时使用。
  本 Skill 只负责 Wiki 工作流，不负责 LightRAG 入库、远端问答或 GraphRAG。
---

# Evo Wiki · Wiki 子 Skill

本 Skill 的目标是维护一套“来源可追溯、页面可导航、内容不越界”的静态 Wiki。Agent 负责理解语料
并维护 Markdown 源文件，Python 工具负责渲染、基础 lint、报告和 HTML 交付。

## 1. 工作边界

Wiki 工作流（lane）只读写以下运行产物：

```text
workspace/artifacts/wiki/
  wiki-src/
    index.md
    concepts/
    entities/
    sources/
  dist/
  reports/
  progress.json
  audit/
  log/
  outputs/
```

必须遵守：

- 输入优先使用 `workspace/corpus/` 原始语料，不修改原始文件。
- 不把 Wiki 页面默认当作 LightRAG 输入。
- 本 Skill 不启动 LightRAG，也不直接导出完整平台；主 Skill 在正文完成后统一调用
  `generate`。
- 不用模型常识补写语料没有支持的事实；推断必须明确标为推断。
- 不覆盖用户手工编辑区：`<!-- evo:user-edit:start -->` 到
  `<!-- evo:user-edit:end -->` 之间的内容必须原样保留。
- 只要修改了 `wiki-src/`，交付前必须运行 `render-wiki` 或 `run --lane wiki`。

## 2. 页面模型

每个 Wiki 项目至少维护以下页面类型：

1. `index.md`：入口和全局索引。
2. `concepts/*.md`：概念页，一页一个概念。
3. `entities/*.md`：实体页，例如人物、组织、工具、论文。
4. `sources/*.md`：原文页，必须同时包含摘要和完整原文。

推荐导航关系：

```text
入口 → 概念 → 实体 → 原文 → 其他
```

概念页和实体页只覆盖语料实际讨论的范围。证据路径可以放在 frontmatter 的 `sources`、报告
和 audit 中；页面正文不必重复一段 `## Sources`。

### 概念页

```markdown
---
title: "概念名称"
type: concept
sources:
  - workspace/corpus/raw/source.md
tags:
  - 领域标签
---

# 概念名称

一句话定义：只写语料能支撑的定义。

## 摘要

综合语料中关于该概念的事实、关系和上下文。

## 关键性质

- 只列语料明确讨论过的性质。

## 相关页面

- [[相关概念]]
- [[相关实体]]

## 未决问题

- 语料没有覆盖、需要后续查证的问题。
```

### 实体页

实体页只描述实体在当前语料中的角色，不补充语料外的传记或背景：

```markdown
---
title: "实体名称"
type: entity
graph_label: "LightRAG 图谱中的精确 label"
aliases:
  - "可选别名"
sources:
  - workspace/corpus/raw/source.md
---

# 实体名称

说明该实体在语料中是什么。

## 摘要

概括其在语料中的角色、行为、贡献和关联概念。

## 关联概念

- [[概念名称]]

## 未决问题

- 语料没有覆盖、需要后续查证的信息。
```

`graph_label` 和 `aliases` 都是可选字段；缺省 `graph_label` 使用页面 `title`。同一 Wiki 内
`graph_label` 必须唯一，否则生成以 `WIKI_REGISTRY_MAPPING_INVALID` 失败。渲染器生成公共
`wiki-registry.json`，把实体 title/graph label/aliases 映射到真实 Wiki slug，并把唯一的
source basename 映射到原文页；公共注册表不得出现 workspace 绝对路径。

### 原文页

原文页必须保留全文；摘要不能替代原文：

````markdown
---
title: "来源文件原文"
type: source
sources:
  - workspace/corpus/raw/source.md
---

# 来源文件原文

## 摘要

用中文概括原文，只写原文能够支持的内容。

## 原文内容

# 完整原文

对已抽取的概念和实体使用 `[[概念名]]`、`[[实体名]]` 建立内链。
````

## 3. 五种操作

每次操作都在 `workspace/artifacts/wiki/log/YYYYMMDD.md` 记录时间、操作、输入、触达页面和
结果摘要。发现证据不足、事实冲突或结构不确定时，优先创建 audit，不要自行补写结论。

### ingest：摄入资料

触发示例：

```text
ingest workspace/corpus/raw/xxx.md
把 corpus/raw 里的新资料编进 Wiki
```

执行顺序：

1. 阅读指定原文，不修改 `corpus/raw/`。
2. 创建或更新 `sources/` 原文页，写入摘要、完整原文和必要的 `[[wikilink]]`。
3. 创建或更新概念页和实体页，只写语料支持的内容。
4. 更新 `index.md`，确保新页面从入口可达。
5. 记录 log；证据不足或冲突写入 audit。
6. 渲染并检查报告：

```bash
evo-wiki render-wiki --root /path/to/workspace
```

`render-wiki` 适合写作过程中的快速检查。最终交付由主 Skill 运行 `generate`。

### compile：重组 Wiki

读取现有 `wiki-src/`，处理过长、重复、孤立或互相不可达的页面：

1. 拆分过长页面，合并明显重复页面，并保留用户手工编辑区。
2. 重建概念、实体和原文之间的 `[[wikilink]]`。
3. 更新入口索引、相关页面和未决问题。
4. 对不能确定的合并或事实冲突创建 audit。
5. 记录 log，重新运行 `render-wiki`。

### query：基于 Wiki 回答

1. 只搜索 `artifacts/wiki/wiki-src/` 中已有页面。
2. 严格基于 Wiki 内容回答；未覆盖的内容明确说明，不用模型常识补齐。
3. 用页面路径或 `[[wikilink]]` 指出依据。
4. 有复用价值的问答可保存到 `outputs/queries/<slug>.md`。
5. 发现缺口、冲突或过期信息时创建 self-audit，并记录 log。

### lint：健康检查

运行：

```bash
evo-wiki lint-wiki --root /path/to/workspace
```

基础检查必须包括：

- `[[wikilink]]` 是否指向存在的页面；
- 页面是否有入链，是否能从 `index.md` 发现；
- 高频候选词是否可能缺少概念页或链接；
- `log/YYYYMMDD.md` 的日期标题和基本结构；
- audit 的必填字段、`severity`、`status`、`source` 枚举；
- open audit 指向的目标文件是否存在；
- 原文页是否同时包含“摘要”和“原文内容”。

渲染结果还应人工检查：

- 概念/实体页“来源依据”是否链接到对应原文页；
- 实体页进入图谱时是否使用 `graph_label`，图谱返回 Wiki 时是否使用注册表中的真实 slug；
- 当前页自引用是否为非链接文本；
- 法律原文的“一、…”和“（一）…”独立章节是否成为语义标题；
- 390px 宽度下正文直接进入首屏，目录抽屉可由遮罩和 Escape 关闭。

lint 不负责页面审美、语义质量评分、重复标题、别名冲突或目录与页面类型的一致性。这些问题
在 `compile` 或 `audit` 中处理，避免把基础健康检查变成重量级语义审查。

### audit：处理反馈

- 已有充分证据：修改目标页面，在 `audit/resolved/` 写 resolved audit 记录修复结果。
- 证据不足或存在冲突：创建 open audit，保留 `quote`、`target`、`severity`、`author`、
  `source`、`created`、`status`，等待后续证据。
- 批量处理：逐个读取 open audit，修复目标页，填写 Resolution，改为 resolved 并移动到
  `audit/resolved/`。
- audit 只要修改了 `wiki-src/`，就必须重新渲染 HTML。

## 4. 渲染、进度和交付检查

渲染会写入 `artifacts/wiki/progress.json`。该文件只记录渲染和报告阶段的进度，至少包含：

- `current_phase`；
- `total_pages`、`completed_pages`、`failed_pages`；
- lint 阶段和 lint 结果；
- `resume_hint`。

渲染中断时，先读取 `progress.json`，再决定从哪个页面或阶段恢复，不要盲目重写全部页面。

交付前读取：

```text
artifacts/wiki/reports/wiki-health.json
artifacts/wiki/reports/wiki-report.json
artifacts/wiki/dist/index.html
```

至少确认：HTML 已生成，链接、索引、audit、log 没有基础结构问题，原文页满足渲染器要求。

纯文档站交付使用：

```bash
evo-wiki generate \
  --root /path/to/workspace \
  --target wiki \
  --json
```

完整平台不要在本 Skill 中手工串联 LightRAG 和导出命令；返回主 Skill，由
`evo-wiki generate` 统一编排。

## 5. 视觉约定

默认样式是轻量的数字图书馆：白色背景、品牌色强调、深色标题、适合中文阅读的正文、固定左侧
导航和可展开的相关链接面板。站点名称、说明、Logo 和主色来自 `wiki.json`。

保持纯静态输出；颜色、字体和布局集中由渲染器管理。视觉质量不属于 `lint-wiki` 的默认判定项，
但新增页面应保持现有导航和阅读体验。

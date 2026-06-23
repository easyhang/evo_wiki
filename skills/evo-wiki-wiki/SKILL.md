---
name: evo-wiki-wiki
summary: 生成、维护、渲染可溯源的静态 Wiki 知识库（Wiki lane）。
description: |
  Evo Wiki 的 Wiki 子 Skill。用于把 corpus/ 原始语料编译成人可读、可探索、可审计的静态 HTML Wiki。
  必须生成 index / concepts / entities / sources 页面；概念页与实体页必须严格基于语料自然归纳，
  不得编造；原文页必须在同一页包含摘要与完整原文，并在原文中加入概念/实体链接。
---

# Evo Wiki · Wiki 子 Skill

## 1. 适用场景

当用户想要：

- “先把资料生成 Wiki”
- “只更新 Wiki，不动 LightRAG”
- “把这些资料整理成可读知识库”
- “审阅/修复/重组 Wiki 页面”
- “生成 learnbuffett 风格的 HTML 样例页”

使用本 Skill。不要在本 Skill 内构建 LightRAG；如果用户要 Agent QA / GraphRAG，转到 `skills/evo-wiki-lightrag/SKILL.md`。

## 2. Wiki lane 边界

Wiki lane 只负责：

```text
workspace/artifacts/wiki/
  wiki-src/
    index.md
    concepts/
    entities/
    sources/
  dist/
  reports/
    wiki-report.json
    wiki-health.json
  progress.json            # 渲染进度检查点，用于断点续处理
  state/
  audit/
  log/
  outputs/
```

禁止：

- 默认把 Wiki 页面作为 LightRAG 输入。
- 在用户未确认时启动 LightRAG 构建。
- 用模型常识补写语料没有支撑的事实。
- 删除或覆盖用户手工编辑区：`<!-- evo:user-edit:start --> ... <!-- evo:user-edit:end -->`。

## 3. 页面类型与强制结构

每次摄入语料后，必须同步考虑并维护五类页面：

1. `index.md`：全局入口 / 索引大厅。
2. `concepts/*.md`：概念页，一个概念一个文件。
3. `entities/*.md`：实体页，人物、工具、论文、组织等。
4. `sources/*.md`：原文页，必须包含「摘要」与「原文内容」，并保留完整原文（摘要直接附在原文页内，不再单独建摘要页）；原文正文中要给已抽取的概念/实体加 `[[wikilink]]`，渲染后右侧会按概念/实体分组展示这些链接及其在原文中的上下文摘录。

导航层级必须保持：

```text
入口 → 概念 → 实体 → 原文 → 其他
```

## 4. 语料约束：自然归纳，禁止编造

概念页和实体页必须基于语料自然归纳，不要把“社区摘要”作为页面卖点或固定标题：

1. **先抽取**：从 `workspace/corpus/` 原始语料抽取候选实体与概念。
2. **再聚类**：根据共现、引用与上下文，把相关实体 / 概念归成主题社区。
3. **后摘要**：综合多个来源中该社区的事实、关系和语境，生成概念页 / 实体页正文。

必须遵守：

- 页面正文不需要单独展示 `## Sources`；证据链可保留在 `frontmatter.sources`、报告和审计文件中。
- 语料没有证据的内容不写；宁可写入 `## 未决问题` 或创建 audit。
- 推断必须显式标注为推断，不得伪装成事实。
- 只覆盖语料讨论到的范围，不扩写成通用百科。
- 主要语言必须与语料一致；中文语料项目中，标题、正文、导航、说明以中文为主。

## 5. 视觉目标：典雅书卷气数字图书馆

构建一个典雅、书卷气的「数字图书馆」式 Wiki 知识库。配色采用三色体系：纸感暖背景（主背景 `#FAF7F2`、次背景 `#F3EDE4`、卡片纯白 `#FFFFFF`、暖色描边 `#E0D6C8`）营造护眼的纸张质感；一个品牌强调色（示例用暗金 `#B8860B` + 亮金 `#D4A843` + 半透明光晕 `rgba(184,134,11,.12)`，可按主题替换为任意主色）用于徽标、强调与 hover 发光；一个深色权威色（藏青 `#1A2332/#2C3E50`）用于标题与深色块；文本走双层级（近黑 `#1B1B18` 正文 + 灰 `#6B6560` 次级）。

字体中英文衬线混排：标题与正文用衬线体（思源宋体 / Crimson Pro / Georgia）传递典籍阅读感，数据与 UI 标签用无衬线体（DM Sans / PingFang SC），通过 Google Fonts 加载 400–900 多字重以拉开排版层级。

布局为「固定左侧多级导航（约 260px）+ 右侧内容区」：左栏按内容大类分组，每组带可折叠箭头与数量徽章（badge）做全站目录与检索；首页作为「索引大厅」门户——顶部 Hero 用大号汇总数字（总量/条目数/交叉链接数）建立规模感，下方用卡片网格平铺核心入口（总览、概念索引、实体索引、人物、下载等）。

内容呈现强调可探索性与可信度：为每个条目标注「被引用/热度计数」把内容量化，提供 TOP 排行榜与首字母头像卡制造榜单式浏览，并突出「交叉链接 + 一键溯源到原文」作为核心叙事。工程层面保持纯静态轻量（单 CSS + 极少 JS，CDN 托管），用 CSS 变量集中管理设计 token 便于换肤。

整体气质用一句话概括：以「暖色纸感背景 + 一个品牌强调色 + 一个深色权威色」配中英文衬线字体，把静态内容库包装成可溯源、可探索的数字图书馆，左栏负责检索、首页用数据徽章与排行榜激发探索欲。

## 6. 页面模板

### 6.1 概念页

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

一句话定义：只写语料中能支撑的定义。

## 摘要

综合语料中围绕该概念的事实、关系与上下文。

## 关键性质

- 只列出语料明确讨论过的性质。

## 相关页面

- [[相关概念]]
- [[相关实体]]

## 未决问题

- 语料证据不足或需要后续查证的问题。

```

### 6.2 实体页

```markdown
---
title: "实体名称"
type: entity
sources:
  - workspace/corpus/raw/source.md
---

# 实体名称

说明这个实体在语料中是什么；不要补充语料之外的传记或背景。

## 摘要

综合语料中该实体的角色、行为、贡献与相关概念。

## 关联概念

- [[概念 A]]

## 未决问题

- 语料没有覆盖、需要后续查证的信息。

```

### 6.3 原文页

````markdown
---
title: "来源文件原文"
type: source
sources:
  - workspace/corpus/raw/source.md
---

# 来源文件原文

## 摘要

用中文概括原文，只引用原文可支撑内容。

## 原文内容

# 在这里粘贴完整原文

必须保留原文全文；如果原文很长，也不要删减。对已抽取的概念和实体，用 `[[概念名]]` / `[[实体名]]` 形成内链。

````

## 7. 操作流程

### ingest：摄入资料

1. 阅读 `workspace/corpus/raw/*` 原文。
2. 创建/更新 `sources/` 原文页（同一页包含摘要 + 完整原文；原文段落中加入概念/实体 `[[wikilink]]`）。
3. 抽取概念并创建/更新 `concepts/` 页面。
4. 抽取实体并创建/更新 `entities/` 页面。
5. 添加 `[[wikilink]]`，尤其要在原文内容中链接已抽取的概念/实体；更新 `index.md`。
6. 运行：

```bash
evo-wiki run --lane wiki
```

8. 读取并解释：

```text
workspace/artifacts/wiki/reports/wiki-report.json
workspace/artifacts/wiki/reports/wiki-health.json
workspace/artifacts/manifest.json
```

### compile：重组 Wiki

当页面过长、重复、链接差或结构混乱时：

1. 扫描 `wiki-src/`。
2. 拆分超长页面、合并重复页面。
3. 重建交叉链接与 `index.md`。
4. 运行：

```bash
evo-wiki lint-wiki
evo-wiki render-wiki
```

### audit：处理反馈

用户指出事实错误或来源缺失时：

- 明确可修复：修改页面并在 `audit/resolved/` 写 resolved audit。
- 不确定：在 `audit/` 写 open audit，等待进一步证据。

## 8. 断点续处理与结束 lint

每次运行 Wiki lane 都会写入：

```text
workspace/artifacts/wiki/progress.json
```

该文件用于断点续处理和故障定位，必须记录：

- 当前阶段 `current_phase`。
- 总页面数 `total_pages`。
- 已完成页面 `completed_pages`。
- 失败页面 `failed_pages`。
- lint 阶段与 lint 结果。
- `resume_hint`，提示下一步从哪里恢复。

如果渲染中断，Claude Code 应先读取 `progress.json`，再决定从哪一页、哪一类检查或哪一阶段继续，不要盲目重写全部页面。

Wiki 结束时必须运行 lint，并读取：

```text
workspace/artifacts/wiki/reports/wiki-health.json
workspace/artifacts/wiki/reports/wiki-report.json
```

lint 必须覆盖：

- 死链与孤儿页。
- 页面是否被 `index.md` 收录。
- 页面类型与目录是否一致。
- 重复标题与概念 / 实体别名冲突。
- 原文页是否包含「摘要」与「原文内容」。
- audit / log 形状是否符合约定。

发现概念冲突时，应优先判断是同义合并、重命名消歧，还是保留两个不同概念并补充上下文说明。

## 9. 脚本与样例

本 Skill 自带：

```text
skills/evo-wiki-wiki/
  scripts/render-example.py          # 重新渲染 HTML 样例
  examples/learnbuffett-style/       # learnbuffett 风格中文 HTML 样例
```

重新生成样例：

```bash
PYTHONPATH=src python3 skills/evo-wiki-wiki/scripts/render-example.py
```

样例入口：

```text
skills/evo-wiki-wiki/examples/learnbuffett-style/site/index.html
```

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

## 7. 操作流程：llm-wiki-demo 式 Skill 化五操作

Wiki lane 采用 `llm-wiki-demo` 的 Skill 化工作法：**用户用自然语言触发操作，Agent 直接维护 Markdown Wiki 源文件，Python 只负责渲染、简单 lint、报告和静态 HTML 交付**。

每次 `ingest / compile / query / lint / audit` 都应在 `workspace/artifacts/wiki/log/YYYYMMDD.md` 中记一笔，格式保持简洁：时间、操作、输入、触达页面、结果摘要。发现不确定事实或结构问题时，优先通过 `audit/` 记录，而不是扩写无证据内容。

**最终渲染要求：** 任何会新增、修改或删除 `wiki-src/` Markdown 页面的操作（尤其是 `ingest / compile / audit`）结束前，必须运行 `evo-wiki render-wiki` 或 `evo-wiki run --lane wiki`，将 Markdown 源渲染成最终静态 HTML，并确认 `workspace/artifacts/wiki/dist/index.html` 与报告已生成。

### 7.1 ingest：摄入资料

触发方式示例：

```text
ingest workspace/corpus/raw/xxx.md
把 corpus/raw 里的新资料编译进 wiki
```

Agent 执行：

1. 阅读指定 `workspace/corpus/raw/*` 原文；不要改写原始语料。
2. 创建/更新 `wiki-src/sources/` 原文页：同一页包含「摘要 + 原文内容」，保留完整原文，并在原文段落中为已抽取概念/实体加入 `[[wikilink]]`。
3. 创建/更新 `wiki-src/concepts/` 概念页：一概念一页，只写语料支撑的内容。
4. 创建/更新 `wiki-src/entities/` 实体页：人物、工具、论文、组织等实体只覆盖语料涉及范围。
5. 更新 `wiki-src/index.md`，确保本次新增/修改页面可从入口页发现。
6. 写入 `log/YYYYMMDD.md`；如发现证据不足或矛盾，写入 `audit/`。
7. 最后必须渲染 Markdown 为静态 HTML：

```bash
evo-wiki render-wiki
# 或完整 lane：evo-wiki run --lane wiki
```

### 7.2 compile：重组 Wiki

触发方式示例：

```text
compile wiki-src/concepts/
重组这些页面，让结构更清楚
```

Agent 执行：

1. 读取 `wiki-src/` 当前结构，识别过长页面、重复页面、孤立页面、链接不足页面。
2. 拆分超长页面，合并明显重复内容；保留用户手工编辑区。
3. 重建 `[[wikilink]]`，让概念、实体、原文页互相可达。
4. 更新 `index.md` 与相关页面的「相关页面 / 未决问题」。
5. 对不确定的合并、冲突或事实问题创建 audit，而不是自行定论。
6. 写入 log；最后必须运行 `evo-wiki render-wiki`，将 Markdown 重组结果渲染成 HTML。

### 7.3 query：基于 Wiki 回答

触发方式示例：

```text
这个 wiki 里怎么解释 X？
```

Agent 执行：

1. 只搜索 `workspace/artifacts/wiki/wiki-src/` 中已有页面。
2. 严格基于 Wiki 内容回答；Wiki 没覆盖就明确说明，不用模型常识补齐。
3. 用 `[[wikilink]]` 或文件路径指出依据页面。
4. 有复用价值的问答可保存到 `workspace/artifacts/wiki/outputs/queries/<slug>.md`。
5. 回答时发现缺口、矛盾或过期信息，创建 self-audit。
6. 写入 log。

### 7.4 lint：健康检查

触发方式示例：

```bash
evo-wiki lint-wiki
```

lint 保持 `llm-wiki-demo` 的轻量模型，只检查维护 Wiki 所需的基础健康项；除 HTML 渲染必需项外，不做语义级或审美级判断。

必须覆盖：

1. 死链：`[[wikilink]]` 指向不存在页面。
2. 孤儿页：页面没有入链。
3. `index.md` 收录：页面是否能从入口发现。
4. 潜在未链接概念：高频候选词但没有页面或链接。
5. log 形状：`log/YYYYMMDD.md` 与日期标题。
6. audit 形状：必填字段、severity/status/source 枚举。
7. audit target：open audit 指向的目标文件是否存在。
8. HTML 必需项：`sources/*.md` 是否包含「摘要」与「原文内容」，以支持原文页渲染和右侧相关面板。

不再把以下内容作为 lint/health 的默认职责：页面类型与目录是否一致、重复标题、概念/实体别名冲突、页面审美、内容质量评分。发现这些问题时，Agent 可以在 compile/audit 中处理，但不应让健康检查变重。

### 7.5 audit：处理反馈

用户指出事实错误、来源缺失、结构问题时：

- 明确可修复：修改目标页面，并在 `audit/resolved/` 写 resolved audit 作为记录。
- 不确定：在 `audit/` 写 open audit，保留 quote、target、severity、author、source、created、status，等待进一步证据。
- 批量处理：读取所有 open audit，逐个修复目标页、填写 Resolution、改为 resolved 并移动到 `audit/resolved/`。
- 只要 audit 处理修改了 `wiki-src/` 页面，最后必须运行 `evo-wiki render-wiki`，确保 HTML 与 Markdown 源同步。

## 8. 渲染进度与报告

Python 渲染仍会写入：

```text
workspace/artifacts/wiki/progress.json
```

该文件只描述渲染/报告阶段的进度，用于断点续处理和故障定位，必须记录：

- 当前阶段 `current_phase`。
- 总页面数 `total_pages`。
- 已完成页面 `completed_pages`。
- 失败页面 `failed_pages`。
- lint 阶段与 lint 结果。
- `resume_hint`，提示下一步从哪里恢复。

如果渲染中断，Claude Code 应先读取 `progress.json`，再决定从哪一页、哪一类检查或哪一阶段继续，不要盲目重写全部页面。

Wiki 渲染结束时读取：

```text
workspace/artifacts/wiki/reports/wiki-health.json
workspace/artifacts/wiki/reports/wiki-report.json
```

向用户解释时只报告：HTML 是否生成、链接/索引/audit/log 是否有基础健康问题、原文页结构是否满足 HTML 需要，以及下一步建议。

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

# Evo Wiki 2.0 开发标准

本标准定义 Evo Wiki 2.0 新建知识平台的内容、链接和前端渲染契约。目标是让不同语料、不同
Agent 和不同 LightRAG 服务生成的平台具有一致、可检查、可安全降级的行为。

## 1. 内容建模

Wiki 保持四类页面：入口、概念、实体和来源。`index.md` 必须能发现所有交付页面；来源页必须
包含 `## 摘要` 和 `## 原文内容`，摘要不能替代完整原文。

每个 corpus 文件必须由唯一来源页声明：

```yaml
---
title: "来源标题"
type: source
sources:
  - corpus/raw/path/source.txt
---
```

`sources` 使用 workspace 内、以 `corpus/` 开头的规范相对路径。公共链接按 Unicode NFKC 和
大小写归一后的 basename 匹配；因此 corpus 中 basename 冲突必须先通过重命名消除。

实体页使用以下最小契约：

```yaml
---
title: "实体名称"
type: entity
graph_label: "LightRAG 中的精确 label"
aliases:
  - "受当前语料确认的别名"
sources:
  - corpus/raw/path/source.txt
---
```

`graph_label` 必须唯一。标题或别名冲突不会阻断构建，但冲突词永不自动链接。

## 2. 构建与质量门禁

新建 workspace 的 `wiki.json` 写入 `content_contract_version: 2`。现有文件缺少该字段时继续按
契约 v1 运行，不会被工具自动改写。

契约 v2 的 error 会阻断 Wiki lane 和后续 LightRAG 写入，包括：

- corpus 文件没有唯一来源页；
- corpus basename 冲突或一个 basename 映射多个来源页；
- 来源页缺少规范 frontmatter、摘要或完整原文；
- 页面未被首页发现、Wikilink 失效或 graph label 重复。

孤立页候选、实体词歧义等保留为 warning。`wiki-health.json` 与 `wiki-report.json` 记录契约版本、
语料覆盖率、来源映射数、实体映射数和歧义统计。

## 3. 问答到 Wiki 的链接

结构化 `citations` 是唯一可信引用输入。模型正文中的 References 只用于显示清理，不能创建
证据链接。引用卡只有在 citation 的 source basename 唯一命中 `wiki-registry.json` 时可点击；
任何缺失、冲突或非法路径都降级为普通卡片。

回答正文的实体链接必须同时满足：

1. 实体存在于 registry，Wiki 路径通过白名单；
2. 名称或别名全局唯一；
3. 本次 citation 映射的来源声明了该实体 graph label；
4. 不是裸匿名名称；
5. 当前回答尚未链接过该实体。

代码、已有链接、URL、引用编号和原始 HTML 不参与实体替换。问答结果不附加知识子图；图谱是
独立浏览能力。

## 4. 安全 Markdown

所有问答入口和审核详情复用同一安全渲染器。支持标题、段落、软换行、强调、删除线、列表、
任务项、引用、表格、分隔线、行内代码和 fenced code。

- 普通文本先转义，再生成受控 HTML；原始 HTML 永不执行。
- 只允许 `http`/`https` Markdown 外链；其他协议保持普通文本。
- Markdown 图片不创建 `img`，仅显示图像文字提示，避免远程跟踪和混合内容。
- 表格与代码块必须在窄屏容器内滚动，不能撑破问答卡片。
- 未闭合或不支持的 Markdown 保持可读文本，不猜测补全。

## 5. 状态和失败降级

问答页面只在当前标签页的 `sessionStorage` 保存成功回答、裁剪后的结构化引用、最近对话、草稿
和检索参数。恢复时重新运行安全渲染，不保存或回放 HTML。失败结果、加载状态、审核详情和密钥
不得持久化；清空操作同时清除会话快照。

registry 缺失、损坏或路径非法时，回答正文和引用内容仍显示，但链接关闭。Markdown 解析失败
不得影响查询状态、证据状态或审核状态。

## 6. 交付验收

交付前至少完成：

```bash
evo-wiki lint-wiki --root /path/to/workspace
evo-wiki render-wiki --root /path/to/workspace
evo-wiki generate --root /path/to/workspace --dry-run --json
```

随后检查 `wiki-health.json` 的 source coverage 为 1、生成的 JavaScript 语法、桌面与 390px
布局、问答到来源 Wiki 再返回的会话恢复，以及 registry 异常、恶意 Markdown 和缺失内容的降级。

# LearnBuffett 风格 HTML 样例

本目录把原 `references/style-samples/` 与 `examples/` 合并为一个完整样例：既包含原始语料 `corpus/raw/`，也包含 Wiki 源文件 `artifacts/wiki/wiki-src/` 和可直接打开的 HTML 成品 `site/`。

视觉风格参考 [learnbuffett.com](https://learnbuffett.com/)：暖色纸张底 + navy 侧栏 + 金色点缀 + 衬线标题的「典藏书卷」气质。该风格已固化进 `src/evo_wiki/wiki.py` 的 `STYLE` 与 `page_template`，所以 `evo-wiki render-wiki` 默认产出即为此风格。

## 目录结构

```text
skills/evo-wiki-wiki/examples/learnbuffett-style/
  README.md
  corpus/raw/                         # 样例原始语料（中文）
    sample-notes.md
    1986-letter.md
    berkshire-notes.md                # 组织与策略分析（较长语料）
  artifacts/wiki/wiki-src/            # Wiki Markdown 源
    index.md                          # 入口页
    concepts/                         # 概念页：护城河、内在价值
    entities/                         # 实体页：沃伦·巴菲特（人物）、伯克希尔·哈撒韦（组织）
    sources/                          # 原文页：摘要 + 原文全文（含短/长两种示例）
  site/                               # 已渲染 HTML 成品，可直接打开
    index.html
    concepts/*.html
    entities/*.html
    sources/*.html
    assets/style.css
    assets/app.js
    search-index.json
```

直接在浏览器打开 `site/index.html` 即可预览（字体、Mermaid、KaTeX 走 CDN，需联网）。

## 样例内容要求

- **导航层级**：入口 → 概念 → 实体 → 原文，侧栏按同样层级分组展示，分组可折叠。
- **原文页**：`sources/*.md` 必须由「摘要」和「原文内容」组成，且必须保留完整原文（摘要直接附在原文页内）；原文段落中为概念/实体加入 `[[wikilink]]`，右侧面板会按概念/实体分组展示这些链接，并可展开查看原文中的上下文摘录。
- **语料约束**：概念页、实体页均严格基于 `corpus/raw/`，不使用模型常识补充未在语料中出现的事实。
- **语言一致**：本样例语料、页面标题、正文、导航与说明均以中文为主。

## 页面变体

为帮助 Agent 面对不同语料时稳定产出正确结构的页面，本样例演示了以下模板变体：

| 页面类型 | 变体 1（已有） | 变体 2（新增） | 说明 |
|---|---|---|---|
| 概念页 | `intrinsic-value.md`（纯文本） | `moat.md`（含 Mermaid 图） | 展示有/无图表的两种概念页 |
| 实体页 | `warren-buffett.md`（人物类） | `berkshire-hathaway.md`（组织类） | 组织实体不写人物专属措辞 |
| 原文页 | `1986-letter.md`（短原文） | `berkshire-notes.md`（长原文） | 长原文中 wikilink 分散在多个段落，右侧面板可跨段提取上下文摘录 |

## 设计系统（Design Tokens）

| 角色 | 值 |
|---|---|
| 页面底 `--bg` | `#FFFFFF` |
| 次级底 `--bg2` | `#F8FAFC` |
| 正文 `--text` | `#111111` |
| 次级文字 `--text2` | `#64748B` |
| 蓝色 `--blue` | `#2563EB` |
| 浅蓝 `--blue-light` | `#3B82F6` |
| 标题/深色 `--navy` | `#111111` |
| 深色浅 `--navy-light` | `#374151` |
| 浅蓝（引用底） `--cream` | `#EFF6FF` |
| 边框 `--border` | `#E2E8F0` |
| 卡片 `--card` | `#FFFFFF` |
| 链接 `--link` | `#2563EB` |
| 衬线字族 | `Noto Serif SC, Crimson Pro, Georgia, serif` |
| 无衬线字族 | `DM Sans, -apple-system, PingFang SC, sans-serif` |

## 关键版式规则

- **布局**：左侧 260px 固定白色侧栏（右侧 1px 边框）；正文 `.article` 居中、最大宽 820px、`padding:48px`；原文页可带右侧相关链接面板。
- **侧栏导航**：按 `入口 / 概念 / 实体 / 摘要 / 原文 / 其他` 分组；每组可折叠，当前页用蓝色左边框 + 高亮底。
- **标题**：全部用衬线字体、黑色 `#111`；`h1` 900 字重 30px；`h2` 21px 带 2px 底边线；`h3` 同为黑色。
- **引用块**：浅蓝底、蓝色左边框、衬线斜体，营造「书摘」感。
- **wikilink**：蓝色下划线高亮，失效链接 `.missing` 为灰色删除线。
- **类型徽标**：概念、实体、摘要、原文使用不同颜色徽标。
- **原文内容**：用普通 Markdown 段落保留原文，并在原文中为概念/实体加入 `[[wikilink]]`，以便右侧面板自动聚合。

## 如何重新生成 HTML 样例

```bash
# 在临时项目里渲染 skills/evo-wiki-wiki/examples/learnbuffett-style/artifacts/wiki/wiki-src，
# 并将 dist 复制回 skills/evo-wiki-wiki/examples/learnbuffett-style/site/
PYTHONPATH=src python3 - <<'PY'
import shutil, tempfile
from pathlib import Path
from evo_wiki.paths import ProjectPaths
from evo_wiki.config import EvoConfig
from evo_wiki.wiki import render_wiki

repo = Path.cwd()
example = repo / "skills/evo-wiki-wiki/examples/learnbuffett-style"
src = example / "artifacts/wiki/wiki-src"
out = example / "site"
tmp = Path(tempfile.mkdtemp())
p = ProjectPaths.from_root(tmp); p.ensure_base_dirs()
for md in src.rglob("*.md"):
    d = p.wiki_src / md.relative_to(src); d.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(md, d)
cfg = EvoConfig(); cfg.wiki = dict(cfg.wiki); cfg.wiki["title"] = "巴菲特知识库（样例）"
report = render_wiki(p, cfg)
if report["health"]["issue_count"] != 0:
    raise SystemExit(report["health"])
if out.exists(): shutil.rmtree(out)
shutil.copytree(p.wiki_dist, out)
shutil.rmtree(tmp, ignore_errors=True)
PY
```

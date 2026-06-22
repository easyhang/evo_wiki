# Wiki 风格参考 · learnbuffett 风格

本目录是 Evo wiki 的**视觉风格参考**，用于让 Claude Code 在生成 Wiki 时对齐目标观感。
风格取自 [learnbuffett.com](https://learnbuffett.com/)（巴菲特致股东信中文知识库）：
暖色纸张底 + navy 侧栏 + 金色点缀 + 衬线标题的「典藏书卷」气质。

> 该风格已固化进渲染器 `src/evo_wiki/wiki.py` 的 `STYLE` 与 `page_template`，
> 所以 `evo-wiki render-wiki` 默认产出即为此风格。本目录提供**可直接打开的成品样例**。

## 目录结构

```text
references/style-samples/
  README.md            # 本文件
  wiki-src/            # 渲染所用的 Markdown 源（也是页面结构范例）
    index.md           # 入口/索引页
    concepts/          # 概念页：护城河、内在价值
    entities/          # 实体页：沃伦·巴菲特
    summaries/         # 摘要页：1986 致股东信
  site/                # 用更新后的渲染器渲染出的静态 HTML 成品样例
    index.html
    concepts/*.html
    entities/*.html
    summaries/*.html
    assets/style.css
    assets/app.js
    search-index.json
```

直接在浏览器打开 `site/index.html` 即可预览（字体、Mermaid、KaTeX 走 CDN，需联网）。

## 设计系统（Design Tokens）

| 角色 | 值 |
|---|---|
| 纸张底 `--bg` | `#FAF7F2` |
| 次级底 `--bg2` | `#F3EDE4` |
| 正文 `--text` | `#1B1B18` |
| 次级文字 `--text2` | `#6B6560` |
| 金色 `--gold` | `#B8860B` |
| 浅金 `--gold-light` | `#D4A843` |
| 藏青侧栏 `--navy` | `#1A2332` |
| 藏青浅 `--navy-light` | `#2C3E50` |
| 奶油（引用底） `--cream` | `#FFF8EE` |
| 边框 `--border` | `#E0D6C8` |
| 卡片 `--card` | `#FFFFFF` |
| 链接 `--link` | `#8B5E0B` |
| 衬线字族 | `Noto Serif SC, Crimson Pro, Georgia, serif` |
| 无衬线字族 | `DM Sans, -apple-system, PingFang SC, sans-serif` |

## 关键版式规则

- **布局**：左侧 260px 固定 navy 侧栏；正文 `.article` 居中、最大宽 820px、`padding:48px`。
- **侧栏导航**：`.nav-group` 分组（入口/概念/实体/摘要），`.nav-link` 当前页用金色左边框 + 高亮底。
- **标题**：全部用衬线字体、navy 色；`h1` 900 字重 30px；`h2` 21px 带 2px 底边线；`h3` navy-light。
- **引用块**：奶油底、金色左边框、衬线斜体，营造「书摘」感。
- **wikilink**：金色下划线高亮（gradient），失效链接 `.missing` 为灰色删除线。
- **类型徽标**：页面顶部 `.type-badge` 按类型着色（概念=棕、实体=青、摘要=绿、索引=灰）。
- **代码块**：navy 底浅色字；行内 `code` 用 `--bg2` 底 + 金棕字。

## 如何重新生成样例

样例由仓库内的渲染器直接产出（保证与真实输出一致）：

```bash
# 在临时项目里渲染 references/style-samples/wiki-src，并将 dist 复制回 site/
PYTHONPATH=src python3 - <<'PY'
import shutil, tempfile
from pathlib import Path
from evo_wiki.paths import ProjectPaths
from evo_wiki.config import EvoConfig
from evo_wiki.wiki import render_wiki

repo = Path.cwd()
src = repo / "references/style-samples/wiki-src"
out = repo / "references/style-samples/site"
tmp = Path(tempfile.mkdtemp())
p = ProjectPaths.from_root(tmp); p.ensure_base_dirs()
for md in src.rglob("*.md"):
    d = p.wiki_src / md.relative_to(src); d.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(md, d)
cfg = EvoConfig(); cfg.wiki = dict(cfg.wiki); cfg.wiki["title"] = "巴菲特知识库（样例）"
render_wiki(p, cfg)
if out.exists(): shutil.rmtree(out)
shutil.copytree(p.wiki_dist, out)
shutil.rmtree(tmp, ignore_errors=True)
PY
```

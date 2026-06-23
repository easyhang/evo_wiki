# Evo wiki

**Evo wiki 是面向 Claude Code 的 LLM Wiki 知识平台开发工具。**

它不是 CLI-first 的传统工具箱，而是 Claude Code-first / AI-native 的开发工具：用户用自然语言描述目标，Claude Code 负责决策与内容生成，Python 工具负责可重复、可验证、可恢复的底层动作。

## 首版 MVP 能力

- `wiki` lane：吸收 `llm-wiki-demo` 的可演进 Markdown Wiki 做法，由 Claude Code 维护 `index + concepts/entities/summaries/sources + audit/log`，再由 Python 渲染为最终静态 HTML 页面。
- `lightrag` lane：从 `corpus/` 准备 LightRAG 输入，并通过 `lightrag-hku` 直接构建 workspace。
- `artifacts` 协议：写入顶层 `manifest.json`、lane manifest、reports、state、agent plan/summary。
- 增量基础：扫描 corpus hash，输出 added / modified / deleted change set。
- Docker 导出：按已存在产物导出 Wiki / LightRAG Dockerfile 与 compose。
- 完全分离：Wiki 与 LightRAG 可以独立运行、独立更新、独立部署。

## Skill 拆分

Evo Wiki 现在采用主 Skill + 两个 lane 子 Skill 的结构：

| Skill | 职责 | 目录 |
|---|---|---|
| 主 Skill | 只做目标判断、lane 路由与边界说明 | `SKILL.md` |
| Wiki 子 Skill | Wiki 写作、页面结构、HTML 样例、渲染脚本 | `skills/evo-wiki-wiki/` |
| LightRAG 子 Skill | LightRAG 输入准备、dry-run、构建与删除重建安全协议 | `skills/evo-wiki-lightrag/` |

两个子 Skill 都有独立的 `SKILL.md`、`scripts/` 与 `examples/`。

## 安装

```bash
cd workspace/evo-wiki
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

> `pyproject.toml` 直接依赖 `lightrag-hku`。真实构建 LightRAG 时，还需要按 LightRAG 的要求配置 LLM / embedding 环境变量。

## 运行数据目录

为避免 `corpus/`、`artifacts/`、`project.json`、`wiki.json` 等运行数据和工具源码混在一起，Evo wiki 默认把所有项目运行数据放入当前工程的：

```text
workspace/
```

也就是说，在 `workspace/evo-wiki` 工具目录内直接运行：

```bash
evo-wiki init
```

会创建：

```text
workspace/
  corpus/
  artifacts/
  project.json
  wiki.json
```

如果你要操作其他项目，可以显式指定：

```bash
evo-wiki init --root /path/to/project-workspace
```

## 快速开始

```bash
cd workspace/evo-wiki
evo-wiki init
cp /path/to/source.md workspace/corpus/raw/
```

### 只生成 Wiki

1. 让 Claude Code 基于 `workspace/corpus/` 生成或更新 Markdown Wiki 源文件：

```text
workspace/artifacts/wiki/wiki-src/
  index.md
  concepts/
  entities/
  summaries/
  sources/
```

其中：

- `index.md` 是全局入口。
- `concepts/` 放概念页，一个概念一个文件。
- `entities/` 放人物、工具、论文、组织等实体页。
- `summaries/` 放每个原始资料的摘要页。
- `sources/` 放原文页，每页必须由「摘要」和「原文内容」组成，并保留完整原文。
- 导航层级保持：入口 → 概念 → 实体 → 摘要 → 原文 → 其他。
- 页面之间用 `[[wikilink]]` 交叉引用。
- 每页用 `## Sources` 标注来源。
- 概念页、实体页必须严格基于语料做 community summary，不使用模型常识编造。
- 主要语言必须与语料保持一致；中文语料项目中，页面、提示与说明以中文为主。

2. 调用 Python 渲染：

```bash
evo-wiki run --lane wiki
```

输出：

```text
workspace/artifacts/wiki/dist/index.html
workspace/artifacts/wiki/dist/search-index.json
workspace/artifacts/wiki/reports/wiki-report.json
workspace/artifacts/wiki/reports/wiki-health.json
workspace/artifacts/wiki/state/wiki-dependency-graph.json
```

### 只生成 LightRAG

```bash
evo-wiki run --lane lightrag
```

输出：

```text
workspace/artifacts/lightrag/input/documents.jsonl
workspace/artifacts/lightrag/workspace/
workspace/artifacts/lightrag/reports/lightrag-report.json
workspace/artifacts/lightrag/state/lightrag-import-ledger.json
```

如果当前环境未配置 LightRAG 所需 LLM / embedding，命令会失败并写入失败报告。可以先 dry-run：

```bash
evo-wiki run --lane lightrag --lightrag-dry-run
```

### 推荐路径：先 Wiki，后 LightRAG

```bash
evo-wiki run --lane wiki
# 用户审阅 workspace/artifacts/wiki/dist/index.html
# 确认后：
evo-wiki run --lane lightrag
```

### 同时运行两条 lane

```bash
evo-wiki run --lane both
```

注意：即使同时运行，两条 lane 的产物、状态、报告仍然分离。

## HTML 样例

完整样例已合并到：

```text
skills/evo-wiki-wiki/examples/learnbuffett-style/
```

其中包含中文原始语料 `corpus/raw/`、Wiki 源文件 `artifacts/wiki/wiki-src/`，以及参考 [learnbuffett.com](https://learnbuffett.com/) 风格渲染出的 HTML 成品 `site/`。直接打开 `skills/evo-wiki-wiki/examples/learnbuffett-style/site/index.html` 即可查看页面样例。

该样例演示：

- 导航层级：入口 → 概念 → 实体 → 摘要 → 原文。
- 原文页：由「摘要」和「原文内容」组成，并保留完整原文。
- 概念页 / 实体页：严格基于语料做 community summary，不编造语料外事实。
- 主要语言保持中文一致。

## 命令

| 命令 | 作用 |
|---|---|
| `evo-wiki init` | 初始化项目目录、默认配置和 wiki-src 占位页 |
| `evo-wiki scan` | 扫描 corpus，输出增量 change set |
| `evo-wiki render-wiki` | 渲染 Markdown Wiki 为静态 HTML |
| `evo-wiki lint-wiki` | 对 wiki-src/audit/log 做健康检查，输出 `wiki-health.json` |
| `evo-wiki prepare-lightrag` | 生成 LightRAG 输入包 |
| `evo-wiki build-lightrag` | 调用 `lightrag-hku` 构建 workspace |
| `evo-wiki run --lane wiki|lightrag|both` | 编排运行一个或两个 lane |
| `evo-wiki export-docker` | 导出 Docker 交付物 |
| `evo-wiki inspect` | 查看 manifest 和报告 |

## 项目结构

```text
evo-wiki/
  src/                    # 工具代码
  tests/                  # 测试
  SKILL.md                # 主路由 Skill：只说明 Wiki / LightRAG 子 Skill 如何使用
  skills/
    evo-wiki-wiki/        # Wiki lane 子 Skill（SKILL.md / scripts / examples）
    evo-wiki-lightrag/    # LightRAG lane 子 Skill（SKILL.md / scripts / examples）
  README.md

  workspace/              # 默认运行数据根目录，和工具代码分开
    corpus/
      raw/
      assets/
    project.json
    wiki.json
    artifacts/
      manifest.json
      agent/
        evo-plan.md
        delta-plan.json
        run-summary.md
      wiki/
        wiki-src/
          index.md
          concepts/
          entities/
          summaries/
          sources/
        audit/
          resolved/
        log/
        outputs/
          queries/
        dist/
        reports/wiki-report.json
        reports/wiki-health.json
        state/wiki-dependency-graph.json
      lightrag/
        input/documents.jsonl
        workspace/
        reports/lightrag-report.json
        state/lightrag-import-ledger.json
      docker/
```

## Claude Code 与 Python 的分工

Claude Code：

- 理解用户目标。
- 判断本次是 Wiki-only、LightRAG-only，还是 Wiki-first。
- 规划 Wiki 页面结构，包含概念页、实体页、摘要页与原文页。
- 基于原始语料生成或更新 `workspace/artifacts/wiki/wiki-src/*.md`，其中概念页/实体页必须做基于语料的 community summary，原文页必须保留完整原文。
- 阅读 reports 并向用户解释风险。

Python：

- 维护目录结构。
- 扫描 corpus 和 change set。
- 渲染 Markdown 为 HTML。
- 生成搜索索引和报告。
- 准备并调用 LightRAG。
- 导出 Docker 交付物。

## 设计边界

- Wiki 不默认作为 LightRAG 入库源。
- LightRAG 默认从 `workspace/corpus/` / normalized input 建库。
- Wiki 与 LightRAG 不共享索引、不共享状态、不要求一起构建。
- Python 首版不负责 LLM 写作，内容生成由 Claude Code 完成。

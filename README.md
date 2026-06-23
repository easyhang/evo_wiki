# Evo wiki

**Evo wiki 是面向 Claude Code 的 LLM Wiki 知识平台开发工具。**

它不是 CLI-first 的传统工具箱，而是 Claude Code-first / AI-native 的开发工具：用户用自然语言描述目标，Claude Code 负责决策与内容生成，Python 工具负责可重复、可验证、可恢复的底层动作。

## 首版 MVP 能力

- `wiki` lane：吸收 `llm-wiki-demo` 的可演进 Markdown Wiki 做法，由 Claude Code 维护 `index + concepts/entities/sources + audit/log`，再由 Python 渲染为最终静态 HTML 页面。
- `lightrag` lane：从 `corpus/` 准备 LightRAG 输入，并提交到一个已有的 LightRAG Server 服务。
- `artifacts` 协议：写入顶层 `manifest.json`、lane manifest、reports、state、agent plan/summary。
- 增量基础：扫描 corpus hash，输出 added / modified / deleted change set。
- Docker 导出：按已存在产物导出 Wiki Dockerfile / compose，并提供外部 LightRAG 服务配置样例。
- 完全分离：Wiki 与 LightRAG 可以独立运行、独立更新、独立部署。

## Skill 拆分

Evo Wiki 现在采用主 Skill + 两个 lane 子 Skill 的结构：

| Skill | 职责 | 目录 |
|---|---|---|
| 主 Skill | 只做目标判断、lane 路由与边界说明 | `SKILL.md` |
| Wiki 子 Skill | Wiki 写作、页面结构、HTML 样例、渲染脚本 | `skills/evo-wiki-wiki/` |
| LightRAG 子 Skill | LightRAG 输入准备、dry-run、提交到已有服务与删除重建安全协议 | `skills/evo-wiki-lightrag/` |

两个子 Skill 都有独立的 `SKILL.md`、`scripts/` 与 `examples/`。

## 安装

```bash
cd workspace/evo-wiki
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

> Evo Wiki 不再在本进程内安装或启动 `lightrag-hku`。真实写入 LightRAG 时，需要先准备一个已运行的 LightRAG Server；默认地址是 `http://127.0.0.1:9621`，可通过 `project.json` 的 `lightrag.base_url` 或环境变量 `LIGHTRAG_BASE_URL` 覆盖。若服务启用了 API key 或登录鉴权，分别设置 `LIGHTRAG_API_KEY` 或 `LIGHTRAG_BEARER_TOKEN`。

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

Wiki lane 采用 `llm-wiki-demo` 式 Skill 化工作法：**Claude Code 通过自然语言操作 Markdown Wiki 源文件，Python CLI 只负责渲染、轻量 lint、报告和静态 HTML 交付**。

1. 把资料放入：

```text
workspace/corpus/raw/
```

2. 让 Claude Code 执行 Skill 操作，例如：

```text
ingest workspace/corpus/raw/source.md
compile workspace/artifacts/wiki/wiki-src/concepts/
这个 wiki 里怎么解释 X？
处理所有 open audit
```

Claude Code 维护：

```text
workspace/artifacts/wiki/wiki-src/
  index.md
  concepts/
  entities/
  sources/
workspace/artifacts/wiki/audit/
workspace/artifacts/wiki/log/
workspace/artifacts/wiki/outputs/queries/
```

其中：

- `index.md` 是全局入口。
- `concepts/` 放概念页，一个概念一个文件。
- `entities/` 放人物、工具、论文、组织等实体页。
- `sources/` 放原文页，每页必须由「摘要」和「原文内容」组成，并保留完整原文；摘要直接附在原文页内，不再单独建摘要页；原文段落中要为已抽取概念/实体加入 `[[wikilink]]`。
- `audit/` 是反馈队列；明确修复的问题归档到 `audit/resolved/`。
- `log/` 记录每次 `ingest / compile / query / lint / audit` 操作。
- `outputs/queries/` 可保存有复用价值的 Wiki 问答。
- 页面之间用 `[[wikilink]]` 交叉引用。
- 概念页、实体页必须严格基于语料做自然摘要，不使用模型常识编造。
- 主要语言必须与语料保持一致；中文语料项目中，页面、提示与说明以中文为主。

3. 需要 HTML 预览或交付时调用 Python 渲染：

```bash
evo-wiki run --lane wiki
# 或只渲染：evo-wiki render-wiki
```

输出：

```text
workspace/artifacts/wiki/dist/index.html
workspace/artifacts/wiki/dist/search-index.json
workspace/artifacts/wiki/reports/wiki-report.json
workspace/artifacts/wiki/reports/wiki-health.json
workspace/artifacts/wiki/state/wiki-dependency-graph.json
```

### 只更新 LightRAG 服务

先确认已有 LightRAG Server 正在运行：

```bash
export LIGHTRAG_BASE_URL=http://127.0.0.1:9621
# 如服务启用了 API key：
# export LIGHTRAG_API_KEY=...
# 如服务使用登录后获得的 Bearer token：
# export LIGHTRAG_BEARER_TOKEN=...
```

然后提交当前语料：

```bash
evo-wiki run --lane lightrag
```

输出：

```text
workspace/artifacts/lightrag/input/documents.jsonl
workspace/artifacts/lightrag/reports/lightrag-report.json
workspace/artifacts/lightrag/state/lightrag-import-ledger.json
workspace/artifacts/lightrag/queries/smoke-test.json   # 仅传入 --smoke-query 时
```

如果只是检查哪些文档会提交到服务，可以先 dry-run：

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

- 导航层级：入口 → 概念 → 实体 → 原文；左侧分组可折叠。
- 原文页：由「摘要」和「原文内容」组成，并保留完整原文；原文中要插入概念/实体 `[[wikilink]]`，渲染后右侧展示这些链接。
- 概念页 / 实体页：严格基于语料做自然摘要，不编造语料外事实。
- 主要语言保持中文一致。

## 命令

| 命令 | 作用 |
|---|---|
| `evo-wiki init` | 初始化项目目录、默认配置和 wiki-src 占位页 |
| `evo-wiki scan` | 扫描 corpus，输出增量 change set |
| `evo-wiki render-wiki` | 渲染 Markdown Wiki 为静态 HTML |
| `evo-wiki lint-wiki` | 对 wiki-src/audit/log 做健康检查，输出 `wiki-health.json` |
| `evo-wiki prepare-lightrag` | 生成 LightRAG 输入包 |
| `evo-wiki build-lightrag` | 调用已有 LightRAG Server API 提交输入文档 |
| `evo-wiki run --lane wiki|lightrag|both` | 编排运行一个或两个 lane |
| `evo-wiki export-docker` | 导出 Wiki Docker 交付物与外部 LightRAG 服务配置样例 |
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
          sources/
        audit/
          resolved/
        log/
        outputs/
          queries/
        dist/
        progress.json
        reports/wiki-report.json
        reports/wiki-health.json
        state/wiki-dependency-graph.json
      lightrag/
        input/documents.jsonl
        reports/lightrag-report.json
        state/lightrag-import-ledger.json
        queries/
      docker/
```

## Claude Code 与 Python 的分工

Claude Code：

- 理解用户目标，判断本次是 Wiki-only、LightRAG-only，还是 Wiki-first。
- 按 `llm-wiki-demo` 式 Skill 操作维护 Wiki：`ingest / compile / query / lint / audit`。
- 基于原始语料生成或更新 `workspace/artifacts/wiki/wiki-src/*.md`，其中概念页/实体页必须做基于语料的自然摘要，原文页必须在同一页保留「摘要 + 原文内容」，并在原文中为已抽取概念/实体加入 `[[wikilink]]`。
- 维护 `index.md`、`audit/`、`log/`、`outputs/queries/`；发现不确定事实时写 audit，不用模型常识补齐。
- 读取 `wiki-health.json` / `wiki-report.json`，只向用户解释基础健康问题、HTML 生成状态和下一步建议。

Python：

- 维护目录结构。
- 扫描 corpus 和 change set。
- 渲染 Markdown 为静态 HTML。
- 生成搜索索引、依赖图、进度文件和报告。
- 执行轻量健康检查：demo 风格链接/索引/audit/log 检查 + HTML 必需的原文页结构检查。
- 准备 LightRAG 输入并调用已有 LightRAG Server API。
- 导出 Docker 交付物。

## 设计边界

- Wiki 不默认作为 LightRAG 入库源。
- LightRAG 默认从 `workspace/corpus/` / normalized input 提交到已有 LightRAG 服务。
- Wiki 与 LightRAG 不共享索引、不共享状态、不要求一起运行或一起部署。
- Python 首版不负责 LLM 写作，内容生成由 Claude Code 完成。

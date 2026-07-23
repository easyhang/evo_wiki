# evo_wiki 环境与项目结构检查

## 环境信息

【实测结果】

- 工作目录：`/Users/vincentxing/Downloads/Open_WIKI/evo_wiki`
- 实验目录：`experiment/evo_wiki_test/`
- Python：`Python 3.13.2`
- Node：`v25.8.2`
- CLI：`.venv/bin/evo-wiki`
- 安装方式：仓库 `STATUS.md` 记录为 editable install：`.venv/bin/python -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -e '.[dev]'`
- 运行依赖：`pyproject.toml` 的 `[project].dependencies = []`
- 开发依赖：`pytest>=8.0`
- CLI sanity：`.venv/bin/evo-wiki --help` 成功，支持 `init / scan / render-wiki / lint-wiki / prepare-lightrag / build-lightrag / export-platform / inspect / run`
- 测试：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest`，结果 `23 passed in 1.22s`
- Git 工作区执行前已有非本次改动：`M tests/test_fixes.py`、`?? STATUS.md`；本评估未修改源码和测试文件。

## 测试数据

【实测结果】

- 来源目录：`/Users/vincentxing/Downloads/Open_WIKI/openwiki_legal_evaluation/test_data/legal_docs/`
- 实验副本：`experiment/evo_wiki_test/corpus/raw/legal_docs/`
- 文件数量：9
- 文件格式：`.txt`
- 总字节数：115345
- 编码：`file -I` 检测均为 `text/plain; charset=utf-8`
- 逐文件大小、SHA256、MIME：见 `source_stats/file_inventory.tsv`

## 启动命令

【实测结果】

```bash
.venv/bin/evo-wiki init --root experiment/evo_wiki_test
.venv/bin/evo-wiki scan --root experiment/evo_wiki_test
.venv/bin/evo-wiki run --root experiment/evo_wiki_test --lane wiki
.venv/bin/evo-wiki prepare-lightrag --root experiment/evo_wiki_test
LIGHTRAG_BASE_URL=http://127.0.0.1:9621 .venv/bin/evo-wiki run --root experiment/evo_wiki_test --lane lightrag --smoke-query "某案件的判决依据是什么？"
```

## 项目结构与模块职责

【架构推断】

- `src/evo_wiki/cli.py`：命令入口和 lane 编排，负责 `init`、`scan`、`run`、`inspect` 等命令。
- `src/evo_wiki/paths.py`：集中定义 runtime workspace 目录，如 `corpus/`、`artifacts/wiki/`、`artifacts/lightrag/`。
- `src/evo_wiki/config.py`：默认 `project.json`、`wiki.json`、LightRAG 配置加载和 deep merge。
- `src/evo_wiki/corpus.py`：扫描 corpus、计算 SHA256、识别 added/modified/deleted。
- `src/evo_wiki/artifacts.py`：写入 manifest、agent delta plan、run summary。
- `src/evo_wiki/wiki.py`：把 `artifacts/wiki/wiki-src/*.md` 渲染成静态 HTML、搜索索引和依赖图。
- `src/evo_wiki/wiki_health.py`：检查 wikilink、孤儿页、index 收录、audit/log 形状、source 页结构。
- `src/evo_wiki/lightrag_lane.py`：准备 `documents.jsonl`，提交到已有 LightRAG Server，维护导入 ledger。
- `src/evo_wiki/platform_export.py`：导出只读 Web 平台目录和 nginx 配置。
- `src/evo_wiki/spa_assets.py`：生成固定 SPA 壳，提供问答、图谱、实体页面前端资源。

## 目录边界

【实测结果】

- 原版 runtime 状态均写入 `experiment/evo_wiki_test/` 下的 `corpus/`、`project.json`、`wiki.json`、`artifacts/`。
- 本次新增的实验日志位于 `experiment/evo_wiki_test/logs/`。
- 未修改 `src/`、`tests/`、`pyproject.toml`、原始测试数据目录。

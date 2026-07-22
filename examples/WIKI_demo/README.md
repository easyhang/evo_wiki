# WIKI_demo：Evo Wiki 2.0.0 可复现示例

这个目录用于复现 Evo Wiki 2.0.0 的构建流程和网页功能。它不包含原项目的
九份案件语料、案件 Wiki 正文、生成页面、SQLite、审核记录、日志、凭据或
LightRAG 运行状态。

合作者需要提供自己的语料；接入自己的 LightRAG 服务后，可以完整体验：

- Wiki 2.0 内容契约和静态 Wiki；
- 问答、结构化引用卡片和引用约束的实体 Wiki 链接；
- 安全 Markdown 渲染与问答会话恢复；
- 独立图谱页和实体邻域；
- `local_single_user` 模式下的网页审核中心。

## 1. 准备环境

要求 Python 3.10 或更高版本。完整问答和图谱功能还要求一个已经运行的
LightRAG 服务；Evo Wiki 不负责安装、启动或托管 LightRAG。

从仓库根目录执行：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
evo-wiki --version
```

当前示例面向 `2.0.0`。如果只下载了本目录，请先克隆对应分支：

```bash
git clone --branch XZT https://github.com/easyhang/evo_wiki.git
cd evo_wiki
```

## 2. 建立本地 workspace

以下命令把运行数据放在仓库已忽略的 `workspace/WIKI_demo`，不会修改本示例：

```bash
DEMO_ROOT="$(pwd)/workspace/WIKI_demo"
evo-wiki init --root "$DEMO_ROOT" --profile local-platform
cp examples/WIKI_demo/wiki.json "$DEMO_ROOT/wiki.json"
```

复制一份自己的资料，并保持示例模板所声明的文件名：

```bash
mkdir -p "$DEMO_ROOT/corpus/raw"
cp /path/to/your-source.md "$DEMO_ROOT/corpus/raw/your-source.md"
```

将通用 Wiki 模板复制到 workspace：

```bash
cp -R examples/WIKI_demo/wiki-src-template/. \
  "$DEMO_ROOT/artifacts/wiki/wiki-src/"
```

然后编辑 `artifacts/wiki/wiki-src/` 中的页面：来源页必须保留完整原文，首页必须
能够访问所有交付页面，实体和概念必须只写入当前语料能够支持的内容。若使用其他
文件名，还要同步修改各页面 frontmatter 中的 `sources` 路径。

## 3. 只复现静态 Wiki

此路径不需要 LightRAG：

```bash
evo-wiki scan --root "$DEMO_ROOT"
evo-wiki lint-wiki --root "$DEMO_ROOT"
evo-wiki render-wiki --root "$DEMO_ROOT"
python3 -m http.server 8080 --directory \
  "$DEMO_ROOT/artifacts/wiki/dist"
```

打开 `http://127.0.0.1:8080/`。`lint-wiki` 在内容契约 v2 下会阻止来源覆盖缺失、
重复 source basename、重复 graph label 和首页不可达等问题进入交付流程。

## 4. 复现完整问答平台

复制 LightRAG 配置模板，但不要提交生成的实际配置：

```bash
cp examples/WIKI_demo/lightrag-config.example.json \
  "$DEMO_ROOT/lightrag-config.json"
```

编辑其中的 `base_url` 和 `workspace`，再通过环境变量注入实际凭据：

```bash
export LIGHTRAG_API_KEY='由秘密管理系统提供的值'
export EVO_WIKI_QUERY_AUDIT_KEY='至少 16 字节的本地随机值'
```

若服务使用 Bearer token，则设置 `LIGHTRAG_BEARER_TOKEN`。不要把任何真实值写进
JSON、Markdown、SQLite、日志或 Git。

先检查并预演，再正式生成和启动：

```bash
evo-wiki doctor --root "$DEMO_ROOT" --check-service
evo-wiki generate --root "$DEMO_ROOT" --dry-run --json
evo-wiki generate --root "$DEMO_ROOT"
evo-wiki serve --root "$DEMO_ROOT"
```

打开 `http://127.0.0.1:18765/app/`。本机模式下会显示问答、图谱、实体和审核入口；
回答引用只信任结构化 citations 与生成的 `wiki-registry.json`，不会把模型自由格式的
References 当作可信链接。

## 5. 验收清单

- `evo-wiki lint-wiki` 无 error，语料覆盖率为 100%。
- 引用卡片只在 source basename 唯一映射时可点击。
- 回答正文只链接本次引用材料覆盖的唯一已登记实体。
- 从来源 Wiki 返回问答页后，成功回答、引用、草稿和检索参数仍在。
- Markdown 原始 HTML 被转义，远程图片不会加载，表格和代码块不会撑破移动端。
- 审核中心只在 `local_single_user` 模式出现。
- `git status` 不包含语料、`lightrag-config.json`、数据库、日志或生成产物。

## 安全边界

本示例只适合本机单用户复现。不要把 `local_single_user` 网关直接暴露到公网；生产
环境应使用 `production-export`、可信反向代理和独立的身份认证与密钥管理。

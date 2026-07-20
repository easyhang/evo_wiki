# Evo Wiki

Evo Wiki 是面向二次开发者的 AI 驱动知识平台生成器。

它把“内容生成”和“工程构建”分成两层：

- Codex、Claude Code 等 Agent 按 Skill 理解项目语料，生成可追溯的 Wiki 正文；
- `evo-wiki generate` 自动完成状态迁移、Wiki 渲染、LightRAG 同步、查询网关检查和平台导出。

默认交付物是一个可继续开发和部署的完整平台：

```text
静态 Wiki + 问答页 + 图谱页 + 实体枢纽 + 查询网关 + Nginx 配置
```

只有明确使用 `--target wiki` 时，才只生成文档站。

## 产品边界

Evo Wiki 负责：

- 从项目语料构建结构化 Wiki；
- 接入用户已经运行的 LightRAG 服务；
- 生成问答、图谱和实体枢纽 SPA；
- 用查询网关约束浏览器到 LightRAG 的读取路径；
- 自动迁移并校验本地 SQLite 状态；
- 导出可预览、可继续二次开发的静态平台目录。

Evo Wiki 不负责：

- 在 CLI 内调用模型供应商 API 生成正文；
- 安装、启动或托管 LightRAG；
- 保存 API key、Bearer token 或审计密钥；
- 自动删除、批量替换或重建 LightRAG 远端数据；
- 多域 ACL、OAuth/RBAC、NFS/SMB、多主机或零停机部署。

## 安装

开发安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

发布包安装：

```bash
pip install evo_wiki-1.0.1-py3-none-any.whl
```

安装后提供两个等价入口：

```bash
evo-wiki --version
evo --version
```

## 五分钟生成一个本地平台

### 1. 初始化 workspace

```bash
evo-wiki init \
  --root ./demo \
  --profile local-platform
```

profile 有三种：

| profile | 用途 |
| --- | --- |
| `local-platform` | 默认；完整平台，本机 loopback 预览 |
| `production-export` | 生成面向反向代理部署的完整平台 |
| `wiki-only` | 关闭问答、图谱和实体枢纽 |

初始化会创建配置、目录和当前版本 SQLite，但不会生成真实 Wiki 正文，也不会保存密钥。

### 2. 放入语料

```bash
cp /path/to/source.md ./demo/corpus/raw/
```

Agent 按根目录 `SKILL.md` 和 Wiki 子 Skill，把语料整理到：

```text
demo/artifacts/wiki/wiki-src/
  index.md
  concepts/
  entities/
  sources/
```

初始化产生的 `index.md` 是占位页。完整平台检测到 stub 时会返回
`GENERATION_WIKI_STUB`，防止空壳平台被误当成交付物。

### 3. 配置平台外观和功能

编辑 `demo/wiki.json`：

```json
{
  "title": "项目知识库",
  "description": "面向二次开发的知识平台",
  "brand": {
    "logo_path": null,
    "primary_color": "#2563eb"
  },
  "navigation": {
    "wiki": true,
    "qa": true,
    "graph": true,
    "entity_hub": true
  },
  "query_defaults": {
    "mode": "mix",
    "top_k": 20,
    "history_turns": 3
  },
  "graph_defaults": {
    "max_depth": 2,
    "max_nodes": 50,
    "popular_limit": 12
  }
}
```

约束：

- `logo_path` 必须是 workspace 内的规范相对路径；
- `primary_color` 使用 `#RRGGBB`；
- `query_defaults.mode` 必须是受支持的 LightRAG 查询模式；
- `top_k` 范围为 1–100；
- `history_turns` 范围为 0–3，默认携带最近 3 个成功展示的完整问答对；
- `graph_defaults` 默认使用 depth 2、最多 50 节点；客户端硬上限为 200；
- `entity_hub=true` 要求 `qa=true` 且 `graph=true`。

配置会同时作用于 Wiki 和 SPA。Logo 会被复制到公共资源目录，不会引用 workspace 私有路径。

### 4. 配置真实 LightRAG

完整平台要求一个已经运行的 LightRAG 服务：

```bash
cp ./demo/lightrag-config.example.json \
  ./demo/lightrag-config.json
```

编辑本地文件：

```json
{
  "mode": "service",
  "base_url": "http://YOUR_LIGHTRAG_SERVER:9621",
  "workspace": "evo_wiki",
  "api_key_env": "LIGHTRAG_API_KEY",
  "bearer_token_env": "LIGHTRAG_BEARER_TOKEN"
}
```

`base_url` 和 `workspace` 必须明确填写。Evo Wiki 不猜测 localhost，也不会启动
`lightrag-server`。凭据只通过环境变量注入：

```bash
export LIGHTRAG_API_KEY='...'
export EVO_WIKI_QUERY_AUDIT_KEY='至少 16 字节的随机值'
```

若服务使用 Bearer token，则改用 `LIGHTRAG_BEARER_TOKEN`。不要把真实密钥写入配置、仓库、
SQLite、日志或报告。

可以先单独做只读服务检查：

```bash
evo-wiki doctor --root ./demo --check-service
```

### 5. 预演（dry-run）

```bash
evo-wiki generate \
  --root ./demo \
  --dry-run \
  --json
```

完整预演保证：

- 不写 workspace；
- 不创建或修改 SQLite；
- 不提交 LightRAG；
- 不替换已有平台；
- 只返回配置、语料、迁移、服务和导出计划。

若当前语料已有 `UNKNOWN/BLOCKED` LightRAG binding，预演返回
`status=blocked`、`GENERATION_RECONCILE_REQUIRED` 和安全计数，并列出只读 review、
`--apply` 与重试命令。此路径使用 immutable SQLite 读取，不创建 WAL/SHM。

### 6. 生成平台

```bash
evo-wiki generate \
  --root ./demo \
  --smoke-query "这个项目解决什么问题？" \
  --json
```

默认 `--target platform`。执行顺序固定为：

1. 只读检查配置、语料和 Wiki 正文；
2. 在任何 Wiki staging 改写前检查当前 binding gate；已知阻断要求先执行
   `state reconcile`；
3. 自动将 legacy JSON 状态切换到 SQLite；
4. 自动备份并将 SQLite schema v1–v4 升级到当前 v5；
5. 执行 `state verify`；
6. 渲染并检查 Wiki；
7. 检查、提交并轮询 LightRAG；
8. 检查查询网关；
9. 在 staging 中构建并原子替换 `artifacts/platform/`。

失败语义：

- 迁移或校验失败：不调用 LightRAG；
- Wiki 质量失败：不调用 LightRAG；
- LightRAG 或网关失败：不替换已有平台；
- 语料删除触发 `requires_rebuild`：停止，不自动删除或重建远端数据；
- 当前 schema 重复生成：不重复创建迁移备份。

查询接口使用 schema v2，把回答生成、证据质量和人工审核拆成独立状态：

- `generation_status=succeeded`：正文立即展示，并进入最近三轮会话历史；
- `evidence_status=grounded`：至少一条知识库证据有效且全部检查通过；
- `evidence_status=partially_grounded`：存在有效证据，但部分引用、短问题或关键事实待核验；
- `evidence_status=ungrounded`：首轮没有可用知识库证据，网关使用同一 LightRAG 服务的
  `mode=bypass` 生成通用知识回答；
- `review_status=pending`：后台待人工审核，不折叠、不隐藏正文。

只有鉴权、容量、维护、超时、服务异常或最终空响应返回
`generation_status=failed`。模型正文末尾的自由格式 References 不会被当作可信来源；可信
来源只来自结构化 `citations`，前端将行内编号映射到“回答依据 → 片段 → Wiki 来源”证据卡。

只要存在结构化引用，SPA 会在正文和证据卡显示后异步加载一个引用关联知识子图。候选 seed
仅来自引用显式携带的实体 `graph_label`，或引用来源在 `wiki-registry.json` 中关联的实体
`graph_label`；多个候选按当前问题关键词与对应引用片段的词项相关性排序。返回子图必须包含
与候选 label 精确匹配的节点，否则继续尝试下一候选，全部未命中则不展示子图。该子图固定为
1 跳、最多 24 个节点、最多尝试 3 个候选；拉图失败只隐藏子图区，不会改变回答、证据状态或
人工审核状态。

稳定报告写入：

```text
demo/artifacts/generation/report.json
```

报告包含步骤状态、迁移前后版本、备份信息、本地/远端是否发生写入、产物路径、错误码和下一条
预览命令；不记录密钥、问题正文或远端响应正文。

### 7. 本地预览

```bash
evo-wiki serve \
  --root ./demo \
  --listen 127.0.0.1:8080
```

同一个端口提供：

```text
/                  Wiki
/app/              问答、图谱和实体枢纽
/api/query         受控查询接口
/api/graphs        受控图谱接口
```

也可以从 CLI 调用同一条 schema v2 问答链路：

```bash
evo-wiki query \
  --root ./demo \
  --gateway \
  --query "这个项目解决什么问题？"
```

旧参数名 `--require-evidence` 仍作为兼容别名，但它不再表示“证据不足就不回答”。输出始终分别
报告生成、回答来源、证据和审核状态。

`serve` 只允许 loopback 和 `local_single_user`，并封禁 `/status/`、数据库、配置、README 和
Nginx 文件。它是本地开发预览，不是公网服务器。

## 只生成 Wiki

明确不需要问答和图谱时：

```bash
evo-wiki init --root ./docs-demo --profile wiki-only
# 先由 Agent 完成 wiki-src 正文
evo-wiki generate \
  --root ./docs-demo \
  --target wiki \
  --json
```

该模式不要求 LightRAG、查询网关或审计密钥，输出位于
`artifacts/wiki/dist/index.html`。

## 生成产物

```text
workspace/
  corpus/                         原始语料
  project.json                    运行和安全配置
  wiki.json                       品牌、导航和查询默认值
  lightrag-config.json            本地 LightRAG 配置，禁止提交
  artifacts/
    state/evo_wiki.sqlite3        唯一业务状态事实源
    generation/report.json        脱敏生成报告
    wiki/
      wiki-src/                   Agent 维护的 Markdown
      dist/                       静态 Wiki
        wiki-registry.json        实体/图谱/source basename 公共映射
      reports/                    lint 和渲染报告
    lightrag/
      input/                      从 corpus 生成的输入
      reports/                    提交和 track 状态
    platform/                     原子导出的最终平台
      index.html
      app/
      assets/
      nginx.conf
```

Wiki 和 LightRAG 是两条独立工作流（lane）。它们都以原始语料为输入，但不共享索引、报告或运行
基线；生成的 Wiki 不默认再次进入 LightRAG。

## Agent 与 CLI 的分工

Agent：

- 识别用户目标和交付 target；
- 阅读语料并维护 `wiki-src`；
- 建立概念、实体、原文和入口之间的 `[[wikilink]]`；
- 实体有图谱别名时填写可选 `graph_label` 和 `aliases`；`graph_label` 必须唯一，实体页声明的
  `sources` 会建立“引用文档 → graph label”公共映射；
- 保留完整原文和用户手工编辑区；
- 对证据不足、事实冲突和结构不确定内容创建 audit；
- 完成正文后调用 `generate`。

CLI：

- 管理目录、配置和 SQLite；
- 扫描 corpus 和增量变化；
- 渲染 Wiki、搜索索引和依赖图；
- 对接现有 LightRAG HTTP API；
- 轮询 track 并维护绑定关系；
- 执行查询证据门禁；
- 原子导出平台。

CLI 首版不直接调用模型 API，因此“只执行 `generate`”不会凭空把 stub 写成高质量 Wiki。

## 高级与诊断命令

普通生成使用 `generate`。以下命令保留给定向调试和运维：

| 命令 | 风险 | 用途 |
| --- | --- | --- |
| `run --lane wiki\|lightrag\|both` | 本地/远端写入 | 单独运行工作流，不导出完整平台 |
| `render-wiki` / `lint-wiki` | 本地写入/只读检查 | 写作过程快速检查 |
| `prepare-lightrag` | 本地写入 | 只准备输入 |
| `build-lightrag --dry-run` | 本地报告写入 | LightRAG 局部预演 |
| `export-platform` | 本地写入 | 单独重导出，要求前置状态已完成 |
| `migrate-state` | 预演只读，`--apply` 本地写入 | 诊断 legacy 切换 |
| `state migrate-schema` | 预演只读，`--apply` 本地写入 | 诊断 schema 升级 |
| `state verify` / `state backup` | 只读/本地写入 | 校验和备份 |
| `state reconcile` | 默认只读 | 对账（reconcile）远端观察结果 |
| `gateway check\|serve\|status` | 只读/运行服务 | 独立查询网关运维 |
| `audit list\|show` | 只读 | 查看后台审核队列；`show --include-content` 显式读取受保护快照 |
| `audit resolve` | 本地写入 | `APPROVED`/`REJECTED` 结案并删除正文快照 |
| `alerts status\|dispatch\|retry` | 只读/远端通知 | 通知 outbox 运维 |

HTTP 409 的 `state replace-*` 只支持受审查的单文档替换，不属于自动生成流程，也不支持批量替换。
详细安全规则见 `skills/evo-wiki-operations/SKILL.md`。

## Evidence Subgraph

Evidence Subgraph 是随 wheel 发布的开发者运行时 Skill，不注册为普通 UI Skill。它根据显式 seed
构造受资源预算约束的证据子图，只返回 scope 内证据：

```bash
evo-wiki query \
  --root ./demo \
  --skill evidence-subgraph \
  --only-context \
  --query "问题" \
  --seed "实体或概念" \
  --explain-retrieval
```

它不生成答案，不调用 LightRAG `global` 搜索，也不提供无边界 fallback。

## 真实服务与 Mock 测试

测试套件会启动一个进程内 Mock LightRAG，模拟 `/health`、`/openapi.json`、提交和 track 轮询，
验证 Evo Wiki 的完整编排和失败边界。这个 Mock 不是 LightRAG 产品，也不会验证真实向量、图谱、
模型或检索效果。

真实接入成立的条件是：用户提供实际 `base_url` 和 `workspace`，只读服务检查通过，文档提交成功，
对应 track 达到 `processed` 且产生有效 chunk。

## 开发与发布

运行测试：

```bash
.venv/bin/python -m pytest -q
```

构建 Python 包：

```bash
.venv/bin/python -m build
```

构建开发套件：

```bash
.venv/bin/python scripts/build_release.py \
  --output-dir ./dist
```

发布目录：

```text
evo-wiki-1.0.1/
  python/*.whl
  python/*.tar.gz
  skills/evo-wiki/
  skills/evo-wiki-wiki/
  skills/evo-wiki-lightrag/
  skills/evo-wiki-operations/
  examples/local-platform/
  README.md
  LICENSE
  SHA256SUMS
```

实验目录、SQLite、生成 HTML、真实 LightRAG 配置和内部报告不会进入发布包。

## 更多文档

- [架构说明](docs/architecture.md)
- [Wiki Skill](skills/evo-wiki-wiki/SKILL.md)
- [LightRAG Skill](skills/evo-wiki-lightrag/SKILL.md)
- [Operations Skill](skills/evo-wiki-operations/SKILL.md)
- [Evidence Subgraph Skill](src/evo_wiki/retrieval_skills/evidence_subgraph/SKILL.md)

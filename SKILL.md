---
name: evo-wiki
description: |
  使用 Evo Wiki 把项目语料生成面向二次开发者的知识平台。默认交付完整 platform，包括静态
  Wiki、问答/图谱 SPA、受控查询网关配置和可预览产物；只有用户明确要求纯文档站时才选择
  wiki target。涉及 Wiki 内容生成、LightRAG 接入、SQLite 自动迁移或平台交付时使用。
---

# Evo Wiki

Evo Wiki 是“Skill 生成内容，CLI 构建平台”的开发工具。Agent 负责理解语料并维护 Wiki 正文；
`evo-wiki generate` 负责迁移状态、渲染、LightRAG 同步、查询网关检查和平台导出。

## 选择交付目标

| 用户目标 | 处理方式 |
| --- | --- |
| 未明确限制，或要求可二次开发的平台 | 默认 `platform` |
| 明确只要文档站、不需要问答和图谱 | `--target wiki` |
| 只调试 Wiki 内容 | 读取 `$evo-wiki-wiki` |
| 只调试 LightRAG 入库 | 读取 `$evo-wiki-lightrag` |
| 迁移、备份、对账或网关运维 | 读取 `$evo-wiki-operations` |
| 只返回受限证据，不生成答案 | 使用 Evidence Subgraph 运行时 Skill |

不要把 `run --lane both` 当作完整交付命令。它只运行两条工作流（lane），不会替代平台导出、
自动迁移和最终验收。

## 默认平台流程

### 1. 初始化

```bash
evo-wiki init \
  --root /path/to/workspace \
  --profile local-platform
```

可选 profile：

- `local-platform`：默认；生成完整平台并仅在 loopback 本地预览；
- `production-export`：生成带生产 Nginx 边界的产物；
- `wiki-only`：关闭问答、图谱和实体枢纽。

初始化不会生成真实正文，也不会保存密钥。

### 2. 整理语料并生成 Wiki 正文

读取 `$evo-wiki-wiki`，从 `<workspace>/corpus/` 生成或更新：

```text
artifacts/wiki/wiki-src/
  index.md
  concepts/
  entities/
  sources/
```

不能把初始化产生的占位页当成完成结果。完整平台会在 Wiki 仍为 stub、语料为空或 lint 存在
错误时失败闭锁。

### 3. 完成个性化配置

编辑 `<workspace>/wiki.json`：

```json
{
  "content_contract_version": 2,
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

`logo_path` 必须是 workspace 内的相对路径。`entity_hub` 依赖 `qa` 和 `graph` 同时开启。
`history_turns` 不得超过 3；所有 `generation_status=succeeded` 的完整问答对都可进入上下文，
证据状态不阻断会话连续性。

新建 workspace 使用内容契约 v2：每份 corpus 文件都必须由唯一来源页声明，来源路径必须是
`corpus/` 下的规范相对路径，所有交付页面必须从首页发现。现有 `wiki.json` 缺少
`content_contract_version` 时按 v1 兼容，不要自动补写或升级；只有用户明确升级后才加入字段。

### 4. 配置真实 LightRAG

完整平台要求用户已经运行一个 LightRAG 服务。Evo Wiki 不安装、不启动也不托管 LightRAG。

```bash
cp /path/to/workspace/lightrag-config.example.json \
  /path/to/workspace/lightrag-config.json
```

必须填写真实的 `base_url` 和 `workspace`。不要猜测 localhost。API key、Bearer token 和查询
审计密钥只从环境变量读取：

```bash
export LIGHTRAG_API_KEY='...'
export EVO_WIKI_QUERY_AUDIT_KEY='至少 16 字节的随机值'
```

不要把凭据写入仓库、配置、报告或命令参数。

### 5. 预演并生成

完成正文和配置后先运行预演（dry-run）：

```bash
evo-wiki generate \
  --root /path/to/workspace \
  --dry-run \
  --json
```

该预演不写 workspace，也不调用远端写接口。确认计划后执行：

```bash
evo-wiki generate \
  --root /path/to/workspace \
  --smoke-query "用于验收的问题" \
  --json
```

默认 `target` 是 `platform`。固定执行顺序为：

1. 只读检查配置、语料和 Wiki 正文；
2. 以 immutable SQLite 检查当前 binding gate；
3. 自动将 legacy JSON 状态切换到 SQLite；
4. 自动备份并把 SQLite schema 升级到当前版本；
5. 执行 `state verify`；
6. 渲染并检查 Wiki；
7. 检查、提交并轮询 LightRAG；
8. 检查查询网关；
9. 原子导出 `artifacts/platform/`。

任一步失败都会停止后续步骤。旧平台只在新平台完整构建成功后才被替换。
若返回 `GENERATION_RECONCILE_REQUIRED`，按结果中的 review → apply → retry 命令恢复；不得
手工修改 binding 或 SQLite。

### 6. 本地预览

```bash
evo-wiki serve \
  --root /path/to/workspace \
  --listen 127.0.0.1:8080
```

它在一个本地端口提供 Wiki、问答/图谱 SPA 和 `/api/*` 查询网关。只允许
`local_single_user` 和 loopback；生产交付使用导出的 Nginx 配置。

## 纯 Wiki 分支

用户明确不要问答和图谱时：

```bash
evo-wiki init --root /path/to/workspace --profile wiki-only
# 先由 Wiki Skill 完成正文
evo-wiki generate \
  --root /path/to/workspace \
  --target wiki \
  --json
```

该分支不要求 LightRAG、查询网关或审计密钥。

## 工作流边界

- Wiki 使用原始语料生成可读页面；LightRAG 也从原始语料准备输入，不默认读取生成的 Wiki。
- 两条工作流的状态、报告和索引保持分离。
- 语料删除触发 `requires_rebuild` 时停止完整平台生成；不得自动删除、批量替换或重建远端数据。
- SQLite 是切换后的唯一业务状态事实源；不得手工编辑数据库或兼容 JSON。
- `migrate-state`、`state migrate-schema`、`run` 和 `export-platform` 保留给诊断、恢复和高级运维；
  普通交付使用 `generate`。
- Evidence Subgraph 只做显式种子驱动的受限检索，不生成答案，也不回退到全局搜索。
- 查询响应使用 schema v2；所有 `generation_status=succeeded` 的正文立即展示。
- `grounded`、`partially_grounded`、`ungrounded` 只说明证据质量；后两者进入后台人工审核，
  但不折叠或隐藏正文。
- 只有鉴权、容量、维护、超时、服务异常或最终空响应显示生成失败。
- 公共 `wiki-registry.json` 是图谱 label、真实实体 slug 和 source basename 的映射来源，
  不得把 workspace 绝对路径写入公共产物。
- 模型正文中的自由格式 References 不是可信来源；引用和实体链接必须由结构化 citations 与
  registry 共同约束。registry 缺失或歧义时保留正文并关闭链接。
- 所有回答和审核详情必须经过同一安全 Markdown 渲染器；不得执行原始 HTML 或加载 Markdown
  远程图片，也不得持久化已渲染 HTML。

## 回复要求

- 使用自然中文，先说明结果，再给必要命令和风险。
- 首次出现写“工作流（lane）”“预演（dry-run）”“对账（reconcile）”；后文使用中文简称。
- 命令名、配置键、API、状态值、错误码和库名保持英文。
- 不把 Mock LightRAG 集成测试描述成真实服务验收。只有实际 `base_url`、`workspace` 和服务检查
  通过，才能说明真实 LightRAG 已接入。

---
name: evo-wiki-operations
description: |
  安全维护 Evo Wiki 的 SQLite workspace。用户要迁移旧版 JSON、校验或备份状态、升级 schema、
  对账 LightRAG 绑定关系、处理 HTTP 409 替换、运行查询网关、执行维护排空、查看审计或处理通知
  时使用；也用于诊断 generate 的迁移或网关失败。所有远端副作用和业务状态写入都必须经过本
  Skill 的预演、确认、备份和恢复规则。
---

# Evo Wiki · Operations 子 Skill

本 Skill 面向运维和开发者。SQLite 切换完成后，SQLite 是业务状态的唯一事实源；兼容 JSON、迁移
记录、绑定关系、操作日志和报告都不能手工编辑。命令中的状态值、错误码和 API 名称保持英文，说明
使用中文。

普通用户生成平台时只运行 `evo-wiki generate`。它会在内部完成 legacy JSON 切换、backup-first
schema 升级和 `state verify`。本 Skill 中的迁移命令保留给预演、诊断、恢复和独立运维，不应成为
普通交付教程里的必做步骤。

## 1. 先判断 workspace 状态

所有命令从工具仓库执行，并显式传入运行时 workspace：

```bash
.venv/bin/evo-wiki state verify --root /path/to/workspace --json
```

执行第一步：读取 `<workspace>/project.json`。

| 观察到的状态 | 处理方式 |
| --- | --- |
| `state.backend` 缺失或为 `legacy_json` | 进入 JSON → SQLite 迁移流程 |
| `state.backend` 为 `sqlite` | 先运行 `state verify` |
| SQLite 已存在但配置仍为 legacy | 停止普通写入；重新运行迁移以续接，或仅在用户明确选择保留旧状态时隔离候选库 |

风险标签：

- `[只读]`：不写 workspace，也不调用远端写接口。
- `[本地写入]`：只写 SQLite、报告、导出或运维记录。
- `[远端写入]`：可能调用远端 DELETE/POST，必须有明确确认和可恢复状态。

## 2. 诊断 legacy JSON → SQLite 自动迁移

`generate` 会先完成只读预检，再自动调用可续接迁移。迁移失败时不会提交 LightRAG，也不会替换
已有平台。只有需要单独查看计划、恢复中断状态，或用户明确要求独立迁移时，才使用以下命令。

### 2.1 预演 `[只读]`

独立运维始终先预演（dry-run）：

```bash
.venv/bin/evo-wiki migrate-state \
  --root /path/to/workspace \
  --json
```

结果必须包含 `workspace_mutated: false`。工具可以在 workspace 外创建临时数据库做校验，但不
能写入运行目录。

### 2.2 应用切换 `[本地写入]`

只有用户明确要求脱离 `generate` 单独切换，或正在恢复已中断的切换时才运行：

```bash
.venv/bin/evo-wiki migrate-state \
  --root /path/to/workspace \
  --apply \
  --json
```

应用流程会：

1. 保留原始 JSON 字节和 `project.json` 到 `artifacts/migration-backup/`；
2. 导入并校验候选 SQLite；
3. 安装数据库；
4. 原子替换 `project.json`。

数据库安装和配置替换不是一个跨文件事务；如果中途进程退出，重新运行同一条 `--apply` 应继续
完成支持的中断状态。只有用户明确要求放弃非活动候选库时，才使用：

```bash
.venv/bin/evo-wiki migrate-state \
  --root /path/to/workspace \
  --apply \
  --abort-candidate \
  --json
```

不要删除或手工移动活动数据库、迁移记录或备份。

## 3. 校验、导出和备份

```bash
# [只读] 校验 SQLite 事实和派生产物
.venv/bin/evo-wiki state verify --root /path/to/workspace --json

# [本地写入] 从 SQLite 重新生成兼容 JSON
.venv/bin/evo-wiki state export --root /path/to/workspace --json

# [本地写入] 创建并校验在线备份
.venv/bin/evo-wiki state backup --root /path/to/workspace --json
```

- `PASS` 和 `WARN` 返回 0。`WARN` 通常表示可修复的派生问题，例如导出过期或日志不完整；
  `FAIL` 表示数据库事实或核心不变量失败，返回 5。
- `state export` 只在命令边界生成兼容 JSON，不把 JSON 变成第二个事实源，也不推进
  `state_commit_seq`。
- `state backup` 使用 SQLite Backup API，验证备份结果，不覆盖已存在的文件，也不推进业务状态。

## 4. 日志校验和迁移

```bash
# [只读] 校验操作日志、序列号和 hash chain
.venv/bin/evo-wiki logs verify --root /path/to/workspace

# [只读] 预演旧 JSONL 日志迁移
.venv/bin/evo-wiki logs migrate-legacy --root /path/to/workspace

# [本地写入] 明确授权后应用一次性迁移
.venv/bin/evo-wiki logs migrate-legacy \
  --root /path/to/workspace \
  --apply
```

迁移日志时保留原始证据，不要把凭据、原始远端响应、查询、答案或正文写入运维日志。

## 5. 对账 LightRAG 观察结果

完整平台生成会在 Wiki staging 改写前，以 immutable SQLite 检查当前语料对应的 binding。
发现 `UNKNOWN/BLOCKED` 时返回 `GENERATION_RECONCILE_REQUIRED`、安全数量和以下恢复顺序，
失败报告不得记录 binding ID、语料路径或远端响应：

### 5.1 预演 `[只读]`

```bash
.venv/bin/evo-wiki state reconcile \
  --root /path/to/workspace \
  --json
```

对账会读取远端状态，但不提交、删除、替换或自动重试。

### 5.2 记录观察结果 `[本地写入]`

```bash
.venv/bin/evo-wiki state reconcile \
  --root /path/to/workspace \
  --apply \
  --json
```

它只记录已观察到的事实。成功处理的观察结果可以解除绑定关系的阻断；失败、缺失、格式错误、
超时或未知结果仍保持 blocked。远端或后端观察失败返回 6。

对账 apply 后先重新运行：

```bash
.venv/bin/evo-wiki state verify --root /path/to/workspace --json
.venv/bin/evo-wiki generate --root /path/to/workspace --dry-run --json
```

只有预演恢复为 `ready` 才执行正式生成。不要为解除门禁而手工改 SQLite。

## 6. 处理 HTTP 409 文档替换

### 6.1 生成审查方案 `[只读]`

当提交被归类为 `REMOTE_HTTP_409` 时，先生成零写入方案：

```bash
.venv/bin/evo-wiki state replace-plan \
  --root /path/to/workspace \
  --json
```

方案会检查健康状态、OpenAPI、pipeline 状态、分页文档清单、唯一 canonical basename、远端文档
是否已终态、workspace/backend 身份，以及目标和回滚快照。即使结果为 `ready`，也只代表“可供
审查”，不代表已经获准执行。方案必须保持 `execution_authorized=false`。

该流程没有 `--apply`。不要调用 `/documents/delete_document`，不要重试提交，不要手工修改绑定
关系。完整保存 `plan_id` 和 `plan_digest`，不要截短或重建 digest。`blocked` 或 `failed` 返回 6。

### 6.2 升级 schema `[本地写入]`

普通 `generate` 会自动把 schema v1–v3 备份并升级到当前版本，当前 schema 不会重复备份。以下
命令只用于独立运维。替换至少要求 SQLite schema v2；查询治理要求 v3；通知功能要求 v4。先预演，
再明确应用不可变迁移链：

```bash
.venv/bin/evo-wiki state migrate-schema \
  --root /path/to/workspace \
  --json

.venv/bin/evo-wiki state migrate-schema \
  --root /path/to/workspace \
  --apply \
  --json
```

应用升级前会创建并校验在线备份，不修改已应用的旧 migration；新规则必须增加新的 migration 和
checksum。只有维护窗口和独占写入时段得到批准后，才在被 `.gitignore` 忽略的运行时配置中开启
`lightrag.replacement.enabled=true`。

### 6.3 执行已审查方案 `[远端写入]`

```bash
.venv/bin/evo-wiki state replace-execute \
  --root /path/to/workspace \
  --plan-id <plan-id> \
  --confirm-digest <完整-sha256-plan-digest> \
  --smoke-query "针对目标文档的验收问题" \
  --json
```

该确认最多授权两次 DELETE 和两次文档提交：删除旧文档、提交目标文档，以及仅在失败目标可明确
归属时清理失败目标并恢复旧快照。它不是 RBAC，也不是双人审批。命令会在第一处远端写入前建立
意图记录，并自动创建新的已验证 SQLite 备份。

不要在意图状态不明确时重试 DELETE 或 POST。查询保持排空，使用只读远端检查和状态命令确认
实际进度；只有状态明确说明回滚安全时才执行补偿：

```bash
.venv/bin/evo-wiki state replace-status \
  --root /path/to/workspace \
  --json

.venv/bin/evo-wiki state replace-recover \
  --root /path/to/workspace \
  --operation-id <operation-id> \
  --action rollback \
  --confirm <operation-id> \
  --json
```

`COMPLETED` 和 `ROLLED_BACK` 返回 0；`BLOCKED`、`NEEDS_AUDIT`、验收失败和远端不确定返回 6。
不要删除备份，也不要手工编辑 operation、revision、binding、ledger 或 journal。该流程只处理
一个经过审查的冲突文档，不支持批量替换。

## 7. 运行可信查询网关

生产读取路径必须是：

```text
Browser → Nginx/认证 → 查询网关 → LightRAG
```

Nginx 不得绕过拒绝结果或维护窗口，直接把 reader 路由指向 LightRAG。

### 7.1 检查和启动

密钥必须在仓库外准备：

```bash
export EVO_WIKI_QUERY_AUDIT_KEY='<secret-manager-value>'

# [只读] 检查网关、后端映射和证据能力
.venv/bin/evo-wiki gateway check \
  --root /path/to/workspace \
  --json

# 启动和查看状态
.venv/bin/evo-wiki gateway serve --root /path/to/workspace
.venv/bin/evo-wiki gateway status \
  --root /path/to/workspace \
  --json
```

本地 `local-platform` 预览无需分别启动静态站和网关：

```bash
evo-wiki serve \
  --root /path/to/workspace \
  --listen 127.0.0.1:8080
```

该命令只允许 loopback 和 `local_single_user`。生产仍使用生成的 Nginx 配置与独立网关进程。

`gateway check` 要求 schema v3、一个活动配置分区、健康检查/OpenAPI 中的 workspace 和 storage
映射一致，并且远端支持 chunk 内容读取。检查本身不写 workspace。

生产 `enforce` 模式必须监听 loopback，使用可信代理身份、持久化审计和 `fail_closed=true`。
TLS 和外部认证放在 Nginx；不要把网关直接绑定到公网，也不要允许 reader 路由直达 LightRAG。

网关只保存 HMAC、hash、计数和状态，不保存原始问题、答案、引用正文、凭据或上游完整错误。
`query_run` 和 heartbeat 只属于运维元数据，不推进 `state_commit_seq`。

### 7.2 维护排空

查询网关为 `shadow` 或 `enforce` 时，替换前必须有新鲜 heartbeat。第一次远端 DELETE 前会：

1. 建立分区维护 fence；
2. 拒绝新的 query/graph reader；
3. 等待现有 reader lease 排空。

heartbeat 丢失、lease 过期或排空超时都必须在远端写入前阻断。`FAILED` fence 在人工审计前保持
关闭，不恢复 reader。正在返回的请求如果遇到维护 fence，也不能在 fence 建立后继续交付结果。

## 8. 审计、通知和 lease

### 8.1 审计队列

```bash
.venv/bin/evo-wiki audit list \
  --root /path/to/workspace \
  --json

.venv/bin/evo-wiki audit show \
  --root /path/to/workspace \
  --audit-id <id> \
  --json

.venv/bin/evo-wiki audit resolve \
  --root /path/to/workspace \
  --audit-id <id> \
  --confirm <id> \
  --resolution RESOLVED \
  --json
```

只在公共 platform 之外审查过授权证据后解决 audit。解决 audit 会记录本地 actor/host 并推进
治理业务序列，但不会重放查询或替换副作用。

### 8.2 通知 outbox

通知默认关闭。Webhook URL 和签名密钥只能来自环境变量，不能写入 `project.json`、journal、
报告或命令行。生产 Webhook 必须使用 HTTPS，签名密钥至少 32 字节；loopback HTTP 只用于隔离验收。

```bash
.venv/bin/evo-wiki alerts status \
  --root /path/to/workspace \
  --json

.venv/bin/evo-wiki alerts dispatch \
  --root /path/to/workspace \
  --limit 20 \
  --json

.venv/bin/evo-wiki alerts retry \
  --root /path/to/workspace \
  --notification-id <notification-id> \
  --confirm <notification-id> \
  --json
```

只有明确为 `FAILED` 的通知可以重试。尝试记录只保留结果、HTTP 状态类别、稳定错误码和耗时，
不保留 URL、响应体、异常全文、查询、答案、chunk、引用、路径、用户名或凭据。

`MAINTENANCE_DRAINING` 且标记为 delivery-required 的事件，必须先达到 `DELIVERED` 才允许替换
发出 DELETE；超时或终态失败会把 fence 改为 `FAILED`，并保证 DELETE 次数为 0。普通 audit 通知
失败不会改变已经提交的拒绝，也不会暂停无关的普通查询。

### 8.3 恢复过期 query lease

只能放弃已经过期且仍处于 `RETRIEVING` 的 lease：

```bash
.venv/bin/evo-wiki gateway lease-recover \
  --root /path/to/workspace \
  --request-id <id> \
  --action abandon \
  --confirm <id> \
  --json
```

活动中的 lease、终态 lease、未过期 lease 或确认值不匹配都必须拒绝。不要为了缩短维护等待而
放弃活动 lease。

## 9. 运行 QG-001 运维验收

先做 source-zero-write 预检：

```bash
.venv/bin/evo-wiki gateway acceptance \
  --root /path/to/source-workspace \
  --report /path/to/qg001_ops_acceptance_summary.json \
  --json
```

隔离应用需要精确确认值和只读 provider 环境文件；`--allow-image-pull` 只允许固定的
`nginx:1.27-alpine`：

```bash
.venv/bin/evo-wiki gateway acceptance \
  --root /path/to/source-workspace \
  --apply \
  --confirm QG-001-OPS-ACCEPTANCE \
  --provider-env-file /path/to/lightrag/.env \
  --report /path/to/qg001_ops_acceptance_summary.json \
  --allow-image-pull \
  --json
```

应用会复制 source workspace，只在副本中升级到 schema v5，并使用随机 Docker network、临时
LightRAG workspace、绑定存储、Basic Auth、签名 Webhook 和合成文档。它覆盖 `shadow` →
`enforce` 部署兼容、两种模式下相同的答案交付、bypass 通用回答、审核快照读取与删除、
heartbeat/lease/通知阻断、实时排空和单文档替换。结束时只保留脱敏 JSON 报告。

如果进程崩溃导致清理逻辑未执行，只能按 run ID 清理：

```bash
.venv/bin/evo-wiki gateway acceptance-cleanup \
  --run-id <qg001-run-id> \
  --confirm <qg001-run-id> \
  --json
```

不得猜测 ID、手工扩大 Docker 过滤器或扩大文件系统清理边界。

## 10. 不变量和验证

- 不要在持有 SQLite 事务时等待网络调用。
- 普通事务使用 SQLite busy handling；不要再增加第二套跨进程 writer lock。
- 已应用 migration 不可修改；新增版本和 checksum。
- `state_commit_seq` 只记录业务事实，不因导出、校验、备份、迁移元数据或 journal 增长。
- 旧绑定关系默认是 `UNKNOWN/BLOCKED`；旧 ledger 时间戳不能证明远端成功。
- POSIX 下 state 和 backup 目录权限为 `0700`，文件权限为 `0600`。
- 不要把 SQLite 放在 NFS/SMB 上，也不要把它当作多主机数据库。

修改代码后从工具仓库运行：

```bash
.venv/bin/python -m pytest -q
```

如果命令、schema、失败语义或维护约束发生变化，还要同步 README、技术文档和 `STATUS.md`。

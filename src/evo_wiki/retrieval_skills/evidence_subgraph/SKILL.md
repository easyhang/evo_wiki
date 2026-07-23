---
name: evidence-subgraph
description: |
  Evidence Subgraph 是 Evo Wiki 的开发者运行时检索 Skill。它根据显式种子构造受资源预算约束的
  证据子图，只返回经过 scope 校验的证据，不生成最终答案，不调用 LightRAG 全局检索，也不提供
  无边界 fallback。需要开发、调试或验证受限证据检索时使用。
---

# Evidence Subgraph 开发者 Skill

这是运行时检索契约，不是 Web UI 功能，也不是只靠 prompt 维持的工作流。当前版本只做 retrieval：

```text
显式种子
  → workspace 预检
  → 子图扩展
  → 本地来源映射
  → content-unit allow-list
  → allow-list 内 BM25
  → scope 断言
  → 证据片段 + 脱敏 trace
```

禁止在本包中调用 LightRAG `/query`、`mix`、`hybrid` 或 `global`，禁止生成最终答案，也禁止在失败
时回退到无边界全局搜索。

## 1. 调用方式

```bash
evo-wiki query \
  --root /path/to/workspace \
  --skill evidence-subgraph \
  --only-context \
  --query "问题" \
  --seed "实体或概念" \
  --max-depth 8 \
  --explain-retrieval
```

`--seed` 可以重复，表示多个显式种子。`--max-depth` 接受任意正整数；深度不是资源上限，实际
工作量仍受节点、边、content unit 和总耗时预算约束。

## 2. 配置契约

```json
{
  "retrieval": {
    "evidence_subgraph": {
      "max_depth": 2,
      "max_nodes": 300,
      "max_edges": 3000,
      "max_content_units": 1000,
      "top_k": 5,
      "timeout_seconds": 30,
      "target_chars": 1200,
      "overlap_chars": 120,
      "require_scoped_retrieval": true,
      "deny_unbounded_global_search": true,
      "generation_enabled": false
    }
  }
}
```

字段含义：

- `max_depth`：默认遍历深度，必须是正整数，无固定深度上限；
- `max_nodes`：节点预算，硬上限 10000；
- `max_edges`：边预算，硬上限 50000；
- `max_content_units`：本地候选预算，硬上限 50000；
- `top_k`：返回证据数量，硬上限 20；
- `timeout_seconds`：单次调用总耗时，硬上限 300 秒；
- `target_chars`、`overlap_chars`：证据片段切分参数；
- `require_scoped_retrieval`：必须为 `true`；
- `deny_unbounded_global_search`：必须为 `true`；
- `generation_enabled`：必须为 `false`。

项目配置可以在硬上限内调整资源预算。CLI 可以临时收紧项目配置，但不能临时放宽节点、边、
content unit、top-k 或超时上限。

## 3. 检索语义

运行时必须先确认 workspace、storage workspace、图能力、成功导入 ledger、来源 SHA 和本地
content-unit 映射。只有通过校验的来源才能进入 allow-list；BM25 只能在这个 allow-list 内打分。

即使子图覆盖全部 ACTIVE 文档，也仍然是受约束的本地检索：

```json
{
  "candidate_reduction_ratio": 0,
  "scope_reduced": false,
  "out_of_scope_evidence": 0
}
```

这不是失败，也不触发 UI 提示。它与 LightRAG 全局检索不同，因为实际打分输入仍来自显式构造并
验证过的 allow-list。

## 4. 必须失败的情况

以下情况必须 fail closed，不能回退：

- workspace 或 storage workspace 不一致；
- `/graphs` 能力缺失；
- seed、图结构或来源映射无效；
- ledger、workspace 或 source SHA 无法确认；
- 服务端图被截断，或节点、边、content unit、总耗时预算耗尽；
- 检索器返回 allow-list 外的证据；
- allow-list 内没有正分证据。

资源预算耗尽统一返回 `GRAPH_BUDGET_EXCEEDED`。失败 trace 不保存原问题、正文、节点 description、
凭据或完整远端错误。

## 5. 二次开发边界

允许扩展：

- seed 生成器；
- 图边类型、置信度、新鲜度和 hub penalty 策略；
- allow-list 内的向量检索或 reranker；
- 更精确的 remote chunk 到本地 content-unit 映射。

不得绕过：

- workspace 门禁；
- ACTIVE ledger/SHA 校验；
- allow-list 构造和返回前 scope assertion；
- 资源预算；
- `generation_enabled=false`；
- 脱敏 trace；
- 无全局 fallback。

修改后至少运行：

```bash
.venv/bin/python -m pytest -q \
  tests/test_evidence_subgraph.py \
  tests/test_cli_smoke.py
```

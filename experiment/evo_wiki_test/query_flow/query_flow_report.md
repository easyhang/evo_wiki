# LightRAG 启动后查询流程实验报告

- 实验 ID：`20260717T054505Z`
- 服务：`http://127.0.0.1:9621`
- 总体状态：**warning**
- 范围：只读 `/health`、`/openapi.json` 和 `/query`，未调用写端点。

## 服务摘要

```json
{
  "base_url": "http://127.0.0.1:9621",
  "health_before": {
    "status": "healthy",
    "pipeline_busy": false,
    "pipeline_active": false,
    "pipeline_scanning": false,
    "core_version": "1.5.4",
    "api_version": "0313",
    "workspace_empty": true,
    "embedding_batch_num": 8,
    "query_queue": {
      "queued": 0,
      "running": 0,
      "failed_total": 0
    }
  },
  "health_after": {
    "status": "healthy",
    "pipeline_busy": false,
    "pipeline_active": false,
    "pipeline_scanning": false,
    "core_version": "1.5.4",
    "api_version": "0313",
    "workspace_empty": true,
    "embedding_batch_num": 8,
    "query_queue": {
      "queued": 0,
      "running": 0,
      "failed_total": 0
    }
  }
}
```

## 阶段结果

| 阶段 | 状态 | HTTP | 耗时(ms) | 错误码 |
| --- | --- | ---: | ---: | --- |
| preflight_health | warning | 200 | 45 | WORKSPACE_UNKNOWN |
| preflight_openapi | passed | 200 | 18 | - |
| context_retrieval | passed | 200 | 1780 | - |
| answer_generation | passed | 200 | 14075 | - |
| negative_control | warning | 200 | 7756 | REFUSAL_WITH_IRRELEVANT_REFERENCES |
| postflight_health | warning | 200 | 21 | WORKSPACE_UNKNOWN |

## 断点判断

- `preflight_health`：`WORKSPACE_UNKNOWN`；建议结合该阶段的 `response_shape` 和 raw 响应检查。
- `negative_control`：`REFUSAL_WITH_IRRELEVANT_REFERENCES`；建议结合该阶段的 `response_shape` 和 raw 响应检查。
- `postflight_health`：`WORKSPACE_UNKNOWN`；建议结合该阶段的 `response_shape` 和 raw 响应检查。

## 修复建议

- `EMPTY_CONTEXT`：查询前确认文档已 processed 且 chunks_count > 0；当前提交流程仍需后续接入 track polling。
- `WORKSPACE_UNKNOWN`：服务虽然 healthy，但 workspace 为空；在配置 workspace/security-domain 映射前，不应宣称已完成细粒度 ACL 隔离。
- `REFERENCES_EMPTY` 或 `CHUNK_CONTENT_EMPTY`：保持 evidence-required 请求，按 capability detection 拒绝不支持 chunk content 的服务。
- `UNSUPPORTED_CLAIM`：增加 evidence verifier 和无证据拒答 gate；该码属于可信度问题，不等同于 HTTP 故障。
- `REFUSAL_WITH_IRRELEVANT_REFERENCES`：拒答文本本身可能正确，但引用未证明该拒答；需要做引用相关性校验并在不相关时清空或标记 references。
- HTTP/网络错误：先检查服务健康、query queue 和 provider，再决定是否采用有上限的重试。

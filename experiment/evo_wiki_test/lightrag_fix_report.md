# LightRAG embedding batch 修复报告

## 1. 原因分析

【实测结果】修复前，LightRAG 服务的运行配置为 `EMBEDDING_BATCH_NUM=32`。实验日志显示 embedding provider 返回 HTTP 400：`batch size is invalid, it should not be larger than 10`；文档状态为 `pending=8`、`failed=1`、`processed=0`。证据见 [`15_lightrag_pipeline_status.log`](logs/15_lightrag_pipeline_status.log)。

【架构推断】实际调用链如下：

```text
evo_wiki.cli.cmd_run
  -> evo_wiki.lightrag_lane.prepare_lightrag_input
  -> evo_wiki.lightrag_lane.build_lightrag
  -> LightRAGServiceClient.post_json("/documents/text")
  -> LightRAG document_routes.insert_text
  -> pipeline_index_texts
  -> NanoVectorDBStorage._flush_pending_locked
  -> optimized_embedding_function
  -> openai_embed
  -> OpenAI-compatible /embeddings provider
```

【架构推断】evo_wiki 不在本进程内执行 embedding，也没有可以直接控制 LightRAG 内部 embedding 分批的客户端 API。LightRAG 服务读取 `EMBEDDING_BATCH_NUM`，并将它作为向量存储 flush 的最大 embedding batch。故此次兼容修复的实际生效点是 LightRAG 服务运行配置，evo_wiki adapter 同时增加了同名语义的配置校验和可观测日志。

## 2. 修改文件列表

【实测结果】实际修改如下：

- `src/evo_wiki/lightrag_lane.py`
  - 默认 batch size 设为 `8`。
  - 接受 `lightrag.embedding.batch_size` 或 `LIGHTRAG_EMBEDDING_BATCH_SIZE`。
  - 拒绝小于 `1` 或大于 `10` 的值。
  - 每次真实 LightRAG lane 运行输出 `Embedding batch: current=8 total=9`。
  - 在 service/report 元数据中记录 batch 配置。
- `src/evo_wiki/config.py`
  - 默认项目配置和示例配置加入 `lightrag.embedding.batch_size: 8`。
- `lightrag-config.example.json`
  - 增加 embedding batch 配置示例。
- `experiment/evo_wiki_test/project.json`
  - 实验项目显式固定 batch size 为 `8`。
- `experiment/evo_wiki_test/lightrag-config.example.json`
  - 同步实验目录示例配置。
- `/Users/vincentxing/Downloads/Open_WIKI/lightrag/.env`
  - 将实际运行服务的 `EMBEDDING_BATCH_NUM` 从 `32` 改为 `8`。

【实测结果】未修改 `Wiki lane`、CLI 参数设计、文档数据模型、LightRAG 源码、原始法律数据或新增依赖。LightRAG 服务通过容器重建加载 `.env`，持久化数据目录未清理。

## 3. 修改内容说明

【实测结果】evo_wiki 的配置解析现在默认使用 `8`，并将 `10` 作为服务兼容上限。配置值为 `11` 的实测调用会失败并返回：`embedding.batch_size must be between 1 and 10`。

【实测结果】服务重建后 `/health` 返回：

```json
{"embedding_binding":"openai","embedding_model":"text-embedding-v3","embedding_batch_num":8}
```

【实测结果】已有 LightRAG provider 的重试链路未被改动。修复后通过官方 `/documents/reprocess_failed` 重新处理原先挂起/失败的 9 个文档，实际 pipeline 完成了 embedding、chunk、实体/关系合并和向量写入。

【架构推断】重试仍由 LightRAG provider 的既有 tenacity 包装负责；本次没有在 evo_wiki adapter 叠加 HTTP 重试，避免把异步文档提交误判为可安全重复提交。

## 4. 测试命令

【实测结果】执行了以下命令和操作：

```bash
cd /Users/vincentxing/Downloads/Open_WIKI/lightrag
docker compose -f docker-compose.yml up -d --force-recreate lightrag

curl -X POST http://127.0.0.1:9621/documents/reprocess_failed

cd /Users/vincentxing/Downloads/Open_WIKI/evo_wiki
.venv/bin/evo-wiki prepare-lightrag --root experiment/evo_wiki_test
LIGHTRAG_BASE_URL=http://127.0.0.1:9621 \
  .venv/bin/evo-wiki run \
  --root experiment/evo_wiki_test \
  --lane lightrag \
  --smoke-query '韩永仁案中为什么认定自首？'
```

【实测结果】相关证据日志：

- [`16_lightrag_reprocess_after_batch_fix.log`](logs/16_lightrag_reprocess_after_batch_fix.log)
- [`17_lightrag_reprocess_poll.log`](logs/17_lightrag_reprocess_poll.log)
- [`19_prepare_lightrag_after_fix.log`](logs/19_prepare_lightrag_after_fix.log)
- [`20_run_lightrag_smoke_after_fix.log`](logs/20_run_lightrag_smoke_after_fix.log)
- [`22_lightrag_post_fix_acceptance.log`](logs/22_lightrag_post_fix_acceptance.log)

【实测结果】回归测试：

```text
23 passed in 1.24s
```

## 5. 修复前后日志对比

| 检查项 | 修复前 | 修复后 |
| --- | --- | --- |
| embedding batch 配置 | `32` | `8` |
| 上游 batch 400 | 出现 `batch size ... larger than 10` | `embedding_400_count=0` |
| 文档状态 | `pending=8, failed=1, processed=0` | `pending=0, failed=0, processed=9` |
| chunks | 无有效完成结果 | 9 个文档均有值，分别为 `3,4,3,4,3,3,4,5,5` |
| pipeline | `Enqueued document processing pipeline stopped` | `pipeline_busy=false`，9 个文档完成 |
| smoke answer | `[no-context]` | 1184 字符，未出现 `[no-context]` |
| references | `[]` | 5 条，包含 `102_韩永仁故意伤害案.txt` |

【实测结果】修复后的 LightRAG 容器日志包含多条 `batch_num=8`，例如实体 `38 vectors in 5 batch(es)`、关系 `41 vectors in 6 batch(es)`；验收脚本统计 `embedding_batch_num_8_log_count=24`。

## 6. 是否完成端到端闭环

【实测结果】已完成当前实验数据的端到端闭环：

```text
documents.jsonl
  -> LightRAG document records
  -> reprocess_failed
  -> embedding batch <= 8
  -> chunks_count > 0
  -> vector/entity/relation indexing
  -> /query
  -> non-empty answer + 5 references
```

【实测结果】9/9 文档最终为 `processed`，状态计数为 `processed=9, failed=0`。query 原始响应保存在 [`artifacts/lightrag/queries/smoke-test.json`](artifacts/lightrag/queries/smoke-test.json)。

【边界说明】由于 LightRAG 持久化存储已经含有本次实验文档，且服务会对相同 `file_source` 返回 HTTP 409，本次没有清空服务数据后重复上传。修复后的实际 embedding/index 成功来自对这些已提交文档执行官方 `reprocess_failed`；随后按要求重新执行了 `prepare-lightrag` 和 evo_wiki smoke lane。实验目录仍保留 `version1/`、`version2/`，没有修改原始数据。


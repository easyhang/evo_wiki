# LightRAG 子 Skill 示例

本示例只属于 LightRAG 工作流（lane），与 Wiki 工作流完全分离。

```text
skills/evo-wiki-lightrag/examples/basic/
  corpus/raw/lightrag-intro.md
```

运行预演（dry-run）：

```bash
PYTHONPATH=src python3 skills/evo-wiki-lightrag/scripts/dry-run-example.py
```

预期结果：

- 创建临时 workspace；
- 扫描 `corpus/raw/lightrag-intro.md`。
- 生成 `artifacts/lightrag/input/documents.jsonl`；
- 生成预演报告 `artifacts/lightrag/reports/lightrag-report.json`；
- 不调用真实 LightRAG 服务，不需要服务地址或鉴权环境变量。

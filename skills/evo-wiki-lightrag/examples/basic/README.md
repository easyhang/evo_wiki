# LightRAG 子 Skill 样例

本样例只属于 LightRAG lane，和 Wiki lane 完全分离。

```text
skills/evo-wiki-lightrag/examples/basic/
  corpus/raw/lightrag-intro.md
```

运行 dry-run：

```bash
PYTHONPATH=src python3 skills/evo-wiki-lightrag/scripts/dry-run-example.py
```

预期结果：

- 创建临时 workspace。
- 扫描 `corpus/raw/lightrag-intro.md`。
- 生成 `artifacts/lightrag/input/documents.jsonl`。
- 生成 dry-run 的 `artifacts/lightrag/reports/lightrag-report.json`。
- 不调用真实 LightRAG 服务，不需要服务地址或鉴权环境变量。

# LightRAG 样例语料

这是 LightRAG 子 Skill 的最小样例。它只用于验证：

- 从 `corpus/raw/` 扫描文本文件。
- 生成 `artifacts/lightrag/input/documents.jsonl`。
- 在 dry-run 模式下生成 `lightrag-report.json`。

该样例不生成 Wiki 页面，也不依赖 Wiki lane。

## 内容

Evo Wiki 的 LightRAG lane 面向智能体问答知识库。默认输入必须来自原始语料 `corpus/`，而不是模型生成的 Wiki 页面。

如果语料新增，LightRAG 可以准备新的输入；如果语料修改，需要通过报告确认重新导入；如果语料删除，必须标记 `requires_rebuild`，因为旧知识未必能从已有图谱或向量中安全删除。

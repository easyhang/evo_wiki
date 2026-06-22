# 示例语料

Evo wiki 是一个面向 Claude Code 的 LLM Wiki 知识平台开发工具。

它支持两条完全分离的流程：

- Wiki lane：生成面向人阅读的静态 Wiki。
- LightRAG lane：生成面向智能体问答的 GraphRAG 知识库。

默认推荐先生成 Wiki，人工确认内容结构后，再决定是否生成 LightRAG。

# Changelog

## 2.0.1

- 增加 `XZT` 分支的克隆、ZIP 下载、内置示例和自有语料开箱使用入口。
- 补齐外部 LightRAG 多 workspace 请求头传递；一个项目目录固定绑定一个 workspace，
  多个项目可共享同一服务。
- 修复 wiki-only 自检和静态示例预览，且不再依赖或清理相邻 LightRAG 目录。

## 2.0.0

- 新建 workspace 默认采用可执行的 Wiki 内容契约 v2，旧 workspace 无迁移兼容。
- 增加 corpus 到来源 Wiki 的完整覆盖、唯一映射、首页可达和实体歧义报告。
- 标准化结构化引用到来源 Wiki、citation 约束实体链接和问答会话恢复。
- 统一安全 Markdown 渲染器，覆盖常用块级与行内语法并保持 XSS/远程图片防护。
- 将本地审核中心、来源链接和安全降级规则纳入产品开发标准与发行套件。

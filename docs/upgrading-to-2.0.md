# 升级到 Evo Wiki 2.0

Evo Wiki 2.0 不修改查询 API schema、`wiki-registry.json` schema 或 SQLite schema。现有 1.x
workspace 可以直接使用 2.0 CLI；缺少 `wiki.json.content_contract_version` 时继续采用兼容契约
v1，工具不会写回配置。

## 为现有项目启用 2.0 内容契约

先升级 CLI 并保持原配置，运行：

```bash
evo-wiki lint-wiki --root /path/to/workspace
```

补齐每份 corpus 文件对应的来源页、首页入口、实体 `graph_label` 和必要别名。确认报告无 error
后，在 `wiki.json` 顶层加入：

```json
{
  "content_contract_version": 2
}
```

再依次运行 lint、render 和 generate dry-run。若 basename 冲突，应重命名 corpus 文件并同步
来源 frontmatter；不要通过猜测或多目标映射绕过冲突。

## 兼容性说明

- 契约 v1：保持 1.x 内容检查与运行行为。
- 契约 v2：增加 corpus 来源覆盖、规范路径、首页可发现性和歧义报告。
- 查询响应仍为 schema v2，公共 registry 仍为 schema v1。
- SQLite 仍使用当前 v5 schema，不执行新的数据库 migration。
- 2.0 前端会重新渲染已保存的纯文本回答，不保存旧 HTML，因此无需会话迁移。

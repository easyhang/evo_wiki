# 本地平台示例

该目录只提供可提交的配置模板，不包含 SQLite、生成 HTML 或真实凭据。

```bash
evo-wiki init --root ./demo --profile local-platform
cp examples/local-platform/wiki.example.json ./demo/wiki.json
cp examples/local-platform/lightrag-config.example.json \
  ./demo/lightrag-config.json
```

编辑 `wiki.json` 和 `lightrag-config.json`，把资料放入
`demo/corpus/raw/`，再让 Evo Wiki Skill 整理正文。`base_url` 和 `workspace`
必须指向真实的已有 LightRAG 服务；Evo Wiki 不会自行启动服务。

`wiki.example.json` 默认启用 `content_contract_version: 2`。可参考
`wiki-src-template/` 建立首页、来源、实体和概念页；每份 corpus 文件必须由唯一来源页声明，
且所有交付页面都应从首页发现。

示例默认只携带最近 3 个已验证问答对，并以 depth 2、最多 50 节点加载图谱。若预演返回
`GENERATION_RECONCILE_REQUIRED`，先运行输出中的 `state reconcile` review 命令，确认远端
观察后再显式 `--apply`；不要手工编辑 SQLite。

完成正文后先预演，再生成：

```bash
export EVO_WIKI_QUERY_AUDIT_KEY='至少 16 bytes 的本地随机值'
evo-wiki generate --root ./demo --dry-run --json
evo-wiki generate --root ./demo
evo-wiki serve --root ./demo
```

`local-platform` 只允许 loopback 预览，不能直接用于公网部署。

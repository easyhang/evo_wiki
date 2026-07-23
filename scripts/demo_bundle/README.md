# Evo Wiki 法律案例本地演示包 {{VERSION}}

这是一个清洁、可移植的完整在线演示包，包含：

- 9 份 UTF-8 法律案例原文；
- 22 个 Wiki 源页面及生成后的完整 Web 平台；
- 清洁后的 Evo Wiki SQLite 必要状态与 9 份可用来源快照；
- 已构建的 LightRAG 文档、图谱和 1024 维向量索引；
- Evo Wiki {{VERSION}} Python wheel。

源码版本：`{{SOURCE_COMMIT}}`

## 运行环境

- macOS 或 Linux；
- Python 3.10 或更高版本；
- Docker Desktop/Engine，支持 `docker compose`；
- 首次运行可以联网下载 Python 依赖和固定版本的 LightRAG 镜像；
- 可访问 OpenAI 兼容接口的 `qwen-plus` 和
  `text-embedding-v3`（1024 维）凭据。

Docker 会拉取以下固定、多架构镜像：

`{{LIGHTRAG_IMAGE}}`

## 首次启动

解压后进入本目录：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

- `LLM_BINDING_HOST`
- `LLM_BINDING_API_KEY`
- `EMBEDDING_BINDING_HOST`
- `EMBEDDING_BINDING_API_KEY`

不要更改 `EMBEDDING_MODEL=text-embedding-v3` 或
`EMBEDDING_DIM=1024`，否则现有向量索引不兼容。

然后执行：

```bash
./check.sh
./start.sh
```

首次启动会创建 `.runtime/venv`、联网安装 `requirements.txt` 中的
Python 依赖，并在 `.runtime/` 内生成仅供本机使用的审核 HMAC 密钥。
密钥不会写入语料、SQLite 或生成页面。

启动完成后访问：

- Wiki：<http://127.0.0.1:8080/>
- 问答、图谱和实体：<http://127.0.0.1:8080/app/>
- LightRAG WebUI：<http://127.0.0.1:9621/webui/>

按 `Ctrl+C` 停止 Evo Wiki。随后运行：

```bash
./stop.sh
```

以停止 LightRAG 容器。

## 预期检查结果

`./check.sh` 应报告：

- 文件 SHA-256 校验通过；
- 9 份案例语料；
- 22 个 Wiki Markdown 页面；
- Docker Compose 配置有效；
- 模型和 embedding 配置完整。

启动期间，`evo-wiki doctor --check-service` 应确认：

- LightRAG workspace 为 `evo_wiki`；
- `text-embedding-v3` 的维度为 1024；
- 9 个文档 binding 均为 `PROCESSED/OPEN`；
- 查询、图谱和结构化引用接口可用。

推荐验收问题：

```text
韩永仁案为什么认定自首？
```

答案应包含非空正文，并在本地证据足够时显示结构化引用卡片。
模型生成结果可能随服务时间和模型版本变化。

## 常见问题

- `pip` 报 CA/证书校验错误：先修复本机 Python CA 证书，或配置组织提供的
  受信任 Python 包镜像后重试。启动脚本不会自动关闭 TLS 校验。
- `9621` 或 `8080` 端口被占用：先停止占用端口的旧服务，再重新运行。
- `doctor --check-service` 报 workspace 或 embedding 不匹配：确认 `.env`
  中仍为 `WORKSPACE=evo_wiki`、`EMBEDDING_MODEL=text-embedding-v3` 和
  `EMBEDDING_DIM=1024`，并执行 `./stop.sh` 后重启。
- 问答失败但 Wiki 可访问：用 `docker compose logs lightrag` 检查模型
  endpoint、额度和网络；不要把填有密钥的 `.env` 发给其他人。

## 数据与目录

- `workspace/corpus/raw/legal_docs/`：9 份原始案例；
- `workspace/artifacts/wiki/wiki-src/`：22 个 Wiki 源页面；
- `workspace/artifacts/platform/`：已生成的 Wiki/SPA；
- `workspace/artifacts/state/`：清洁后的 SQLite 和来源快照；
- `lightrag-data/rag_storage/evo_wiki/`：图、KV 和向量索引；
- `packages/`：Evo Wiki wheel；
- `bundle-manifest.json`：版本、模型、计数与排除项；
- `SHA256SUMS`：包内文件校验值。

## 隐私与安全边界

此包不包含分发者的模型密钥、`.env`、查询/审核历史、通知、运行日志、
数据库备份、LightRAG LLM response cache、本机绝对路径或临时锁文件。

服务只绑定 `127.0.0.1`，用于单机演示。不要把本包的
`local_single_user` 服务直接暴露到公网。

## 已知限制

- 这不是完全离线包；首次启动需要联网安装依赖和拉取镜像。
- 真实问答需要收件人自己的模型和 embedding 凭据，并会产生相应费用。
- 现有索引只能与 `text-embedding-v3/1024` 兼容配置共同使用。
- 本包不提供重新建索引流程；切换 embedding 模型需回到完整开发仓库重建。
- 模型、供应商网络或额度异常时，静态 Wiki 仍可读取，但问答会失败。
- 本包是法律案例演示，不构成法律意见。

## 许可

Evo Wiki 许可见 `LICENSE`。LightRAG 许可见
`licenses/LightRAG-LICENSE`，第三方说明见 `THIRD_PARTY_NOTICES.md`。
案例数据的分发权由本包分发者确认，本包不另行授予第三方数据权利。

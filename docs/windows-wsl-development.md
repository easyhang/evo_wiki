# Windows + WSL2 开发指南

Evo Wiki 的完整开发和运行基线是 Linux/POSIX。Windows 用户应在 WSL2 的 Ubuntu 中运行
Evo Wiki，并通过 Docker Desktop 的 WSL Integration 使用 Linux 容器。原生 Windows、
PowerShell 或 CMD 不保证能够完成日志、状态迁移、网关和运维流程。

## 支持范围

| 环境 | 支持状态 | 说明 |
| --- | --- | --- |
| Linux | 完整支持 | 推荐的开发和部署环境 |
| macOS | 支持本地开发 | 具备所需 POSIX 文件锁和权限语义 |
| Windows + WSL2 | 完整开发路径 | 在 Ubuntu 终端中按 Linux 流程运行 |
| 原生 Windows | 不支持完整流程 | 缺少 `fcntl` 文件锁和部分 Unix 进程语义 |

本指南不改变产品边界：Evo Wiki 连接用户已经运行的 LightRAG 服务，但不会安装、启动或
托管 LightRAG，也不会保存模型服务凭据。

## 1. 安装 WSL2 Ubuntu

要求 Windows 11，或支持 WSL2 的 Windows 10。以管理员身份打开 PowerShell：

```powershell
wsl --install -d Ubuntu
```

安装完成后重启 Windows，从开始菜单打开 Ubuntu，并按提示创建 Linux 用户名和密码。然后在
PowerShell 中更新并检查 WSL：

```powershell
wsl --update
wsl -l -v
```

Ubuntu 的 `VERSION` 必须为 `2`。若不是，执行：

```powershell
wsl --set-version Ubuntu 2
wsl --set-default-version 2
```

参考 Microsoft 官方文档：[安装 WSL](https://learn.microsoft.com/windows/wsl/install) 和
[WSL 基本命令](https://learn.microsoft.com/windows/wsl/basic-commands)。

## 2. 配置 Docker Desktop

安装并启动 Docker Desktop，然后检查以下设置：

1. 在 `Settings > General` 启用 `Use WSL 2 based engine`；
2. 在 `Settings > Resources > WSL Integration` 启用 Ubuntu；
3. 确认 Docker Desktop 使用 Linux containers，而不是 Windows containers；
4. 点击 `Apply & restart`。

打开 Ubuntu 终端验证：

```bash
docker version
docker compose version
```

`docker version` 应同时显示 Client 和 Server。若 Ubuntu 中找不到 `docker`，先重新检查 WSL
Integration，不要在 Ubuntu 内重复安装另一套 Docker daemon。

参考 Docker 官方文档：[Docker Desktop WSL2 backend](https://docs.docker.com/desktop/features/wsl/)
和 [Develop with Docker and WSL2](https://docs.docker.com/desktop/features/wsl/use-wsl/)。

## 3. 将仓库放入 Linux 文件系统

在 Ubuntu 终端中安装基础工具：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip build-essential curl ca-certificates
```

把仓库克隆到 WSL 的 Linux 文件系统：

```bash
mkdir -p ~/src
cd ~/src
git clone <repository-url> Open_WIKI
cd ~/src/Open_WIKI/evo_wiki
```

必须满足以下约束：

- 仓库、`.venv`、Evo Wiki workspace 和 SQLite 状态位于 `/home/<user>/...`；
- 不要从 `/mnt/c/...`、`/mnt/d/...` 等 Windows 挂载目录运行项目；
- 不要在 Windows 和 WSL 之间共用 `.venv`；
- 不要把运行状态放在 SMB、NFS 或同步盘中。

用下面的命令确认当前位置：

```bash
pwd
uname -s
```

`pwd` 不应以 `/mnt/` 开头，`uname -s` 应输出 `Linux`。

## 4. 安装 Evo Wiki

在 Ubuntu 终端中运行：

```bash
cd ~/src/Open_WIKI/evo_wiki
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

验证安装和当前代码基线：

```bash
evo-wiki --version
python -m pytest -q
```

之后每次打开新的 Ubuntu 终端，先重新激活环境：

```bash
cd ~/src/Open_WIKI/evo_wiki
. .venv/bin/activate
```

## 5. 准备 LightRAG

完整平台需要一个真实、已经运行的 LightRAG 服务。可以连接团队共享服务，也可以通过
Docker Desktop 在本机启动 LightRAG。具体模型、Embedding 和存储配置遵循 LightRAG 自身
文档；不要把 API key 写入仓库。

若 LightRAG 的 9621 端口已发布到本机，可在 Windows 浏览器和 WSL 中分别检查：

```bash
curl http://localhost:9621/health
docker ps
```

Evo Wiki 的 `<workspace>/lightrag-config.json` 至少需要真实的服务地址和 workspace：

```json
{
  "mode": "service",
  "base_url": "http://localhost:9621",
  "workspace": "evo_wiki",
  "api_key_env": "LIGHTRAG_API_KEY",
  "bearer_token_env": "LIGHTRAG_BEARER_TOKEN"
}
```

若使用团队共享服务，把 `base_url` 改为该服务的真实可访问地址。不要默认猜测
`localhost`。只通过 Ubuntu 当前 shell 注入凭据：

```bash
export LIGHTRAG_API_KEY='...'
export EVO_WIKI_QUERY_AUDIT_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

如果服务使用 Bearer token，则设置 `LIGHTRAG_BEARER_TOKEN`。不要把真实密钥写入
`lightrag-config.json`、Git、SQLite、日志或报告。

## 6. 初始化并生成平台

以下示例把运行 workspace 放在 WSL 的 Linux 文件系统：

```bash
mkdir -p ~/work
evo-wiki init --root ~/work/demo --profile local-platform
```

把语料放入 `~/work/demo/corpus/raw/`，再按照根 `SKILL.md` 和 Wiki Skill 完成
`artifacts/wiki/wiki-src/` 中的正文。初始化产生的占位页不能作为完整交付。

完整平台需要复制并编辑 LightRAG 配置：

```bash
cp ~/work/demo/lightrag-config.example.json ~/work/demo/lightrag-config.json
evo-wiki doctor --root ~/work/demo --check-service
```

服务检查通过后，先执行预演（dry-run），再生成和预览：

```bash
evo-wiki generate --root ~/work/demo --dry-run --json
evo-wiki generate --root ~/work/demo --smoke-query "这个项目解决什么问题？" --json
evo-wiki serve --root ~/work/demo --listen 127.0.0.1:8080
```

在 Windows 浏览器中打开 `http://localhost:8080`。WSL2 和 Docker Desktop 通常会把本机
发布端口转发给 Windows；本地开发不需要额外配置 `portproxy`。

如果只需要静态 Wiki，不需要 LightRAG、问答或图谱，可使用：

```bash
evo-wiki init --root ~/work/wiki-demo --profile wiki-only
evo-wiki generate --root ~/work/wiki-demo --target wiki --json
```

## 7. 日常自检

遇到问题时，在 Ubuntu 终端依次检查：

```bash
uname -s
pwd
python3 --version
docker version
curl http://localhost:9621/health
evo-wiki doctor --root ~/work/demo --check-service
```

预期结果：

- `uname -s` 为 `Linux`；
- 项目和 workspace 不在 `/mnt/<drive>/...`；
- Python 版本不低于 3.10；
- Docker Client 和 Server 均可用；
- LightRAG health endpoint 可达；
- `doctor` 使用的 `base_url`、workspace 和凭据与真实服务一致。

## 常见问题

### `JOURNAL_LOCK_UNSUPPORTED`

命令实际运行在原生 Windows Python 中。停止当前流程，进入 Ubuntu/WSL 终端，重新创建
Linux `.venv` 后再运行。

### Ubuntu 中找不到 `docker`

确认 Docker Desktop 正在运行，并在 `Settings > Resources > WSL Integration` 中启用了
当前 Ubuntu。若该选项不存在，确认 Docker Desktop 已切换到 Linux containers。

### `doctor --check-service` 无法连接 LightRAG

先执行 `docker ps` 和 `curl http://localhost:9621/health`。若连接远程服务，确认 WSL 能访问
其域名或 IP，并检查 `lightrag-config.json` 中的 `base_url` 和 workspace。

### 文件操作慢、权限异常或 SQLite 锁异常

检查 `pwd`。如果路径位于 `/mnt/c`、`/mnt/d` 或其他 Windows 挂载目录，把仓库和 workspace
重新克隆或复制到 `~/src`、`~/work` 等 Linux 路径，并重新创建 `.venv`；不要复制旧运行数据库。

### Windows 浏览器无法访问本地端口

确认服务监听地址和端口正确，并先在 WSL 中使用 `curl` 验证。开发预览保持
`127.0.0.1`；不要为了排查问题直接把未鉴权服务暴露到局域网或公网。

"""export-platform: produce a deployable read-only Web platform directory.

The platform directory is the *single core artifact* of the Web platform:

    platform/
      index.html …            ← Wiki static site (render-wiki output, verbatim)
      app/                    ← fixed SPA shell (问答 / 图谱 / 实体枢纽)
      assets/                 ← shared theme.css + nav.js + wiki style/app
      nginx.conf              ← static delivery + trusted gateway proxy
      README.md               ← how to run (local nginx) + self-package Docker

Docker is *not* provided by the tool — the platform dir is a standard static
site, so developers self-package it with a 3-line Dockerfile. The tool is only
responsible for the correctness of the directory + nginx config, not for the
container. See design_update/2026-07-14.html §5.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

from .config import EvoConfig
from .paths import ProjectPaths
from .query_gateway import gateway_settings
from .utils import read_json


def export_platform(paths: ProjectPaths, config: EvoConfig) -> dict:
    """Materialize the platform directory under ``artifacts/platform/``."""
    config.validate(paths.root, target="platform")
    if not (paths.wiki_dist / "index.html").exists():
        raise RuntimeError(
            "Wiki dist not found — run `evo-wiki run --lane wiki` before export-platform."
        )
    lightrag_manifest = read_json(paths.lightrag / "manifest.json", {})
    lightrag_report = read_json(paths.lightrag_reports / "lightrag-report.json", {})
    if lightrag_manifest.get("status") != "success" or lightrag_report.get("status") != "success":
        raise RuntimeError(
            "LightRAG lane has not completed successfully — run `evo-wiki run --lane lightrag` "
            "or `evo-wiki run --lane both` before export-platform so Q&A and graph pages are backed by LightRAG."
        )

    base_url = str(config.project.get("lightrag", {}).get("base_url") or "").strip().rstrip("/")
    if not base_url or "YOUR_LIGHTRAG_SERVER" in base_url:
        raise RuntimeError(
            "LightRAG base_url is required for platform export. Create `lightrag-config.json` "
            "from `lightrag-config.example.json` and set `base_url` to your LightRAG Server, "
            "for example {\"base_url\": \"http://172.20.105.79:9621\"}."
        )

    settings = gateway_settings(config.project)
    if settings.mode == "disabled":
        raise RuntimeError(
            "Trusted query gateway is disabled. Set query_gateway.mode to "
            "shadow or enforce and run gateway check before export-platform."
        )
    gateway_host = (
        "127.0.0.1"
        if settings.listen_host in {"0.0.0.0", "::"}
        else settings.listen_host
    )
    gateway_url = f"http://{gateway_host}:{settings.listen_port}"

    paths.artifacts.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=".platform-staging-",
            dir=paths.artifacts,
        )
    )
    try:
        _materialize_platform(
            paths,
            staging,
            gateway_url=gateway_url,
            auth_mode=settings.auth_mode,
            principal_header=settings.principal_header,
            max_body_bytes=settings.max_body_bytes,
            request_timeout_seconds=settings.request_timeout_seconds,
        )
        _activate_platform(paths.platform, staging)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    return {
        "path": str(paths.platform),
        "wiki": True,
        "lightrag": True,
        "query_gateway_url": gateway_url,
        "status_baked": [],
        "status_public": False,
        "lightrag_mode": "private_external_service",
        "query_gateway_mode": settings.mode,
    }


def _materialize_platform(
    paths: ProjectPaths,
    destination: Path,
    *,
    gateway_url: str,
    auth_mode: str,
    principal_header: str,
    max_body_bytes: int,
    request_timeout_seconds: float,
) -> None:
    """Build a complete platform in an unpublished staging directory."""
    _copy_tree(paths.wiki_dist, destination)

    public_status = destination / "status"
    if public_status.is_symlink() or public_status.is_file():
        public_status.unlink()
    elif public_status.is_dir():
        shutil.rmtree(public_status)

    (destination / "nginx.conf").write_text(
        _nginx_conf(
            gateway_url,
            auth_mode=auth_mode,
            principal_header=principal_header,
            max_body_bytes=max_body_bytes,
            request_timeout_seconds=request_timeout_seconds,
        ),
        encoding="utf-8",
    )
    (destination / "README.md").write_text(
        _readme(gateway_url, auth_mode=auth_mode),
        encoding="utf-8",
    )


def _activate_platform(destination: Path, staging: Path) -> None:
    """Replace the generated platform without exposing a partial directory."""
    previous = destination.parent / (
        f".platform-previous-{uuid.uuid4().hex}"
    )
    had_previous = destination.exists()
    if had_previous:
        os.replace(destination, previous)
    try:
        os.replace(staging, destination)
    except Exception:
        if had_previous and previous.exists() and not destination.exists():
            os.replace(previous, destination)
        raise
    if previous.exists():
        shutil.rmtree(previous)


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy a directory tree, skipping the destination itself."""
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def _nginx_conf(
    gateway_url: str,
    *,
    auth_mode: str,
    principal_header: str,
    max_body_bytes: int,
    request_timeout_seconds: float,
) -> str:
    authentication = (
        """      auth_basic "EvoWiki";
      auth_basic_user_file conf/htpasswd;
      proxy_set_header {principal_header} $remote_user;""".format(
            principal_header=principal_header
        )
        if auth_mode == "trusted_proxy"
        else f"      proxy_set_header {principal_header} local-single-user;"
    )
    timeout = max(1, int(request_timeout_seconds))
    return f"""# EvoWiki read-only platform. RAG reader traffic is governed by the trusted query gateway.
# Run locally:  nginx -p . -c nginx.conf   (or copy into your nginx conf.d)
# The LightRAG address and credentials exist only in the gateway process.

worker_processes 1;
events {{ worker_connections 1024; }}

http {{
  include       /etc/nginx/mime.types;
  default_type  application/octet-stream;
  sendfile      on;
  limit_req_zone $binary_remote_addr zone=evo_query:10m rate=5r/s;
  server {{
    listen 8080;
    server_name _;
    root .;   # platform/ is the document root

    # Wiki static site (render-wiki output, verbatim).
    location / {{
      try_files $uri $uri/ /index.html;
    }}

    # SPA shell (问答 / 图谱 / 实体枢纽).
    location /app/ {{
      try_files $uri $uri/ /app/index.html;
    }}

    # Deployment files are stored beside the static output but are never
    # reader-facing; nginx.conf contains the internal upstream address.
    location = /nginx.conf {{ return 404; }}
    location = /README.md {{ return 404; }}

    # Never expose LightRAG's native API surface through the public origin.
    location = /query {{ return 404; }}
    location = /health {{ return 404; }}
    location = /openapi.json {{ return 404; }}
    location = /graphs {{ return 404; }}
    location ^~ /documents/ {{ return 404; }}
    location ^~ /graph/ {{ return 404; }}

    # Q&A → trusted query gateway. Incoming identity headers are replaced.
    location /api/query {{
      limit_req zone=evo_query burst=10 nodelay;
      client_max_body_size {max_body_bytes};
      proxy_pass {gateway_url}/api/query;
      proxy_set_header Host $host;
      proxy_set_header Content-Type $content_type;
{authentication}
      proxy_connect_timeout 5s;
      proxy_read_timeout {timeout}s;
      proxy_send_timeout {timeout}s;
    }}

    # Graph reader endpoints pass through the same identity/domain/maintenance gate.
    location /api/graphs {{
      limit_req zone=evo_query burst=10 nodelay;
      proxy_pass {gateway_url}/api/graphs;
      proxy_set_header Host $host;
{authentication}
      proxy_read_timeout {timeout}s;
    }}

    location /api/graph/label/ {{
      limit_req zone=evo_query burst=10 nodelay;
      proxy_pass {gateway_url}/api/graph/label/;
      proxy_set_header Host $host;
{authentication}
      proxy_read_timeout {timeout}s;
    }}

    # Defense in depth: internal state must remain inaccessible even if a
    # future packaging change accidentally creates this path.
    location ^~ /status/ {{
      deny all;
      return 404;
    }}
  }}
}}
"""


def _readme(gateway_url: str, *, auth_mode: str) -> str:
    authentication = (
        "创建 `conf/htpasswd`，并由部署方安全管理密码。"
        if auth_mode == "trusted_proxy"
        else "当前是仅限本机开发的 local_single_user 模式，不得公网部署。"
    )
    return f"""# Evo wiki platform

只读 Web 知识平台产物：Wiki 静态站、SPA 和 nginx 配置。RAG reader 请求全部转发到可信查询网关 `{gateway_url}`；Nginx 不再持有 LightRAG 地址或凭据。

{authentication}

## 启动顺序

```bash
export EVO_WIKI_QUERY_AUDIT_KEY='由秘密管理系统提供的随机值'
evo-wiki gateway check --root <workspace>
evo-wiki gateway serve --root <workspace>
cd artifacts/platform
nginx -p . -c nginx.conf
```

打开 `http://localhost:8080`(`Wiki`)、`http://localhost:8080/app`(`问答/图谱`)。

## 上线：网关规则

nginx.conf 只代理可信查询网关的 reader 路径。LightRAG 应只绑定 loopback 或私有网络，浏览器和 Nginx 都不能直连它。trusted-proxy 模式要求 Basic Auth、OAuth 或等价的已验证身份来源。

## Docker 自封装(工具不生成镜像,开发者自理)

平台目录是标准静态站,Docker 封装只需:

```dockerfile
FROM nginx:1.27-alpine
COPY . /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/nginx.conf
EXPOSE 8080
```

```yaml
# docker-compose.yml
services:
  platform:
    build: ./artifacts/platform
    ports: ["8080:8080"]
    # gateway 必须在容器可访问的私有地址运行；不要把 LightRAG 凭据
    # 放入 platform 容器。
```

工具只对“平台目录 + nginx 配置的正确性”负责,不对“容器能否跑起来”负责。
"""

"""export-platform: produce a deployable read-only Web platform directory.

The platform directory is the *single core artifact* of the Web platform:

    platform/
      index.html …            ← Wiki static site (render-wiki output, verbatim)
      app/                    ← fixed SPA shell (问答 / 图谱 / 实体枢纽)
      assets/                 ← shared theme.css + nav.js + wiki style/app
      status/*.json           ← baked-in RAG state snapshots (留档, not shown)
      nginx.conf              ← routing + LightRAG proxy + key injection
      README.md               ← how to run (local nginx) + self-package Docker

Docker is *not* provided by the tool — the platform dir is a standard static
site, so developers self-package it with a 3-line Dockerfile. The tool is only
responsible for the correctness of the directory + nginx config, not for the
container. See design_update/2026-07-14.html §5.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .config import EvoConfig
from .paths import ProjectPaths
from .utils import read_json

# RAG-state artifacts to bake into platform/status/ (留档备查, not a reader view).
RAG_STATUS_FILES = [
    ("manifest.json", "lightrag", "manifest.json"),
    ("lightrag-report.json", "lightrag_reports", "lightrag-report.json"),
    ("lightrag-import-ledger.json", "lightrag_state", "lightrag-import-ledger.json"),
]


def export_platform(paths: ProjectPaths, config: EvoConfig) -> dict:
    """Materialize the platform directory under ``artifacts/platform/``."""
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

    base_url = config.project.get("lightrag", {}).get("base_url", "http://127.0.0.1:9621")

    # Fresh platform dir.
    if paths.platform.exists():
        shutil.rmtree(paths.platform)
    paths.platform.mkdir(parents=True)

    # 1. Wiki static site (verbatim) — includes app/ SPA + assets/shared/.
    _copy_tree(paths.wiki_dist, paths.platform)

    # 2. RAG state snapshots (留档).
    status_dir = paths.platform / "status"
    status_dir.mkdir(exist_ok=True)
    baked = []
    for out_name, path_attr, fname in RAG_STATUS_FILES:
        src = getattr(paths, path_attr) / fname
        if src.exists():
            shutil.copy2(src, status_dir / out_name)
            baked.append(out_name)

    # 3. nginx.conf: routing + LightRAG proxy + key injection.
    (paths.platform / "nginx.conf").write_text(_nginx_conf(base_url), encoding="utf-8")

    # 4. README: local run + Docker self-packaging.
    (paths.platform / "README.md").write_text(_readme(base_url), encoding="utf-8")

    return {
        "path": str(paths.platform),
        "wiki": True,
        "lightrag": True,
        "lightrag_base_url": base_url,
        "status_baked": baked,
        "lightrag_mode": "external_service",
    }


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy a directory tree, skipping the destination itself."""
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def _nginx_conf(lightrag_base_url: str) -> str:
    # Key is injected from env at the proxy layer — never shipped to the browser.
    # Only reader-facing endpoints are proxied; all write endpoints are absent.
    return f"""# Evo wiki read-only platform. Single nginx process: static files + LightRAG proxy.
# Run locally:  nginx -p . -c nginx.conf   (or copy into your nginx conf.d)
# LightRAG runs as an external service at {lightrag_base_url}; its key is injected
# here from $LIGHTRAG_API_KEY / $LIGHTRAG_BEARER_TOKEN and never reaches the browser.

worker_processes 1;
events {{ worker_connections 1024; }}

http {{
  include       mime.types;
  default_type  application/octet-stream;
  sendfile      on;
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

    # Q&A → LightRAG POST /query (key injected, never shipped to browser).
    location /api/query {{
      proxy_pass {lightrag_base_url}/query;
      proxy_set_header Host $host;
      proxy_set_header Content-Type $content_type;
      proxy_set_header X-API-Key ${{LIGHTRAG_API_KEY}};
      proxy_set_header Authorization "Bearer ${{LIGHTRAG_BEARER_TOKEN}}";
    }}

    # Graph subgraph → LightRAG GET /graphs (reader-facing only; no write endpoints).
    location /api/graphs {{
      proxy_pass {lightrag_base_url}/graphs;
      proxy_set_header Host $host;
      proxy_set_header X-API-Key ${{LIGHTRAG_API_KEY}};
      proxy_set_header Authorization "Bearer ${{LIGHTRAG_BEARER_TOKEN}}";
    }}

    # Graph label helpers → LightRAG /graph/label/* (list / popular / search).
    location /api/graph/label/ {{
      proxy_pass {lightrag_base_url}/graph/label/;
      proxy_set_header Host $host;
      proxy_set_header X-API-Key ${{LIGHTRAG_API_KEY}};
      proxy_set_header Authorization "Bearer ${{LIGHTRAG_BEARER_TOKEN}}";
    }}

    # RAG state snapshots (留档备查, not a reader view).
    location /status/ {{}}
  }}
}}
"""


def _readme(lightrag_base_url: str) -> str:
    return f"""# Evo wiki platform

只读 Web 知识平台产物:Wiki 静态站 + 固定 SPA 壳(问答/图谱/实体枢纽)+ RAG 状态快照(留档)+ nginx 配置。生成该目录前必须已完成 Wiki lane 与 LightRAG lane, 因为平台需要完整提供 Wiki、问答、图谱三个页面。

唯一进程是 nginx:发静态文件、把 `/api/query`、`/api/graphs` 与 `/api/graph/label/*` 转发到外部 LightRAG Server({lightrag_base_url})。LightRAG 的 key 在 nginx proxy 层从环境变量注入,**永不下发浏览器**。

## 本地起(裸 nginx,开发期最直接)

```bash
cd artifacts/platform
export LIGHTRAG_API_KEY=...        # 或 LIGHTRAG_BEARER_TOKEN=... (按 LightRAG 鉴权方式)
nginx -p . -c nginx.conf            # 监听 :8080
```

打开 `http://localhost:8080`(`Wiki`)、`http://localhost:8080/app`(`问答/图谱`)。

## 上线:网关规则(只放行读端点)

nginx.conf 只代理了 `/api/query`、`/api/graphs` 与 `/api/graph/label/*`——LightRAG 的所有写端点(`/documents/*`、`/graph/*/edit|create|merge|delete`)对读者不可达。上线公网时再前置一层鉴权(nginx basic auth / OAuth)。

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
    environment:
      - LIGHTRAG_API_KEY=${{LIGHTRAG_API_KEY}}
      - LIGHTRAG_BEARER_TOKEN=${{LIGHTRAG_BEARER_TOKEN}}
    # 若 LightRAG 在宿主机(Linux),取消下一行注释:
    # extra_hosts: ["host-gateway"]
```

工具只对“平台目录 + nginx 配置的正确性”负责,不对“容器能否跑起来”负责。
"""

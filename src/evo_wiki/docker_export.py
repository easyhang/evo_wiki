from __future__ import annotations

from .paths import ProjectPaths


def export_docker(paths: ProjectPaths) -> dict:
    paths.docker.mkdir(parents=True, exist_ok=True)
    wiki_exists = (paths.wiki_dist / "index.html").exists()
    lightrag_manifest_exists = (paths.lightrag / "manifest.json").exists()

    (paths.docker / "wiki.Dockerfile").write_text(WIKI_DOCKERFILE, encoding="utf-8")
    (paths.docker / "docker-compose.yml").write_text(compose(wiki_exists), encoding="utf-8")
    (paths.docker / "README.md").write_text(readme(wiki_exists, lightrag_manifest_exists), encoding="utf-8")
    (paths.docker / "lightrag-service.env.example").write_text(LIGHTRAG_SERVICE_ENV_EXAMPLE, encoding="utf-8")

    # L3：构建上下文是项目根（context: ../..），不写 .dockerignore 会把 corpus/ 及缓存
    # 全量发给 docker daemon。仅在缺失时生成，避免覆盖用户自定义文件。
    dockerignore = paths.root / ".dockerignore"
    dockerignore_written = False
    if not dockerignore.exists():
        dockerignore.write_text(DOCKERIGNORE, encoding="utf-8")
        dockerignore_written = True

    return {
        "wiki": wiki_exists,
        "lightrag": lightrag_manifest_exists,
        "lightrag_mode": "external_service",
        "path": str(paths.docker),
        "dockerignore_written": dockerignore_written,
    }


DOCKERIGNORE = """corpus/
**/__pycache__/
*.pyc
.git/
.venv/
venv/
.env
.DS_Store
"""


WIKI_DOCKERFILE = """FROM nginx:1.27-alpine
COPY artifacts/wiki/dist/ /usr/share/nginx/html/
EXPOSE 80
"""


LIGHTRAG_SERVICE_ENV_EXAMPLE = """# Evo Wiki expects LightRAG to be an already-running external service.
LIGHTRAG_BASE_URL=http://host.docker.internal:9621
# LIGHTRAG_API_KEY=
# LIGHTRAG_BEARER_TOKEN=
"""


def compose(wiki_exists: bool) -> str:
    services = ["services:"]
    if wiki_exists:
        services.append(
            "  wiki:\n"
            "    build:\n"
            "      context: ../..\n"
            "      dockerfile: artifacts/docker/wiki.Dockerfile\n"
            "    ports:\n"
            "      - \"8080:80\""
        )
    if len(services) == 1:
        services.append("  # No Wiki service exported yet. Run the wiki lane first.")
    return "\n".join(services) + "\n"


def readme(wiki_exists: bool, lightrag_manifest_exists: bool) -> str:
    lines = [
        "# Evo wiki Docker Artifacts",
        "",
        "这些文件基于当前 artifacts 导出。Wiki 可以作为静态站镜像部署；LightRAG 现在被视为外部已有服务，不在这里构建镜像。",
        "",
        "```bash",
        "cd artifacts/docker",
        "docker compose up --build",
        "```",
        "",
        f"- Wiki service exported: `{wiki_exists}`",
        f"- LightRAG service manifest exists: `{lightrag_manifest_exists}`",
        "",
        "如容器内的其它组件需要访问 LightRAG，请参考 `lightrag-service.env.example` 配置 `LIGHTRAG_BASE_URL` / `LIGHTRAG_API_KEY` / `LIGHTRAG_BEARER_TOKEN`。",
    ]
    return "\n".join(lines) + "\n"

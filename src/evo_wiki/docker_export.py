from __future__ import annotations

from .paths import ProjectPaths


def export_docker(paths: ProjectPaths) -> dict:
    paths.docker.mkdir(parents=True, exist_ok=True)
    wiki_exists = (paths.wiki_dist / "index.html").exists()
    lightrag_exists = paths.lightrag_workspace.exists()

    (paths.docker / "wiki.Dockerfile").write_text(WIKI_DOCKERFILE, encoding="utf-8")
    (paths.docker / "lightrag.Dockerfile").write_text(LIGHTRAG_DOCKERFILE, encoding="utf-8")
    (paths.docker / "docker-compose.yml").write_text(compose(wiki_exists, lightrag_exists), encoding="utf-8")
    (paths.docker / "README.md").write_text(readme(wiki_exists, lightrag_exists), encoding="utf-8")

    # L3：构建上下文是项目根（context: ../..），不写 .dockerignore 会把 corpus/ 及缓存
    # 全量发给 docker daemon。仅在缺失时生成，避免覆盖用户自定义文件。
    dockerignore = paths.root / ".dockerignore"
    dockerignore_written = False
    if not dockerignore.exists():
        dockerignore.write_text(DOCKERIGNORE, encoding="utf-8")
        dockerignore_written = True

    return {
        "wiki": wiki_exists,
        "lightrag": lightrag_exists,
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

LIGHTRAG_DOCKERFILE = """FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir lightrag-hku
COPY artifacts/lightrag/workspace/ /app/workspace/
COPY artifacts/lightrag/input/ /app/input/
EXPOSE 9621
CMD ["python", "-m", "lightrag.api.lightrag_server", "--working-dir", "/app/workspace", "--host", "0.0.0.0", "--port", "9621"]
"""


def compose(wiki_exists: bool, lightrag_exists: bool) -> str:
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
    if lightrag_exists:
        services.append(
            "  lightrag:\n"
            "    build:\n"
            "      context: ../..\n"
            "      dockerfile: artifacts/docker/lightrag.Dockerfile\n"
            "    ports:\n"
            "      - \"9621:9621\"\n"
            "    env_file:\n"
            "      - ../../.env"
        )
    if len(services) == 1:
        services.append("  # No services exported yet. Run wiki and/or lightrag lanes first.")
    return "\n".join(services) + "\n"


def readme(wiki_exists: bool, lightrag_exists: bool) -> str:
    lines = [
        "# Evo wiki Docker Artifacts",
        "",
        "这些文件基于当前 artifacts 导出。Wiki 与 LightRAG 服务可以独立构建、独立部署。",
        "",
        "```bash",
        "cd artifacts/docker",
        "docker compose up --build",
        "```",
        "",
        f"- Wiki service exported: `{wiki_exists}`",
        f"- LightRAG service exported: `{lightrag_exists}`",
        "",
        "LightRAG 服务需要你在项目根目录 `.env` 中提供 LightRAG 所需的 LLM / embedding 配置。",
    ]
    return "\n".join(lines) + "\n"

#!/usr/bin/env python3
"""Evo Wiki 全链路 Demo：corpus → Wiki → LightRAG → platform → 浏览器.

用法:
  python scripts/run_demo.py              # 增量运行（跳过已完成的步骤）
  python scripts/run_demo.py --clean      # 删掉 workspace/ 从头跑
  python scripts/run_demo.py --no-browser # 最后不自动打开浏览器
  python scripts/run_demo.py --skip-lightrag  # 只跑 Wiki，不碰 LightRAG

前置条件:
  1. evo-wiki 已安装: pip install -e .
  2. LightRAG Server 已在 localhost:9621 运行:
     cd ../LightRAG && .venv\\Scripts\\activate
     python -m lightrag.api.lightrag_server --workspace demo --port 9621
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = PROJECT_ROOT / "skills" / "evo-wiki-wiki" / "examples" / "learnbuffett-style"
WORKSPACE = PROJECT_ROOT / "workspace"
LIGHTRAG_URL = "http://localhost:9621"
LIGHTRAG_WS = "demo"
# LightRAG 项目根目录（假设和 evo_wiki-main 同级）
LIGHTRAG_ROOT = PROJECT_ROOT.parent / "LightRAG"

# ── terminal colours ──
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✅{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠️{RESET}  {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}❌{RESET} {msg}")


def title(msg: str) -> None:
    print(f"\n{BOLD}{'─' * 55}{RESET}")
    print(f"{BOLD}  {msg}{RESET}")
    print(f"{BOLD}{'─' * 55}{RESET}")


def run_cli(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run evo-wiki CLI inside PROJECT_ROOT."""
    cmd = [sys.executable, "-m", "evo_wiki.cli", *args, "--root", str(WORKSPACE)]
    label = " ".join(cmd)
    print(f"  $ {label}")
    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=capture,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else "(no stderr)"
        fail(f"Command failed (exit {result.returncode}): {stderr}")
        sys.exit(result.returncode)
    return result


def step_done(step_name: str) -> bool:
    """Rudimentary check: has this step already produced output?"""
    markers = {
        "init": WORKSPACE / "project.json",
        "render-wiki": WORKSPACE / "artifacts" / "wiki" / "dist" / "index.html",
        "prepare-lightrag": WORKSPACE / "artifacts" / "lightrag" / "input" / "documents.jsonl",
        "build-lightrag": WORKSPACE / "artifacts" / "lightrag" / "reports" / "lightrag-report.json",
        "export-platform": WORKSPACE / "artifacts" / "platform" / "nginx.conf",
    }
    return markers.get(step_name, Path("__nonexistent__")).exists()


def check_lightrag() -> None:
    """Check if LightRAG port is listening (TCP connect, not /health).

    Multi-workspace LightRAG lazily initialises shared dicts, so /health
    returns 500 until the first workspace-tagged request wakes up RagPool.
    A raw TCP connect is a more reliable "is the server up?" check.
    """
    host, port = "localhost", 9621
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
        ok(f"LightRAG TCP 端口可达: {host}:{port}")
        print("     (首次 API 调用时会自动初始化 demo workspace，需等待数秒)")
    except OSError as exc:
        fail(f"无法连接 LightRAG: {host}:{port} —— {exc}")
        print()
        print("  请先启动 LightRAG Server:")
        print(f"    cd ../LightRAG")
        print(f"    .venv\\Scripts\\activate")
        print(f"    python -m lightrag.api.lightrag_server --workspace {LIGHTRAG_WS} --port 9621")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evo Wiki 全链路 Demo — 一键从语料到浏览器",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="删除 workspace/ 从头开始",
    )
    parser.add_argument(
        "--skip-lightrag", action="store_true",
        help="跳过 LightRAG 步骤，只生成 Wiki 静态站",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="最后不自动打开浏览器",
    )
    parser.add_argument(
        "--clean-lightrag", action="store_true",
        help=f"同时删除 LightRAG 的 rag_storage/{LIGHTRAG_WS}/ 和 inputs/{LIGHTRAG_WS}/（解决 409 重复文档冲突）",
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="构建完成后启动 Python 开发服务器（无需 nginx），Ctrl+C 停止",
    )
    args = parser.parse_args()

    # ── Step 0: 检查前置条件 ──────────────────────────────────────
    title("Step 0: 检查前置条件")
    if not args.skip_lightrag:
        check_lightrag()
    else:
        warn("跳过 LightRAG 检查 (--skip-lightrag)")

    # ── Step 1: 清理（可选）──────────────────────────────────────
    if args.clean and WORKSPACE.exists():
        title("Step 1: 清理 workspace/")
        shutil.rmtree(WORKSPACE)
        ok("workspace/ 已删除")
    elif args.clean:
        ok("workspace/ 不存在，无需清理")

    if args.clean_lightrag:
        title("Step 1b: 清理 LightRAG demo workspace")
        for sub in ["rag_storage", "inputs"]:
            target = LIGHTRAG_ROOT / sub / LIGHTRAG_WS
            if target.exists():
                shutil.rmtree(target)
                ok(f"已删除 LightRAG/{sub}/{LIGHTRAG_WS}/")
            else:
                ok(f"LightRAG/{sub}/{LIGHTRAG_WS}/ 不存在，跳过")
        warn("请重启 LightRAG Server 以重建干净的 workspace")

    # ── Step 2: 初始化 ───────────────────────────────────────────
    title("Step 2: evo-wiki init")
    if step_done("init"):
        warn("已初始化，跳过")
    else:
        run_cli("init")
        ok("项目结构已创建")

    # ── Step 3: 复制语料 ─────────────────────────────────────────
    title("Step 3: 复制语料 (learnbuffett corpus)")
    corpus_dst = WORKSPACE / "corpus" / "raw"
    corpus_src = EXAMPLE / "corpus" / "raw"
    copied = 0
    for src in sorted(corpus_src.glob("*.md")):
        dst = corpus_dst / src.name
        if dst.exists() and dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"):
            continue
        corpus_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        ok(src.name)
        copied += 1
    if copied == 0:
        warn("语料已是最新，跳过")

    # ── Step 4: 写入 lightrag-config.json ────────────────────────
    title("Step 4: 写入 lightrag-config.json")
    config_path = WORKSPACE / "lightrag-config.json"
    config = {
        "mode": "service",
        "base_url": LIGHTRAG_URL,
        "workspace": LIGHTRAG_WS,
        "api_key_env": "LIGHTRAG_API_KEY",
        "bearer_token_env": "LIGHTRAG_BEARER_TOKEN",
        "timeout_seconds": 30,
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok(f"已写入: {config_path.relative_to(PROJECT_ROOT)}")

    # ── Step 5: 复制 wiki-src ────────────────────────────────────
    title("Step 5: 复制 wiki-src (learnbuffett 预制页面)")
    wiki_dst = WORKSPACE / "artifacts" / "wiki" / "wiki-src"
    wiki_src = EXAMPLE / "artifacts" / "wiki" / "wiki-src"
    count = 0
    for md in sorted(wiki_src.rglob("*.md")):
        dst = wiki_dst / md.relative_to(wiki_src)
        if dst.exists() and dst.read_text(encoding="utf-8") == md.read_text(encoding="utf-8"):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(md, dst)
        ok(str(dst.relative_to(wiki_dst)))
        count += 1
    if count == 0:
        warn("wiki-src 已是最新，跳过")

    # ── Step 6: 渲染 Wiki ────────────────────────────────────────
    title("Step 6: evo-wiki render-wiki")
    result = run_cli("render-wiki")
    report = json.loads(result.stdout)
    ok(f"已渲染 {report['page_count']} 页 → {report['html_output']}")

    health_issues = report.get("health", {}).get("issue_count", 0)
    if health_issues:
        warn(f"健康检查发现 {health_issues} 个问题，详见 artifacts/wiki/reports/wiki-health.json")
    else:
        ok("健康检查通过")

    if args.skip_lightrag:
        title("完成！（仅 Wiki）")
        _open_browser(args, WORKSPACE / "artifacts" / "wiki" / "dist" / "index.html")
        return

    # ── Step 7: 准备 LightRAG 输入 ───────────────────────────────
    title("Step 7: evo-wiki prepare-lightrag")
    result = run_cli("prepare-lightrag")
    prep = json.loads(result.stdout)
    ok(f"已准备 {prep['document_count']} 篇文档")

    # ── Step 8: 提交到 LightRAG ──────────────────────────────────
    title("Step 8: evo-wiki build-lightrag")
    print("  提交文档到 LightRAG 并等待索引完成 …")
    result = run_cli("build-lightrag")
    build = json.loads(result.stdout)
    if build.get("status") == "success":
        ok(f"成功提交 {len(build.get('imported', []))} 篇文档"
           f"（跳过 {len(build.get('skipped_unchanged', []))} 篇未变）")
    else:
        fail(f"LightRAG 提交失败: {build.get('error', 'unknown')}")
        sys.exit(1)

    # ── Step 9: 导出 platform ────────────────────────────────────
    title("Step 9: evo-wiki export-platform")
    result = run_cli("export-platform")
    plat = json.loads(result.stdout)
    ok(f"Platform 已导出: {plat['path']}")

    # ── Step 10: 完成 / 启动服务器 ────────────────────────────────
    platform_dir = WORKSPACE / "artifacts" / "platform"

    if args.skip_lightrag:
        # Wiki-only: serve from dist/
        serve_dir = WORKSPACE / "artifacts" / "wiki" / "dist"
        title("🎉 Wiki 构建完成！")
    else:
        serve_dir = platform_dir
        title("🎉 Demo 全链路完成！")

    if args.serve:
        _start_dev_server(serve_dir, args.no_browser)
        return  # never returns (loops until Ctrl+C)

    print(f"""
  产物目录:  {serve_dir}

  启动开发服务器（无需 nginx）:
    python scripts/run_demo.py --serve

  或在浏览器直接用 file:// 打开:
    {serve_dir / 'index.html'}
""")

    if not args.no_browser:
        index_html = serve_dir / "index.html"
        if index_html.exists():
            webbrowser.open(str(index_html))
            ok("已在浏览器中打开 Wiki 首页")


def _start_dev_server(serve_dir: Path, no_browser: bool = False) -> None:
    """Python dev server with SPA routing + LightRAG API proxy (no nginx needed)."""
    import http.server
    import json as _json
    import urllib.request as _urllib

    LIGHTRAG_BASE = LIGHTRAG_URL.rstrip("/")

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(serve_dir), **kw)

        def do_GET(self):
            # SPA routing: /app → redirect to /app/ so relative paths resolve correctly
            if self.path == "/app":
                self.send_response(301)
                self.send_header("Location", "/app/")
                self.end_headers()
                return
            # SPA fallback: only for paths that don't match real files
            if self.path.startswith("/app/"):
                file_path = serve_dir / self.path.lstrip("/")
                if not file_path.exists():
                    self.path = "/app/index.html"
            # API proxy
            if self.path.startswith("/api/"):
                return self._proxy("GET")
            super().do_GET()

        def do_POST(self):
            if self.path.startswith("/api/"):
                return self._proxy("POST")
            self.send_error(404)

        def _proxy(self, method):
            # Strip /api prefix + keep query string (LightRAG doesn't use /api)
            raw_path = self.path
            _, _, qs = raw_path.partition("?")
            lightrag_path = raw_path[4:] if raw_path.startswith("/api/") else raw_path
            target = LIGHTRAG_BASE + lightrag_path
            body = None
            if method == "POST":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else None
            req = _urllib.Request(target, data=body, method=method)
            req.add_header("LIGHTRAG-WORKSPACE", LIGHTRAG_WS)
            req.add_header("Accept", "application/json")
            if body:
                req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
            try:
                with _urllib.urlopen(req, timeout=120) as resp:
                    self.send_response(resp.status)
                    for k, v in resp.getheaders():
                        if k.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(resp.read())
            except Exception as exc:
                self.send_error(502, f"LightRAG unreachable: {exc}")

        def log_message(self, fmt, *args):
            print(f"  {self.command} {self.path}")

    port = 8080
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"""
{BOLD}  ✅ 开发服务器已启动！{RESET}

  浏览器访问:
    {BOLD}http://localhost:{port}/{RESET}          ← Wiki
    {BOLD}http://localhost:{port}/app{RESET}        ← 问答
    {BOLD}http://localhost:{port}/app#graph{RESET}  ← 图谱

  按 {BOLD}Ctrl+C{RESET} 停止
""")
    if not no_browser:
        webbrowser.open(f"http://localhost:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{GREEN}服务器已停止。{RESET}")


def _open_browser(args: argparse.Namespace, path: Path) -> None:
    if not args.no_browser and path.exists():
        webbrowser.open(str(path))
        ok("已在浏览器中打开")


if __name__ == "__main__":
    main()

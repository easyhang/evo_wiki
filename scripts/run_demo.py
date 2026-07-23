#!/usr/bin/env python3
"""Evo Wiki 全链路 Demo：corpus → Wiki → LightRAG → platform → 浏览器.

用法:
  python scripts/run_demo.py              # 增量运行（跳过已完成的步骤）
  python scripts/run_demo.py --clean      # 删掉 workspace/ 从头跑
  python scripts/run_demo.py --no-browser # 最后不自动打开浏览器
  python scripts/run_demo.py --skip-lightrag --serve  # 只跑 Wiki 并本地预览

前置条件:
  1. evo-wiki 已安装: pip install -e .
  2. 完整平台模式需要用户自行运行 LightRAG；可用 LIGHTRAG_BASE_URL 和
     LIGHTRAG_WORKSPACE 指定服务及 workspace。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = PROJECT_ROOT / "skills" / "evo-wiki-wiki" / "examples" / "learnbuffett-style"
WORKSPACE = PROJECT_ROOT / "workspace"
LIGHTRAG_URL = os.environ.get("LIGHTRAG_BASE_URL", "http://127.0.0.1:9621")
LIGHTRAG_WS = os.environ.get("LIGHTRAG_WORKSPACE", "demo")

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
    parsed = urlparse(LIGHTRAG_URL)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        fail(f"无效的 LIGHTRAG_BASE_URL: {LIGHTRAG_URL}")
        sys.exit(1)
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
        ok(f"LightRAG TCP 端口可达: {host}:{port} (workspace: {LIGHTRAG_WS})")
    except OSError as exc:
        fail(f"无法连接 LightRAG: {host}:{port} —— {exc}")
        print()
        print("  请先启动自己的 LightRAG 服务，或设置：")
        print("    export LIGHTRAG_BASE_URL='http://服务器地址:9621'")
        print("    export LIGHTRAG_WORKSPACE='demo'")
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
        "--serve", action="store_true",
        help="构建完成后在 127.0.0.1 启动本地预览，Ctrl+C 停止",
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

    # ── Step 2: 初始化 ───────────────────────────────────────────
    title("Step 2: evo-wiki init")
    if step_done("init"):
        warn("已初始化，跳过")
    else:
        profile = "wiki-only" if args.skip_lightrag else "local-platform"
        run_cli("init", "--profile", profile)
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

    if not args.skip_lightrag:
        # ── Step 4: 写入 lightrag-config.json ────────────────────
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
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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
        serve_dir = WORKSPACE / "artifacts" / "wiki" / "dist"
        if args.serve:
            _start_static_server(serve_dir, args.no_browser)
        else:
            _open_browser(args, serve_dir / "index.html")
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

    serve_dir = platform_dir
    title("🎉 Demo 全链路完成！")

    if args.serve:
        run_cli("serve", capture=False)
        return  # never returns (loops until Ctrl+C)

    print(f"""
  产物目录:  {serve_dir}

  启动受控本地预览:
    evo-wiki serve --root {WORKSPACE}

  浏览器访问:
    http://127.0.0.1:8080/
""")

def _start_static_server(serve_dir: Path, no_browser: bool = False) -> None:
    """Serve generated Wiki files on loopback without a LightRAG proxy."""
    import http.server

    port = 8080
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args,
        directory=str(serve_dir),
        **kwargs,
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"""
{BOLD}  ✅ 开发服务器已启动！{RESET}

  浏览器访问:
    {BOLD}http://127.0.0.1:{port}/{RESET}

  按 {BOLD}Ctrl+C{RESET} 停止
""")
    if not no_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")
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

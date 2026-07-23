#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum --check SHA256SUMS
elif command -v shasum >/dev/null 2>&1; then
  shasum -a 256 --check SHA256SUMS
else
  echo "缺少 sha256sum 或 shasum。" >&2
  exit 2
fi

command -v python3 >/dev/null 2>&1 || {
  echo "缺少 python3（需要 3.10+）。" >&2
  exit 2
}
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 版本过低；需要 3.10+。" >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || {
  echo "缺少 Docker。" >&2
  exit 2
}
docker compose version >/dev/null 2>&1 || {
  echo "缺少 docker compose。" >&2
  exit 2
}

if [[ ! -f .env ]]; then
  echo "尚未创建 .env；请执行 cp .env.example .env 并填写凭据。" >&2
  exit 2
fi
if grep -q 'REPLACE_WITH_YOUR_' .env; then
  echo ".env 仍包含占位符。" >&2
  exit 2
fi

for key in \
  LLM_BINDING_HOST \
  LLM_BINDING_API_KEY \
  EMBEDDING_BINDING_HOST \
  EMBEDDING_BINDING_API_KEY; do
  if ! grep -Eq "^${key}=.+" .env; then
    echo ".env 缺少 ${key}。" >&2
    exit 2
  fi
done
grep -qx 'EMBEDDING_MODEL=text-embedding-v3' .env || {
  echo "EMBEDDING_MODEL 必须为 text-embedding-v3。" >&2
  exit 2
}
grep -qx 'EMBEDDING_DIM=1024' .env || {
  echo "EMBEDDING_DIM 必须为 1024。" >&2
  exit 2
}

corpus_count="$(
  find workspace/corpus/raw/legal_docs -type f -name '*.txt' | wc -l | tr -d ' '
)"
wiki_count="$(
  find workspace/artifacts/wiki/wiki-src -type f -name '*.md' | wc -l | tr -d ' '
)"
[[ "${corpus_count}" == "9" ]] || {
  echo "案例语料数量错误：${corpus_count}（预期 9）。" >&2
  exit 2
}
[[ "${wiki_count}" == "22" ]] || {
  echo "Wiki 页面数量错误：${wiki_count}（预期 22）。" >&2
  exit 2
}

python3 - <<'PY'
from pathlib import Path
import sqlite3

root = Path.cwd()
database = root / "workspace" / "artifacts" / "state" / "evo_wiki.sqlite3"
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
try:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("SQLite integrity_check 未通过。")
    if list(connection.execute("PRAGMA foreign_key_check")):
        raise SystemExit("SQLite foreign_key_check 未通过。")
    if connection.execute("PRAGMA user_version").fetchone()[0] != 5:
        raise SystemExit("SQLite schema 不是版本 5。")
    if connection.execute(
        "SELECT COUNT(*) FROM source_document"
    ).fetchone()[0] != 9:
        raise SystemExit("SQLite source_document 数量不是 9。")
    if connection.execute(
        """
        SELECT COUNT(*) FROM lightrag_binding
        WHERE remote_status = 'PROCESSED'
          AND action_gate = 'OPEN'
        """
    ).fetchone()[0] != 9:
        raise SystemExit("SQLite 有效 LightRAG binding 数量不是 9。")
    for table in (
        "query_run",
        "audit_item",
        "audit_event",
        "notification_outbox",
        "notification_attempt",
        "gateway_instance",
        "maintenance_fence",
        "replacement_operation",
        "lane_run_revision",
        "lane_run",
    ):
        if connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]:
            raise SystemExit(f"SQLite 清洁检查失败：{table} 非空。")
    snapshots = connection.execute(
        """
        SELECT snapshot_path FROM source_revision
        WHERE snapshot_status = 'AVAILABLE'
        """
    ).fetchall()
finally:
    connection.close()
if len(snapshots) != 9:
    raise SystemExit("SQLite 可用来源快照数量不是 9。")
for (relative,) in snapshots:
    if not (root / "workspace" / relative).is_file():
        raise SystemExit(f"缺少来源快照：{relative}")

index_root = root / "lightrag-data" / "rag_storage" / "evo_wiki"
index_files = [path for path in index_root.iterdir() if path.is_file()]
if len(index_files) != 11:
    raise SystemExit("LightRAG 索引文件数量不是 11。")
if (index_root / "kv_store_llm_response_cache.json").exists():
    raise SystemExit("包内不应包含 LightRAG LLM response cache。")
PY

docker compose config --quiet

echo "检查通过：语料/Wiki、SQLite、LightRAG 索引、校验和与 Compose 配置有效。"

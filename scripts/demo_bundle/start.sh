#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${ROOT_DIR}/.runtime"
VENV_DIR="${RUNTIME_DIR}/venv"
AUDIT_KEY_FILE="${RUNTIME_DIR}/evo_wiki_query_audit_key"

cd "${ROOT_DIR}"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已创建 .env。请填写模型 endpoint/API key 后重新运行 ./start.sh。" >&2
  exit 2
fi

if grep -q 'REPLACE_WITH_YOUR_' .env; then
  echo ".env 仍包含占位符；请先填写模型 endpoint 和 API key。" >&2
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

mkdir -p "${RUNTIME_DIR}"
chmod 700 "${RUNTIME_DIR}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/python" -m pip install --requirement requirements.txt

if [[ ! -f "${AUDIT_KEY_FILE}" ]]; then
  "${VENV_DIR}/bin/python" -c \
    'import secrets; print(secrets.token_hex(32))' > "${AUDIT_KEY_FILE}"
  chmod 600 "${AUDIT_KEY_FILE}"
fi
export EVO_WIKI_QUERY_AUDIT_KEY
EVO_WIKI_QUERY_AUDIT_KEY="$(tr -d '\r\n' < "${AUDIT_KEY_FILE}")"

docker compose up --detach lightrag

echo "等待 LightRAG 就绪..."
"${VENV_DIR}/bin/python" - <<'PY'
import json
import time
import urllib.error
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:9621/health",
    headers={"LIGHTRAG-WORKSPACE": "evo_wiki"},
)
deadline = time.monotonic() + 120
last_error = "not started"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        if payload.get("status") == "healthy":
            configuration = payload.get("configuration") or {}
            if configuration.get("workspace") != "evo_wiki":
                raise RuntimeError("LightRAG workspace mismatch")
            print("LightRAG healthy (workspace=evo_wiki)")
            break
        last_error = f"unexpected health payload: {payload!r}"
    except Exception as exc:
        last_error = str(exc)
    time.sleep(2)
else:
    raise SystemExit(f"LightRAG 未在 120 秒内就绪：{last_error}")
PY

"${VENV_DIR}/bin/evo-wiki" doctor \
  --root "${ROOT_DIR}/workspace" \
  --check-service

echo "Evo Wiki 启动于 http://127.0.0.1:8080/"
exec "${VENV_DIR}/bin/evo-wiki" serve \
  --root "${ROOT_DIR}/workspace" \
  --listen 127.0.0.1:8080

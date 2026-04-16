#!/usr/bin/env bash
set -euo pipefail

PORT="${FX_PORT:-8000}"
LOG_DIR="${FX_LOG_DIR:-data/logs}"
LOG_FILE="${LOG_DIR}/codespaces-app.log"
ERR_FILE="${LOG_DIR}/codespaces-app.err.log"

mkdir -p "${LOG_DIR}"

if python - <<'PY'
import os
import socket

port = int(os.environ.get("FX_PORT", "8000"))
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.5)
    result = sock.connect_ex(("127.0.0.1", port))
raise SystemExit(0 if result == 0 else 1)
PY
then
    echo "FX TRY Risk Lab is already running on port ${PORT}."
    exit 0
fi

nohup python -m app.serve >"${LOG_FILE}" 2>"${ERR_FILE}" &
echo "Starting FX TRY Risk Lab on port ${PORT}."
echo "If the browser does not open automatically, open the forwarded port ${PORT} in Codespaces."

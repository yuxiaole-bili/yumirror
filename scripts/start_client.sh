#!/usr/bin/env sh
# yumirror 客户端启动器（Linux / macOS）
cd "$(dirname "$0")/.." || exit 1

PY=python3
[ -x venv/bin/python ] && PY=venv/bin/python

if [ ! -f client/config.json ]; then
    echo "[setup] 未找到 client/config.json，从 config.example.json 复制一份"
    cp client/config.example.json client/config.json
    echo "[setup] 请先编辑 client/config.json 里的 server_host / client_id / sync_folders"
fi

echo "[run] $PY client/client.py    (Ctrl+C 停止)"
exec "$PY" client/client.py

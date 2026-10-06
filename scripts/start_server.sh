#!/usr/bin/env sh
# yumirror 服务端启动器（Linux / macOS）
cd "$(dirname "$0")/.." || exit 1

PY=python3
[ -x venv/bin/python ] && PY=venv/bin/python

if [ ! -f server/config.json ]; then
    echo "[setup] 未找到 server/config.json，从 config.example.json 复制一份"
    cp server/config.example.json server/config.json
fi

echo "[run] $PY server/server.py    (Ctrl+C 停止)"
exec "$PY" server/server.py

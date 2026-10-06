@echo off
chcp 65001 >nul
cd /d "%~dp0.."

set PY=python
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe

if not exist "client\config.json" (
    echo [setup] 未找到 client\config.json，从 config.example.json 复制一份
    copy "client\config.example.json" "client\config.json" >nul
    echo [setup] 请先编辑 client\config.json 里的 server_host / client_id / sync_folders
)

echo [run] %PY% client\client.py    ^(Ctrl+C 停止^)
%PY% "client\client.py"
pause

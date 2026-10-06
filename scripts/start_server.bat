@echo off
chcp 65001 >nul
cd /d "%~dp0.."

set PY=python
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe

if not exist "server\config.json" (
    echo [setup] 未找到 server\config.json，从 config.example.json 复制一份
    copy "server\config.example.json" "server\config.json" >nul
)

echo [run] %PY% server\server.py    ^(Ctrl+C 停止^)
%PY% "server\server.py"
pause

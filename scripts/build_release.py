#!/usr/bin/env python3
"""把服务端/客户端打成免安装的可执行程序，并组装成发布包。

    python scripts/build_release.py            # 在本机平台构建
    python scripts/build_release.py --no-zip   # 只构建，不打包 zip

产物：dist/yumirror-<平台>-<架构>.zip（Windows）或 .tar.gz（Linux/macOS），内含
    server/yumirror-server(.exe) + config.json（示例） + 启动脚本
    client/yumirror-client(.exe) + config.json（示例） + 启动脚本
    README.md / CHANGELOG.md / LICENSE / 使用说明.txt

CI（.github/workflows/build.yml）在打标签时自动跑这个脚本，并把结果挂到 Release。
注意：PyInstaller 的 --add-data 分隔符各平台不同（Windows 是 ;），所以这里用 Python
按 os.pathsep 拼，避免在 CI 的 bash 里踩坑。
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, 'dist')
BUILD = os.path.join(ROOT, 'build', 'pyinstaller')

# flask-socketio / engineio / watchdog 都有动态导入，静态分析看不到；漏了会在启动时
# 直接 ValueError: Invalid async_mode specified（实测如此）
PYI_COMMON = [
    '--noconfirm', '--clean', '--onefile', '--console',
    '--collect-submodules', 'engineio',
    '--collect-submodules', 'socketio',
    '--collect-submodules', 'watchdog',
    '--hidden-import', 'engineio.async_drivers.threading',
    '--hidden-import', 'engineio.async_drivers.threading_websocket',
    '--hidden-import', 'flask_socketio',
]

BAT_SERVER = '''@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   yumirror 服务端
echo   首次运行会生成自签证书，请把横幅里的
echo   SHA256 指纹抄给客户端核对
echo ============================================
yumirror-server.exe
pause
'''

BAT_CLIENT = '''@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   yumirror 客户端
echo   先在 config.json 里改 server_host（服务端 IP）
echo ============================================
yumirror-client.exe
pause
'''

SH_SERVER = '''#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "=== yumirror 服务端（首次运行会生成自签证书）==="
./yumirror-server
'''

SH_CLIENT = '''#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "=== yumirror 客户端（先在 config.json 里改 server_host）==="
./yumirror-client
'''

README_TXT = '''yumirror —— 免安装版使用说明
================================

这个包里的程序自带 Python 运行环境，不需要另外安装 Python。

一、起服务端（找一台常开的机器）
  1. 双击 server/启动服务端.bat（Linux/macOS 用 ./启动服务端.sh）
  2. 首次运行会自动生成自签证书，窗口里会打印证书指纹（SHA256）
  3. 记下指纹；数据都在 server/ 目录下（backups 备份、certs 证书、logs 日志）

二、起客户端（每台要同步的机器）
  1. 打开 client/config.json，把 server_host 改成服务端的 IP
  2. 把 sync_folders 里的 path 改成你要同步的目录
  3. 双击 client/启动客户端.bat
  4. 浏览器打开 http://127.0.0.1:8087 —— 就是客户端面板

三、传输加密
  默认开启。客户端第一次连上会把服务端证书指纹固定下来（写入 config.json 的 tls.fingerprint），
  之后指纹不一致会拒绝连接。服务端换了证书，需要清空客户端的 tls.fingerprint 重新固定。

四、注意事项
  · 服务端和客户端必须在同一个局域网（或走 SSH 隧道）
  · 端口默认 9999（数据）、8086（服务端面板）、8087（客户端面板）
  · 这是面向可信局域网的工具，别直接暴露到公网
  · 详细文档见 README.md，流程编辑器有 40 个内置模板可以直接用
'''


def pyinstaller(name, script, datas):
    args = [sys.executable, '-m', 'PyInstaller'] + PYI_COMMON + [
        '--distpath', DIST, '--workpath', BUILD, '--specpath', BUILD, '--name', name]
    for src, dst in datas:
        args += ['--add-data', '%s%s%s' % (src, os.pathsep, dst)]
    args.append(script)
    print('  构建 %s ...' % name, flush=True)
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit('PyInstaller 失败: %s' % name)
    exe = os.path.join(DIST, name + ('.exe' if os.name == 'nt' else ''))
    if not os.path.isfile(exe):
        raise SystemExit('没找到产物: %s' % exe)
    print('    ✅ %.1f MB' % (os.path.getsize(exe) / 1024 / 1024))
    return exe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-zip', action='store_true')
    ap.add_argument('--skip-build', action='store_true')
    args = ap.parse_args()

    is_win = os.name == 'nt'
    tag = platform.system().lower() + '-' + ('x64' if platform.machine().endswith('64') else platform.machine())
    stage = os.path.join(DIST, 'yumirror-' + tag)

    if not args.skip_build:
        if os.path.isdir(stage):
            shutil.rmtree(stage)
        srv = pyinstaller('yumirror-server', os.path.join(ROOT, 'server', 'server.py'),
                          [(os.path.join(ROOT, 'server', 'static'), 'static')])
        cli = pyinstaller('yumirror-client', os.path.join(ROOT, 'client', 'client.py'),
                          [(os.path.join(ROOT, 'client', 'templates'), 'templates'),
                           (os.path.join(ROOT, 'client', 'static'), 'static')])
    else:
        srv = os.path.join(DIST, 'yumirror-server' + ('.exe' if is_win else ''))
        cli = os.path.join(DIST, 'yumirror-client' + ('.exe' if is_win else ''))

    os.makedirs(os.path.join(stage, 'server'), exist_ok=True)
    os.makedirs(os.path.join(stage, 'client'), exist_ok=True)
    shutil.copy2(srv, os.path.join(stage, 'server'))
    shutil.copy2(cli, os.path.join(stage, 'client'))
    shutil.copy2(os.path.join(ROOT, 'server', 'config.example.json'),
                 os.path.join(stage, 'server', 'config.json'))
    shutil.copy2(os.path.join(ROOT, 'client', 'config.example.json'),
                 os.path.join(stage, 'client', 'config.json'))
    for f in ('README.md', 'CHANGELOG.md', 'LICENSE', 'SECURITY.md'):
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            shutil.copy2(p, stage)
    with open(os.path.join(stage, '使用说明.txt'), 'w', encoding='utf-8') as f:
        f.write(README_TXT)
    if is_win:
        for d, name, body in (('server', '启动服务端.bat', BAT_SERVER), ('client', '启动客户端.bat', BAT_CLIENT)):
            with open(os.path.join(stage, d, name), 'w', encoding='utf-8', newline='\r\n') as f:
                f.write(body)
    else:
        for d, name, body in (('server', '启动服务端.sh', SH_SERVER), ('client', '启动客户端.sh', SH_CLIENT)):
            p = os.path.join(stage, d, name)
            with open(p, 'w', encoding='utf-8', newline='\n') as f:
                f.write(body)
            os.chmod(p, 0o755)

    if args.no_zip:
        print('  组装完成（未压缩）: %s' % stage)
        return 0
    if is_win:
        out = stage + '.zip'
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            for dp, _, fns in os.walk(stage):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    z.write(full, os.path.relpath(full, DIST))
    else:
        import tarfile
        out = stage + '.tar.gz'
        with tarfile.open(out, 'w:gz') as t:
            t.add(stage, arcname=os.path.basename(stage))
    print('\n发布包: %s（%.1f MB）' % (out, os.path.getsize(out) / 1024 / 1024))
    return 0


if __name__ == '__main__':
    sys.exit(main())

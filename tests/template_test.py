#!/usr/bin/env python3
"""预设工作流验收测试：起真服务端 + 真客户端，把模板逐个跑一遍，任何一个报错就算失败。

    python tests/template_test.py          # 跑全部内置模板
    python tests/template_test.py -k       # 保留临时目录
    python tests/template_test.py 分类 审计  # 只跑名字里含这些关键字的模板

跟"能不能加载"不同：这里真的执行，检查每步退出码与日志里有没有 ❌。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = '-k' in sys.argv
KW = [a for a in sys.argv[1:] if not a.startswith('-')]
sys.path.insert(0, ROOT)
from shared.flow_engine import FLOW_TEMPLATES, TEMPLATE_META, list_templates  # noqa: E402


# 单机离线环境跑不了的模板：需要第二个客户端（同伴）或需要真实外网 webhook。
# 这些不是产品缺陷，但也不能算通过——单独统计成 SKIP。
NEEDS_PEER = {'hash_check', 'backup_verify', 'hash_three_way', 'mirror_consistency',
              'manual_mirror', 'peer_status_probe', 'three_way_guard'}
NEEDS_WEBHOOK = {'webhook_notify', 'webhook_broadcast', 'large_file_alert',
                 'backup_then_notify', 'backup_health', 'full_pipeline'}


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


WAIT_SCALE = float(os.environ.get('YM_TEST_SCALE', '1') or 1)


def wait_for(fn, timeout, interval=0.4):
    end = time.time() + timeout * WAIT_SCALE
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def call(url, body=None, method='GET', timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8', 'replace'))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {'error': str(e)}


def build_env(tmp):
    app = os.path.join(tmp, 'app')
    shutil.copytree(os.path.join(ROOT, 'shared'), os.path.join(app, 'shared'),
                    ignore=shutil.ignore_patterns('__pycache__'))
    os.makedirs(os.path.join(app, 'server'))
    os.makedirs(os.path.join(app, 'client'))
    for src, dst in [('server/server.py', 'server/server.py'),
                     ('server/__init__.py', 'server/__init__.py'),
                     ('client/client.py', 'client/client.py')]:
        shutil.copy2(os.path.join(ROOT, src), os.path.join(app, dst))
    shutil.copytree(os.path.join(ROOT, 'client', 'templates'), os.path.join(app, 'client', 'templates'))
    return app


def prepare_files(sync):
    """造一批有代表性的文件：文档/图片/大文件/子目录"""
    os.makedirs(os.path.join(sync, '文档'), exist_ok=True)
    os.makedirs(os.path.join(sync, '图片'), exist_ok=True)
    with open(os.path.join(sync, '合同.txt'), 'w', encoding='utf-8') as f:
        f.write('合同正文\n' * 20)
    with open(os.path.join(sync, '文档', '季度报表.md'), 'w', encoding='utf-8') as f:
        f.write('# 季度报表\n\n- 收入 20000\n')
    with open(os.path.join(sync, '图片', 'logo.png'), 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + os.urandom(2048))
    with open(os.path.join(sync, '大文件.bin'), 'wb') as f:      # > 1MB，触发压缩分支
        f.write(os.urandom(1024 * 1024 + 4096))
    return '合同.txt'


def main():
    tmp = tempfile.mkdtemp(prefix='yumirror_tpl_')
    print('临时目录: %s\n' % tmp)
    app = build_env(tmp)
    sync = os.path.join(tmp, 'sync')
    os.makedirs(sync)
    target = prepare_files(sync)
    backups = os.path.join(tmp, 'backups')
    srv_port, srv_web, cli_web = free_port(), free_port(), free_port()

    with open(os.path.join(app, 'server', 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({'server_name': 'TPL-Server', 'bind_host': '127.0.0.1', 'bind_port': srv_port,
                   'web_host': '127.0.0.1', 'web_port': srv_web, 'backup_dir': backups,
                   'flows_share_dir': os.path.join(tmp, 'shared_flows'),
                   'users_db_path': os.path.join(tmp, 'users.json'),
                   'logs_dir': os.path.join(tmp, 'srv_logs'),
                   'heartbeat_interval': 15, 'stats_interval': 30}, f, indent=2)
    with open(os.path.join(app, 'client', 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({'client_id': 'TPL-Client', 'group_id': 'tpl',
                   'server_host': '127.0.0.1', 'server_port': srv_port, 'server_web_port': srv_web,
                   'web_host': '127.0.0.1', 'web_port': cli_web,
                   'heartbeat_interval': 15, 'sync_folders': [{'name': 'work', 'path': sync}],
                   'ignore_patterns': ['归档', 'vault'], 'auto_backup_on_start': True,
                   'backup_passphrase': 'tpl-pass', 'ssh_tunnel': {'enabled': False}}, f, indent=2)

    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    srv_log = open(os.path.join(tmp, 'server.out'), 'wb')
    cli_log = open(os.path.join(tmp, 'client.out'), 'wb')
    proc_srv = proc_cli = None
    results = []
    try:
        proc_srv = subprocess.Popen([sys.executable, '-u', os.path.join(app, 'server', 'server.py')],
                                    cwd=app, stdout=srv_log, stderr=subprocess.STDOUT, env=env)
        if not wait_for(lambda: port_open(srv_port), 30):
            print('服务端未启动'); return 1
        proc_cli = subprocess.Popen([sys.executable, '-u', os.path.join(app, 'client', 'client.py')],
                                    cwd=app, stdout=cli_log, stderr=subprocess.STDOUT, env=env)
        base = 'http://127.0.0.1:%d' % cli_web
        if not wait_for(lambda: call(base + '/api/status')[0] == 200, 40):
            print('客户端未启动'); return 1
        wait_for(lambda: (call(base + '/api/status')[1] or {}).get('watching') is True, 30)

        def is_connected():
            return bool((call(base + '/api/status')[1] or {}).get('connected'))

        # 关键：显式等"真的连上服务端"（而不是 sleep 碰运气）。
        # CI runner 慢，之前这里没等，导致上传类模板全报"未连接服务端"。
        if not wait_for(is_connected, 60):
            print('  ⚠ 客户端 60 秒内未连上服务端，上传类模板预计会失败')
        # 等首轮备份对账把文件推到服务端（verify_backup / list_versions 需要）
        time.sleep(8)
        wait_for(is_connected, 30)

        full = os.path.join(sync, target)
        cases = []
        for key, tpl in FLOW_TEMPLATES.items():
            name = TEMPLATE_META.get(key, ['', ''])[0]
            title = tpl.get('name', key)
            if KW and not any(k in (title + key + name) for k in KW):
                continue
            cases.append((key, title, tpl))

        print('=== 逐个执行 %d 个模板（真服务端 + 真客户端 + 真文件）===\n' % len(cases))
        skipped = []
        for key, title, tpl in cases:
            if key in NEEDS_PEER or key in NEEDS_WEBHOOK:
                why = '需要同伴在线' if key in NEEDS_PEER else '需要真实外网 webhook'
                skipped.append((key, title, why))
                print('  [SKIP] %-22s %s   (%s)' % (title, key, why))
                continue
            # 执行前确保还连着（长时间跑测试时可能被踢，客户端会自动重连）
            if not is_connected():
                wait_for(is_connected, 45)
            flow = dict(tpl)
            # preflight_guard 里的 webhook 是占位地址，测试时指向本机面板，避免真的外呼
            if key == 'preflight_guard':
                flow = json.loads(json.dumps(flow).replace('https://example.com/webhook',
                                                           base + '/api/status'))
            code, res = call(base + '/api/flow/execute', {
                'flow': flow, 'folder': 'work', 'relpath': target, 'full_path': full},
                method='POST')
            logs = (res or {}).get('logs') or []
            blob = '\n'.join(logs)
            failed = int((res or {}).get('failed', 0) or 0)
            # 拿不到日志 = 请求没真正跑起来，必须算失败（否则会假通过）
            bad = failed or ('❌' in blob) or not logs
            results.append((key, title, not bad, failed, blob))
            mark = 'PASS' if not bad else 'FAIL'
            print('  [%s] %-22s %s   (%d 行日志, 失败 %d 处)'
                  % (mark, title, key, len(logs), failed))
            if not logs:
                print('        ⚠ 没拿到日志，HTTP %s，原始响应: %s'
                      % (code, json.dumps(res, ensure_ascii=False)[:260]))
            if bad and logs:
                # 失败时打印所有含 ❌/⚠ 的行（出问题的那行不一定在尾部）
                for line in logs:
                    st = line.strip()
                    if '❌' in st or '⚠' in st:
                        print('        ' + st[:130])
            # 清掉模板产生的归档/vault，避免污染后续模板的扫描与核对
            for junk in ('归档', 'vault'):
                shutil.rmtree(os.path.join(sync, junk), ignore_errors=True)

        print('\n' + '=' * 66)
        ok = sum(1 for r in results if r[2])
        print('执行 %d 个模板：通过 %d，失败 %d；另有 %d 个因环境不具备被跳过'
              % (len(results), ok, len(results) - ok, len(skipped)))
        for key, title, good, failed, blob in results:
            if not good:
                print('  FAIL: %s (%s)' % (title, key))
        print('\n服务端备份目录: %d 个文件'
              % sum(len(f) for _, _, f in os.walk(backups)))
        return 0 if ok == len(results) and results else 1
    finally:
        for p in (proc_cli, proc_srv):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.kill()
        srv_log.close()
        cli_log.close()
        if KEEP:
            print('临时目录已保留: %s' % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())

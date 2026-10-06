#!/usr/bin/env python3
"""yumirror 流程演示：一条命令真起服务端 + 客户端，依次跑备份模板并打印每一步输出。

不是 mock：服务端、客户端都是本项目真实进程，文件真的落进服务端备份目录。

用法：
    python scripts/demo_flows.py              # 跑全部演示
    python scripts/demo_flows.py 1 3          # 只跑第 1、3 个
    python scripts/demo_flows.py --keep       # 保留临时目录便于查看产物
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = '--keep' in sys.argv

CASES = [
    ('① 变更即备份', 'backup_on_change', 'file',
     '单个文件变更 → 上传服务端 → 校验哈希 → 分支出结论'),
    ('② 每日打包归档', 'backup_zip_daily', 'folder',
     '扫描目录 → 打包带时间戳 zip → 上传 → 生成 Markdown 清单'),
    ('③ 整目录批量备份', 'bulk_backup_folder', 'folder',
     '扫描目录 → foreach 逐文件上传（首次补齐历史文件的做法）'),
    ('④ 备份清单报告', 'backup_manifest_report', 'folder',
     '生成 MD + JSON 清单（含 SHA256）→ 上传留档'),
]


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for(fn, timeout, interval=0.3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def api(base, path, method='GET', body=None, timeout=60):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8', 'replace'))


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def build_app(tmp):
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


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def main():
    picked = [a for a in sys.argv[1:] if a.isdigit()]
    cases = [c for i, c in enumerate(CASES, 1) if not picked or str(i) in picked]

    tmp = tempfile.mkdtemp(prefix='yumirror_demo_')
    app = build_app(tmp)
    sync_dir = os.path.join(tmp, 'sync')
    os.makedirs(os.path.join(sync_dir, '报告'))
    backups = os.path.join(tmp, 'backups')
    srv_port, srv_web, cli_web = free_port(), free_port(), free_port()

    def short(p):
        """打印时缩短本机路径：演示输出常被直接贴到聊天/issue，不该带上用户名和临时目录"""
        s = str(p)
        for prefix, tag in ((tmp, '«临时目录»'), (os.path.expanduser('~'), '«用户目录»')):
            if prefix:
                s = s.replace(prefix, tag)
        return s

    with open(os.path.join(app, 'server', 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({'server_name': 'DEMO-Server', 'bind_host': '127.0.0.1', 'bind_port': srv_port,
                   'web_host': '127.0.0.1', 'web_port': srv_web, 'backup_dir': backups,
                   'flows_share_dir': os.path.join(tmp, 'shared_flows'),
                   'users_db_path': os.path.join(tmp, 'users.json'),
                   'logs_dir': os.path.join(tmp, 'srv_logs'),
                   'heartbeat_interval': 2, 'stats_interval': 30}, f, indent=2)
    with open(os.path.join(app, 'client', 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({'client_id': 'DEMO-Client', 'group_id': 'demo',
                   'server_host': '127.0.0.1', 'server_port': srv_port, 'server_web_port': srv_web,
                   'web_host': '127.0.0.1', 'web_port': cli_web,
                   'heartbeat_interval': 2, 'reconnect_base_delay': 1, 'reconnect_max_delay': 5,
                   'sync_folders': [{'name': 'work', 'path': sync_dir}],
                   'ignore_patterns': ['__pycache__', '*.pyc'],
                   'ssh_tunnel': {'enabled': False}}, f, indent=2)

    files = {
        '合同.txt': '甲方乙方合同正文（演示用）\n',
        '报告/月度汇总.md': '# 月度汇总\n\n- 收入\n- 支出\n',
        '报告/明细.csv': 'item,amount\n服务器,1200\n',
    }
    for rel, content in files.items():
        with open(os.path.join(sync_dir, rel), 'w', encoding='utf-8') as f:
            f.write(content)

    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    srv_log = open(os.path.join(tmp, 'server.out'), 'wb')
    cli_log = open(os.path.join(tmp, 'client.out'), 'wb')
    proc_srv = proc_cli = None
    ok_all = True
    try:
        print('=' * 78)
        print('yumirror 流程演示   （真实服务端 + 真实客户端，非模拟）')
        print('=' * 78)
        print('临时目录: %s' % (tmp if KEEP else short(tmp)))
        print('同步目录: %s  （预置 %d 个文件）' % (short(sync_dir), len(files)))
        print('备份目录: %s\n' % short(backups))

        print('[1/3] 启动服务端 ...')
        proc_srv = subprocess.Popen([sys.executable, '-u', os.path.join(app, 'server', 'server.py')],
                                    cwd=app, stdout=srv_log, stderr=subprocess.STDOUT, env=env)
        if not wait_for(lambda: port_open(srv_port), 25):
            print('  服务端启动失败')
            return 1
        print('  ✅ TCP %d · 面板 http://127.0.0.1:%d' % (srv_port, srv_web))

        print('[2/3] 启动客户端并连接 ...')
        proc_cli = subprocess.Popen([sys.executable, '-u', os.path.join(app, 'client', 'client.py')],
                                    cwd=app, stdout=cli_log, stderr=subprocess.STDOUT, env=env)
        base = 'http://127.0.0.1:%d' % cli_web
        if not wait_for(lambda: api(base, '/api/status', timeout=5).get('watching') is True, 30):
            print('  客户端未进入监听状态')
            return 1
        st = api(base, '/api/status')
        rec = st.get('reconcile') or {}
        print('  ✅ 已连接 %s · 监听中 · 启动对账 checked=%s uploaded=%s'
              % (st.get('server'), rec.get('checked'), rec.get('uploaded')))

        cat = api(base, '/api/flow/templates')
        print('  内置模板 %s 个 · 分类 %s' % (cat.get('total'), '/'.join(cat.get('categories', []))))
        blocks = api(base, '/api/flow/blocks').get('blocks', [])
        print('  可用积木 %d 个\n' % len(blocks))

        print('[3/3] 依次执行备份流程\n')
        for idx, (title, key, scope, desc) in enumerate(cases, 1):
            tpl = api(base, '/api/flow/template/%s' % key).get('flow')
            payload = {'flow': tpl, 'folder': 'work'}
            if scope == 'file':
                rel = '合同.txt'
                payload['relpath'] = rel
                payload['full_path'] = os.path.join(sync_dir, rel)
            print('-' * 78)
            print('演示 %d. %s   [%s]' % (idx, title, key))
            print('      %s' % desc)
            print('-' * 78)
            t0 = time.time()
            try:
                res = api(base, '/api/flow/execute', 'POST', payload)
            except Exception as e:
                print('  ❌ 调用失败: %s' % e)
                ok_all = False
                continue
            if res.get('status') != 'ok':
                print('  ❌ %s' % res.get('error'))
                ok_all = False
                continue
            for line in res.get('logs', []):
                print('   ' + short(line))
            bad = res.get('failed') or 0
            mark = '✅' if bad == 0 else '⚠'
            print('  %s %s 行输出 · 失败 %s 处 · 耗时 %.1fs\n' % (mark, res.get('steps'), bad, time.time() - t0))
            ok_all = ok_all and bad == 0
            time.sleep(1)

        print('=' * 78)
        print('结果：服务端备份目录')
        print('=' * 78)
        for dp, _, fns in os.walk(backups):
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                print('  %8d  %s' % (os.path.getsize(full), os.path.relpath(full, backups)))
        print('\n客户端产出的归档与清单：')
        for sub in ('archives', 'manifests'):
            d = os.path.join(app, 'client', sub)
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    print('  %8d  client/%s/%s' % (os.path.getsize(os.path.join(d, fn)), sub, fn))
        demo_backup = os.path.join(backups, 'demo', '合同.txt')
        if os.path.isfile(demo_backup):
            same = sha256(demo_backup) == sha256(os.path.join(sync_dir, '合同.txt'))
            print('\n交叉校验：备份的 合同.txt 与本地 SHA256 %s' % ('一致 ✅' if same else '不一致 ❌'))
            ok_all = ok_all and same
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
            print('\n临时目录已保留: %s' % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print('\n演示结束：%s' % ('全部通过 ✅' if ok_all else '有失败项 ❌'))
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())

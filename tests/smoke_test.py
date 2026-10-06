#!/usr/bin/env python3
"""yumirror v3 冒烟测试（无需 pytest）

    python tests/smoke_test.py          # 跑完自动清理临时目录
    python tests/smoke_test.py -k       # 保留临时目录，便于排查

覆盖内容：
  1. 配置键别名兼容：server/config.json 用旧键 server_port / device_name 也能生效
  2. BackupManager.store 的 mtime 去重：乱序到达的空内容不得覆盖已有非空备份
  3. client._ignore 的祖先目录规则：ignore_patterns 里的目录名对嵌套文件同样生效
  4. 端到端：起服务端 + 客户端，运行中新建的文件落到服务端备份且 sha256 一致
  5. 端到端：命中忽略规则的文件不进备份
  6. 端到端：客户端启动前已存在的文件不会自动上备份（已知限制），
     用 client/seed_push.py 全量播种后能补上
"""
import hashlib
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = '-k' in sys.argv

_results = []


def check(name, ok, detail=''):
    _results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   ({detail})" if detail else ''))
    return ok


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def wait_for(fn, timeout, interval=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def read_text(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except OSError:
        return ''


def build_app(tmp):
    """把被测代码复制到临时目录，避免污染仓库（配置/日志/备份都落在临时目录）"""
    app = os.path.join(tmp, 'app')
    shutil.copytree(os.path.join(ROOT, 'shared'), os.path.join(app, 'shared'),
                    ignore=shutil.ignore_patterns('__pycache__'))
    os.makedirs(os.path.join(app, 'server'))
    os.makedirs(os.path.join(app, 'client'))
    shutil.copy2(os.path.join(ROOT, 'server', 'server.py'), os.path.join(app, 'server', 'server.py'))
    shutil.copy2(os.path.join(ROOT, 'server', '__init__.py'), os.path.join(app, 'server', '__init__.py'))
    shutil.copy2(os.path.join(ROOT, 'client', 'client.py'), os.path.join(app, 'client', 'client.py'))
    shutil.copytree(os.path.join(ROOT, 'client', 'templates'), os.path.join(app, 'client', 'templates'))
    if os.path.isfile(os.path.join(ROOT, 'client', 'seed_push.py')):
        shutil.copy2(os.path.join(ROOT, 'client', 'seed_push.py'), os.path.join(app, 'client', 'seed_push.py'))
    return app


def main():
    tmp = tempfile.mkdtemp(prefix='yumirror_smoke_')
    print(f'临时目录: {tmp}\n')
    app = build_app(tmp)

    srv_port, srv_web, cli_web = free_port(), free_port(), free_port()

    # ---------- 0. 兼容性静态检查 ----------
    # Python 3.13 及以前，函数/变量注解是「立即求值」的：某个类型名忘了 import，
    # 在 3.14（PEP 649 延迟求值）上跑得好好的，到 3.10 一导入就 NameError 起不来。
    # 这里静态扫一遍，避免再出现这种只有旧版本才暴露的坑。
    print('[0] 兼容性静态检查（注解引用的名字必须有定义）')
    import ast
    import builtins as _bi
    bad = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in ('__pycache__', '.git', 'docs', 'venv', '.venv')]
        for fn in fns:
            if not fn.endswith('.py'):
                continue
            p = os.path.join(dp, fn)
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    src = f.read()
                tree = ast.parse(src)
            except SyntaxError as e:
                bad.append('%s 语法错误: %s' % (os.path.relpath(p, ROOT), e))
                continue
            if 'from __future__ import annotations' in src:
                continue
            defined = set(dir(_bi))
            annotations = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    defined.update((a.asname or a.name.split('.')[0]) for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    defined.update(a.asname or a.name for a in node.names)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    defined.add(node.id)
                elif isinstance(node, ast.arg):
                    defined.add(node.arg)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    defined.add(node.name)
                elif isinstance(node, ast.Global):
                    defined.update(node.names)
                ann = None
                if isinstance(node, ast.AnnAssign):
                    ann = node.annotation
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
                    ann = node.returns
                elif isinstance(node, ast.arg) and node.annotation:
                    ann = node.annotation
                if ann is not None:
                    annotations += [s.id for s in ast.walk(ann) if isinstance(s, ast.Name)]
            missing = sorted({n for n in annotations if n not in defined})
            if missing:
                bad.append('%s 用了未定义的名字 %s' % (os.path.relpath(p, ROOT), ','.join(missing)))
    check('注解里的名字都有定义（Python 3.10 也能 import）', not bad, '; '.join(bad))
    passed = not bad

    sync_dir = os.path.join(tmp, 'sync_work')
    os.makedirs(sync_dir)
    backups = os.path.join(tmp, 'backups')
    srv_log = os.path.join(tmp, 'server.out')
    cli_log = os.path.join(tmp, 'client.out')

    # 服务端配置故意只用“旧键名”，验证别名兼容
    with open(os.path.join(app, 'server', 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'device_name': 'SmokeServer',
            'server_port': srv_port,
            'bind_host': '127.0.0.1',
            'web_host': '127.0.0.1',
            'web_port': srv_web,
            'backup_dir': backups,
            'flows_share_dir': os.path.join(tmp, 'shared_flows'),
            'users_db_path': os.path.join(tmp, 'users.json'),
            'logs_dir': os.path.join(tmp, 'srv_logs'),
            'heartbeat_interval': 2,
            'stats_interval': 5,
        }, f, indent=2)

    with open(os.path.join(app, 'client', 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'client_id': 'SmokeClient',
            'group_id': 'smoke',
            'server_host': '127.0.0.1',
            'server_port': srv_port,
            'server_web_port': srv_web,
            'web_host': '127.0.0.1',
            'web_port': cli_web,
            'heartbeat_interval': 2,
            'reconnect_base_delay': 1,
            'reconnect_max_delay': 5,
            'sync_folders': [{'name': 'work', 'path': sync_dir}],
            'ignore_patterns': ['__pycache__', '*.pyc', 'node_modules'],
            'backup_passphrase': 'smoke-pass-123',
            'ssh_tunnel': {'enabled': False},
        }, f, indent=2)

    # 客户端启动前就存在的文件：用于验证“已知限制 + seed_push 补偿”
    preseed = os.path.join(sync_dir, 'preseed.txt')
    with open(preseed, 'w', encoding='utf-8') as f:
        f.write('existed before client start')

    sys.path.insert(0, app)

    # ---------- 1. BackupManager.store mtime 去重 ----------
    print('[1] BackupManager.store 的 mtime 去重（防 0 字节乱序覆盖）')
    srv = importlib.import_module('server.server')
    unit_dir = os.path.join(tmp, 'unit_backups')
    bm = srv.BackupManager(unit_dir)
    target = os.path.join(unit_dir, 'g', 'a.txt')
    bm.store('g', 'a.txt', b'v1-content', 1000.0)
    check('首次写入成功', os.path.isfile(target) and open(target, 'rb').read() == b'v1-content')
    bm.store('g', 'a.txt', b'', 1000.0)
    check('同 mtime 的空内容被忽略（不再清空备份）', open(target, 'rb').read() == b'v1-content')
    bm.store('g', 'a.txt', b'v2-newer', 1001.0)
    check('mtime 前进的正常更新仍会写入', open(target, 'rb').read() == b'v2-newer')
    bm.store('g', 'a.txt', b'', 1002.0)
    check('mtime 前进的合法清空会被接受', open(target, 'rb').read() == b'')

    # ---------- 1b. 版本化备份与回滚 ----------
    print('\n[1b] 版本化备份与回滚')
    vdir = os.path.join(tmp, 'ver_backups')
    vbm = srv.BackupManager(vdir, max_versions=3)
    vbm.store('g', 'f.txt', b'v1', 1000.0)
    vbm.store('g', 'f.txt', b'v2-content', 1001.0)
    vbm.store('g', 'f.txt', b'v3-content', 1002.0)
    cur_file = os.path.join(vdir, 'g', 'f.txt')
    info = vbm.list_versions('g', 'f.txt')
    check('覆盖前自动留存历史版本', len(info['versions']) == 2, [v['id'] for v in info['versions']])
    check('当前版本内容正确', open(cur_file, 'rb').read() == b'v3-content')
    r = vbm.restore('g', 'f.txt', 'latest')
    check('回滚到上一版成功', bool(r.get('ok')) and open(cur_file, 'rb').read() == b'v2-content', r)
    info2 = vbm.list_versions('g', 'f.txt')
    check('回滚前的内容被留档（回滚可撤销）', len(info2['versions']) >= 3, len(info2['versions']))
    snap = vbm.snapshot('g', 'f.txt', reason='test')
    check('显式打版本点成功', bool(snap) and snap.get('reason') == 'test')
    for i in range(6):
        vbm.store('g', 'f.txt', ('x%d' % i).encode(), 2000.0 + i)
    check('版本数量受 max_versions 限制',
          len(vbm.list_versions('g', 'f.txt')['versions']) <= 3)
    check('备份列表不把 .versions 当业务文件',
          not any('.versions' in f for f in vbm.list_group_backups('g')),
          vbm.list_group_backups('g'))
    check('没有残留 .tmp 临时文件',
          not [f for f in os.listdir(os.path.join(vdir, 'g')) if f.endswith('.tmp')])

    # ---------- 2. client._ignore 祖先目录规则 ----------
    print('\n[2] client._ignore 忽略规则')
    cli = importlib.import_module('client.client')
    stub = types.SimpleNamespace(cfg={'ignore_patterns': ['node_modules', '*.pyc', 'build/']})
    ignore = cli.MirrorClient._ignore
    check('祖先目录名命中 (a/node_modules/b.js)', ignore(stub, 'a/node_modules/b.js') is True)
    check('文件名通配命中 (a/b.pyc)', ignore(stub, 'a/b.pyc') is True)
    check('Windows 反斜杠路径同样命中', ignore(stub, os.path.join('a', 'node_modules', 'b.js')) is True)
    check('正常文件不误伤 (a/b.js)', ignore(stub, 'a/b.js') is False)

    # ---------- 2b. watchdog 事件在“暂停”期间不得丢失 ----------
    print('\n[2b] 暂停期间的 watchdog 事件必须补发')
    from shared.sync_core import SyncEventHandler
    calls = []
    h = SyncEventHandler(lambda rp: calls.append(('create', rp)),
                         lambda rp: calls.append(('modify', rp)),
                         lambda rp: calls.append(('delete', rp)),
                         lambda s, d: calls.append(('rename', s, d)),
                         lambda rp: False, sync_dir)

    class _Ev:
        def __init__(self, p, d=False):
            self.src_path = p
            self.dest_path = p
            self.is_directory = d

    h.pause()
    h.on_created(_Ev(os.path.join(sync_dir, 'paused_a.txt')))
    h.on_modified(_Ev(os.path.join(sync_dir, 'paused_b.txt')))
    check('暂停期间不立即回调（避免处理自己的写入）', not calls, calls)
    h.resume()
    check('恢复后补发全部事件（不再永久漏同步）',
          sorted(calls) == [('create', 'paused_a.txt'), ('modify', 'paused_b.txt')], calls)
    check('补发后队列清空', not h._pending)

    # ---------- 3. 端到端 ----------
    print('\n[3] 端到端：服务端 + 客户端')
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    srv_out = open(srv_log, 'wb')
    cli_out = open(cli_log, 'wb')
    proc_srv = proc_cli = None
    passed = True
    try:
        proc_srv = subprocess.Popen([sys.executable, '-u', os.path.join(app, 'server', 'server.py')],
                                    cwd=app, stdout=srv_out, stderr=subprocess.STDOUT, env=env)
        up = wait_for(lambda: port_open(srv_port), 25)
        check(f'服务端用旧键 server_port 监听 {srv_port}', up)
        passed &= up
        if not up:
            raise RuntimeError('服务端未启动')

        proc_cli = subprocess.Popen([sys.executable, '-u', os.path.join(app, 'client', 'client.py')],
                                    cwd=app, stdout=cli_out, stderr=subprocess.STDOUT, env=env)
        registered = wait_for(lambda: '客户端已就绪' in read_text(
            os.path.join(tmp, 'srv_logs', sorted(os.listdir(os.path.join(tmp, 'srv_logs')))[-1])
            if os.path.isdir(os.path.join(tmp, 'srv_logs')) and os.listdir(os.path.join(tmp, 'srv_logs')) else ''), 30)
        check('客户端注册成功（服务端日志出现「客户端已就绪」）', registered)
        passed &= registered

        base = f'http://127.0.0.1:{cli_web}'

        def _api(path, method='GET', body=None, raw=False):
            url = base + path
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, method=method)
            if data is not None:
                req.add_header('Content-Type', 'application/json')
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode('utf-8', 'replace')
                return text if raw else json.loads(text)

        # 关键：必须等客户端真的进入“监听中”，否则“服务端已就绪”只是代理信号，
        # 此刻创建的文件会落在监听就绪之前的窗口里（实机 2/3 概率漏同步）
        watching = wait_for(lambda: _api('/api/status').get('watching') is True, 30)
        check('客户端进入监听状态 (watching=true)', watching)
        passed &= watching

        # 启动兜底对账：预存文件（客户端启动前就存在）应被补传，不再依赖手工 seed_push
        rec = wait_for(lambda: (_api('/api/status').get('reconcile') or {}).get('uploaded', 0) >= 1, 30)
        check('启动备份对账把历史文件补传到服务端', rec,
              _api('/api/status').get('reconcile'))
        passed &= rec

        # 3a. 运行中新建的文件应进入服务端备份
        live = os.path.join(sync_dir, 'live.txt')
        payload = 'live content ' + str(time.time())
        with open(live, 'w', encoding='utf-8') as f:
            f.write(payload)
        live_backup = os.path.join(backups, 'smoke', 'live.txt')
        ok = wait_for(lambda: os.path.isfile(live_backup) and os.path.getsize(live_backup) > 0
                      and sha256_file(live_backup) == sha256_file(live), 30)
        check('运行中新建文件已备份且 sha256 一致', ok,
              f'{os.path.getsize(live_backup)} 字节' if os.path.isfile(live_backup) else '未出现')
        passed &= ok

        # 3b. 忽略规则命中的文件不进备份
        ignored_dir = os.path.join(sync_dir, '__pycache__')
        os.makedirs(ignored_dir, exist_ok=True)
        with open(os.path.join(ignored_dir, 'junk.pyc'), 'w', encoding='utf-8') as f:
            f.write('junk')
        time.sleep(3)
        check('忽略规则生效（__pycache__/junk.pyc 未进备份）',
              not os.path.exists(os.path.join(backups, 'smoke', '__pycache__', 'junk.pyc')))
        passed &= not os.path.exists(os.path.join(backups, 'smoke', '__pycache__', 'junk.pyc'))

        # 3c. 启动前已存在的文件（历史文件）：由启动对账自动补传，不再需要手工播种
        pre_backup = os.path.join(backups, 'smoke', 'preseed.txt')
        pre_ok = wait_for(lambda: os.path.isfile(pre_backup)
                          and sha256_file(pre_backup) == sha256_file(preseed), 20)
        check('启动前已存在的文件由启动对账自动补传', pre_ok)
        passed &= pre_ok

        seed = os.path.join(app, 'client', 'seed_push.py')
        if os.path.isfile(seed):
            seed_log = os.path.join(tmp, 'seed_push.out')
            with open(seed_log, 'wb') as fh:
                rc = subprocess.call([sys.executable, seed], cwd=app, stdout=fh,
                                     stderr=subprocess.STDOUT, env=env, timeout=120)
            last = (read_text(seed_log).strip().splitlines() or [''])[-1]
            check('seed_push.py 手工全量播种仍可用', rc == 0, last)
            passed &= (rc == 0)
        else:
            check('seed_push.py 存在', False)

        # ---------- 4. 备份流程编辑器 ----------
        print('\n[4] 备份流程编辑器')
        api_call = _api

        try:
            cat = api_call('/api/flow/templates')
            tpls = cat.get('templates', [])
            check('模板目录接口返回内置模板', len(tpls) >= 25,
                  f"{len(tpls)} 个 / {len(cat.get('categories', []))} 个分类")
            backup_tpls = [t for t in tpls if t.get('category') == '备份']
            check('提供多个备份类模板', len(backup_tpls) >= 4,
                  ', '.join(t['key'] for t in backup_tpls))
            check('每个模板都有步骤数与说明',
                  all(t.get('steps', 0) > 0 and t.get('description') for t in tpls))
            passed &= len(tpls) >= 25

            with urllib.request.urlopen(base + '/editor', timeout=10) as resp:
                editor = resp.read().decode('utf-8', 'replace')
            engine_blocks = api_call('/api/flow/blocks').get('blocks', [])
            missing = [b['type'] for b in engine_blocks if (b['type'] + ':{') not in editor]
            check('编辑器覆盖引擎全部积木（防再次脱节）', not missing,
                  f"缺失: {missing}" if missing else f'{len(engine_blocks)} 个积木全部可拖拽')
            check('编辑器支持 ?load= 打开流程 + 模板目录接线',
                  'URLSearchParams' in editor and '/api/flow/templates' in editor)
            passed &= not missing

            qflow = {"name": "smoke_queue_flow", "queues": [
                {"id": "q1", "label": "L1", "steps": [
                    {"id": "s1", "type": "log", "config": {"message": "a"}},
                    {"id": "s2", "type": "log", "config": {"message": "b"}}]},
                {"id": "q2", "label": "L2", "steps": [
                    {"id": "s3", "type": "log", "config": {"message": "c"}}]}],
                "merge_at": []}
            api_call('/api/flow/save', 'POST', qflow)
            lst = api_call('/api/flow/list').get('flows', [])
            row = next((f for f in lst if f.get('name') == 'smoke_queue_flow'), None)
            check('/api/flow/list 的步骤数不再是 0', bool(row) and row.get('steps') == 3, row)
            passed &= bool(row) and row.get('steps') == 3

            payload = os.path.join(tmp, 'flow_payload.bin')
            with open(payload, 'wb') as f:
                f.write(os.urandom(4096))
            tpl = api_call('/api/flow/template/backup_on_change').get('flow')
            res = api_call('/api/flow/execute', 'POST',
                           {'flow': tpl, 'full_path': payload, 'relpath': 'flowtest/payload.bin'})
            blob = '\n'.join(res.get('logs', []))
            check('备份模板试跑返回日志', res.get('status') == 'ok' and len(res.get('logs', [])) > 0,
                  f"{res.get('steps')} 行输出 · 失败 {res.get('failed')} 处")
            check('试跑已注入上传通道（无“未注入”）', '未注入' not in blob)
            bpath = os.path.join(backups, 'smoke', 'flowtest', 'payload.bin')
            up_ok = wait_for(lambda: os.path.isfile(bpath)
                             and sha256_file(bpath) == sha256_file(payload), 20)
            check('流程上传的文件真的进了服务端备份且哈希一致', up_ok,
                  f'{os.path.getsize(bpath)} 字节' if os.path.isfile(bpath) else '未出现')
            check('流程日志确认校验通过', '备份完成且校验通过' in blob)
            passed &= up_ok and res.get('status') == 'ok' and '未注入' not in blob

            # ---------- 5. 版本 / 加密 积木（对着运行中的服务端） ----------
            print('\n[5] 版本与加密积木')
            with urllib.request.urlopen(base + '/blocks', timeout=10) as resp:
                editor_blocks = resp.read().decode('utf-8', 'replace')
            missing_b = [b['type'] for b in engine_blocks if (b['type'] + ':{') not in editor_blocks]
            check('积木模式编辑器同样覆盖全部积木', not missing_b, f'缺失: {missing_b}' if missing_b else '')
            passed &= not missing_b

            vt = os.path.join(sync_dir, 'versioned.txt')
            v1_bytes = '第一版内容'.encode('utf-8')
            v2_bytes = '第二版内容变了'.encode('utf-8')
            vpath = os.path.join(backups, 'smoke', 'versioned.txt')
            with open(vt, 'wb') as f:
                f.write(v1_bytes)
            check('第一版已进服务端备份',
                  wait_for(lambda: os.path.isfile(vpath) and open(vpath, 'rb').read() == v1_bytes, 30))
            time.sleep(1.2)
            with open(vt, 'wb') as f:
                f.write(v2_bytes)
            check('第二版覆盖并触发留存历史',
                  wait_for(lambda: os.path.isfile(vpath) and open(vpath, 'rb').read() == v2_bytes, 30))
            vinfo = srv.BackupManager(backups, 20).list_versions('smoke', 'versioned.txt')
            check('服务端已保留第一版历史', len(vinfo['versions']) >= 1,
                  [v['id'] for v in vinfo['versions']])
            passed &= len(vinfo['versions']) >= 1

            def run_steps(steps, rel='versioned.txt'):
                return api_call('/api/flow/execute', 'POST',
                                {'flow': {'name': 'smoke_ver', 'steps': steps},
                                 'relpath': rel, 'folder': 'work'})

            r = run_steps([{'type': 'list_versions',
                            'config': {'file_path': '{{_relpath}}', 'result_var': '_versions'}}])
            blob = '\n'.join(r.get('logs', []))
            check('「列出版本」积木可用', r.get('status') == 'ok'
                  and (r.get('vars') or {}).get('_version_count', 0) >= 1,
                  f"version_count={(r.get('vars') or {}).get('_version_count')}")
            r2 = run_steps([{'type': 'restore_version',
                             'config': {'file_path': '{{_relpath}}', 'version': 'latest',
                                        'result_var': '_restore_ok'}}])
            check('「服务端回滚」积木把备份退回上一版',
                  r2.get('status') == 'ok'
                  and wait_for(lambda: open(vpath, 'rb').read() == v1_bytes, 10),
                  '\n'.join(r2.get('logs', []))[-200:])
            passed &= r2.get('status') == 'ok'
            fetched = os.path.join(tmp, 'fetched_version.txt')
            r3 = run_steps([{'type': 'fetch_version',
                             'config': {'file_path': '{{_relpath}}', 'version': 'latest',
                                        'out_path': fetched, 'result_var': '_fetched_path'}}])
            check('「取回旧版本」积木落地到本地',
                  r3.get('status') == 'ok' and os.path.isfile(fetched)
                  and os.path.getsize(fetched) > 0,
                  '\n'.join(r3.get('logs', []))[-200:])
            passed &= os.path.isfile(fetched)

            enc_flow = {'name': 'smoke_enc', 'steps': [
                {'type': 'encrypt_file', 'config': {
                    'file_path': '{{_full_path}}',
                    'passphrase_secret': 'backup_passphrase',
                    'out_path': '{{_client_dir}}/vault/{{_relpath}}.enc',
                    'result_var': '_enc_path'}},
                {'type': 'upload_to_server', 'config': {
                    'file_path': '{{_enc_path}}', 'remote_path': 'vault/{{_relpath}}.enc',
                    'result_var': '_upload_ok'}},
                {'type': 'log', 'config': {'message': '🔐 加密上传完成 {{_enc_path}}'}},
            ]}
            r4 = api_call('/api/flow/execute', 'POST',
                          {'flow': enc_flow, 'relpath': 'versioned.txt', 'folder': 'work',
                           'full_path': vt})
            enc_backup = os.path.join(backups, 'smoke', 'vault', 'versioned.txt.enc')
            enc_ok = wait_for(lambda: os.path.isfile(enc_backup)
                              and open(enc_backup, 'rb').read(6) == b'LMENC1', 20)
            check('「加密文件」+上传：服务端只存密文', enc_ok,
                  '\n'.join(r4.get('logs', []))[-200:])
            if os.path.isfile(enc_backup):
                check('密文里不含明文内容', v2_bytes not in open(enc_backup, 'rb').read())
                check('日志无错误行', '❌' not in '\n'.join(r4.get('logs', [])))
            passed &= enc_ok

            # ---------- 6. 多语言（i18n） ----------
            print('\n[6] 多语言与语言包接口')
            import shared.i18n as i18n
            import shared.flow_engine as fe
            zh, en = i18n.UI['zh-CN'], i18n.UI['en-US']
            missing_en = sorted(set(zh) - set(en))
            missing_zh = sorted(set(en) - set(zh))
            check('两个语言的词条完全对齐（不会漏翻）', not missing_en and not missing_zh,
                  '缺英文: %s 缺中文: %s' % (missing_en[:4], missing_zh[:4]))
            passed &= not missing_en and not missing_zh
            blocks_missing = sorted(set(fe.BlockRegistry.all()) - set(i18n.BLOCK_I18N))
            check('每个积木都有英文名', not blocks_missing, '缺: %s' % blocks_missing[:5])
            passed &= not blocks_missing
            tpl_missing = sorted(set(fe.FLOW_TEMPLATES) - set(i18n.TEMPLATE_I18N))
            check('每个模板都有英文名', not tpl_missing, '缺: %s' % tpl_missing[:5])
            passed &= not tpl_missing
            cat_missing = sorted(set(fe.TEMPLATE_CATEGORIES) - set(i18n.CATEGORY_I18N))
            check('每个模板分类都有英文名', not cat_missing, '缺: %s' % cat_missing)
            passed &= not cat_missing

            d = _api('/api/i18n?lang=en-US')
            en_ok = bool(isinstance(d, dict) and d.get('lang') == 'en-US'
                         and len(d.get('strings') or {}) > 50 and d.get('text_map'))
            check('GET /api/i18n?lang=en-US 返回英文语言包', en_ok,
                  'lang=%s 词条=%s 映射=%s' % ((d or {}).get('lang'),
                                              len((d or {}).get('strings') or {}),
                                              len((d or {}).get('text_map') or {})))
            passed &= en_ok
            d = _api('/api/i18n?lang=zh-CN')
            check('中文语言包可用且不需要翻译映射',
                  d.get('lang') == 'zh-CN' and not d.get('text_map'), str(d.get('lang')))
            d = _api('/api/i18n?lang=en')
            check('语言别名归一（en → en-US）', d.get('lang') == 'en-US', str(d.get('lang')))
            d = _api('/api/i18n?lang=klingon')
            check('不支持的语言回退到默认（zh-CN）', d.get('lang') == 'zh-CN', str(d.get('lang')))
            d = _api('/api/i18n')
            check('无参数时按 Accept-Language 协商', d.get('lang') in i18n.LANGS, str(d.get('lang')))
            check('英文包带积木与模板英文名',
                  len(d.get('blocks') or {}) >= 30 and len(d.get('templates') or {}) >= 32,
                  'blocks=%s templates=%s' % (len(d.get('blocks') or {}), len(d.get('templates') or {})))
            page = _api('/files', raw=True)
            check('页面已注入语言脚本与切换器',
                  isinstance(page, str) and 'YM_T' in page and 'data-lang-switcher' in page)
            passed &= isinstance(page, str) and 'YM_T' in page
        except Exception as e:
            check(f'流程编辑器检查异常: {e}', False)
            passed = False

        html_ok = False
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{srv_web}/login', timeout=5) as resp:
                html_ok = resp.status == 200
        except Exception as e:
            html_ok = False
            print(f'       (web 检查异常: {e})')
        check('服务端面板 /login 可访问', html_ok)
        passed &= html_ok

        unauth_ok = False
        try:
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{srv_web}/api/admin/users', timeout=5)
                unauth_ok = False
            except urllib.error.HTTPError as he:
                unauth_ok = he.code == 401
        except Exception:
            unauth_ok = False
        check('未登录访问 /api/admin/users 返回 401', unauth_ok)
        passed &= unauth_ok

    except Exception as e:
        check(f'端到端异常: {e}', False)
        passed = False
    finally:
        for p in (proc_cli, proc_srv):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.kill()
        srv_out.close()
        cli_out.close()

    print('\n' + '=' * 60)
    failed = [n for n, ok in _results if not ok]
    print(f'共 {len(_results)} 项，通过 {len(_results) - len(failed)} 项，失败 {len(failed)} 项')
    if failed:
        for n in failed:
            print(f'  FAIL: {n}')
        print(f'\n服务端输出尾部:\n{read_text(srv_log)[-1500:]}')
        print(f'\n客户端输出尾部:\n{read_text(cli_log)[-1500:]}')
    if KEEP:
        print(f'\n临时目录已保留: {tmp}')
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

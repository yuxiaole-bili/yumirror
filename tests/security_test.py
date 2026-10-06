#!/usr/bin/env python3
"""yumirror 安全性测试（可复现的攻防验证，不依赖 pytest）

    python tests/security_test.py          # 跑完自动清理
    python tests/security_test.py -k       # 保留临时目录

覆盖四组：
  A 服务端面板鉴权   未登录不得读写管理接口、不得注入/删除共享方案
  B 客户端面板       account 为空（最坏情况）时，命令执行积木不得落地、文件 API 不得越界
  C 裸协议层         路径/组名/版本号穿越不得写出备份根目录
  D 口令与转义        口令不得是无盐 SHA-256；文件名不得注入到内联 JS

每项输出 [PASS]=已防住 / [FAIL]=可利用 / [INFO]=事实记录（非缺陷）。
退出码非 0 表示存在 [FAIL]。
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = '-k' in sys.argv

_results = []


def check(name, ok, detail=''):
    _results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   ({detail})" if detail else ''))
    return ok


def info(name, detail=''):
    print(f"  [INFO] {name}" + (f"   ({detail})" if detail else ''))


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


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


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


class Http:
    """极简 HTTP 客户端：记录 cookie，支持 X-Auth-Token"""

    def __init__(self, base):
        self.base = base
        self.cookie = ''
        self.headers = {}
        self.opener = urllib.request.build_opener()

    def call(self, path, method='GET', body=None, token='', raw=False):
        url = self.base + path
        data = None
        headers = {}
        if body is not None and not raw:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        elif isinstance(body, bytes):
            data = body
        if self.cookie:
            headers['Cookie'] = self.cookie
        if token:
            headers['X-Auth-Token'] = token
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                self.headers = dict(resp.headers)
                sc = resp.headers.get('Set-Cookie')
                if sc:
                    self.cookie = sc.split(';')[0]
                payload = resp.read().decode('utf-8', 'replace')
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
            payload = e.read().decode('utf-8', 'replace')
        except Exception as e:
            return 0, str(e)
        try:
            return code, json.loads(payload)
        except Exception:
            return code, payload


def main():
    tmp = tempfile.mkdtemp(prefix='yumirror_sec_')
    print(f'临时目录: {tmp}\n')
    app = build_app(tmp)
    sync_dir = os.path.join(tmp, 'sync')
    os.makedirs(sync_dir)
    backups = os.path.join(tmp, 'backups')
    srv_port, srv_web, cli_web = free_port(), free_port(), free_port()
    outside = os.path.join(tmp, 'outside.txt')          # 同步目录之外的诱饵文件
    with open(outside, 'w', encoding='utf-8') as f:
        f.write('TOP-SECRET-OUTSIDE-SYNC')

    with open(os.path.join(app, 'server', 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({'server_name': 'SecServer', 'bind_host': '127.0.0.1', 'bind_port': srv_port,
                   'web_host': '127.0.0.1', 'web_port': srv_web, 'backup_dir': backups,
                   'flows_share_dir': os.path.join(tmp, 'shared_flows'),
                   'users_db_path': os.path.join(tmp, 'users.json'),
                   'logs_dir': os.path.join(tmp, 'srv_logs'),
                   'heartbeat_interval': 5, 'stats_interval': 30,
                   'group_keys': {'locked': 'k-123'}}, f, indent=2)
    # 最坏情况：客户端没配 account（面板无鉴权）
    with open(os.path.join(app, 'client', 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({'client_id': 'SecClient', 'group_id': 'sec',
                   'server_host': '127.0.0.1', 'server_port': srv_port, 'server_web_port': srv_web,
                   'web_host': '127.0.0.1', 'web_port': cli_web,
                   'heartbeat_interval': 5, 'sync_folders': [{'name': 'work', 'path': sync_dir}],
                   'ignore_patterns': [], 'auto_backup_on_start': True,
                   'ssh_tunnel': {'enabled': False}}, f, indent=2)

    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    srv_log = open(os.path.join(tmp, 'server.out'), 'wb')
    cli_log = open(os.path.join(tmp, 'client.out'), 'wb')
    proc_srv = proc_cli = None
    passed = True
    try:
        proc_srv = subprocess.Popen([sys.executable, '-u', os.path.join(app, 'server', 'server.py')],
                                    cwd=app, stdout=srv_log, stderr=subprocess.STDOUT, env=env)
        if not wait_for(lambda: port_open(srv_port), 25):
            print('服务端未启动'); return 1
        proc_cli = subprocess.Popen([sys.executable, '-u', os.path.join(app, 'client', 'client.py')],
                                    cwd=app, stdout=cli_log, stderr=subprocess.STDOUT, env=env)
        srv = Http(f'http://127.0.0.1:{srv_web}')
        cli = Http(f'http://127.0.0.1:{cli_web}')
        if not wait_for(lambda: cli.call('/api/status')[0] == 200, 30):
            print('客户端未就绪'); return 1

        # ── A 服务端面板鉴权 ──
        print('[A] 服务端面板鉴权（未登录攻击者）')
        code, _ = srv.call('/api/admin/users')
        passed &= check('未登录读取用户列表被拒 (401)', code == 401, f'HTTP {code}')
        code, _ = srv.call('/api/admin/users', 'POST', {'username': 'evil', 'password': 'evil'})
        passed &= check('未登录创建管理员被拒 (401)', code == 401, f'HTTP {code}')
        code, _ = srv.call('/api/admin/backups', 'DELETE', {'group': 'sec', 'path': 'x'})
        passed &= check('未登录删除备份被拒 (401)', code == 401, f'HTTP {code}')
        code, _ = srv.call('/api/admin/backup-path', 'POST', {'path': os.path.join(tmp, 'evil_dir')})
        passed &= check('未登录改备份路径被拒 (401)', code == 401, f'HTTP {code}')
        req = urllib.request.Request(f'http://127.0.0.1:{srv_web}/')

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(_NoRedirect()).open(req, timeout=8)
            passed &= check('未登录访问面板首页被跳转登录', False, 'HTTP 200 直接放行')
        except urllib.error.HTTPError as e:
            loc = e.headers.get('Location', '')
            passed &= check('未登录访问面板首页被跳转登录',
                            e.code in (301, 302, 303) and 'login' in loc.lower(),
                            f'HTTP {e.code} → {loc}')

        code, _ = srv.call('/api/clients')
        passed &= check('未登录读取在线设备列表被拒', code == 401, f'HTTP {code}')
        code, _ = srv.call('/api/status')
        passed &= check('未登录读取服务端状态被拒', code == 401, f'HTTP {code}')

        code, r = srv.call('/api/workshop/upload', 'POST',
                           {'name': 'evil-flow', 'author': 'Official', 'flow': {
                               'name': 'evil-flow', 'steps': [{'type': 'run_command',
                                                              'config': {'command': 'echo pwned'}}]}})
        passed &= check('未登录上传共享方案被拒 (401)', code == 401, f'HTTP {code}')
        evil_id = (r or {}).get('id', '') if isinstance(r, dict) else ''
        code, _ = srv.call(f'/api/workshop/delete/{evil_id or "nonexist"}', 'POST')
        passed &= check('未登录删除共享方案被拒 (401)', code == 401, f'HTTP {code}')

        code, r = srv.call('/api/register', 'POST', {'username': 'admin', 'password': 'admin-pass-1'})
        ok_reg = code == 200 and isinstance(r, dict) and r.get('status') == 'ok'
        code, r = srv.call('/api/login', 'POST', {'username': 'admin', 'password': 'wrong-pass'})
        passed &= check('错误口令登录被拒 (401)', code == 401, f'HTTP {code}')
        code, r = srv.call('/api/login', 'POST', {'username': 'admin', 'password': 'admin-pass-1'})
        token = (r or {}).get('token', '') if isinstance(r, dict) else ''
        passed &= check('正确口令可登录（门禁可用，不是全封死）',
                        ok_reg and code == 200 and bool(token), f'HTTP {code}')
        code, _ = srv.call('/api/admin/users')     # 带 cookie
        passed &= check('登录后可正常读取管理接口', code == 200, f'HTTP {code}')
        srv_authed = Http(f'http://127.0.0.1:{srv_web}')
        srv_authed.cookie = srv.cookie
        code, _ = srv_authed.call('/api/admin/users', token=token)
        passed &= check('X-Auth-Token 亦可访问（客户端代理用）', code == 200, f'HTTP {code}')

        # ── B 客户端面板（account 为空） ──
        print('\n[B] 客户端面板（未配置 account 的最坏情况）')
        marker = os.path.join(tmp, 'owned_by_flow.txt')
        code, r = cli.call('/api/flow/execute', 'POST', {
            'flow': {'name': 'rce', 'steps': [{'type': 'run_command',
                                               'config': {'command': f'echo pwned > "{marker}"'}}]}})
        time.sleep(1.0)
        executed = os.path.isfile(marker)
        passed &= check('命令执行积木默认不得落地（无配置即拒绝）', not executed,
                        '已生成 ' + marker if executed else f'HTTP {code}')
        if isinstance(r, dict):
            logs = '\n'.join(r.get('logs', []))
            if not executed:
                info('拒绝原因来自流程日志', (logs.strip().splitlines() or [''])[-1][:80])

        code, body = cli.call('/api/fs/read?folder=work&path=../outside.txt')
        leaked = isinstance(body, dict) and 'TOP-SECRET' in json.dumps(body, ensure_ascii=False)
        passed &= check('文件 API 不得读出同步目录之外的文件', not leaked, f'HTTP {code}')

        esc = os.path.join(tmp, 'escape.txt')
        code, _ = cli.call('/api/fs/write', 'POST',
                           {'folder': 'work', 'path': '../escape.txt', 'content': 'x'})
        passed &= check('文件 API 不得写出同步目录之外', not os.path.isfile(esc), f'HTTP {code}')

        code, _ = cli.call('/api/fs/delete', 'POST', {'folder': 'work', 'path': '../outside.txt'})
        passed &= check('文件 API 不得删除同步目录之外的文件', os.path.isfile(outside), f'HTTP {code}')

        code, body = cli.call(f'/api/fs/read?folder=work&path=' +
                              urllib.parse.quote('..\\..\\outside.txt'))
        leaked = isinstance(body, dict) and 'TOP-SECRET' in json.dumps(body, ensure_ascii=False)
        passed &= check('Windows 反斜杠穿越同样被拒', not leaked, f'HTTP {code}')

        # ── B2 显式开启后功能必须仍然可用（避免"修死了"） ──
        print('\n[B2] 显式开启 allow_command_block 后命令积木仍可用')
        cli2_dir = os.path.join(app, 'client2')
        shutil.copytree(os.path.join(app, 'client'), cli2_dir)
        sync2 = os.path.join(tmp, 'sync2')
        os.makedirs(sync2, exist_ok=True)
        cli2_web = free_port()
        with open(os.path.join(cli2_dir, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump({'client_id': 'SecClient2', 'group_id': 'sec',
                       'server_host': '127.0.0.1', 'server_port': srv_port, 'server_web_port': srv_web,
                       'web_host': '127.0.0.1', 'web_port': cli2_web,
                       'heartbeat_interval': 5, 'sync_folders': [{'name': 'work', 'path': sync2}],
                       'ignore_patterns': [], 'allow_command_block': True,
                       'ssh_tunnel': {'enabled': False}}, f, indent=2)
        cli2_log = open(os.path.join(tmp, 'client2.out'), 'wb')
        proc_cli2 = subprocess.Popen([sys.executable, '-u', os.path.join(cli2_dir, 'client.py')],
                                     cwd=app, stdout=cli2_log, stderr=subprocess.STDOUT, env=env)
        cli2 = Http(f'http://127.0.0.1:{cli2_web}')
        ready = wait_for(lambda: cli2.call('/api/status')[0] == 200, 30)
        marker2 = os.path.join(tmp, 'owned_optin.txt')
        code, _ = cli2.call('/api/flow/execute', 'POST', {
            'flow': {'name': 'optin', 'steps': [{'type': 'run_command',
                                                 'config': {'command': f'echo ok > "{marker2}"'}}]}})
        ok2 = wait_for(lambda: os.path.isfile(marker2), 8)
        passed &= check('显式配置后命令积木正常工作', ready and ok2,
                        f'HTTP {code}·client2 ready={ready}')
        proc_cli2.terminate()
        try:
            proc_cli2.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc_cli2.kill()
        cli2_log.close()

        # ── C 裸协议层 ──
        print('\n[C] 裸协议层（绕过面板直接说协议）')
        sys.path.insert(0, app)
        from shared.protocol import Connection, MsgType
        import hashlib

        def tls_wrap(raw):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE      # 攻击者视角：不管证书是谁的
            raw.settimeout(8)
            return ctx.wrap_socket(raw, server_hostname='127.0.0.1')

        def plain_session(gid='sec2', cid='PlainAttacker'):
            """明文连接（加固前的老客户端/扫描器）：应当连应用层都进不去"""
            body = json.dumps({'client_id': cid, 'group_id': gid, 'sync_folders': []}).encode()
            try:
                raw = socket.create_connection(('127.0.0.1', srv_port), timeout=6)
                raw.sendall((2).to_bytes(2, 'big') + len(body).to_bytes(4, 'big') + body)
                time.sleep(0.8)
                data = raw.recv(64)
                raw.close()
                return data
            except Exception:
                return b''

        def raw_session(gid='sec2', cid='RawAttacker', key=None, plain=False):
            s = socket.create_connection(('127.0.0.1', srv_port), timeout=8)
            if not plain:
                s = tls_wrap(s)
            c = Connection(s)
            hello = {'client_id': cid, 'group_id': gid, 'sync_folders': []}
            if key is not None:
                hello['group_key'] = key
            c.send(MsgType.HELLO, json.dumps(hello).encode())
            ack = c.recv(timeout=8)
            return c, ack

        plain_data = plain_session()
        passed &= check('明文连接无法进入应用层（TLS 已强制）',
                        b'"status"' not in (plain_data or b''), f'收到 {plain_data[:24]!r}')

        conn, ack = raw_session()
        ok_hello = bool(ack) and ack[0] == MsgType.HELLO_ACK
        info('TLS 之后再走原来的协议：攻击者要能建连必须先过 TLS 握手',
             'ack=' + ('ok' if ok_hello else 'none'))
        payload = b'pwned-by-raw-protocol'
        conn.send(MsgType.FILE_CREATE, json.dumps({
            'relpath': '../../pwned.txt', 'size': len(payload), 'mtime': time.time(),
            'sha256': hashlib.sha256(payload).hexdigest()}).encode())
        conn.send(MsgType.FILE_DATA, payload)
        conn.send(MsgType.FILE_DATA, b'')
        time.sleep(1.2)
        escaped = os.path.join(tmp, 'pwned.txt')
        passed &= check('FILE_CREATE 相对路径穿越不得写出备份根目录', not os.path.isfile(escaped))
        passed &= check('穿越路径也不得在备份组内落盘',
                        not any('pwned' in f for f in _walk(backups)), _walk(backups))

        conn2, _ = raw_session(gid='../../evil_group', cid='RawAttacker2')
        conn2.send(MsgType.FILE_CREATE, json.dumps({
            'relpath': 'g.txt', 'size': 3, 'mtime': time.time(),
            'sha256': hashlib.sha256(b'abc').hexdigest()}).encode())
        conn2.send(MsgType.FILE_DATA, b'abc')
        conn2.send(MsgType.FILE_DATA, b'')
        time.sleep(1.0)
        root = os.path.dirname(backups)
        stray = [d for d in os.listdir(root) if d.startswith('evil_group') and d != 'backups']
        passed &= check('组名穿越不得在备份目录之外建目录', not stray, stray)

        conn3, _ = raw_session(gid='sec', cid='RawAttacker3')
        rid = 'x1'
        conn3.send(MsgType.VERSION_OP_REQUEST, json.dumps({
            'request_id': rid, 'op': 'fetch', 'file_path': 'x.txt',
            'version': '../../../../etc/passwd'}).encode())
        r = conn3.recv(timeout=8)
        vok = True
        if r and r[0] == MsgType.VERSION_OP_RESPONSE:
            d = json.loads(r[2].decode())
            vok = not d.get('ok') and not d.get('content_b64')
        passed &= check('版本号穿越不得读出任意文件', vok)

        # 组密钥：配了密钥的组，无凭证/错密钥入组必须被拒（挡住"知道组名就能注入文件"）
        _, ack_no = raw_session(gid='locked', cid='NoKey')
        _, ack_bad = raw_session(gid='locked', cid='WrongKey', key='bad-guess')
        _, ack_ok = raw_session(gid='locked', cid='GoodKey', key='k-123')
        _, ack_open = raw_session(gid='sec', cid='NoKeyGroup')
        no_ok = bool(ack_no) and b'rejected' in ack_no[2]
        bad_ok = bool(ack_bad) and b'rejected' in ack_bad[2]
        good_ok = bool(ack_ok) and b'"status": "ok"' in ack_ok[2]
        open_ok = bool(ack_open) and b'"status": "ok"' in ack_open[2]
        passed &= check('配了组密钥的组：无密钥入组被拒', no_ok)
        passed &= check('配了组密钥的组：错误密钥入组被拒', bad_ok)
        passed &= check('配了组密钥的组：正确密钥可入组（功能没修死）', good_ok)
        passed &= check('没配密钥的组保持原来的开放行为（向后兼容）', open_ok)

        # ── D 口令与转义 ──
        print('\n[D] 口令存储与输出转义')
        users = {}
        try:
            with open(os.path.join(tmp, 'users.json'), 'r', encoding='utf-8') as f:
                users = json.load(f)
        except Exception as e:
            info('读取 users.json 失败', str(e))
        stored = (users.get('admin') or {}).get('password', '')
        salted = stored.startswith('pbkdf2') or '$' in stored
        passed &= check('口令不得以无盐 SHA-256 存储', salted, '存储形式: ' + stored[:24] + '...')

        srv_html = (srv_authed.call('/')[1] or '')
        page = srv_html if isinstance(srv_html, str) else ''
        has_jsq = 'jsq(' in page or "replace(/'/g" in page
        passed &= check('服务端面板对文件名做 JS 字符串转义（防内联注入）', has_jsq)

        cli_page = cli.call('/files')[1]
        cli_page = cli_page if isinstance(cli_page, str) else ''
        cli_ok = 'jsq(' in cli_page or "replace(/'/g" in cli_page
        passed &= check('客户端资源管理器同样做转义', cli_ok)

        # 攻击者通过正常同步通道投放一个带单引号的文件名（Windows 文件名不能含 / \ : * ? " < > |）
        evil_name = "a');alert(1);(('.txt"
        with open(os.path.join(sync_dir, evil_name), 'w', encoding='utf-8') as f:
            f.write('xss probe')
        ok = wait_for(lambda: any('alert(1)' in f for f in _walk(backups)), 25)
        info('带单引号的文件名已进入服务端备份', '是' if ok else '否（未同步）')
        # ── E 传输加密与加固项 ──
        print('\n[E] 传输加密（TLS/TOFU）与面板加固')
        with open(os.path.join(app, 'client', 'config.json'), 'r', encoding='utf-8') as f:
            ccfg = json.load(f)
        fp = ((ccfg.get('tls') or {}).get('fingerprint') or '')
        passed &= check('客户端首次连接已自动固定服务端指纹（TOFU）',
                        len(fp.replace(':', '')) == 64, fp[:23] + '…')
        srv_cert = os.path.join(app, 'server', 'certs', 'server.crt')
        passed &= check('服务端首次启动自动生成自签证书', os.path.isfile(srv_cert))
        key_path = os.path.join(app, 'server', 'certs', 'server.key')
        if os.path.isfile(key_path) and os.name != 'nt':
            passed &= check('私钥文件权限为 0600',
                            (os.stat(key_path).st_mode & 0o777) == 0o600,
                            oct(os.stat(key_path).st_mode & 0o777))

        # 指纹对不上必须拒绝连接
        cli3 = os.path.join(app, 'client3')
        shutil.copytree(os.path.join(app, 'client'), cli3)
        cli3_web = free_port()
        with open(os.path.join(cli3, 'config.json'), 'r', encoding='utf-8') as f:
            c3 = json.load(f)
        c3['web_port'] = cli3_web
        c3.setdefault('tls', {})['fingerprint'] = ':'.join(['AA'] * 32)
        with open(os.path.join(cli3, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump(c3, f, indent=2, ensure_ascii=False)
        log3 = open(os.path.join(tmp, 'client3.out'), 'wb')
        p3 = subprocess.Popen([sys.executable, '-u', os.path.join(cli3, 'client.py')],
                              cwd=app, stdout=log3, stderr=subprocess.STDOUT, env=env)
        cli3_http = Http(f'http://127.0.0.1:{cli3_web}')
        wait_for(lambda: cli3_http.call('/api/status')[0] == 200, 25)
        time.sleep(6)
        st3 = cli3_http.call('/api/status')[1]
        connected3 = bool(isinstance(st3, dict) and st3.get('connected'))
        p3.terminate()
        try:
            p3.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p3.kill()
        log3.close()
        passed &= check('指纹不匹配时客户端拒绝连接（防中间人）', not connected3,
                        f'connected={connected3}')

        # 登录限速
        codes = []
        for _ in range(6):
            codes.append(srv.call('/api/login', 'POST',
                                  {'username': 'admin', 'password': 'wrong-pass'})[0])
        passed &= check('连续登录失败触发限速（429）', 429 in codes, str(codes))

        # 安全响应头
        srv_authed.call('/api/status')
        h = srv_authed.headers
        passed &= check('面板返回安全响应头（nosniff / X-Frame-Options / CSP）',
                        h.get('X-Content-Type-Options') == 'nosniff'
                        and 'DENY' in (h.get('X-Frame-Options') or '')
                        and 'default-src' in (h.get('Content-Security-Policy') or ''),
                        ','.join(k for k in h if k.lower().startswith('x-') or k == 'Content-Security-Policy'))
        cli.call('/api/status')
        passed &= check('客户端面板同样返回安全响应头',
                        cli.headers.get('X-Content-Type-Options') == 'nosniff')

        code, listing = srv_authed.call('/api/backups/sec')
        names = [f for f in (listing.get('files') if isinstance(listing, dict) else []) or []]
        info('备份列表返回的原始文件名', str(names)[:90])
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
        print('\n' + '=' * 62)
        failed = [n for n, ok in _results if not ok]
        print(f'共 {len(_results)} 项，通过 {len(_results) - len(failed)} 项，失败 {len(failed)} 项')
        for n in failed:
            print('  FAIL: ' + n)
        if KEEP:
            print(f'\n临时目录已保留: {tmp}')
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


def _walk(root):
    out = []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            out.append(os.path.join(dp, fn))
    return out


if __name__ == '__main__':
    import urllib.parse  # noqa: E402  (供 quote 使用)
    sys.exit(main())

#!/usr/bin/env python3
"""
yumirror v3 - 客户端（Web GUI 面板版 / Backup Framework 集成）
"""
import os, sys, json, time, socket, shutil, threading, signal, traceback, fnmatch, logging, uuid, warnings
import urllib.request as urllib_req
import urllib.parse
from datetime import datetime
# Python 3.13 及以前注解是立即求值的：这里必须先导入，否则 3.10 上直接 NameError
from typing import Optional

# 关闭 Flask 开发服务器警告
warnings.filterwarnings('ignore', '.*development server.*')
warnings.filterwarnings('ignore', '.*Do not use.*')

# 控制台统一 UTF-8（中文 Windows 默认 GBK 无法编码 emoji/中文日志）
try:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import tls_util  # noqa: E402  传输层 TLS 工具
from shared import i18n  # noqa: E402  多语言词条与语言包


def _save_tls_fingerprint(fp):
    """首次连接后把服务端指纹写回 client/config.json（TOFU 的"记住"那一步）"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
            d = json.load(f)
        d.setdefault('tls', {})
        if isinstance(d['tls'], dict):
            d['tls']['fingerprint'] = fp
        else:
            d['tls'] = {'fingerprint': fp}
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.getLogger('tls').warning(f'写入指纹失败（不影响本次连接）: {e}')


FROZEN = getattr(sys, 'frozen', False)
if FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, '_MEIPASS', APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = APP_DIR
BASE_DIR = APP_DIR
ROOT_DIR = os.path.dirname(BASE_DIR)
if not FROZEN:
    sys.path.insert(0, ROOT_DIR)

from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for
from flask_socketio import SocketIO
from watchdog.observers import Observer

from shared.protocol import Connection, MsgType
from shared.sync_core import (
    compute_sha256, scan_folder, FileTransfer,
    SyncEventHandler, safe_relpath, ensure_dir
)
from shared.backup_framework import (
    PipelineLogger, PipelineEvent, PipelineContext, EventType,
    BackupPipeline, PipelineFactory
)

# ============================================================
DATA_DIR = os.path.join(BASE_DIR, 'data')
ensure_dir(DATA_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

def load_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8-sig') as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_config():
    return load_json(CONFIG_PATH, {
        'client_id': socket.gethostname(), 'group_id': 'default', 'group_key': '',
        # 传输层 TLS：默认开；fingerprint 首次连接时自动写入（TOFU）
        'tls': {'enabled': True, 'fingerprint': '', 'server_hostname': ''},
        # 界面语言：留空 = 跟随浏览器；也可 'zh-CN' / 'en-US'
        'language': '',
        'server_host': '127.0.0.1', 'server_port': 9999,
        'web_host': '0.0.0.0', 'web_port': 8087,
        'heartbeat_interval': 10, 'reconnect_base_delay': 2,
        'reconnect_max_delay': 60, 'sync_folders': [], 'sync_groups': {},
        'auto_backup_on_start': True, 'max_reconcile_files': 2000,
        'ignore_patterns': ['.DS_Store', 'Thumbs.db', '~$*', '*.tmp', '.git/*', '__pycache__/*']
    })

def save_config(cfg):
    save_json(CONFIG_PATH, cfg)

CONFIG = load_config()

FLOWS_DIR = os.path.join(BASE_DIR, 'flows')
ensure_dir(FLOWS_DIR)

LOGS_DIR = os.path.join(BASE_DIR, 'logs')
ensure_dir(LOGS_DIR)

try:
    from shared.flow_engine import (
        BlockRegistry, FlowEngine, FlowContext,
        validate_flow, FLOW_TEMPLATES,
        list_templates, TEMPLATE_CATEGORIES, _count_flow_steps,
    )
    HAS_FLOW_ENGINE = True
except ImportError:
    HAS_FLOW_ENGINE = False

# ============================================================
app = Flask(__name__, template_folder=os.path.join(RES_DIR, 'templates'))
app.config['SECRET_KEY'] = os.urandom(16).hex()
app.config['TEMPLATES_AUTO_RELOAD'] = True
try:
    from markupsafe import Markup

    def _i18n_js():
        # 首次访问跟随浏览器语言；用户用右下角切换器选过之后由 cookie 决定
        try:
            al = request.headers.get('Accept-Language', '')
        except Exception:
            al = ''
        lang = i18n.negotiate(al, CONFIG.get('language') or '')
        return Markup(i18n.frontend_js(lang))

    app.jinja_env.globals['i18n_js'] = _i18n_js
except Exception:
    pass

@app.after_request
def _no_cache(resp):
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.after_request
def _security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    resp.headers.setdefault('Content-Security-Policy',
                            "default-src 'self'; img-src 'self' data:; "
                            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
    if request.path.startswith('/api/'):
        resp.headers.setdefault('Cache-Control', 'no-store')
    return resp


@app.before_request
def _auth_gate():
    """若配置了 account（账户登录），则 Web 界面需要登录"""
    if not CONFIG.get('account'):
        return None
    if request.path in ('/login', '/api/login', '/api/register') or request.path.startswith('/static/'):
        return None
    if session.get('authed'):
        return None
    if request.path.startswith('/api/'):
        return jsonify({'status': 'error', 'msg': '未登录'}), 401
    return redirect(url_for('login'))

@app.route('/login')
def login():
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    d = request.get_json(force=True) or {}
    uname = (d.get('username') or '').strip()
    pwd = d.get('password') or ''
    r = _proxy_to_server('/api/login', 'POST', {'username': uname, 'password': pwd})
    if r.get('status') == 'ok':
        session['authed'] = True
        session['user'] = uname
        session['token'] = r.get('token', '')
        session['devices'] = r.get('devices', [])
        return jsonify({'status': 'ok', 'username': uname, 'devices': r.get('devices', [])})
    return jsonify({'status': 'error', 'msg': r.get('msg') or '登录失败'}), 401

@app.route('/api/register', methods=['POST'])
def api_register():
    d = request.get_json(force=True) or {}
    r = _proxy_to_server('/api/register', 'POST', {'username': d.get('username', '').strip(), 'password': d.get('password', '')})
    if r.get('status') == 'ok':
        session['authed'] = True
        session['user'] = r.get('username', '')
        session['token'] = r.get('token', '')
        session['devices'] = r.get('devices', [])
        return jsonify({'status': 'ok', 'username': r.get('username', ''), 'devices': r.get('devices', [])})
    return jsonify({'status': 'error', 'msg': r.get('msg') or '注册失败'}), 400

@app.route('/api/devices')
def api_devices():
    """当前登录账户名下的设备列表（在线状态实时从服务端刷新）"""
    devs = session.get('devices') or []
    token = session.get('token') or ''
    if token:
        try:
            req = urllib_req.Request(f"{_server_api_base()}/api/account/devices", method='GET')
            req.add_header('X-Auth-Token', token)
            with urllib_req.urlopen(req, timeout=5) as resp:
                d = json.loads(resp.read().decode('utf-8', errors='replace'))
                if d.get('status') == 'ok':
                    devs = d.get('devices', [])
                    session['devices'] = devs
        except Exception:
            pass
    return jsonify({'status': 'ok', 'username': session.get('user', ''), 'devices': devs})

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.pop('authed', None)
    session.pop('user', None)
    session.pop('token', None)
    session.pop('devices', None)
    return jsonify({'status': 'ok'})

logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('flask').setLevel(logging.WARNING)
logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)

sio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ============================================================
class C:
    G = '\033[92m'; R = '\033[91m'; Y = '\033[93m'; B = '\033[94m'
    C = '\033[96m'; D = '\033[90m'; M = '\033[95m'; W = '\033[97m'; RST = '\033[0m'

def cprint(color, *args):
    print(color + ' '.join(str(a) for a in args) + C.RST)

# ============================================================
state = {
    'connected': False, 'watching': False, 'server': '', 'peers': [], 'events': [],
    'stats': {'in': 0, 'out': 0, 'del_in': 0, 'del_out': 0, 'conflict': 0, 'error': 0},
    'sync_groups': CONFIG.get('sync_groups', {}),
    'reconcile': {'checked': 0, 'uploaded': 0, 'at': 0},
}
state_lock = threading.Lock()

_conn = None
conn_lock = threading.Lock()
running = True

def add_event(etype, data):
    with state_lock:
        state['events'].append({'type': etype, 'data': data, 'time': time.time()})
        if len(state['events']) > 500:
            state['events'] = state['events'][-500:]

# ============================================================
class MirrorClient:
    def __init__(self):
        self.cfg = CONFIG
        self.running = True
        self._paused = False
        self.obs = None
        self._handlers = []
        self.peers = []
        self._hash_requests = {}
        self._hash_requests_lock = threading.Lock()

        self._inbound_pipeline: Optional[BackupPipeline] = None
        self._outbound_pipeline: Optional[BackupPipeline] = None
        self._pipeline_logger: Optional[PipelineLogger] = None
        self._pipeline_ctx: Optional[PipelineContext] = None
        self._flow_debounce = {}
        self._triggers: list = []
        self._triggers_lock = threading.Lock()

        self._build_pipelines()

    def _build_pipelines(self):
        cid = self.cfg.get('client_id', socket.gethostname())

        out, out_logger, out_ctx = PipelineFactory.create_outbound(
            pipeline_name='outbound', log_dir=LOGS_DIR,
            get_conn=self._get_conn, find_folder=self._find_folder,
            folder_enabled=self._folder_enabled,
            external_stats=state['stats'], stats_lock=state_lock,
        )
        out_ctx.client_id = cid
        out_ctx.server_addr = f"{self.cfg.get('server_host','?')}:{self.cfg.get('server_port','?')}"
        self._outbound_pipeline = out

        inbound, in_logger, in_ctx = PipelineFactory.create_inbound(
            pipeline_name='inbound', log_dir=LOGS_DIR,
            get_conn=self._get_conn, find_folder=self._find_folder,
            folder_enabled=self._folder_enabled,
            rpc_peer_hash=self._rpc_peer_hash,
            rpc_server_hash=self._rpc_server_hash,
            get_peers=lambda: list(self.peers),
            flow_provider=self._get_active_flow_for_folder,
            flow_executor=self._execute_flow_in_pipeline,
            external_stats=state['stats'], stats_lock=state_lock,
        )
        in_ctx.client_id = cid
        in_ctx.server_addr = f"{self.cfg.get('server_host','?')}:{self.cfg.get('server_port','?')}"
        self._inbound_pipeline = inbound
        self._pipeline_logger = in_logger
        self._pipeline_ctx = in_ctx

        def on_log(module, level, msg, ts):
            if level in ('WARNING', 'ERROR'):
                add_event('pipeline_log', {
                    'module': module, 'level': level, 'message': msg, 'ts': ts
                })
        self._pipeline_logger.add_listener(on_log)

    def _get_conn(self):
        with conn_lock: return _conn

    def _find_folder(self, rp):
        for fc in self.cfg.get('sync_folders', []):
            lp = os.path.abspath(fc['path'])
            full = os.path.normpath(os.path.join(lp, rp))
            if os.path.commonpath([full, lp]) == os.path.normpath(lp):
                return fc
        arr = self.cfg.get('sync_folders', [])
        return arr[0] if arr else None

    @staticmethod
    def _safe_full(base_path, rel):
        """在 base_path 内安全拼接相对路径，越界返回 None"""
        base = os.path.abspath(base_path)
        full = os.path.abspath(os.path.join(base, rel or ''))
        if os.path.commonpath([full, base]) != base:
            return None
        return full

    def _folder_enabled(self, folder_name):
        sg = self.cfg.get('sync_groups', {}).get(folder_name, {})
        if not sg.get('enabled', True): return False
        if sg.get('active_flow', '') == '被动同步': return False
        return True

    def _get_active_flow_for_folder(self, folder_name: str) -> Optional[str]:
        sg = self.cfg.get('sync_groups', {}).get(folder_name, {})
        if not sg.get('enabled', True): return None
        return sg.get('active_flow', '') or None

    def _execute_flow_in_pipeline(self, flow_name, event, ctx, logger):
        if not HAS_FLOW_ENGINE: return None

        now = time.time()
        dk = (flow_name, event.type.value, event.relpath)
        if now - self._flow_debounce.get(dk, 0) < 5:
            logger.info('flow_bridge', f'防抖跳过: {flow_name} / {event.relpath}')
            return None
        self._flow_debounce[dk] = now
        if len(self._flow_debounce) > 200:
            self._flow_debounce = {k: v for k, v in self._flow_debounce.items() if now - v < 60}

        # 1️⃣ 优先查内置模板（更快，无需文件 IO）
        flow_def = FLOW_TEMPLATES.get(flow_name) if HAS_FLOW_ENGINE else None
        if flow_def:
            logger.info('flow_bridge', f'使用内置模板: {flow_name}')
        else:
            # 2️⃣ 回退到本地文件
            fn = flow_name.replace('/', '_').replace('\\', '_')
            fp = os.path.join(FLOWS_DIR, f"{fn}.json")
            if not os.path.isfile(fp):
                logger.warning('flow_bridge', f'流程不存在: {fp}')
                return None
            try:
                with open(fp, 'r', encoding='utf-8-sig') as f:
                    flow_def = json.load(f)
            except Exception as e:
                logger.error('flow_bridge', f'读取流程文件失败: {e}')
                return None

        try:
            engine = FlowEngine(flow_def)
            fctx = FlowContext(
                event_type=event.type.value,
                source_device=event.source_device,
                file_path=event.relpath,
                relpath=event.relpath,
                full_path=event.full_path,
                file_size=event.file_size,
                folder=event.folder_name,
                local_sha=event.local_sha256,
                remote_sha=event.remote_sha256,
                timestamp=now,
                client_dir=BASE_DIR, flows_dir=FLOWS_DIR, logs_dir=LOGS_DIR,
                backup_key_file=CONFIG.get('backup_key_file', ''),
                vault_dir=CONFIG.get('vault_dir', ''),
            )
            fctx.request_peer_hash = lambda pid, fp: self._rpc_peer_hash(pid, fp)
            fctx.request_server_backup_hash = lambda fp: self._rpc_server_hash(fp)
            fctx.request_peer_path_info = lambda pid, fp, it: self._rpc_peer_path(pid, fp, it)
            fctx.request_server_path_info = lambda fp, it: self._rpc_server_path(fp, it)
            fctx.request_mirror_from_peer = lambda pid, folder: self._request_mirror(pid, folder)
            fctx.upload_file_to_server = lambda fp, rel: self._send_file_to_server(fp, rel)
            fctx.version_list = lambda fp: self._rpc_version_list(fp)
            fctx.version_op = lambda op, fp, ver='latest', reason='flow': self._rpc_version_op(op, fp, ver, reason)
            fctx.secret_resolver = lambda name: (CONFIG.get(name) or '')
            fctx.allow_command = bool(CONFIG.get('allow_command_block', False))
            fctx.folder_resolver = self._folder_abs_path

            fctx = engine.execute(fctx)

            for log_msg in fctx.logs:
                mod = 'flow_engine'
                if '扫描' in log_msg: mod = 'scanner'
                elif '哈希' in log_msg: mod = 'hash_checker'
                elif '服务端' in log_msg: mod = 'backup_sync'
                elif '同伴' in log_msg: mod = 'peer_sync'
                elif 'HTTP' in log_msg or '请求' in log_msg: mod = 'http_client'
                logger.info(mod, log_msg)

            add_event('flow_executed', {
                'flow': flow_name, 'trigger': event.type.value,
                'relpath': event.relpath, 'steps': len(fctx.logs),
                'logs': fctx.logs, 'folder': event.folder_name, 'log_file': '',
            })

        except Exception as e:
            logger.error('flow_bridge', f'流程执行失败: {e}')

        return None

    # ── 触发器：轮询 / 定时(cron) / 手动 / 文件变更 ──
    def _load_flow_def(self, flow_name):
        if HAS_FLOW_ENGINE and FLOW_TEMPLATES.get(flow_name):
            return FLOW_TEMPLATES[flow_name]
        fn = flow_name.replace('/', '_').replace('\\', '_')
        fp = os.path.join(FLOWS_DIR, f"{fn}.json")
        if not os.path.isfile(fp):
            return None
        try:
            with open(fp, 'r', encoding='utf-8-sig') as f:
                return json.load(f)
        except Exception:
            return None

    def _folder_abs_path(self, folder_name):
        for fc in CONFIG.get('sync_folders', []):
            if fc.get('name') == folder_name:
                return os.path.abspath(fc['path'])
        return ''

    def _reload_triggers(self):
        """从 CONFIG.folder_triggers 收集按备份目录配置的触发器"""
        with self._triggers_lock:
            self._triggers = []
            ft = CONFIG.get('folder_triggers') or {}
            for folder, trigs in ft.items():
                for i, t in enumerate(trigs or []):
                    if not isinstance(t, dict):
                        continue
                    nt = dict(t)
                    nt['folder'] = folder
                    nt['_idx'] = i
                    if not nt.get('flow'):
                        sg = CONFIG.get('sync_groups', {}).get(folder, {})
                        nt['flow'] = sg.get('active_flow') or ''
                    nt['_last'] = 0
                    if nt.get('flow'):
                        self._triggers.append(nt)

    def _run_flow_by_name(self, flow_name, trigger_type, folder='', relpath='', logger=None):
        """按名称执行流程（供触发器/手动使用），返回 (ok, msg)"""
        if not HAS_FLOW_ENGINE:
            return False, '流程引擎未加载'
        flow_def = self._load_flow_def(flow_name)
        if not flow_def:
            return False, f'流程不存在: {flow_name}'
        log = logger or PipelineLogger(LOGS_DIR, flow_name)
        now = time.time()
        try:
            engine = FlowEngine(flow_def)
            full = ''
            if relpath:
                root = self._folder_abs_path(folder) if folder else ''
                full = os.path.join(root, relpath) if root else relpath
            fctx = FlowContext(
                event_type=trigger_type, source_device=self.cfg.get('client_id', '?'),
                file_path=relpath, relpath=relpath, full_path=full, folder=folder, timestamp=now,
                client_dir=BASE_DIR, flows_dir=FLOWS_DIR, logs_dir=LOGS_DIR,
                backup_key_file=CONFIG.get('backup_key_file', ''),
                vault_dir=CONFIG.get('vault_dir', ''),
            )
            fctx.request_peer_hash = lambda pid, fp: self._rpc_peer_hash(pid, fp)
            fctx.request_server_backup_hash = lambda fp: self._rpc_server_hash(fp)
            fctx.request_peer_path_info = lambda pid, fp, it: self._rpc_peer_path(pid, fp, it)
            fctx.request_server_path_info = lambda fp, it: self._rpc_server_path(fp, it)
            fctx.request_mirror_from_peer = lambda pid, folder: self._request_mirror(pid, folder)
            fctx.upload_file_to_server = lambda fp, rel: self._send_file_to_server(fp, rel)
            fctx.version_list = lambda fp: self._rpc_version_list(fp)
            fctx.version_op = lambda op, fp, ver='latest', reason='flow': self._rpc_version_op(op, fp, ver, reason)
            fctx.secret_resolver = lambda name: (CONFIG.get(name) or '')
            fctx.allow_command = bool(CONFIG.get('allow_command_block', False))
            fctx.folder_resolver = self._folder_abs_path
            fctx = engine.execute(fctx)
            for m in fctx.logs:
                mod = 'flow_engine'
                if '扫描' in m: mod = 'scanner'
                elif '哈希' in m: mod = 'hash_checker'
                elif '服务端' in m: mod = 'backup_sync'
                elif '同伴' in m: mod = 'peer_sync'
                elif 'HTTP' in m or '请求' in m: mod = 'http_client'
                log.info(mod, m)
            add_event('flow_executed', {
                'flow': flow_name, 'trigger': trigger_type,
                'relpath': relpath, 'steps': len(fctx.logs),
                'logs': fctx.logs, 'folder': folder,
                # 修复：原先恒为空串，主页的“查看日志”入口永远打不开
                'log_file': os.path.basename(getattr(log, '_current_file', '') or ''),
            })
            return True, 'ok'
        except Exception as e:
            log.error('trigger', f'流程执行失败: {e}')
            return False, str(e)

    def run_flow_manual(self, name, folder=''):
        return self._run_flow_by_name(name, 'manual', folder=folder)

    def manual_triggers(self):
        """主页手动触发按钮列表：folder_triggers 中 type=manual 的条目"""
        out = []
        ft = CONFIG.get('folder_triggers') or {}
        for folder, trigs in ft.items():
            for i, t in enumerate(trigs or []):
                if isinstance(t, dict) and t.get('type') == 'manual':
                    out.append({'folder': folder, 'idx': i,
                                'label': t.get('label') or t.get('flow') or '手动',
                                'flow': t.get('flow') or CONFIG.get('sync_groups', {}).get(folder, {}).get('active_flow', '')})
        return out

    def save_folder_triggers(self, data):
        """保存 folder_triggers 配置（dict: folder -> [trigger...]）"""
        if not isinstance(data, dict):
            return False, '配置格式错误'
        clean = {}
        for folder, trigs in data.items():
            items = []
            for t in trigs or []:
                if not isinstance(t, dict):
                    continue
                n = {k: v for k, v in t.items() if k in ('type', 'interval_sec', 'cron', 'pattern', 'label', 'flow')}
                if n.get('type'):
                    items.append(n)
            if items:
                clean[folder] = items
        CONFIG['folder_triggers'] = clean
        save_config(CONFIG)
        self._reload_triggers()
        return True, 'ok'

    @staticmethod
    def _match_cron(cron, ts):
        parts = (cron or '* * * * *').split()
        if len(parts) != 5:
            return False
        dt = datetime.fromtimestamp(ts)
        fields = [dt.minute, dt.hour, dt.day, dt.month, dt.isoweekday()]
        for pat, val in zip(parts, fields):
            if pat == '*':
                continue
            if '-' in pat:
                a, b = pat.split('-')
                try:
                    if not (int(a) <= val <= int(b)):
                        return False
                except ValueError:
                    return False
            else:
                try:
                    if val != int(pat):
                        return False
                except ValueError:
                    return False
        return True

    def _trigger_tick(self):
        now = time.time()
        with self._triggers_lock:
            trigs = list(self._triggers)
        for t in trigs:
            try:
                tt = t.get('type')
                if tt == 'poll':
                    iv = int(t.get('interval_sec') or 60)
                    if now - t.get('_last', 0) >= iv:
                        t['_last'] = now
                        self._run_flow_by_name(t['flow'], 'poll', folder=t.get('folder', ''))
                elif tt == 'schedule':
                    if self._match_cron(t.get('cron', '* * * * *'), now) and now - t.get('_last', 0) >= 55:
                        t['_last'] = now
                        self._run_flow_by_name(t['flow'], 'schedule', folder=t.get('folder', ''))
            except Exception as e:
                cprint(C.R, f"  ❌ 触发器异常 [{t.get('flow')}]: {e}")

    def _trigger_loop(self):
        while self.running:
            time.sleep(1)
            try:
                self._trigger_tick()
            except Exception:
                pass

    def _disconnect(self):
        global _conn
        with state_lock:
            state['connected'] = False; state['peers'] = []
        with conn_lock: _conn = None
        self.peers = []

    def _ignore(self, rp):
        rp_norm = rp.replace('\\', '/')
        parts = rp_norm.split('/')
        for p in self.cfg.get('ignore_patterns', []):
            pat = p.replace('\\', '/').rstrip('/')
            if fnmatch.fnmatch(rp_norm, pat) or fnmatch.fnmatch(os.path.basename(rp_norm), pat):
                return True
            if any(fnmatch.fnmatch(part, pat) for part in parts[:-1]):
                return True
        return False

    def _pause_handlers(self):
        self._paused = True
        for h in self._handlers: h.pause()

    def _resume_handlers(self):
        self._paused = False
        for h in self._handlers: h.resume()

    def _on_create(self, rp):
        if self._paused: return
        fc = self._find_folder(rp)
        if not fc: return
        event = PipelineEvent(
            type=EventType.FILE_CREATED, folder_name=fc['name'],
            relpath=rp, source_device=self.cfg.get('client_id', '?'), target_device='',
        )
        if self._outbound_pipeline:
            self._outbound_pipeline.run(event, self._pipeline_ctx or PipelineContext())

    def _on_modify(self, rp):
        fc = self._find_folder(rp)
        if not fc: return
        event = PipelineEvent(
            type=EventType.FILE_MODIFIED, folder_name=fc['name'],
            relpath=rp, source_device=self.cfg.get('client_id', '?'), target_device='',
        )
        if self._outbound_pipeline:
            self._outbound_pipeline.run(event, self._pipeline_ctx or PipelineContext())

    def _on_delete(self, rp):
        if self._paused: return
        fc = self._find_folder(rp)
        if not fc: return
        full = os.path.join(os.path.abspath(fc['path']), rp)
        if os.path.exists(full): return
        event = PipelineEvent(
            type=EventType.FILE_DELETED, folder_name=fc['name'],
            relpath=rp, source_device=self.cfg.get('client_id', '?'), target_device='',
        )
        if self._outbound_pipeline:
            self._outbound_pipeline.run(event, self._pipeline_ctx or PipelineContext())

    def _on_rename(self, src, dst):
        if self._paused: return
        fc = self._find_folder(src) or self._find_folder(dst)
        if not fc: return
        event = PipelineEvent(
            type=EventType.FILE_RENAMED, folder_name=fc['name'],
            src_relpath=src, dst_relpath=dst,
            source_device=self.cfg.get('client_id', '?'), target_device='',
        )
        if self._outbound_pipeline:
            self._outbound_pipeline.run(event, self._pipeline_ctx or PipelineContext())

    def connect(self):
        global _conn
        s, p = self.cfg['server_host'], self.cfg['server_port']
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(10)
        sock.connect((s, p))
        _tls = self.cfg.get('tls') or {}
        if _tls.get('enabled', True):
            if not tls_util.crypto_available():
                raise Exception('已启用 TLS 但缺少 cryptography：pip install cryptography '
                                '（或在 client/config.json 里显式设 "tls": {"enabled": false}）')
            try:
                sock = tls_util.wrap_client(sock, tls_util.client_context(),
                                            _tls.get('server_hostname') or s)
            except Exception as e:
                raise Exception(f'TLS 握手失败（服务端可能没开 TLS）: {e}')
            fp = tls_util.peer_fingerprint(sock)
            pinned = tls_util.normalize_fingerprint(_tls.get('fingerprint'))
            if pinned and pinned != fp:
                with state_lock:
                    state['tls_error'] = f'证书指纹不匹配：固定 {pinned} ≠ 实际 {fp}'
                raise Exception('⛔ 服务端证书指纹与固定值不一致，已拒绝连接（可能是中间人；'
                                '若确实换了证书，请清空 client/config.json 里的 tls.fingerprint 后重连）')
            if not pinned:
                cprint(C.C, f'  🔐 TLS 已连接 · 服务端证书指纹 {fp[:23]}…（已固定到 config.json）')
                _save_tls_fingerprint(fp)
            with state_lock:
                state['tls'] = True
                state['tls_fingerprint'] = fp
        c = Connection(sock)
        c.send(MsgType.HELLO, json.dumps({
            'client_id': self.cfg.get('client_id', socket.gethostname()),
            'group_id': self.cfg.get('group_id', 'default'),
            'group_key': self.cfg.get('group_key', ''),
            'sync_folders': self.cfg.get('sync_folders', [])
        }).encode())
        r = c.recv(timeout=15)
        if not r or r[0] != MsgType.HELLO_ACK:
            raise Exception("握手失败")
        ack = json.loads(r[2].decode())
        if ack.get('status') == 'rejected':
            reason = ack.get('reason', '未知原因')
            cprint(C.Y, f'  ⚠ 服务端拒绝入组: {reason}')
            raise Exception(f"服务端拒绝入组: {reason}")
        with conn_lock: _conn = c
        self.peers = ack.get('peers_online', [])
        self._build_pipelines()
        with state_lock:
            state['connected'] = True
            state['server'] = ack.get('server', 'Server')
            state['peers'] = list(self.peers)
        cprint(C.G, f"  ✅ 已连接: {ack.get('server')}")
        cprint(C.C, f"  同伴: {len(self.peers)} 在线")
        add_event('connected', {'server': ack.get('server'), 'peers': self.peers})
        return ack

    def _recv_file_handler(self, meta):
        rp = meta['relpath']; rsha = meta.get('sha256', '')
        fc = self._find_folder(rp)
        if not fc: return
        event = PipelineEvent(
            type=EventType.FILE_RECEIVING, folder_name=fc['name'],
            relpath=rp, file_size=meta.get('size', 0), remote_sha256=rsha,
            mtime=meta.get('mtime', 0),
            source_device='remote', target_device=self.cfg.get('client_id', '?'),
        )
        if self._inbound_pipeline:
            self._inbound_pipeline.run(event, self._pipeline_ctx)

    def full_sync(self, folder_name=None):
        ignore = self.cfg.get('ignore_patterns', [])
        for fc in self.cfg.get('sync_folders', []):
            if folder_name and fc['name'] != folder_name: continue
            if not self._folder_enabled(fc['name']): continue
            lp = os.path.abspath(fc['path']); ensure_dir(lp)
            snap = scan_folder(lp, ignore)
            c = self._get_conn()
            if not c: return
            c.send(MsgType.SNAPSHOT_SEND, json.dumps({
                'client_id': self.cfg['client_id'],
                'folder': fc['name'], 'snapshot': snap
            }).encode())
        cprint(C.G, "  ✅ 快照已发送")
        add_event('snapshot_sent', {})

    def reconcile_backup(self, max_files=None):
        """备份对账：把本地缺少/变更的文件补传到服务端备份目录。

        为什么需要：watchdog 事件驱动本身会漏 —— 启动瞬间、断线重连期间、
        以及任何"监听尚未就绪"的窗口内发生的文件变化，既不在快照内容里也没有事件，
        此前会永久漏掉（实机 2/3 概率复现）。这里在每次连上之后做一次兜底对账，
        把"永久丢失"变成"连上即自愈"，同时也替代了人工跑 seed_push 的常规场景。

        注意：必须在 run_loop 之前（或暂停监听时）调用，避免与其他线程共用同一条
        TCP 连接的读写。
        """
        c = self._get_conn()
        if not c:
            return 0, 0
        if max_files is None:
            max_files = int(self.cfg.get('max_reconcile_files', 2000) or 2000)
        ignore = self.cfg.get('ignore_patterns', [])
        checked = uploaded = 0
        log = PipelineLogger(LOGS_DIR, 'reconcile')
        for fc in self.cfg.get('sync_folders', []):
            if checked >= max_files:
                break
            if not self._folder_enabled(fc.get('name', '')):
                continue
            lp = os.path.abspath(fc.get('path', ''))
            if not os.path.isdir(lp):
                continue
            try:
                snap = scan_folder(lp, ignore)
            except Exception as e:
                log.warning('reconcile', f'扫描失败: {lp} · {e}')
                continue
            for rp in sorted(snap):
                if checked >= max_files:
                    break
                checked += 1
                local_sha = snap[rp].get('sha256') or ''
                if not local_sha:
                    continue
                try:
                    remote_sha = self._rpc_server_hash(rp) or ''
                except Exception:
                    remote_sha = ''
                if local_sha == remote_sha:
                    continue
                ok, msg = self._send_file_to_server(os.path.join(lp, rp), rp)
                if ok:
                    uploaded += 1
                    log.info('reconcile', f'补传: {rp} · {snap[rp].get("size", 0)} 字节')
                else:
                    log.warning('reconcile', f'补传失败: {rp} · {msg}')
        with state_lock:
            state['reconcile'] = {'checked': checked, 'uploaded': uploaded, 'at': int(time.time())}
        if checked:
            log.info('reconcile', f'备份对账完成: 检查 {checked} 个文件，补传 {uploaded} 个')
            add_event('reconcile_done', {'checked': checked, 'uploaded': uploaded})
        return checked, uploaded

    def _request_mirror(self, peer_id, folder=''):
        """手动镜像：请求同伴与我互相重发快照，实现目录双向镜像"""
        c = self._get_conn()
        if not c:
            return False
        try:
            c.send(MsgType.SNAPSHOT_REQUEST, json.dumps({
                'target_peer': peer_id, 'folder': folder,
                'from': self.cfg.get('client_id', '?')
            }).encode())
        except Exception:
            return False
        threading.Thread(target=lambda: (time.sleep(1), self.full_sync(folder or None)),
                         daemon=True).start()
        cprint(C.Y, f"  🔁 手动镜像请求已发给同伴: {peer_id} folder={folder or '(全部)'}")
        return True

    def start_watching(self):
        if getattr(self, 'obs', None):   # 幂等：重连时不要重复 schedule/start
            return
        self.obs = Observer(); watched = set()
        for fc in self.cfg.get('sync_folders', []):
            lp = os.path.abspath(fc['path']); ensure_dir(lp)
            if lp not in watched:
                h = SyncEventHandler(self._on_create, self._on_modify,
                    self._on_delete, self._on_rename, self._ignore, lp)
                self.obs.schedule(h, lp, recursive=True)
                watched.add(lp); self._handlers.append(h)
        self.obs.start()
        with state_lock:
            state['watching'] = True

    def run_loop(self):
        global _conn, running
        self.start_watching()
        iv = self.cfg.get('heartbeat_interval', 10)
        while self.running and running:
            c = self._get_conn()
            if not c: break
            try: r = c.recv(timeout=iv)
            except (ConnectionError, OSError):
                cprint(C.Y, "\n  ⚠ 连接断开"); self._disconnect(); break
            if r is None:
                try: c.send(MsgType.PING)
                except: self._disconnect(); break
                continue
            mt, _, pl = r
            self._pause_handlers()
            try:
                if mt == MsgType.PING: c.send(MsgType.PONG)
                elif mt == MsgType.HELLO: self._h_hello(pl)
                elif mt == MsgType.FILE_CREATE: self._recv_file_handler(json.loads(pl.decode()))
                elif mt in (MsgType.VERSION_LIST_RESPONSE, MsgType.VERSION_OP_RESPONSE,
                            MsgType.FILE_HASH_RESPONSE, MsgType.SERVER_BACKUP_HASH_RESPONSE,
                            MsgType.PATH_INFO_RESPONSE):
                    # RPC 响应可能先被本线程读到：必须交回等待方，否则它会一直等到超时
                    try:
                        dd = json.loads(pl.decode('utf-8', 'replace'))
                        rid = dd.get('request_id', '')
                        if mt in (MsgType.VERSION_LIST_RESPONSE, MsgType.VERSION_OP_RESPONSE):
                            self._rpc_response(rid, dd)
                        else:
                            self._rpc_response(rid, dd.get('sha256', dd.get('value', '')))
                    except Exception:
                        pass
                elif mt == MsgType.FILE_DATA: pass
                elif mt == MsgType.FILE_DELETE:
                    rp = pl.decode(); fc = self._find_folder(rp)
                    if fc:
                        event = PipelineEvent(type=EventType.FILE_DELETED, folder_name=fc['name'],
                                              relpath=rp, source_device='remote',
                                              target_device=self.cfg.get('client_id', '?'))
                        if self._inbound_pipeline: self._inbound_pipeline.run(event, self._pipeline_ctx)
                elif mt == MsgType.FILE_RENAME:
                    parts = pl.split(b'\x00')
                    if len(parts) >= 2:
                        src, dst = parts[0].decode(), parts[1].decode()
                        fc = self._find_folder(src)
                        if fc:
                            event = PipelineEvent(type=EventType.FILE_RENAMED, folder_name=fc['name'],
                                                  src_relpath=src, dst_relpath=dst,
                                                  source_device='remote',
                                                  target_device=self.cfg.get('client_id', '?'))
                            if self._inbound_pipeline: self._inbound_pipeline.run(event, self._pipeline_ctx)
                elif mt == MsgType.FILE_HASH_REQUEST: self._h_hreq(pl)
                elif mt == MsgType.SNAPSHOT_REQUEST:
                    req = json.loads(pl.decode())
                    tp = req.get('target_peer', '')
                    if tp in ('', self.cfg.get('client_id', '')):
                        cprint(C.C, f"  🔁 同伴请求镜像: {req.get('from','?')} folder={req.get('folder') or '(全部)'}")
                        self.full_sync(req.get('folder') or None)
                elif mt == MsgType.FILE_HASH_RESPONSE:
                    d = json.loads(pl.decode()); self._rpc_response(d.get('request_id',''), d.get('sha256',''))
                elif mt == MsgType.SERVER_BACKUP_HASH_REQUEST: self._h_shreq(pl)
                elif mt == MsgType.SERVER_BACKUP_HASH_RESPONSE:
                    d = json.loads(pl.decode()); self._rpc_response(d.get('request_id',''), d.get('sha256',''))
                elif mt == MsgType.PATH_INFO_REQUEST: self._h_pireq(pl)
                elif mt == MsgType.PATH_INFO_RESPONSE:
                    d = json.loads(pl.decode()); self._rpc_response(d.get('request_id',''), d.get('value',''))
                elif mt == MsgType.FLOW_SHARE_RESPONSE:
                    self._rpc_response(json.loads(pl.decode()).get('request_id',''),
                                       json.dumps(json.loads(pl.decode())))
                elif mt == MsgType.FLOW_SHARE_DOWNLOAD_RESPONSE:
                    self._rpc_response(json.loads(pl.decode()).get('request_id',''),
                                       json.dumps(json.loads(pl.decode())))
                elif mt == MsgType.BYE:
                    cprint(C.Y, "\n  👋 服务端断开"); self._disconnect(); break
            finally: self._resume_handlers()
        self._disconnect()

    def _h_hello(self, pl):
        h = json.loads(pl.decode()); evt = h.get('event','')
        if evt == 'peer_online':
            if h['peer_id'] not in self.peers: self.peers.append(h['peer_id'])
            with state_lock: state['peers'] = list(self.peers)
            add_event('peer_online', {'peer': h['peer_id']})
        elif evt == 'peer_offline':
            if h['peer_id'] in self.peers: self.peers.remove(h['peer_id'])
            with state_lock: state['peers'] = list(self.peers)
            add_event('peer_offline', {'peer': h['peer_id']})

    def _h_hreq(self, pl):
        req = json.loads(pl.decode())
        if req.get('target_peer','') not in ('', self.cfg.get('client_id','')): return
        fc = self._find_folder(req.get('file_path','')); sha = ''
        if fc:
            full = self._safe_full(fc['path'], req.get('file_path',''))
            sha = compute_sha256(full) if full and os.path.isfile(full) else ''
        c = self._get_conn()
        if c: c.send(MsgType.FILE_HASH_RESPONSE,
            json.dumps({'request_id': req.get('request_id',''), 'sha256': sha}).encode())

    def _h_shreq(self, pl):
        req = json.loads(pl.decode()); fc = self._find_folder(req.get('file_path','')); sha = ''
        if fc:
            full = self._safe_full(fc['path'], req.get('file_path',''))
            sha = compute_sha256(full) if full and os.path.isfile(full) else ''
        c = self._get_conn()
        if c: c.send(MsgType.SERVER_BACKUP_HASH_RESPONSE,
            json.dumps({'request_id': req.get('request_id',''), 'sha256': sha,
                        'file_path': req.get('file_path','')}).encode())

    def _h_pireq(self, pl):
        req = json.loads(pl.decode()); target = req.get('target_peer','')
        cid = self.cfg.get('client_id',''); is_srv = req.get('source') == 'server'
        if target not in ('', cid) and not is_srv: return
        fc = self._find_folder(req.get('file_path','')); val = ''
        if fc:
            full = self._safe_full(fc['path'], req.get('file_path',''))
            val = self._path_info(full, req.get('info_type','exists')) if full else ''
        c = self._get_conn()
        if c: c.send(MsgType.PATH_INFO_RESPONSE,
            json.dumps({'request_id': req.get('request_id',''), 'value': val,
                        'info_type': req.get('info_type','')}).encode())

    @staticmethod
    def _path_info(fp, it):
        try:
            if it == 'exists': return 'true' if os.path.exists(fp) else 'false'
            elif it == 'size':
                if os.path.isfile(fp): return str(os.path.getsize(fp))
                elif os.path.isdir(fp):
                    return str(sum(os.path.getsize(os.path.join(dp,fn))
                                   for dp,_,fns in os.walk(fp) for fn in fns))
                return '0'
            elif it == 'mtime': return str(int(os.path.getmtime(fp))) if os.path.exists(fp) else '0'
            elif it == 'hash': return compute_sha256(fp) or '' if os.path.exists(fp) else ''
            elif it == 'count': return str(len(os.listdir(fp))) if os.path.isdir(fp) else '0'
            elif it == 'is_file': return 'true' if os.path.isfile(fp) else 'false'
            elif it == 'is_dir': return 'true' if os.path.isdir(fp) else 'false'
        except: pass
        return ''

    def _rpc_send_and_wait(self, msg_type, payload_dict, timeout=10):
        rid = uuid.uuid4().hex[:12]
        result = [None]; evt = threading.Event()
        with self._hash_requests_lock:
            self._hash_requests[rid] = (evt, result, time.time())
        c = None
        try:
            c = self._get_conn()
            if not c: return None
            payload_dict['request_id'] = rid
            c.send(msg_type, json.dumps(payload_dict).encode())
            # 在等待期间轮询 socket 并就地分发 RPC 响应，
            # 避免在 run_loop 线程上 evt.wait 导致响应永远读不到（死锁超时）
            deadline = time.time() + timeout
            while not evt.is_set() and time.time() < deadline:
                evt.wait(0.05)
                if evt.is_set(): break
                r = None
                try:
                    r = c.recv(timeout=0.5)
                except (ConnectionError, OSError, socket.timeout):
                    break
                if r is None:
                    continue
                mt, _, pl = r
                try:
                    if mt in (MsgType.FILE_HASH_RESPONSE, MsgType.SERVER_BACKUP_HASH_RESPONSE, MsgType.PATH_INFO_RESPONSE):
                        dd = json.loads(pl.decode('utf-8', 'replace'))
                        self._rpc_response(dd.get('request_id', ''), dd.get('sha256', dd.get('value', '')))
                    elif mt in (MsgType.VERSION_LIST_RESPONSE, MsgType.VERSION_OP_RESPONSE):
                        # 版本类 RPC 返回的是整包字典（列表/回滚结果/取回的版本内容）
                        dd = json.loads(pl.decode('utf-8', 'replace'))
                        self._rpc_response(dd.get('request_id', ''), dd)
                    elif mt == MsgType.PING:
                        c.send(MsgType.PONG)
                except Exception:
                    pass
            return result[0]
        except Exception:
            return None
        finally:
            with self._hash_requests_lock: self._hash_requests.pop(rid, None)

    def _rpc_response(self, rid, value):
        with self._hash_requests_lock:
            entry = self._hash_requests.get(rid)
        if entry: entry[0].set(); entry[1][0] = value

    def _rpc_peer_hash(self, pid, fp):
        return self._rpc_send_and_wait(MsgType.FILE_HASH_REQUEST,
            {'target_peer': pid, 'file_path': fp})
    def _rpc_server_hash(self, fp):
        return self._rpc_send_and_wait(MsgType.SERVER_BACKUP_HASH_REQUEST, {'file_path': fp})

    def _rpc_version_list(self, fp, timeout=15):
        """列出某文件在服务端的历史版本；返回 dict 或 None"""
        return self._rpc_send_and_wait(MsgType.VERSION_LIST_REQUEST, {'file_path': fp}, timeout=timeout)

    def _rpc_version_op(self, op, fp, version='latest', reason='flow', timeout=60):
        """版本操作：snapshot（打版本点）/ restore（回滚）/ fetch（取回内容）"""
        return self._rpc_send_and_wait(MsgType.VERSION_OP_REQUEST,
                                       {'op': op, 'file_path': fp, 'version': version, 'reason': reason},
                                       timeout=timeout)

    def _send_file_to_server(self, full_path, remote_relpath=''):
        """把本地文件真正推送到服务端备份目录（流程引擎 upload_to_server 积木用）。

        返回 (ok, msg)。服务端收到 FILE_CREATE/FILE_DATA 后按 remote_relpath 落盘，
        并按源文件 mtime 去重，因此重复执行不会破坏已有备份。
        """
        c = self._get_conn()
        if not c:
            return False, '未连接服务端'
        fp = full_path or ''
        if not os.path.isfile(fp):
            return False, '文件不存在: ' + (fp or '?')
        rel = (remote_relpath or os.path.basename(fp)).replace('\\', '/').lstrip('/')
        try:
            ok = FileTransfer.send_file(c, os.path.dirname(fp) or '.',
                                        os.path.basename(fp), remote_relpath=rel)
            return (True, 'relpath=' + rel) if ok else (False, '发送失败')
        except Exception as e:
            return False, str(e)
    def _rpc_peer_path(self, pid, fp, it):
        return self._rpc_send_and_wait(MsgType.PATH_INFO_REQUEST,
            {'target_peer': pid, 'file_path': fp, 'info_type': it})
    def _rpc_server_path(self, fp, it):
        return self._rpc_send_and_wait(MsgType.PATH_INFO_REQUEST,
            {'target_peer': '', 'file_path': fp, 'info_type': it, 'source': 'server'})

    def share_flow_to_server(self, flow_dict):
        r = self._rpc_send_and_wait(MsgType.FLOW_SHARE_UPLOAD, {'flow': flow_dict})
        if r is None: return None, '未连接或超时'
        try:
            resp = json.loads(r) if isinstance(r, str) else r
            return resp.get('share_code'), None
        except: return None, '响应解析失败'

    def install_flow_from_server(self, code):
        r = self._rpc_send_and_wait(MsgType.FLOW_SHARE_DOWNLOAD,
            {'share_code': code.strip().lower()})
        if r is None: return None, '未连接或超时'
        try:
            resp = json.loads(r) if isinstance(r, str) else r
            return (resp.get('flow'), None) if resp.get('found') else (None, '方案码不存在或已过期')
        except: return None, '响应解析失败'

    def stop(self):
        self.running = False
        if self.obs: self.obs.stop(); self.obs.join()
        c = self._get_conn()
        if c:
            try: c.send(MsgType.BYE)
            except: pass
            c.close()
        self._disconnect()


client = MirrorClient()

# ============================================================
# 工坊代理（工坊为全局共享库，只存服务端，客户端只做转发）
# ============================================================
def _server_api_base():
    """计算服务端 Web API 的基地址（工坊数据存服务端）"""
    tunnel = CONFIG.get('ssh_tunnel') or {}
    if tunnel.get('enabled'):
        return f"http://127.0.0.1:{tunnel.get('web_local_port', 8086)}"
    return f"http://{CONFIG.get('server_host', '127.0.0.1')}:{CONFIG.get('server_web_port', 8086)}"


def _proxy_to_server(endpoint, method='GET', body=None, timeout=8):
    """代理请求到服务端工坊 API（账户登录后自动附带令牌）"""
    url = f"{_server_api_base()}{endpoint}"
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib_req.Request(url, data=data, method=method)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            token = session.get('token') or ''
        except Exception:
            token = ''
        if token:
            req.add_header('X-Auth-Token', token)
        with urllib_req.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception as e:
        return {'status': 'error', 'msg': f'服务端不可达: {e}'}


# ============================================================
# Web API
# ============================================================
@app.route('/')
def index():
    return render_template('index.html', cfg=CONFIG)

@app.route('/editor')
def flow_editor():
    return render_template('flow_editor.html')

@app.route('/blocks')
def block_editor():
    return render_template('blocks.html')

@app.route('/flows')
def flow_manage():
    return render_template('flows.html')

@app.route('/workshop')
def workshop_page():
    return render_template('workshop.html')

@app.route('/files')
def file_manager():
    return render_template('explorer.html', cfg=CONFIG)

@app.route('/favicon.ico')
def favicon():
    # 浏览器默认会来要 /favicon.ico，直接给静态文件
    return redirect('/static/favicon.ico')


@app.route('/api/i18n')
def api_i18n():
    """语言包接口：面板前端与第三方都可以用它做本地化"""
    lang = i18n.negotiate(request.headers.get('Accept-Language'),
                          request.args.get('lang') or CONFIG.get('language') or '')
    return jsonify(dict(status='ok', **i18n.table(lang)))


@app.route('/api/status')
def api_status():
    with state_lock:
        s = dict(state); s['client_id'] = CONFIG.get('client_id', socket.gethostname())
        s['events'] = s['events'][-50:]
    return jsonify(s)

@app.route('/api/sync/full', methods=['POST'])
def api_sync_full():
    threading.Thread(target=client.full_sync, daemon=True).start()
    return jsonify({'status': 'ok'})

@app.route('/api/folders')
def api_folders():
    folders = []
    for fc in CONFIG.get('sync_folders', []):
        p = os.path.abspath(fc['path'])
        snap = scan_folder(p, CONFIG.get('ignore_patterns', []), need_hash=False)
        folders.append({
            'name': fc['name'], 'path': p, 'file_count': len(snap),
            'files': [{'relpath': rp, 'size': i['size'], 'mtime': i['mtime']}
                      for rp, i in list(snap.items())[:200]]
        })
    return jsonify(folders)

@app.route('/api/file/read', methods=['POST'])
def api_file_read():
    d = request.json
    fc = next((f for f in CONFIG.get('sync_folders', []) if f.get('name') == d.get('folder','')), None)
    if not fc: return jsonify({'status': 'error', 'msg': '文件夹未找到'}), 404
    base = os.path.abspath(fc['path'])
    fp = os.path.abspath(os.path.join(base, d.get('relpath','')))
    # 路径穿越防护：必须在同步目录内
    if os.path.commonpath([fp, base]) != base:
        return jsonify({'status': 'error', 'msg': '非法路径'}), 403
    if not os.path.isfile(fp): return jsonify({'status': 'error', 'msg': '文件不存在'}), 404
    ext = os.path.splitext(d['relpath'])[1].lower()
    text_exts = {'.txt','.md','.json','.py','.html','.css','.js','.log','.cfg','.ini','.yaml','.yml',
                 '.xml','.csv','.sh','.bat','.ps1','.toml','.env','.tex','.c','.cpp','.h','.rs',
                 '.go','.java','.kt','.swift'}
    image_exts = {'.png','.jpg','.jpeg','.gif','.webp','.svg','.bmp','.ico'}
    try:
        if ext in text_exts:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                return jsonify({'status': 'ok', 'type': 'text', 'content': f.read()[:50000],
                                'size': os.path.getsize(fp), 'ext': ext})
        elif ext in image_exts:
            import base64
            mm = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif',
                  '.webp':'image/webp','.svg':'image/svg+xml','.bmp':'image/bmp','.ico':'image/x-icon'}
            with open(fp, 'rb') as f: img = f.read()[:5000000]
            return jsonify({'status': 'ok', 'type': 'image',
                            'content': base64.b64encode(img).decode(),
                            'mime': mm.get(ext, 'image/png'), 'size': len(img)})
        else:
            return jsonify({'status': 'ok', 'type': 'binary', 'size': os.path.getsize(fp),
                            'ext': ext, 'sha256': compute_sha256(fp) or ''})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/reconnect', methods=['POST'])
def api_reconnect():
    global _conn
    with conn_lock:
        c = _conn
        if c:
            try: c.send(MsgType.BYE)
            except: pass
            c.close(); _conn = None
    with state_lock: state['connected'] = False
    return jsonify({'status': 'ok'})

@app.route('/api/sync-groups', methods=['GET'])
def api_get_sync_groups():
    return jsonify(CONFIG.get('sync_groups', {}))

@app.route('/api/sync-groups', methods=['POST'])
def api_set_sync_groups():
    data = request.json
    if not data: return jsonify({'status': 'error', 'msg': '无效数据'}), 400
    old = dict(CONFIG.get('sync_groups', {}))
    CONFIG['sync_groups'] = data
    try:
        save_config(CONFIG)
        with state_lock: state['sync_groups'] = data
        now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
        for gid, gc in data.items():
            o = old.get(gid, {})
            if o.get('enabled', True) != gc.get('enabled', True):
                add_event('sync_group_toggle', {
                    'group': gid, 'enabled': gc.get('enabled', True),
                    'action': f'{now_ts} [sync_group] INFO - 同步组 [{gid}] 状态变更: '
                              f'{"✅ 启用" if gc.get("enabled", True) else "⏸ 暂停"}',
                    'detail': '启用' if gc.get('enabled', True) else '暂停'
                })
            if o.get('active_flow', '') != gc.get('active_flow', ''):
                add_event('sync_group_flow', {
                    'group': gid, 'flow': gc.get('active_flow', ''),
                    'old_flow': o.get('active_flow', ''),
                    'action': f'{now_ts} [sync_group] INFO - 同步组 [{gid}] 流程切换: '
                              f'"{o.get("active_flow","") or "无"}" → "{gc.get("active_flow","") or "无"}"',
                    'detail': f'{o.get("active_flow","") or "无"} → {gc.get("active_flow","") or "无"}'
                })
        for gid in old:
            if gid not in data:
                add_event('sync_group_toggle', {
                    'group': gid, 'enabled': False,
                    'action': f'{now_ts} [sync_group] WARNING - 同步组 [{gid}] 已被移除',
                    'detail': '已移除'
                })
        client._build_pipelines()
        add_event('sync_groups_updated', {'groups': list(data.keys())})
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/config', methods=['GET'])
def api_get_config():
    c = dict(CONFIG)
    if 'ssh_tunnel' in c and 'ssh_password' in c.get('ssh_tunnel', {}):
        c['ssh_tunnel'] = dict(c['ssh_tunnel']); c['ssh_tunnel']['ssh_password'] = '***'
    return jsonify(c)

@app.route('/api/config', methods=['POST'])
def api_update_config():
    data = request.json
    if not data: return jsonify({'status': 'error', 'msg': '无效数据'}), 400
    for k in ['client_id','group_id','server_host','server_port','web_host','web_port',
              'heartbeat_interval','reconnect_base_delay','reconnect_max_delay']:
        if k in data: CONFIG[k] = data[k]
    if 'ssh_tunnel' in data:
        CONFIG.setdefault('ssh_tunnel', {})
        for k in ['enabled','ssh_host','ssh_port','ssh_user','ssh_key_path',
                  'ssh_key_passphrase','web_local_port','web_remote_port']:
            if k in data['ssh_tunnel']: CONFIG['ssh_tunnel'][k] = data['ssh_tunnel'][k]
        if data['ssh_tunnel'].get('ssh_password', '') != '***':
            CONFIG['ssh_tunnel']['ssh_password'] = data['ssh_tunnel']['ssh_password']
    if 'sync_folders' in data: CONFIG['sync_folders'] = data['sync_folders']
    if 'sync_groups' in data: CONFIG['sync_groups'] = data['sync_groups']
    if 'ignore_patterns' in data: CONFIG['ignore_patterns'] = data['ignore_patterns']
    try:
        save_config(CONFIG)
        add_event('config_updated', {'fields': list(data.keys())})
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

# ── 资源管理器 API ──
def _resolve_fs_path(folder_name, relpath=''):
    """在同步文件夹内安全解析路径，返回 (fc, abs_path) 或 (None, None)"""
    if not folder_name:
        return None, None
    for fc in CONFIG.get('sync_folders', []):
        if fc.get('name') == folder_name:
            base = os.path.abspath(fc['path'])
            full = os.path.abspath(os.path.join(base, relpath or ''))
            if os.path.commonpath([full, base]) == base:
                return fc, full
    return None, None

@app.route('/api/fs/list')
def api_fs_list():
    folder_name = request.args.get('folder', '')
    rp = request.args.get('path', '')
    fc, full = _resolve_fs_path(folder_name, rp)
    if not fc or not os.path.isdir(full):
        return jsonify({'status': 'error', 'msg': '目录不存在'}), 404
    entries = []
    try:
        for name in sorted(os.listdir(full), key=lambda n: (not os.path.isdir(os.path.join(full, n)), n.lower())):
            p = os.path.join(full, name)
            if os.path.isdir(p):
                entries.append({'name': name, 'is_dir': True, 'size': 0, 'mtime': int(os.path.getmtime(p))})
            else:
                try:
                    entries.append({'name': name, 'is_dir': False, 'size': os.path.getsize(p),
                                    'mtime': int(os.path.getmtime(p))})
                except OSError:
                    entries.append({'name': name, 'is_dir': False, 'size': 0, 'mtime': 0})
    except OSError as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500
    return jsonify({'status': 'ok', 'folder': folder_name, 'path': rp,
                    'entries': entries, 'count': len(entries)})

@app.route('/api/fs/delete', methods=['POST'])
def api_fs_delete():
    d = request.json or {}
    fc, full = _resolve_fs_path(d.get('folder', ''), d.get('path', ''))
    if not fc:
        return jsonify({'status': 'error', 'msg': '文件夹无效'}), 400
    if full == os.path.abspath(fc['path']):
        return jsonify({'status': 'error', 'msg': '不能删除根目录'}), 400
    if not os.path.exists(full):
        return jsonify({'status': 'error', 'msg': '路径不存在'}), 404
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
        add_event('fs_delete', {'folder': d.get('folder'), 'path': d.get('path')})
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/fs/rename', methods=['POST'])
def api_fs_rename():
    d = request.json or {}
    new_name = (d.get('new_name') or '').strip()
    if not new_name or '/' in new_name or '\\' in new_name:
        return jsonify({'status': 'error', 'msg': '名称无效'}), 400
    fc, full = _resolve_fs_path(d.get('folder', ''), d.get('path', ''))
    if not fc:
        return jsonify({'status': 'error', 'msg': '文件夹无效'}), 400
    if not os.path.exists(full):
        return jsonify({'status': 'error', 'msg': '路径不存在'}), 404
    try:
        os.rename(full, os.path.join(os.path.dirname(full), new_name))
        add_event('fs_rename', {'folder': d.get('folder'), 'path': d.get('path'), 'new_name': new_name})
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/fs/mkdir', methods=['POST'])
def api_fs_mkdir():
    d = request.json or {}
    name = (d.get('name') or '').strip()
    if not name or '/' in name or '\\' in name:
        return jsonify({'status': 'error', 'msg': '名称无效'}), 400
    fc, parent = _resolve_fs_path(d.get('folder', ''), d.get('path', ''))
    if not fc:
        return jsonify({'status': 'error', 'msg': '文件夹无效'}), 400
    target = os.path.join(parent, name)
    if os.path.exists(target):
        return jsonify({'status': 'error', 'msg': '已存在'}), 400
    try:
        os.makedirs(target)
        add_event('fs_mkdir', {'folder': d.get('folder'), 'path': d.get('path'), 'name': name})
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/fs/write', methods=['POST'])
def api_fs_write():
    d = request.json or {}
    fc, full = _resolve_fs_path(d.get('folder', ''), d.get('path', ''))
    if not fc:
        return jsonify({'status': 'error', 'msg': '文件夹无效'}), 400
    try:
        base = os.path.abspath(fc['path'])
        os.makedirs(os.path.dirname(full) or base, exist_ok=True)
        with open(full, 'a' if d.get('mode') == 'append' else 'w', encoding='utf-8') as f:
            f.write(d.get('content', ''))
        add_event('fs_write', {'folder': d.get('folder'), 'path': d.get('path')})
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/fs/upload', methods=['POST'])
def api_fs_upload():
    folder_name = request.form.get('folder', '')
    rp = request.form.get('path', '')
    fc, dir_full = _resolve_fs_path(folder_name, rp)
    if not fc or not os.path.isdir(dir_full):
        return jsonify({'status': 'error', 'msg': '目录无效'}), 400
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'status': 'error', 'msg': '未选择文件'}), 400
    name = os.path.basename(f.filename)
    if not name:
        return jsonify({'status': 'error', 'msg': '文件名无效'}), 400
    try:
        f.save(os.path.join(dir_full, name))
        add_event('fs_upload', {'folder': folder_name, 'path': os.path.join(rp, name)})
        return jsonify({'status': 'ok', 'name': name})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/fs/download')
def api_fs_download():
    fc, full = _resolve_fs_path(request.args.get('folder', ''), request.args.get('path', ''))
    if not fc or not os.path.isfile(full):
        return jsonify({'status': 'error', 'msg': '文件不存在'}), 404
    return send_file(full, as_attachment=True, download_name=os.path.basename(full))

# ── 流程 API ──
@app.route('/api/flow/save', methods=['POST'])
def api_flow_save():
    flow = request.json
    if not flow or not flow.get('name'): return jsonify({'status':'error','msg':'缺少流程名称'}), 400
    if HAS_FLOW_ENGINE:
        errors = validate_flow(flow)
        if errors: return jsonify({'status':'error','msg':'; '.join(errors)}), 400
    fn = flow['name'].replace('/','_').replace('\\','_')
    fp = os.path.join(FLOWS_DIR, f"{fn}.json")
    try:
        with open(fp, 'w', encoding='utf-8') as f: json.dump(flow, f, indent=2, ensure_ascii=False)
        client._reload_triggers()
        add_event('flow_saved', {'name': flow['name']})
        return jsonify({'status':'ok','path':fp})
    except Exception as e:
        return jsonify({'status':'error','msg':str(e)}), 500

@app.route('/api/flow/list')
def api_flow_list():
    flows = []
    if os.path.isdir(FLOWS_DIR):
        for fn in sorted(os.listdir(FLOWS_DIR)):
            if fn.endswith('.json'):
                try:
                    with open(os.path.join(FLOWS_DIR, fn), 'r', encoding='utf-8') as f:
                        d = json.load(f)
                    # 修复：队列式流程没有顶层 steps，原实现一律显示 0 步
                    if HAS_FLOW_ENGINE:
                        n_steps, n_blocks = _count_flow_steps(d)
                    else:
                        n_steps, n_blocks = len(d.get('steps', []) or []), 0
                    flows.append({'name': d.get('name', fn[:-5]), 'steps': n_steps,
                                  'blocks': n_blocks, 'file': fn})
                except: pass
    return jsonify({'flows': flows})

@app.route('/api/flow/templates')
def api_flow_templates():
    """内置模板目录（编辑器「模板」下拉用），可按 category 过滤"""
    if not HAS_FLOW_ENGINE:
        return jsonify({'templates': [], 'categories': [], 'total': 0})
    cat = request.args.get('category', '')
    items = list_templates(cat)
    return jsonify({'templates': items, 'categories': TEMPLATE_CATEGORIES, 'total': len(items)})

@app.route('/api/flow/blocks')
def api_flow_blocks():
    """引擎积木目录（含参数 schema），供编辑器渲染属性面板"""
    if not HAS_FLOW_ENGINE:
        return jsonify({'blocks': []})
    return jsonify({'blocks': list(BlockRegistry.all().values())})

@app.route('/api/flow/load/<name>')
def api_flow_load(name):
    fn = name.replace('/','_').replace('\\','_')
    fp = os.path.join(FLOWS_DIR, f"{fn}.json")
    if not os.path.isfile(fp):
        # 回退到内置模板
        tpl = FLOW_TEMPLATES.get(name) if HAS_FLOW_ENGINE else None
        if tpl: return jsonify({'flow': tpl})
        return jsonify({'flow':None,'error':'未找到'}), 404
    try:
        with open(fp, 'r', encoding='utf-8-sig') as f: return jsonify({'flow': json.load(f)})
    except Exception as e: return jsonify({'flow':None,'error':str(e)}), 500

@app.route('/api/flow/template/<name>')
def api_flow_template(name):
    if not HAS_FLOW_ENGINE: return jsonify({'flow':None,'error':'引擎未加载'}), 500
    t = FLOW_TEMPLATES.get(name)
    if t:
        return jsonify({'flow': t})
    return jsonify({'flow': None, 'error': '模板不存在'}), 404

def _flow_ctx_for_run(payload=None, event_type='manual_test'):
    """构造带全部回调的 FlowContext（编辑器“试跑”与手动运行共用）。

    修复：此前 /api/flow/execute 用的是空上下文，任何远程/上传积木都会报“未注入”，
    编辑器试跑必然失败。现在把与真实事件触发时相同的回调全部注入。
    """
    payload = payload or {}
    folder = (payload.get('folder') or '').strip()
    relpath = (payload.get('relpath') or '').strip()
    full = (payload.get('full_path') or '').strip()
    if not full and relpath and folder:
        try:
            root = client._folder_abs_path(folder)
        except Exception:
            root = ''
        if root:
            full = os.path.join(root, relpath)
    fctx = FlowContext(event_type=event_type, source_device=CONFIG.get('client_id', 'client'),
                       file_path=relpath, relpath=relpath, full_path=full,
                       folder=folder, timestamp=time.time(),
                       client_dir=BASE_DIR, flows_dir=FLOWS_DIR, logs_dir=LOGS_DIR,
                       backup_key_file=CONFIG.get('backup_key_file', ''),
                       vault_dir=CONFIG.get('vault_dir', ''))
    fctx.request_peer_hash = lambda pid, fp: client._rpc_peer_hash(pid, fp)
    fctx.request_server_backup_hash = lambda fp: client._rpc_server_hash(fp)
    fctx.request_peer_path_info = lambda pid, fp, it: client._rpc_peer_path(pid, fp, it)
    fctx.request_server_path_info = lambda fp, it: client._rpc_server_path(fp, it)
    fctx.request_mirror_from_peer = lambda pid, folder: client._request_mirror(pid, folder)
    fctx.upload_file_to_server = lambda fp, rel: client._send_file_to_server(fp, rel)
    fctx.version_list = lambda fp: client._rpc_version_list(fp)
    fctx.version_op = lambda op, fp, ver='latest', reason='flow': client._rpc_version_op(op, fp, ver, reason)
    fctx.secret_resolver = lambda name: (CONFIG.get(name) or '')
    fctx.allow_command = bool(CONFIG.get('allow_command_block', False))
    fctx.folder_resolver = client._folder_abs_path
    return fctx


@app.route('/api/flow/execute', methods=['POST'])
def api_flow_execute():
    """编辑器试跑：返回逐条日志与变量，便于在面板里看每一步结果"""
    if not HAS_FLOW_ENGINE: return jsonify({'status':'error','error':'引擎未加载'}), 500
    body = request.get_json(force=True, silent=True) or {}
    flow = body.get('flow') or body
    if not isinstance(flow, dict) or not flow.get('name'):
        return jsonify({'status':'error','error':'无效流程定义'}), 400
    errors = validate_flow(flow)
    if errors: return jsonify({'status':'error','error':'; '.join(errors)}), 400
    try:
        fctx = _flow_ctx_for_run(body)
        fctx = FlowEngine(flow).execute(fctx)
        add_event('flow_executed', {
            'flow': flow.get('name', ''), 'trigger': 'manual_test',
            'relpath': fctx.vars.get('_relpath', ''), 'steps': len(fctx.logs),
            'logs': fctx.logs, 'folder': fctx.vars.get('_folder', '')
        })
        failed = [l for l in fctx.logs if l.startswith('❌') or l.lstrip().startswith('❌')
                  or '未知类型' in l]
        return jsonify({'status': 'ok', 'steps': len(fctx.logs), 'failed': len(failed),
                        'logs': fctx.logs,
                        'variables': {k: str(v)[:200] for k, v in fctx.vars.items()
                                      if not k.startswith('_')},
                        'vars': {k: (v if isinstance(v, (int, float, bool)) else str(v)[:200])
                                 for k, v in fctx.vars.items()}})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.route('/api/flow/delete/<name>', methods=['POST'])
def api_flow_delete(name):
    fn = name.replace('/','_').replace('\\','_')
    fp = os.path.join(FLOWS_DIR, f"{fn}.json")
    if os.path.isfile(fp):
        os.remove(fp)
        client._reload_triggers()
        return jsonify({'status':'ok'})
    return jsonify({'status':'not_found'}), 404

@app.route('/api/flow/run/<name>', methods=['POST'])
def api_flow_run(name):
    """手动执行一个流程（主页手动触发按钮）"""
    body = (request.get_json(force=True) or {}) if request.is_json else {}
    ok, msg = client.run_flow_manual(name, folder=body.get('folder', ''))
    return jsonify({'status': 'ok' if ok else 'error', 'msg': msg}), (200 if ok else 500)

@app.route('/api/flow/triggers')
def api_flow_triggers():
    return jsonify({'manual': client.manual_triggers(),
                    'config': CONFIG.get('folder_triggers', {}),
                    'folders': [{'name': fc.get('name', ''), 'path': os.path.abspath(fc['path'])}
                                for fc in CONFIG.get('sync_folders', [])],
                    'flows': sorted([fn[:-5] for fn in os.listdir(FLOWS_DIR) if fn.endswith('.json')])})

@app.route('/api/triggers', methods=['GET'])
def api_triggers_get():
    return jsonify({'config': CONFIG.get('folder_triggers', {}),
                    'folders': [fc.get('name', '') for fc in CONFIG.get('sync_folders', [])],
                    'flows': sorted([fn[:-5] for fn in os.listdir(FLOWS_DIR) if fn.endswith('.json')])})

@app.route('/api/triggers', methods=['POST'])
def api_triggers_set():
    data = request.get_json(force=True) or {}
    ok, msg = client.save_folder_triggers(data)
    return jsonify({'status': 'ok' if ok else 'error', 'msg': msg}), (200 if ok else 400)

@app.route('/api/flow/active', methods=['GET'])
def api_flow_active():
    gid = CONFIG.get('group_id', 'default')
    gc = CONFIG.get('sync_groups', {}).get(gid, {})
    an = gc.get('active_flow', '')
    if not an: return jsonify({'active': None, 'group_id': gid})
    fp = os.path.join(FLOWS_DIR, f"{an}.json")
    if os.path.isfile(fp):
        with open(fp, 'r', encoding='utf-8-sig') as f: return jsonify({'active': json.load(f), 'group_id': gid})
    return jsonify({'active': None, 'group_id': gid})

@app.route('/api/flow/active', methods=['POST'])
def api_flow_set_active():
    d = request.json
    fn = d.get('name', ''); gid = d.get('group_id', CONFIG.get('group_id', 'default'))
    sg = CONFIG.get('sync_groups', {})
    old = sg.get(gid, {}).get('active_flow', '')
    if gid not in sg: sg[gid] = {'active_flow': '', 'enabled': True}
    sg[gid]['active_flow'] = fn
    CONFIG['sync_groups'] = sg
    try:
        save_config(CONFIG)
        with state_lock: state['sync_groups'] = sg
        now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
        add_event('sync_group_flow', {
            'group': gid, 'flow': fn, 'old_flow': old,
            'action': f'{now_ts} [sync_group] INFO - 同步组 [{gid}] 流程切换: '
                      f'"{old or "无"}" → "{fn or "无"}"',
            'detail': f'{old or "无"} → {fn or "无"}'
        })
        return jsonify({'status':'ok','group_id':gid,'flow':fn})
    except Exception as e:
        return jsonify({'status':'error','msg':str(e)}), 500

@app.route('/api/flow/share', methods=['POST'])
def api_flow_share():
    flow = request.json
    if not flow or not flow.get('name'): return jsonify({'status':'error','msg':'无效流程数据'}), 400
    fn = flow['name'].replace('/','_').replace('\\','_')
    fp = os.path.join(FLOWS_DIR, f"{fn}.json")
    try:
        with open(fp, 'w', encoding='utf-8') as f: json.dump(flow, f, indent=2, ensure_ascii=False)
    except Exception as e: return jsonify({'status':'error','msg':f'保存失败: {e}'}), 500
    code, err = client.share_flow_to_server(flow)
    if err: return jsonify({'status':'error','msg':err}), 500
    return jsonify({'status':'ok','share_code':code,'flow_name':flow['name']})

@app.route('/api/flow/install', methods=['POST'])
def api_flow_install():
    d = request.json; code = d.get('share_code','').strip()
    if not code: return jsonify({'status':'error','msg':'请输入方案码'}), 400
    flow, err = client.install_flow_from_server(code)
    if err: return jsonify({'status':'error','msg':err}), 500
    if not flow: return jsonify({'status':'error','msg':'方案码无效'}), 404
    fn = flow.get('name', code).replace('/','_').replace('\\','_')
    fp = os.path.join(FLOWS_DIR, f"{fn}.json")
    try:
        with open(fp, 'w', encoding='utf-8') as f: json.dump(flow, f, indent=2, ensure_ascii=False)
    except Exception as e: return jsonify({'status':'error','msg':f'保存失败: {e}'}), 500
    return jsonify({'status':'ok','flow':flow,'flow_name':flow.get('name','')})

@app.route('/api/flow/logs')
def api_flow_logs_list():
    logs = []
    if os.path.isdir(LOGS_DIR):
        for fn in sorted(os.listdir(LOGS_DIR), reverse=True)[:100]:
            if fn.endswith('.log'):
                fp = os.path.join(LOGS_DIR, fn)
                logs.append({'filename': fn, 'size': os.path.getsize(fp), 'mtime': os.path.getmtime(fp)})
    return jsonify({'logs': logs})

@app.route('/api/flow/log/<filename>')
def api_flow_log_read(filename):
    safe = filename.replace('/','_').replace('\\','_')
    fp = os.path.join(LOGS_DIR, safe)
    if not os.path.isfile(fp): return jsonify({'status':'error','msg':'日志文件不存在'}), 404
    try:
        with open(fp, 'r', encoding='utf-8-sig') as f:
            return jsonify({'status':'ok','content':f.read(),'filename':filename})
    except Exception as e: return jsonify({'status':'error','msg':str(e)}), 500

# ── 流程工坊 API（代理到服务端，工坊为全局共享库） ──
@app.route('/api/workshop/list')
def api_workshop_list():
    q = request.args.get('q', '')
    sort = request.args.get('sort', 'newest')
    endpoint = f"/api/workshop/list?q={urllib.parse.quote(q)}&sort={urllib.parse.quote(sort)}"
    return jsonify(_proxy_to_server(endpoint))

@app.route('/api/workshop/upload', methods=['POST'])
def api_workshop_upload():
    data = request.get_json(force=True) or {}
    result = _proxy_to_server('/api/workshop/upload', method='POST', body=data)
    if result.get('status') == 'ok':
        add_event('workshop_upload', {'id': result.get('id'), 'name': data.get('name', '')})
    return jsonify(result)

@app.route('/api/workshop/download/<fid>')
def api_workshop_download(fid):
    return jsonify(_proxy_to_server(f'/api/workshop/download/{urllib.parse.quote(fid)}'))

@app.route('/api/workshop/delete/<fid>', methods=['POST'])
def api_workshop_delete(fid):
    return jsonify(_proxy_to_server(f'/api/workshop/delete/{urllib.parse.quote(fid)}', method='POST'))

# ============================================================
def background_pusher():
    while running:
        sio.sleep(1)
        with state_lock:
            s = {'connected': state['connected'], 'peers': state['peers'],
                 'stats': dict(state['stats']), 'events': state['events'][-30:],
                 'sync_groups': dict(state['sync_groups'])}
        sio.emit('status_update', s)

def sync_thread_func():
    delay = CONFIG.get('reconnect_base_delay', 2)
    max_d = CONFIG.get('reconnect_max_delay', 60)
    while running:
        try:
            cprint(C.C, "\n🔗 连接服务端...")
            client.connect()
            # 先起监听再发快照：full_sync 在大目录上可能耗时很久，
            # 期间的变更既不在快照内容里、也没有事件，会被永久漏掉（实机复现过）
            client.start_watching()
            client.full_sync()
            # 备份兜底对账：补上"监听就绪前/断线期间"漏掉的文件变化。
            # 期间暂停监听并把事件缓存，结束后自动补发，避免共用同一条连接读写。
            if CONFIG.get('auto_backup_on_start', True):
                try:
                    client._pause_handlers()
                    try:
                        checked, uploaded = client.reconcile_backup()
                        if checked:
                            cprint(C.G, f"  🧮 备份对账: 检查 {checked} 个文件，补传 {uploaded} 个")
                    finally:
                        client._resume_handlers()
                except Exception as e:
                    cprint(C.Y, f"  ⚠ 备份对账失败（不影响同步）: {e}")
            delay = CONFIG.get('reconnect_base_delay', 2)
            client.run_loop()
        except (ConnectionRefusedError, socket.timeout) as e:
            cprint(C.R, f"  ❌ 连接失败: {e}")
        except Exception as e:
            cprint(C.R, f"  ❌ 异常: {e}"); traceback.print_exc()
        if not running: break
        cprint(C.Y, f"  ⏳ {delay}s 后重连...")
        time.sleep(delay); delay = min(delay * 2, max_d)

if __name__ == '__main__':
    print(f"""
{C.C}╔══════════════════════════════════════════════════════╗
║   🖥  yumirror v3 - 客户端 (Backup Framework)       ║
║  客户端: {C.W}{CONFIG.get('client_id', '?'):<42}{C.C}║
║  服务端: {C.W}{CONFIG['server_host']}:{CONFIG['server_port']:<34}{C.C}║
║  Web:    {C.W}http://0.0.0.0:{CONFIG.get('web_port', 8087):<30}{C.C}║
║  文件夹: {C.W}{len(CONFIG.get('sync_folders', []))} 个{C.C}                                    ║
║  编辑器: {C.W}http://0.0.0.0:{CONFIG.get('web_port', 8087)}/editor{C.C}                        ║
║  工  坊: {C.W}http://0.0.0.0:{CONFIG.get('web_port', 8087)}/workshop{C.C}                      ║
╚══════════════════════════════════════════════════════╝{C.RST}
""")

    def sig_handler(s, f):
        global running
        print(f"\n{C.Y}🛑 退出...{C.RST}"); running = False; client.stop(); sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    threading.Thread(target=sync_thread_func, daemon=True).start()
    client._reload_triggers()
    threading.Thread(target=client._trigger_loop, daemon=True, name='trigger').start()
    sio.start_background_task(background_pusher)
    _ptls = CONFIG.get('panel_tls') or {}
    _ssl_ctx = None
    if _ptls.get('enabled'):
        _ssl_ctx = (_ptls.get('cert_file'), _ptls.get('key_file'))
        if not all(_ssl_ctx):
            print('  ⚠ panel_tls 已启用但没配 cert_file/key_file，面板仍以 http 启动')
            _ssl_ctx = None
    sio.run(app, host=CONFIG.get('web_host', '0.0.0.0'),
            port=CONFIG.get('web_port', 8087), allow_unsafe_werkzeug=True,
            ssl_context=_ssl_ctx)

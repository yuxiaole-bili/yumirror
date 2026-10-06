#!/usr/bin/env python3
import os, sys, json, time, socket, threading, signal, traceback, hashlib, uuid, base64, re, hmac
from datetime import datetime
from collections import defaultdict

# 控制台统一 UTF-8（中文 Windows 默认 GBK 无法编码 emoji/中文日志）
try:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE_DIR))

from shared.protocol import Connection, MsgType
from shared.backup_framework import PipelineLogger

CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

_BUILTIN = {
    'server_name': 'yumirror-server',
    'bind_host': '0.0.0.0',
    'bind_port': 9999,
    'web_host': '0.0.0.0',
    'web_port': 8086,
    'backup_dir': os.path.join(BASE_DIR, 'backups'),
    'flows_share_dir': os.path.join(BASE_DIR, 'shared_flows'),
    'users_db_path': os.path.join(BASE_DIR, 'users.json'),
    'logs_dir': os.path.join(BASE_DIR, 'logs'),
    'max_versions_per_file': 20,
    'max_clients_per_group': 50,
    'heartbeat_interval': 10,
    'stats_interval': 30,
    # 可选：{"组名": "组密钥"}。配了的组，客户端 HELLO 必须带对 group_key 才允许加入，
    # 用于挡住"知道组名就能加入并往别人目录写文件"的盲注（详见 README 安全说明）。
    'group_keys': {},
    # 界面语言：留空 = 跟随浏览器 Accept-Language；也可写 'zh-CN' / 'en-US'
    'language': '',
    # 传输层 TLS：默认开启，证书缺失时自动生成自签证书；客户端靠指纹固定认服务端。
    # 关掉它 = 文件内容在局域网上明文传输（仅限调试）。
    'tls': {'enabled': True, 'cert_file': '', 'key_file': ''},
    # 网页面板 TLS：面板只绑 127.0.0.1 时可不加；开放给局域网时建议打开
    'panel_tls': {'enabled': False, 'cert_file': '', 'key_file': ''},
}

# 兼容旧版配置键名（历史 config.json 使用 server_port / device_name）
_ALIASES = {
    'bind_port':   ('bind_port', 'server_port', 'port'),
    'bind_host':   ('bind_host', 'host'),
    'server_name': ('server_name', 'device_name', 'name'),
}

_file_cfg = {}
if os.path.isfile(CONFIG_PATH):
    try:
        # utf-8-sig：兼容 Windows 记事本另存为带 BOM 的 config.json
        with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
            _file_cfg = json.load(f)
    except Exception as ex:
        print(f'[WARN] 读取 {CONFIG_PATH} 失败，改用内置默认配置: {ex}')
        _file_cfg = {}

def _cfg(key):
    for name in _ALIASES.get(key, (key,)):
        val = _file_cfg.get(name)
        if val is not None and val != '':
            return val
    return _BUILTIN[key]

def save_config(d):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

BACKUP_DIR = _cfg('backup_dir')
FLOWS_SHARE_DIR = _cfg('flows_share_dir')
LOGS_DIR = _cfg('logs_dir')

# 组密钥表：{"组名": "密钥"}；没配的组不受影响
_gk = _cfg('group_keys')
GROUP_KEYS = {str(k): str(v) for k, v in _gk.items()} if isinstance(_gk, dict) else {}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared import tls_util  # noqa: E402  传输层 TLS 工具（自签证书 + 指纹固定）
from shared import i18n  # noqa: E402  多语言词条与语言包

# 由 main() 填充：TLS 上下文与证书路径（None 表示明文模式）
TLS_CTX = None
CERT_PATH = ''
KEY_PATH = ''
TLS_FINGERPRINT = ''

os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(FLOWS_SHARE_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

logger = PipelineLogger(LOGS_DIR, 'server')
VERSION_FETCH_LIMIT = 16 * 1024 * 1024   # fetch 版本内容走单条响应，超过请改用面板下载


class ClientEntry:
    def __init__(self, conn, addr, client_id, group_id, sync_folders):
        self.conn = conn
        self.addr = addr
        self.client_id = client_id
        self.group_id = group_id
        self.sync_folders = sync_folders
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.bytes_sent = 0
        self.bytes_recv = 0


class ClientManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients = {}
        self._groups = defaultdict(set)

    def register(self, entry):
        cid = entry.client_id
        gid = entry.group_id
        with self._lock:
            old = self._clients.pop(cid, None)
            if old:
                self._groups[old.group_id].discard(cid)
                if not self._groups[old.group_id]:
                    del self._groups[old.group_id]
                logger.info('client_mgr', f'移除旧连接: {cid} (组={old.group_id})')
                try:
                    old.conn.send(MsgType.BYE)
                    old.conn.close()
                except Exception:
                    pass
            current = len(self._groups.get(gid, set()))
            max_cap = _cfg('max_clients_per_group')
            if current >= max_cap:
                logger.warning('client_mgr', f'组 [{gid}] 已满 ({current}/{max_cap})，拒绝: {cid}')
                return False
            self._clients[cid] = entry
            self._groups[gid].add(cid)
            logger.info('client_mgr', f'注册客户端: {cid} → 组 [{gid}] ({entry.addr[0]}:{entry.addr[1]}) · 同步文件夹={len(entry.sync_folders)} · 组内={len(self._groups[gid])} 在线')
        return True

    def unregister(self, client_id):
        with self._lock:
            entry = self._clients.pop(client_id, None)
            if entry:
                self._groups[entry.group_id].discard(client_id)
                if not self._groups[entry.group_id]:
                    del self._groups[entry.group_id]
                logger.info('client_mgr', f'注销客户端: {client_id} · 组 [{entry.group_id}] 剩余 {len(self._groups.get(entry.group_id, set()))} 在线')
                try:
                    entry.conn.close()
                except Exception:
                    pass
                return entry
        return None

    def get(self, client_id):
        with self._lock:
            return self._clients.get(client_id)

    def get_group_peers(self, group_id, exclude_id=''):
        with self._lock:
            return [c for c in self._groups.get(group_id, set()) if c != exclude_id]

    def get_group_entries(self, group_id):
        with self._lock:
            return [self._clients[c] for c in self._groups.get(group_id, set()) if c in self._clients]

    def touch(self, client_id):
        with self._lock:
            e = self._clients.get(client_id)
            if e:
                e.last_seen = time.time()

    def get_stats(self):
        with self._lock:
            return {
                'total_clients': len(self._clients),
                'total_groups': len(self._groups),
                'groups': {g: len(m) for g, m in self._groups.items()},
            }

    def cleanup_stale(self, max_idle=60.0):
        now = time.time()
        stale = []
        with self._lock:
            for cid, e in list(self._clients.items()):
                if now - e.last_seen > max_idle:
                    stale.append(cid)
        for cid in stale:
            logger.warning('client_mgr', f'心跳超时 ({max_idle:.0f}s): {cid}，强制清理')
            self.unregister(cid)


clients = ClientManager()


class BackupManager:
    def __init__(self, base_dir, max_versions=20):
        self.base_dir = base_dir
        self.max_versions = int(max_versions or 0)   # 每个文件保留的历史版本数（0 = 不留）
        os.makedirs(base_dir, exist_ok=True)

    def _safe(self, gid, fp):
        sg = gid.replace('/', '_').replace('\\', '_').replace('..', '')
        sf = fp.replace('\\', '/').lstrip('/')
        while '..' in sf:
            sf = sf.replace('..', '')
        full = os.path.join(self.base_dir, sg, sf)
        # 用 commonpath 判定包含关系：startswith 会把 base 的同级兄弟目录（如 bk_evil）误判为合法
        try:
            if os.path.commonpath([os.path.abspath(full), os.path.abspath(self.base_dir)]) != os.path.abspath(self.base_dir):
                raise ValueError(f'路径越界: {fp}')
        except ValueError:
            raise ValueError(f'路径越界: {fp}')
        return full

    # ── 历史版本存储 ──
    def _ver_root(self, gid):
        return os.path.join(self._safe(gid, '.'), '.versions')

    def _ver_dir(self, gid, fp):
        rel = fp.replace('\\', '/').lstrip('/')
        return os.path.join(self._ver_root(gid), rel)

    def _version_path(self, gid, fp, vid):
        if not re.fullmatch(r'[0-9A-Za-z._-]{1,80}', str(vid or '')):
            raise ValueError(f'版本号非法: {vid}')
        return os.path.join(self._ver_dir(gid, fp), vid + '.bak')

    def _load_index(self, gid, fp):
        try:
            with open(os.path.join(self._ver_dir(gid, fp), 'index.json'), 'r', encoding='utf-8') as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def _save_index(self, gid, fp, idx):
        vdir = self._ver_dir(gid, fp)
        os.makedirs(vdir, exist_ok=True)
        p = os.path.join(vdir, 'index.json')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    def _prune(self, gid, fp, idx):
        if self.max_versions <= 0:
            idx.clear()
            return
        while len(idx) > self.max_versions:
            old = idx.pop(0)
            try:
                os.remove(self._version_path(gid, fp, old.get('id')))
            except OSError:
                pass

    def archive_current(self, gid, fp, reason='replace'):
        """把当前备份归档为一个历史版本；返回条目或 None"""
        full = self._safe(gid, fp)
        if not os.path.isfile(full):
            return None
        try:
            with open(full, 'rb') as f:
                data = f.read()
            mtime = os.path.getmtime(full)
        except OSError as e:
            logger.warning('backup_ver', f'归档失败（读取）: {fp} · {e}')
            return None
        sha = hashlib.sha256(data).hexdigest()
        vid = time.strftime('%Y%m%d-%H%M%S', time.localtime(mtime)) + '-' + sha[:8]
        try:
            os.makedirs(self._ver_dir(gid, fp), exist_ok=True)
            vpath = self._version_path(gid, fp, vid)
            if not os.path.isfile(vpath):
                with open(vpath, 'wb') as f:
                    f.write(data)
                try:
                    os.utime(vpath, (mtime, mtime))
                except OSError:
                    pass
        except OSError as e:
            logger.error('backup_ver', f'归档失败（写入）: {fp} · {e}')
            return None
        idx = self._load_index(gid, fp)
        entry = {'id': vid, 'sha256': sha, 'size': len(data), 'mtime': int(mtime),
                 'archived_at': int(time.time()), 'reason': reason}
        if not any(e.get('id') == vid for e in idx):
            idx.append(entry)
        idx.sort(key=lambda e: e.get('mtime', 0))
        self._prune(gid, fp, idx)
        self._save_index(gid, fp, idx)
        return entry

    def snapshot(self, gid, fp, reason='flow'):
        """显式打一个版本点：当前备份原样归档（内容未变也留点），返回条目"""
        e = self.archive_current(gid, fp, reason=reason)
        if e:
            logger.info('backup_ver', f'版本点: 组=[{gid}] · 文件={fp} · {e["id"]} · 原因={reason}')
        return e

    def list_versions(self, gid, fp):
        idx = sorted(self._load_index(gid, fp), key=lambda e: e.get('mtime', 0), reverse=True)
        full = self._safe(gid, fp)
        cur = None
        if os.path.isfile(full):
            cur = {'sha256': self.get_sha256(gid, fp), 'size': os.path.getsize(full),
                   'mtime': int(os.path.getmtime(full))}
        return {'path': fp, 'group': gid, 'current': cur, 'versions': idx,
                'max_versions': self.max_versions}

    def resolve_version_id(self, gid, fp, vid):
        """把 latest / latest-N / 具体版本号解析成具体 id；解析不到返回 None"""
        idx = sorted(self._load_index(gid, fp), key=lambda e: e.get('mtime', 0))
        if not idx:
            return None
        if vid in ('latest', 'last', '', None):
            return idx[-1].get('id')
        s = str(vid)
        if s.startswith('latest-'):
            try:
                n = int(s.split('-', 1)[1])
            except ValueError:
                return None
            if 1 <= n <= len(idx):
                return idx[-n].get('id')
            return None
        return s if any(e.get('id') == s for e in idx) else None

    def read_version(self, gid, fp, vid):
        rid = self.resolve_version_id(gid, fp, vid)     # 支持 latest / latest-N
        if not rid:
            return None
        vpath = self._version_path(gid, fp, rid)
        if not os.path.isfile(vpath):
            return None
        try:
            with open(vpath, 'rb') as f:
                return f.read()
        except OSError:
            return None

    def restore(self, gid, fp, vid='latest'):
        """回滚到指定版本：先把当前内容归档（可撤销），再原子写回目标版本"""
        idx = sorted(self._load_index(gid, fp), key=lambda e: e.get('mtime', 0))
        if not idx:
            return {'ok': False, 'msg': '该文件没有历史版本'}
        if vid in ('latest', 'last', ''):
            target = idx[-1]
        elif str(vid).startswith('latest-'):
            try:
                n = int(str(vid).split('-', 1)[1])
            except ValueError:
                return {'ok': False, 'msg': f'版本号非法: {vid}'}
            if n < 1 or n > len(idx):
                return {'ok': False, 'msg': f'没有倒数第 {n} 个版本（共 {len(idx)} 个）'}
            target = idx[-n]
        else:
            target = next((e for e in idx if e.get('id') == vid), None)
        if not target:
            return {'ok': False, 'msg': f'版本不存在: {vid}'}
        data = self.read_version(gid, fp, target['id'])
        if data is None:
            return {'ok': False, 'msg': f'版本文件已丢失: {target["id"]}'}
        self.archive_current(gid, fp, reason='rollback')          # 当前内容先留档，回滚可撤销
        full = self._safe(gid, fp)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + '.tmp'
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            mt = float(target.get('mtime') or time.time())
            try:
                os.utime(tmp, (mt, mt))
            except OSError:
                pass
            os.replace(tmp, full)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return {'ok': False, 'msg': f'回滚写入失败: {e}'}
        logger.info('backup_ver', f'版本回滚: 组=[{gid}] · 文件={fp} · → {target["id"]} · {len(data)} 字节')
        return {'ok': True, 'version': target['id'], 'size': len(data),
                'sha256': target.get('sha256', ''), 'mtime': target.get('mtime', 0)}

    def store(self, gid, fp, data, mtime=None):
        """写入备份。

        传入源文件 mtime 时按「后写覆盖先写」判定：只有 mtime 前进的版本才允许覆盖。
        原因：客户端在文件被瞬时截断到 0 字节时也会读到 size=0 并发起上传，
        乱序/重复到达的空内容会把刚存好的备份清空（实测复现）。
        mtime 未前进的重复上传直接跳过，内容相同则静默忽略。
        """
        full = self._safe(gid, fp)
        sha = hashlib.sha256(data).hexdigest()
        if mtime is not None and os.path.isfile(full):
            try:
                prev_mtime = os.path.getmtime(full)
                if float(mtime) <= float(prev_mtime):
                    if sha != self.get_sha256(gid, fp):
                        logger.warning('backup_mgr', f'忽略乱序上传（mtime 未前进）: 组=[{gid}] · 文件={fp} · 已存={os.path.getsize(full)} 字节 · 本次={len(data)} 字节')
                    return sha, 0
            except Exception:
                pass
        archived = None
        if os.path.isfile(full):
            if self.get_sha256(gid, fp) == sha:
                return sha, 0                      # 内容没变，不必重复落盘
            archived = self.archive_current(gid, fp, reason='replace')   # 覆盖前留历史版本
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + '.tmp'                        # 原子写入：中断不会留下半个文件
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if mtime is not None:
                try:
                    os.utime(tmp, (float(mtime), float(mtime)))
                except Exception:
                    pass
            os.replace(tmp, full)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            logger.error('backup_mgr', f'备份落盘失败: 组=[{gid}] · 文件={fp} · {e}')
            raise
        logger.info('backup_mgr', f'备份存储: 组=[{gid}] · 文件={fp} · SHA256={sha[:16]}... · 大小={len(data)} 字节'
                    + (f' · 已留旧版本 {archived["id"]}' if archived else ''))
        return sha, len(data)

    def get_sha256(self, gid, fp):
        full = self._safe(gid, fp)
        if not os.path.isfile(full):
            return None
        sha = hashlib.sha256()
        try:
            with open(full, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    sha.update(chunk)
            return sha.hexdigest()
        except Exception as e:
            logger.error('backup_mgr', f'备份哈希失败: 组=[{gid}] · 文件={fp} · {e}')
            return None

    def get_info(self, gid, fp, it):
        full = self._safe(gid, fp)
        try:
            if it == 'exists':
                return 'true' if os.path.exists(full) else 'false'
            elif it == 'size':
                return str(os.path.getsize(full)) if os.path.isfile(full) else '0'
            elif it == 'mtime':
                return str(int(os.path.getmtime(full))) if os.path.exists(full) else '0'
            elif it == 'hash':
                return self.get_sha256(gid, fp) or ''
            elif it == 'count':
                return str(len(os.listdir(full))) if os.path.isdir(full) else '0'
            elif it == 'is_file':
                return 'true' if os.path.isfile(full) else 'false'
            elif it == 'is_dir':
                return 'true' if os.path.isdir(full) else 'false'
        except Exception as e:
            logger.error('backup_mgr', f'备份元信息失败: {it} · 文件={fp} · {e}')
        return ''

    def list_group_backups(self, gid):
        gd = self._safe(gid, '.')
        files = []
        if os.path.isdir(gd):
            for dp, dns, fns in os.walk(gd):
                dns[:] = [d for d in dns if not d.startswith('.')]   # 跳过 .versions
                for fn in fns:
                    if fn.startswith('.') or fn.endswith('.tmp'):
                        continue
                    files.append(os.path.relpath(os.path.join(dp, fn), gd).replace('\\', '/'))
        return files


backups = BackupManager(BACKUP_DIR, _cfg('max_versions_per_file'))


class FlowShareManager:
    def __init__(self, share_dir):
        self.share_dir = share_dir
        os.makedirs(share_dir, exist_ok=True)
        self._lock = threading.Lock()

    def store(self, flow):
        code = uuid.uuid4().hex[:8].lower()
        meta = {'code': code, 'name': flow.get('name', ''), 'steps': len(flow.get('steps', [])), 'created_at': time.time(), 'flow': flow}
        with self._lock:
            with open(os.path.join(self.share_dir, f"{code}.json"), 'w', encoding='utf-8') as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        logger.info('flow_share', f'分享流程: 方案码={code} · 名称="{flow.get("name","")}" · {len(flow.get("steps",[]))} 步骤')
        return code

    def retrieve(self, code):
        code = code.strip().lower()
        fp = os.path.join(self.share_dir, f"{code}.json")
        if not os.path.isfile(fp):
            return None
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                return json.load(f).get('flow')
        except Exception as e:
            logger.error('flow_share', f'方案读取失败: {code} · {e}')
            return None

    def list_codes(self):
        return [fn[:-5] for fn in os.listdir(self.share_dir) if fn.endswith('.json')] if os.path.isdir(self.share_dir) else []

    def cleanup_stale(self, max_age=86400 * 7):
        now = time.time()
        removed = 0
        if os.path.isdir(self.share_dir):
            for fn in os.listdir(self.share_dir):
                if fn.endswith('.json'):
                    try:
                        with open(os.path.join(self.share_dir, fn), 'r', encoding='utf-8') as f:
                            if now - json.load(f).get('created_at', 0) > max_age:
                                os.remove(os.path.join(self.share_dir, fn))
                                removed += 1
                    except Exception:
                        pass
        if removed:
            logger.info('flow_share', f'清理过期方案: {removed} 个')


flow_shares = FlowShareManager(FLOWS_SHARE_DIR)


# ============================================================
# 工坊存储（全局共享方案库，只存服务端）
# ============================================================
WORKSHOP_DB_PATH = os.path.join(BASE_DIR, 'workshop.json')
_workshop_lock = threading.Lock()
workshop_db = {}


def _load_workshop():
    global workshop_db
    with _workshop_lock:
        try:
            if os.path.isfile(WORKSHOP_DB_PATH):
                with open(WORKSHOP_DB_PATH, 'r', encoding='utf-8') as f:
                    workshop_db = json.load(f) or {}
            else:
                workshop_db = {}
        except Exception as e:
            logger.error('workshop', f'工坊加载失败: {e}')
            workshop_db = {}


def _save_workshop():
    with _workshop_lock:
        try:
            with open(WORKSHOP_DB_PATH, 'w', encoding='utf-8') as f:
                json.dump(workshop_db, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error('workshop', f'工坊保存失败: {e}')


_load_workshop()


def relay_to_group(sender_id, group_id, msg_type, payload, exclude_sender=True):
    peers = clients.get_group_entries(group_id)
    relayed = 0
    for e in peers:
        if exclude_sender and e.client_id == sender_id:
            continue
        try:
            e.conn.send(msg_type, payload)
            e.bytes_sent += len(payload)
            relayed += 1
        except Exception as ex:
            logger.warning('relay', f'中继失败: → {e.client_id} · type={MsgType.name(msg_type)} · {ex}')
    if relayed > 0:
        logger.info('relay', f'中继消息: type={MsgType.name(msg_type)} · 发送者={sender_id} · 组=[{group_id}] · → {relayed}/{len(peers)} 个客户端')


def notify_peer_event(group_id, event, peer_id):
    payload = json.dumps({'event': event, 'peer_id': peer_id}).encode()
    for e in clients.get_group_entries(group_id):
        if e.client_id == peer_id:
            continue
        try:
            e.conn.send(MsgType.HELLO, payload)
        except Exception:
            pass


def handle_client(conn, addr):
    client_id = '?'
    group_id = '?'
    try:
        try:
            r = conn.recv(timeout=30)
        except (ConnectionError, OSError):
            # 端口探测 / 健康检查 / SSH 转发探测会在握手前就断开，属正常现象，
            # 不应记成 ERROR 并打整段 traceback（实测：每次端口探测都会刷一条）
            logger.info('connection', f'握手前断开: {addr[0]}:{addr[1]}')
            return
        if not r or r[0] != MsgType.HELLO:
            logger.warning('connection', f'握手超时或无效: {addr}')
            return
        hello = json.loads(r[2].decode())
        client_id = hello.get('client_id', f'client_{addr[1]}')
        group_id = hello.get('group_id', 'default')
        sync_folders = hello.get('sync_folders', [])

        # 组密钥（可选）：配了 group_keys 的组，HELLO 必须带对密钥。
        # 不配则行为与以前完全一致（向后兼容）。注意它挡的是"知道组名就能加入"的盲注，
        # 挡不住能在链路上嗅探的人（那需要传输层加密）。
        expected_key = (GROUP_KEYS.get(group_id) or '').strip()
        if expected_key and (hello.get('group_key') or '') != expected_key:
            conn.send(MsgType.HELLO_ACK, json.dumps({
                'status': 'rejected', 'reason': i18n.t(_cur_lang(), 'err_group_key'),
                'server': _cfg('server_name')}).encode())
            logger.warning('connection', f'拒绝入组: {client_id} · 组=[{group_id}] · {addr[0]}:{addr[1]} · 组密钥不符')
            conn.close()
            return

        entry = ClientEntry(conn, addr, client_id, group_id, sync_folders)
        if not clients.register(entry):
            conn.send(MsgType.HELLO_ACK, json.dumps({'status': 'rejected', 'reason': '组已满', 'server': _cfg('server_name')}).encode())
            conn.close()
            return

        peers_online = clients.get_group_peers(group_id, exclude_id=client_id)
        conn.send(MsgType.HELLO_ACK, json.dumps({'status': 'ok', 'server': _cfg('server_name'), 'peers_online': peers_online, 'group_id': group_id}).encode())
        notify_peer_event(group_id, 'peer_online', client_id)
        logger.info('connection', f'客户端已就绪: {client_id} · 组=[{group_id}] · {addr[0]}:{addr[1]} · 文件夹={len(sync_folders)} · 组内={len(peers_online)+1} 在线')

        iv = _cfg('heartbeat_interval')
        while True:
            try:
                r = conn.recv(timeout=iv)
            except (ConnectionError, OSError):
                logger.info('connection', f'连接断开: {client_id}')
                break
            if r is None:
                try:
                    conn.send(MsgType.PING)
                    clients.touch(client_id)
                except Exception:
                    logger.info('connection', f'心跳失败: {client_id}')
                    break
                continue
            mt, _, pl = r
            clients.touch(client_id)
            try:
                if mt == MsgType.PING:
                    conn.send(MsgType.PONG)
                elif mt == MsgType.BYE:
                    logger.info('connection', f'客户端主动断开: {client_id}')
                    break
                elif mt in (MsgType.FILE_CREATE, MsgType.FILE_DATA, MsgType.FILE_END):
                    # 随同步在服务端自动留备份（补上备份写入链路）
                    try:
                        if mt == MsgType.FILE_CREATE:
                            meta = json.loads(pl.decode())
                            entry._upload = {'relpath': meta.get('relpath', ''), 'buf': bytearray(), 'mtime': meta.get('mtime')}
                        elif mt == MsgType.FILE_DATA:
                            if getattr(entry, '_upload', None):
                                if pl:
                                    entry._upload['buf'].extend(pl)
                                else:
                                    up = entry._upload; entry._upload = None
                                    try:
                                        backups.store(group_id, up['relpath'], bytes(up['buf']), up.get('mtime'))
                                    except Exception as ex:
                                        logger.error('backup_mgr', f'备份落盘失败: {ex}')
                        elif mt == MsgType.FILE_END:
                            if getattr(entry, '_upload', None):
                                up = entry._upload; entry._upload = None
                                try:
                                    backups.store(group_id, up['relpath'], bytes(up['buf']), up.get('mtime'))
                                except Exception as ex:
                                    logger.error('backup_mgr', f'备份落盘失败: {ex}')
                    except Exception:
                        pass
                    relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.FILE_DELETE:
                    relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.FILE_RENAME:
                    relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.SNAPSHOT_SEND:
                    relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.SNAPSHOT_REQUEST:
                    req = json.loads(pl.decode())
                    tp = req.get('target_peer', '')
                    if tp:
                        te = clients.get(tp)
                        if te:
                            try:
                                te.conn.send(mt, pl)
                                logger.info('relay', f'转发镜像请求: {client_id} → {tp}')
                            except Exception as ex:
                                logger.warning('relay', f'转发镜像请求失败: {tp} · {ex}')
                    else:
                        relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.FILE_HASH_REQUEST:
                    req = json.loads(pl.decode())
                    target = req.get('target_peer', '')
                    if target:
                        te = clients.get(target)
                        if te:
                            try:
                                te.conn.send(mt, pl)
                                te.bytes_sent += len(pl)
                                logger.info('relay', f'转发哈希请求: {client_id} → {target} · 文件={req.get("file_path","")}')
                            except Exception as ex:
                                logger.warning('relay', f'转发哈希请求失败: {target} · {ex}')
                    else:
                        relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.FILE_HASH_RESPONSE:
                    relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.SERVER_BACKUP_HASH_REQUEST:
                    req = json.loads(pl.decode())
                    fp = req.get('file_path', '')
                    sha = backups.get_sha256(group_id, fp) or ''
                    conn.send(MsgType.SERVER_BACKUP_HASH_RESPONSE, json.dumps({'request_id': req.get('request_id', ''), 'sha256': sha, 'file_path': fp}).encode())
                    logger.info('backup_sync', f'服务端备份哈希响应: 请求者={client_id} · 文件={fp} · SHA256={sha[:16] if sha else "(无备份)"}...')
                elif mt == MsgType.SERVER_BACKUP_HASH_RESPONSE:
                    relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.PATH_INFO_REQUEST:
                    req = json.loads(pl.decode())
                    target = req.get('target_peer', '')
                    source = req.get('source', '')
                    if source == 'server':
                        fp = req.get('file_path', '')
                        it = req.get('info_type', 'exists')
                        val = backups.get_info(group_id, fp, it)
                        conn.send(MsgType.PATH_INFO_RESPONSE, json.dumps({'request_id': req.get('request_id', ''), 'value': val, 'info_type': it}).encode())
                        logger.info('backup_sync', f'服务端路径信息: 请求者={client_id} · 文件={fp} · 类型={it} · 值={val}')
                    elif target:
                        te = clients.get(target)
                        if te:
                            try:
                                te.conn.send(mt, pl)
                                te.bytes_sent += len(pl)
                            except Exception as ex:
                                logger.warning('relay', f'转发路径请求失败: {target} · {ex}')
                    else:
                        relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.PATH_INFO_RESPONSE:
                    relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.FLOW_SHARE_UPLOAD:
                    req = json.loads(pl.decode())
                    code = flow_shares.store(req.get('flow', {}))
                    conn.send(MsgType.FLOW_SHARE_RESPONSE, json.dumps({'request_id': req.get('request_id', ''), 'share_code': code, 'found': True}).encode())
                elif mt == MsgType.FLOW_SHARE_DOWNLOAD:
                    req = json.loads(pl.decode())
                    flow = flow_shares.retrieve(req.get('share_code', ''))
                    conn.send(MsgType.FLOW_SHARE_DOWNLOAD_RESPONSE, json.dumps({'request_id': req.get('request_id', ''), 'found': flow is not None, 'flow': flow}).encode())
                elif mt in (MsgType.FLOW_SHARE_RESPONSE, MsgType.FLOW_SHARE_DOWNLOAD_RESPONSE):
                    relay_to_group(client_id, group_id, mt, pl)
                elif mt == MsgType.VERSION_LIST_REQUEST:
                    req = json.loads(pl.decode())
                    rp = req.get('file_path', '')
                    try:
                        info = backups.list_versions(group_id, rp)
                    except Exception as ex:
                        info = {'current': None, 'versions': [], 'max_versions': backups.max_versions}
                        logger.error('backup_ver', f'版本列表失败: {rp} · {ex}')
                    conn.send(MsgType.VERSION_LIST_RESPONSE, json.dumps({
                        'request_id': req.get('request_id', ''), 'file_path': rp,
                        'current': info.get('current'), 'versions': info.get('versions', []),
                        'max_versions': info.get('max_versions', 0)}).encode())
                    logger.info('backup_ver', f'版本列表: 请求者={client_id} · 文件={rp} · {len(info.get("versions", []))} 个版本')
                elif mt == MsgType.VERSION_OP_REQUEST:
                    req = json.loads(pl.decode())
                    op = req.get('op', '')
                    rp = req.get('file_path', '')
                    ver = req.get('version', 'latest')
                    resp = {'request_id': req.get('request_id', ''), 'op': op, 'file_path': rp, 'ok': False}
                    try:
                        if op == 'snapshot':
                            e = backups.snapshot(group_id, rp, reason=req.get('reason', 'flow'))
                            if e:
                                resp.update({'ok': True, 'version': e['id'], 'size': e['size'],
                                             'sha256': e['sha256'], 'mtime': e['mtime']})
                            else:
                                resp['msg'] = '当前没有可归档的备份（该文件尚未备份过）'
                        elif op == 'restore':
                            r = backups.restore(group_id, rp, ver)
                            resp.update(r)
                        elif op == 'fetch':
                            data = backups.read_version(group_id, rp, ver)
                            if data is None:
                                resp['msg'] = f'版本不存在: {ver}'
                            elif len(data) > VERSION_FETCH_LIMIT:
                                resp['msg'] = '版本超过 %dMB，请改用面板下载' % (VERSION_FETCH_LIMIT // 1048576)
                            else:
                                resp.update({'ok': True, 'size': len(data),
                                             'content_b64': base64.b64encode(data).decode('ascii'),
                                             'sha256': hashlib.sha256(data).hexdigest()})
                        else:
                            resp['msg'] = f'未知版本操作: {op}'
                    except Exception as ex:
                        resp['msg'] = str(ex)
                        logger.error('backup_ver', f'版本操作异常: {op} · {rp} · {ex}')
                    conn.send(MsgType.VERSION_OP_RESPONSE, json.dumps(resp).encode())
                    logger.info('backup_ver', f'版本操作: 请求者={client_id} · {op} · 文件={rp} · 版本={ver} · ok={resp["ok"]}')
                elif mt == MsgType.PONG:
                    # 心跳应答：上方 clients.touch() 已刷新活跃时间，此处无需再处理
                    # （此前落到 else 分支，每 10 秒刷一条“未知消息类型: PONG”日志）
                    pass
                else:
                    logger.info('connection', f'未知消息类型: {MsgType.name(mt)} · 来自={client_id}')
                    relay_to_group(client_id, group_id, mt, pl)
            except Exception as ex:
                logger.error('connection', f'消息处理异常: {client_id} · type={MsgType.name(mt)} · {ex}')
                logger.info('connection', traceback.format_exc().strip()[:300])
    except Exception as ex:
        logger.error('connection', f'客户端异常: {client_id} · {traceback.format_exc().strip()[:200]}')
    finally:
        clients.unregister(client_id)
        notify_peer_event(group_id, 'peer_offline', client_id)
        logger.info('connection', f'会话结束: {client_id} · 组=[{group_id}]')
        try:
            conn.close()
        except Exception:
            pass


def cleanup_task():
    while running:
        time.sleep(_cfg('stats_interval'))
        try:
            clients.cleanup_stale(max_idle=60)
            flow_shares.cleanup_stale(max_age=86400 * 7)
        except Exception as ex:
            logger.error('cleanup', f'清理任务异常: {ex}')


def stats_reporter():
    while running:
        time.sleep(_cfg('stats_interval'))
        try:
            s = clients.get_stats()
            logger.info('stats', f'在线: {s["total_clients"]} 客户端 · {s["total_groups"]} 组 · 详情={json.dumps(s["groups"])}')
        except Exception as ex:
            logger.error('stats', f'统计异常: {ex}')


_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>yumirror - 服务端面板</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--green:#34d399;--red:#f87171;--yellow:#fbbf24;--purple:#a78bfa;--r:10px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:14px;padding:20px}
h1{font-size:20px;margin-bottom:6px}
.subtitle{color:var(--muted);font-size:11px;margin-bottom:16px}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px;flex:1;min-width:200px}
.card h2{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:6px}
.stat{text-align:center}
.stat .num{font-size:24px;font-weight:700}
.stat .label{font-size:9px;color:var(--muted);text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:11px;margin-top:8px}
th,td{padding:5px 8px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:500;font-size:9px;text-transform:uppercase}
.mono{font-family:monospace;font-size:10px}
.badge{display:inline-block;padding:1px 5px;border-radius:4px;font-size:9px;font-weight:600}
.badge.green{background:#065f46;color:var(--green)}
.badge.red{background:#7f1d1d;color:var(--red)}
.badge.purple{background:#2a1040;color:var(--purple)}
.btn{padding:4px 10px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--text);cursor:pointer;font-size:10px}
.btn:hover{background:#2a3a56}
.empty{text-align:center;padding:16px;color:var(--muted);font-size:11px}
#tick{font-size:10px;color:var(--muted);margin-top:10px}
</style>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="alternate icon" href="/static/favicon.ico">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
</head>
<body>
<h1>🖥 yumirror 服务端面板</h1>
<div class="subtitle" id="info"></div>
<p style="margin-bottom:12px"><a href="/workshop/admin" style="color:var(--accent);font-size:12px">🏭 工坊管理</a></p>
<div class="row">
<div class="card"><h2>📊 实时统计</h2><div class="stat-grid"><div class="stat"><div class="num" style="color:var(--accent)" id="vTotal">0</div><div class="label">在线客户端</div></div><div class="stat"><div class="num" style="color:var(--purple)" id="vGroups">0</div><div class="label">活跃组</div></div><div class="stat"><div class="num" style="color:var(--green)" id="vFlows">0</div><div class="label">共享方案</div></div><div class="stat"><div class="num" style="color:var(--yellow)" id="vUptime">0</div><div class="label">运行时间</div></div></div></div>
</div>
<div class="row">
<div class="card" style="flex:2"><h2>👥 客户端列表</h2><table><thead><tr><th>设备ID</th><th>IP</th><th>组</th><th>文件夹</th><th>在线时长</th></tr></thead><tbody id="tClients"><tr><td colspan="5" class="empty">暂无连接</td></tr></tbody></table></div>
<div class="card" style="flex:1"><h2>👥 组概览</h2><table><thead><tr><th>组名</th><th>在线</th></tr></thead><tbody id="tGroups"><tr><td colspan="2" class="empty">暂无</td></tr></tbody></table></div>
</div>
<div class="row">
<div class="card" style="flex:2"><h2>📁 备份文件</h2><div id="backupsArea"><span style="font-size:10px;color:var(--muted)">选择组查看：</span> <select id="backupGroupSelect" onchange="loadBackups()"><option value="">--</option></select><table style="margin-top:4px"><thead><tr><th>文件</th><th></th></tr></thead><tbody id="tBackups"><tr><td class="empty">请选择组</td></tr></tbody></table></div></div>
<div class="card" style="flex:1"><h2>🧩 共享方案码</h2><table><thead><tr><th>方案码</th></tr></thead><tbody id="tShares"><tr><td class="empty">暂无</td></tr></tbody></table></div>
</div>
<div class="row">
<div class="card" style="flex:1"><h2>⚙ 备份路径</h2><div style="display:flex;gap:6px;margin-top:4px"><input id="bkPath" style="flex:1;background:#0f1823;border:1px solid var(--border);color:var(--text);padding:6px;border-radius:4px;font-size:11px"><button class="btn" onclick="setBkPath()">保存</button></div><div style="font-size:9px;color:var(--muted);margin-top:6px">修改后即时生效，新备份写入新目录</div></div>
<div class="card" style="flex:1"><h2>👥 用户管理</h2>
<div style="display:flex;gap:6px;margin:4px 0;flex-wrap:wrap"><input id="uName" style="flex:1;min-width:80px;background:#0f1823;border:1px solid var(--border);color:var(--text);padding:6px;border-radius:4px;font-size:11px" placeholder="用户名"><input id="uPwd" type="password" style="flex:1;min-width:80px;background:#0f1823;border:1px solid var(--border);color:var(--text);padding:6px;border-radius:4px;font-size:11px" placeholder="密码"><button class="btn" onclick="addUser()">＋ 创建</button></div>
<div id="uDevices" style="display:flex;gap:4px;flex-wrap:wrap;margin:4px 0"></div>
<table><thead><tr><th>用户</th><th>设备</th><th></th></tr></thead><tbody id="tUsers"><tr><td colspan="3" class="empty">—</td></tr></tbody></table></div>
</div>
<div id="tick"></div>
<script>
function $(id){return document.getElementById(id);}
function fm(v){return String(v||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
/* 内联事件里嵌字符串要用它：先转义反斜杠与单引号，再做 HTML 转义 */
function jsq(v){return String(v||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function ago(ts){var d=(Date.now()/1000)-ts;if(d<60)return Math.floor(d)+'秒';if(d<3600)return Math.floor(d/60)+'分钟';if(d<86400)return Math.floor(d/3600)+'小时';return Math.floor(d/86400)+'天';}
function tick(){
 fetch('/api/status').then(r=>r.json()).then(d=>{
  $('info').textContent='服务名: '+fm(d.server)+' · 绑定: '+fm(d.bind)+' · 启动: '+new Date((Date.now()/1000-d.uptime)*1000).toLocaleString('zh-CN');
  $('vTotal').textContent=(d.stats||{}).total_clients||0;
  $('vGroups').textContent=(d.stats||{}).total_groups||0;
  $('vFlows').textContent=d.shared_flows||0;
  var u=d.uptime||0; var h=Math.floor(u/3600); var m=Math.floor((u%3600)/60);
  $('vUptime').textContent=h+'h '+m+'m';
  var sel=$('backupGroupSelect');
  var cv=sel.value;
  sel.innerHTML='<option value="">-- 选择组 --</option>';
  var gs=(d.stats||{}).groups||{};
  Object.keys(gs).forEach(function(g){sel.innerHTML+='<option value="'+fm(g)+'"'+(g===cv?' selected':'')+'>'+fm(g)+' ('+gs[g]+'在线)</option>';});
  if(!Object.keys(gs).length) sel.innerHTML='<option value="">--</option>';
  $('tick').textContent='刷新于: '+new Date().toLocaleTimeString('zh-CN');
 }).catch(function(){});
 fetch('/api/clients').then(r=>r.json()).then(d=>{
  var cs=d.clients||[];
  if(!cs.length){$('tClients').innerHTML='<tr><td colspan="5" class="empty">暂无连接</td></tr>';}
  else $('tClients').innerHTML=cs.map(function(c){return '<tr><td><span class="mono">'+fm(c.client_id)+'</span></td><td class="mono">'+fm(c.addr)+'</td><td><span class="badge purple">'+fm(c.group_id)+'</span></td><td>'+c.folders+'</td><td>'+ago(c.connected_at)+'</td></tr>';}).join('');
  $('tGroups').innerHTML=cs.length?Object.entries(cs.reduce(function(a,c){a[c.group_id]=(a[c.group_id]||0)+1;return a;},{})).map(function(e){return '<tr><td><span class="badge purple">'+fm(e[0])+'</span></td><td>'+e[1]+' 在线</td></tr>';}).join(''):'<tr><td colspan="2" class="empty">暂无</td></tr>';
  $('tShares').innerHTML=(d.shares||[]).length?(d.shares||[]).map(function(s){return '<tr><td><code style="font-size:11px">'+fm(s)+'</code></td></tr>';}).join(''):'<tr><td class="empty">暂无</td></tr>';
 }).catch(function(){});
 fetch('/api/flow-shares').then(r=>r.json()).then(d=>{
  $('tShares').innerHTML=(d.codes||[]).length?(d.codes||[]).map(function(s){return '<tr><td><code style="font-size:11px">'+fm(s)+'</code></td></tr>';}).join(''):'<tr><td class="empty">暂无</td></tr>';
 }).catch(function(){});
 if($('backupGroupSelect').value) loadBackups();
}
function loadBackups(){
 var g=$('backupGroupSelect').value;
 if(!g){$('tBackups').innerHTML='<tr><td class="empty">请选择组</td></tr>';return;}
 $('tBackups').innerHTML='<tr><td class="empty">加载中...</td></tr>';
 fetch('/api/backups/'+encodeURIComponent(g)).then(r=>r.json()).then(d=>{
  var fs=d.files||[];
  $('tBackups').innerHTML=fs.length?fs.map(function(f){return '<tr><td class="mono">'+fm(f)+'</td><td><button class="btn" onclick="delBk(\''+jsq(g)+'\',\''+jsq(f)+'\')">🗑</button></td></tr>';}).join(''):'<tr><td colspan="2" class="empty">无备份文件</td></tr>';
 }).catch(function(){$('tBackups').innerHTML='<tr><td class="empty">加载失败</td></tr>';});
}
function delBk(g,f){if(!confirm('删除备份 '+f+' ?'))return;fetch('/api/admin/backups',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({group:g,path:f})}).then(r=>r.json()).then(d=>{if(d.status==='ok')loadBackups();});}
function loadBkPath(){fetch('/api/admin/backup-path').then(r=>r.json()).then(d=>{$('bkPath').value=d.backup_dir||'';});}
function setBkPath(){var p=$('bkPath').value;if(!p)return;fetch('/api/admin/backup-path',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p})}).then(r=>r.json()).then(d=>{alert(d.status==='ok'?'✅ 已更新备份路径: '+d.backup_dir:(d.msg||'失败'));});}
function loadUsers(){
 fetch('/api/admin/users').then(r=>r.json()).then(d=>{
  var us=d.users||[];
  $('tUsers').innerHTML=us.length?us.map(function(u){return '<tr><td><b>'+fm(u.username)+'</b></td><td class="mono" style="font-size:9px">'+(u.devices||[]).join(', ')+'</td><td><button class="btn" onclick="delUser(\''+jsq(u.username)+'\')">🗑</button></td></tr>';}).join(''):'<tr><td colspan="3" class="empty">暂无用户</td></tr>';
  var box=$('uDevices');box.innerHTML='';
  fetch('/api/clients').then(function(r){return r.json();}).then(function(cd){
   (cd.clients||[]).forEach(function(c){box.innerHTML+='<label style="font-size:10px;display:flex;align-items:center;gap:3px;background:#0f1823;border:1px solid var(--border);padding:2px 6px;border-radius:4px"><input type="checkbox" value="'+fm(c.client_id)+'" style="accent-color:var(--accent)">'+fm(c.client_id)+'</label>';});
  }).catch(function(){});
 }).catch(function(){});
}
function addUser(){
 var n=$('uName').value.trim(),p=$('uPwd').value;
 if(!n||!p){alert('用户名和密码不能为空');return;}
 var devices=[].map.call(document.querySelectorAll('#uDevices input:checked'),function(x){return x.value;});
 fetch('/api/admin/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:n,password:p,devices:devices})}).then(r=>r.json()).then(d=>{if(d.status==='ok'){$('uName').value='';$('uPwd').value='';loadUsers();}else alert(d.msg||'失败');});
}
function delUser(n){if(!confirm('删除用户 '+n+' ?'))return;fetch('/api/admin/users/'+encodeURIComponent(n),{method:'DELETE'}).then(r=>r.json()).then(d=>{if(d.status==='ok')loadUsers();});}
setInterval(tick,5000);
loadBkPath();loadUsers();
tick();
</script>
{{ I18N_JS }}</body>
</html>"""

_WORKSHOP_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>工坊管理 — yumirror 服务端</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--red:#f87171;--green:#34d399}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);padding:24px;min-height:100vh}
h1{font-size:22px;margin-bottom:8px}
a{color:var(--accent);text-decoration:none}
table{width:100%;border-collapse:collapse;margin-top:16px}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);font-size:13px}
th{background:#111827;color:var(--muted)}tr:hover{background:#1a2a40}
.btn{padding:5px 12px;border:1px solid var(--border);border-radius:5px;background:var(--card);color:var(--text);cursor:pointer;font-size:11px}
.btn:hover{background:#2a3a56}.btn.danger{color:var(--red);border-color:var(--red)}.btn.danger:hover{background:#2a1015}
</style>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="alternate icon" href="/static/favicon.ico">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
</head>
<body>
<h1>🏭 工坊管理</h1>
<p style="color:var(--muted)">服务端全局方案库 · <a href="/">← 面板</a></p>
<div style="display:flex;gap:8px;margin:12px 0"><span style="color:var(--muted)" id="count"></span></div>
<table><thead><tr><th>ID</th><th>名称</th><th>作者</th><th>下载</th><th>时间</th><th>操作</th></tr></thead><tbody id="tbody"></tbody></table>
<script>
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function load(){
 fetch('/api/workshop/list').then(r=>r.json()).then(d=>{
  var items=d.items||[];
  document.getElementById('count').textContent='共 '+items.length+' 个方案';
  var tb=document.getElementById('tbody');tb.innerHTML='';
  if(!items.length){tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:30px">暂无方案</td></tr>';return;}
  items.forEach(function(i){
   var ts=new Date((i.created_at||0)*1000).toLocaleString();
   tb.innerHTML+='<tr><td><code>'+esc(i.id)+'</code></td><td><b>'+esc(i.name)+'</b></td><td>'+esc(i.author)+'</td><td>'+(i.downloads||0)+'</td><td>'+ts+'</td><td><button class="btn" onclick="view(\''+esc(i.id)+'\')">👁</button> <button class="btn danger" onclick="del(\''+esc(i.id)+'\')">🗑</button></td></tr>';
  });
 });
}
function view(id){fetch('/api/workshop/download/'+id).then(r=>r.json()).then(d=>{if(d.flow)alert(JSON.stringify(d.flow,null,2));else alert('加载失败');});}
function del(id){if(!confirm('删除 '+id+' ?'))return;fetch('/api/workshop/delete/'+id,{method:'POST'}).then(r=>r.json()).then(d=>{if(d.status==='ok')load();});}
load();
</script>
{{ I18N_JS }}</body>
</html>"""

_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>登录 — yumirror 服务端</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;align-items:center;justify-content:center}
.box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:32px;width:340px;text-align:center}
.logo{font-size:40px;margin-bottom:10px}
h1{font-size:20px;margin-bottom:20px}
input{width:100%;background:#0f1823;border:1px solid var(--border);color:var(--text);padding:10px;border-radius:8px;font-size:14px;margin-bottom:12px}
button{width:100%;background:var(--accent);color:#0f172a;border:none;padding:11px;border-radius:8px;font-weight:700;cursor:pointer}
.err{color:#f87171;font-size:12px;margin-top:10px;min-height:16px}
</style><link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="alternate icon" href="/static/favicon.ico">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
</head>
<body>
<div class="box"><div class="logo">🖥</div><h1>yumirror 服务端</h1>
<div class="sub" style="color:var(--muted);font-size:12px;margin-bottom:14px">账户登录 · 无账户可点下方注册</div>
<input type="text" id="un" placeholder="账户名" onkeydown="if(event.key==='Enter')document.getElementById('pw').focus()">
<input type="password" id="pw" placeholder="密码" onkeydown="if(event.key==='Enter')doLogin()">
<button onclick="doLogin()">登 录</button>
<button style="margin-top:8px;background:transparent;border:1px solid var(--border);color:var(--muted)" onclick="doReg()">＋ 注册新账户</button>
<div class="err" id="err"></div></div>
<script>
function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());}
function doLogin(){post('/api/login',{username:document.getElementById('un').value.trim(),password:document.getElementById('pw').value}).then(d=>{if(d.status==='ok')location.href='/';else document.getElementById('err').textContent=d.msg||'失败';});}
function doReg(){var n=prompt('注册账户名：');if(!n)return;var p=prompt('密码（至少4位）：');if(!p)return;post('/api/register',{username:n,password:p}).then(d=>{if(d.status==='ok')location.href='/';else document.getElementById('err').textContent=d.msg||'注册失败';});}
document.getElementById('un').focus();
</script>
{{ I18N_JS }}</body></html>"""

try:
    from flask import Flask, jsonify, render_template_string, request, session, redirect, url_for
    web_app = Flask(__name__)
    web_app.config['SECRET_KEY'] = os.urandom(16).hex()
    import logging as _log
    _log.getLogger('werkzeug').setLevel(_log.ERROR)

    _login_fails = {}          # {ip: [失败时间戳]}
    LOGIN_MAX_FAILS = 5
    LOGIN_WINDOW = 300         # 5 分钟内失败 5 次即锁定

    @web_app.after_request
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

    @web_app.before_request
    def _guard_admin_routes():
        """管理接口需要登录会话或有效 X-Auth-Token。

        修复：此前 /api/admin/*（改备份路径、增删用户、删备份）完全无需认证，
        局域网内任何人可直接调用。客户端代理使用 X-Auth-Token，故一并放行。
        """
        p = request.path
        # 面板的只读接口也要鉴权：否则局域网内任何人可枚举在线设备、组与备份清单
        protected = (p.startswith('/api/admin/') or p.startswith('/api/clients')
                     or p.startswith('/api/groups') or p.startswith('/api/status')
                     or p.startswith('/api/backups/') or p.startswith('/api/flow-shares'))
        # 共享方案库的写操作必须登录（读操作保持开放：客户端没账户时也要能下载方案）
        if p == '/api/workshop/upload' or p.startswith('/api/workshop/delete/'):
            protected = True
        if not protected:
            return None
        if session.get('user'):
            return None
        token = request.headers.get('X-Auth-Token') or request.args.get('token', '')
        info = _tokens.get(token) if token else None
        if info and info.get('exp', 0) > time.time():
            return None
        if p.startswith('/api/workshop/'):
            return jsonify({'status': 'error',
                            'msg': i18n.t(_cur_lang(), 'err_share_needs_account')}), 401
        return jsonify({'status': 'error', 'msg': i18n.t(_cur_lang(), 'err_not_logged_in')}), 401

    @web_app.route('/favicon.ico')
    def favicon():
        # 浏览器默认会来要 /favicon.ico，直接给静态文件
        return redirect('/static/favicon.ico')

    @web_app.route('/api/i18n')
    def api_i18n():
        """语言包接口：面板前端与第三方都可以用它做本地化"""
        lang = i18n.negotiate(request.headers.get('Accept-Language'),
                              request.args.get('lang') or _cfg('language'))
        return jsonify(dict(status='ok', **i18n.table(lang)))

    @web_app.route('/login')
    def login():
        return render_template_string(_LOGIN_HTML.replace('{{ I18N_JS }}', _i18n_js()))

    def _cur_lang():
        return i18n.negotiate(request.headers.get('Accept-Language') if request else '',
                              _cfg('language'))

    def _i18n_js():
        """注入到面板 HTML 的语言包脚本（前端套用 data-i18n + 浮动语言切换器）"""
        return i18n.frontend_js(i18n.negotiate(request.headers.get('Accept-Language'),
                                               _cfg('language')))

    def _hash_password(pwd, iterations=120000):
        """PBKDF2-HMAC-SHA256（每账户独立随机盐），替代原先的无盐 SHA-256"""
        salt = os.urandom(16).hex()
        dk = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'), bytes.fromhex(salt), iterations).hex()
        return 'pbkdf2$%d$%s$%s' % (iterations, salt, dk)

    def _verify_password(pwd, stored):
        """校验口令；兼容历史的无盐 SHA-256（调用方验证通过后会透明升级）"""
        stored = stored or ''
        if stored.startswith('pbkdf2$'):
            try:
                _, it, salt, dk = stored.split('$', 3)
                calc = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'),
                                           bytes.fromhex(salt), int(it)).hex()
                return hmac.compare_digest(calc, dk)
            except Exception:
                return False
        return hmac.compare_digest(hashlib.sha256(pwd.encode('utf-8')).hexdigest(), stored)

    @web_app.route('/api/login', methods=['POST'])
    def api_login():
        d = request.get_json(force=True) or {}
        uname = (d.get('username') or '').strip()
        pwd = d.get('password') or ''
        ip = request.remote_addr or '?'
        now = time.time()
        recent = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
        if len(recent) >= LOGIN_MAX_FAILS:
            logger.warning('admin', f'登录限速触发: {ip}（{LOGIN_WINDOW // 60} 分钟内失败 {len(recent)} 次）')
            return jsonify({'status': 'error',
                            'msg': i18n.t(_cur_lang(), 'err_too_many_attempts')}), 429
        users = _load_users()
        u = users.get(uname)
        if u and _verify_password(pwd, u.get('password', '')):
            _login_fails.pop(ip, None)
            if not (u.get('password') or '').startswith('pbkdf2$'):
                users[uname]['password'] = _hash_password(pwd)   # 旧哈希透明升级
                _save_users(users)
                logger.info('admin', f'口令哈希已升级为 PBKDF2: {uname}')
            session['user'] = uname
            token = uuid.uuid4().hex[:24]
            _tokens[token] = {'user': uname, 'exp': time.time() + 3600}
            return jsonify({'status': 'ok', 'username': uname, 'token': token,
                            'devices': _user_devices(uname)})
        recent.append(now)
        _login_fails[ip] = recent
        return jsonify({'status': 'error', 'msg': '用户名或密码错误'}), 401

    @web_app.route('/api/register', methods=['POST'])
    def api_register():
        d = request.get_json(force=True) or {}
        uname = (d.get('username') or '').strip()
        pwd = d.get('password') or ''
        if len(uname) < 2:
            return jsonify({'status': 'error', 'msg': '用户名至少 2 个字符'}), 400
        if len(pwd) < 4:
            return jsonify({'status': 'error', 'msg': '密码至少 4 位'}), 400
        users = _load_users()
        if uname in users:
            return jsonify({'status': 'error', 'msg': '用户名已存在'}), 400
        # 自动把发起注册的在线设备绑定到该账户
        devices = []
        rip = request.remote_addr or ''
        with clients._lock:
            for cid, e in clients._clients.items():
                if e.addr and e.addr[0] == rip:
                    devices.append(cid)
        users[uname] = {'password': _hash_password(pwd),
                        'devices': devices, 'created_at': time.time()}
        _save_users(users)
        session['user'] = uname
        token = uuid.uuid4().hex[:24]
        _tokens[token] = {'user': uname, 'exp': time.time() + 3600}
        logger.info('admin', f'注册用户: {uname} devices={devices}')
        return jsonify({'status': 'ok', 'username': uname, 'token': token,
                        'devices': _user_devices(uname)})

    @web_app.route('/api/account/devices')
    def api_account_devices():
        token = request.headers.get('X-Auth-Token') or request.args.get('token', '')
        uname = session.get('user')
        if not uname and token and token in _tokens and _tokens[token]['exp'] > time.time():
            uname = _tokens[token]['user']
        if not uname:
            return jsonify({'status': 'error', 'msg': '未登录'}), 401
        return jsonify({'status': 'ok', 'username': uname, 'devices': _user_devices(uname)})

    def _user_devices(uname):
        """返回账户名下设备及其在线状态"""
        users = _load_users()
        dev_ids = users.get(uname, {}).get('devices', []) or []
        online = {}
        with clients._lock:
            for cid, e in clients._clients.items():
                online[cid] = {'online': True, 'addr': f"{e.addr[0]}:{e.addr[1]}", 'group': e.group_id}
        out = []
        for dev in dev_ids:
            info = online.get(dev)
            out.append({'id': dev, 'online': bool(info),
                        'addr': info['addr'] if info else '',
                        'group': info['group'] if info else ''})
        return out

    @web_app.route('/api/logout', methods=['POST'])
    def api_logout():
        session.pop('authed', None)
        return jsonify({'status': 'ok'})

    # ── 用户与设备管理 ──
    USERS_DB_PATH = _cfg('users_db_path')
    _users_lock = threading.Lock()
    _tokens = {}

    def _load_users():
        try:
            if os.path.isfile(USERS_DB_PATH):
                with open(USERS_DB_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f) or {}
        except Exception:
            pass
        return {}

    def _save_users(users):
        with _users_lock:
            try:
                with open(USERS_DB_PATH, 'w', encoding='utf-8') as f:
                    json.dump(users, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    @web_app.route('/api/admin/users')
    def admin_users_list():
        users = _load_users()
        return jsonify({'status': 'ok', 'users': [{'username': u, 'devices': d.get('devices', []),
                                                   'created_at': d.get('created_at', 0)} for u, d in users.items()]})

    @web_app.route('/api/admin/users', methods=['POST'])
    def admin_users_create():
        d = request.get_json(force=True) or {}
        uname = (d.get('username') or '').strip()
        pwd = d.get('password') or ''
        if not uname or not pwd:
            return jsonify({'status': 'error', 'msg': '用户名和密码不能为空'}), 400
        users = _load_users()
        if uname in users:
            return jsonify({'status': 'error', 'msg': '用户已存在'}), 400
        users[uname] = {'password': _hash_password(pwd),
                        'devices': d.get('devices', []), 'created_at': time.time()}
        _save_users(users)
        logger.info('admin', f'创建用户: {uname}')
        return jsonify({'status': 'ok'})

    @web_app.route('/api/admin/users/<uname>', methods=['DELETE'])
    def admin_users_delete(uname):
        users = _load_users()
        if uname in users:
            del users[uname]
            _save_users(users)
            logger.info('admin', f'删除用户: {uname}')
        return jsonify({'status': 'ok'})

    @web_app.route('/api/admin/users/<uname>/devices', methods=['POST'])
    def admin_users_devices(uname):
        d = request.get_json(force=True) or {}
        users = _load_users()
        if uname not in users:
            return jsonify({'status': 'error', 'msg': '用户不存在'}), 404
        users[uname]['devices'] = d.get('devices', [])
        _save_users(users)
        return jsonify({'status': 'ok'})

    # ── 备份管理 ──
    @web_app.route('/api/admin/backups')
    def admin_backups_list():
        groups = {}
        bd = BACKUP_DIR
        if os.path.isdir(bd):
            for g in sorted(os.listdir(bd)):
                gp = os.path.join(bd, g)
                if os.path.isdir(gp):
                    groups[g] = backups.list_group_backups(g)
        return jsonify({'status': 'ok', 'backup_dir': bd, 'groups': groups})

    @web_app.route('/api/admin/backups', methods=['DELETE'])
    def admin_backups_delete():
        d = request.get_json(force=True) or {}
        gid, path = d.get('group', ''), d.get('path', '')
        if not gid or not path:
            return jsonify({'status': 'error', 'msg': '缺少参数'}), 400
        try:
            full = backups._safe(gid, path)
            if os.path.isfile(full):
                os.remove(full)
                logger.info('admin', f'删除备份: 组={gid} 文件={path}')
            return jsonify({'status': 'ok'})
        except Exception as e:
            return jsonify({'status': 'error', 'msg': str(e)}), 500

    @web_app.route('/api/admin/backup-path', methods=['GET', 'POST'])
    def admin_backup_path():
        global backups, BACKUP_DIR
        if request.method == 'POST':
            d = request.get_json(force=True) or {}
            p = (d.get('path') or '').strip()
            if not p:
                return jsonify({'status': 'error', 'msg': '路径不能为空'}), 400
            _file_cfg['backup_dir'] = p
            save_config(_file_cfg)
            BACKUP_DIR = os.path.abspath(p)
            backups = BackupManager(BACKUP_DIR, _cfg('max_versions_per_file'))
            logger.info('admin', f'备份路径已修改: {BACKUP_DIR}')
            return jsonify({'status': 'ok', 'backup_dir': BACKUP_DIR})
        return jsonify({'status': 'ok', 'backup_dir': BACKUP_DIR})

    @web_app.route('/')
    def index():
        # 未登录先跳登录页，避免面板空转（管理接口已要求认证）
        if not session.get('user'):
            return redirect('/login')
        return render_template_string(_HTML.replace('{{ I18N_JS }}', _i18n_js()))

    @web_app.route('/api/status')
    def api_status():
        s = clients.get_stats()
        return jsonify({'server': _cfg('server_name'), 'bind': f"{_cfg('bind_host')}:{_cfg('bind_port')}", 'uptime': time.time() - startup_time, 'stats': s, 'shared_flows': len(flow_shares.list_codes())})

    @web_app.route('/api/clients')
    def api_clients():
        with clients._lock:
            r = [{'client_id': cid, 'group_id': e.group_id, 'addr': f"{e.addr[0]}:{e.addr[1]}", 'connected_at': e.connected_at, 'last_seen': e.last_seen, 'folders': len(e.sync_folders)} for cid, e in clients._clients.items()]
        return jsonify({'clients': r, 'total': len(r)})

    @web_app.route('/api/groups')
    def api_groups():
        return jsonify(clients.get_stats())

    @web_app.route('/api/backups/<group_id>')
    def api_backups(group_id):
        f = backups.list_group_backups(group_id)
        return jsonify({'group': group_id, 'files': f, 'count': len(f)})

    @web_app.route('/api/flow-shares')
    def api_flow_shares():
        c = flow_shares.list_codes()
        return jsonify({'codes': c, 'count': len(c)})

    # ── 工坊 API（全局共享方案库） ──
    @web_app.route('/api/workshop/list')
    def server_workshop_list():
        q = (request.args.get('q') or '').strip().lower()
        sort = request.args.get('sort', 'newest')
        items = list(workshop_db.values())
        if q:
            items = [i for i in items if q in i.get('name', '').lower()
                     or q in i.get('description', '').lower()
                     or any(q in t.lower() for t in i.get('tags', []))]
        if sort == 'popular':
            items.sort(key=lambda x: -(x.get('downloads', 0) or 0))
        elif sort == 'name':
            items.sort(key=lambda x: (x.get('name') or '').lower())
        else:
            items.sort(key=lambda x: -(x.get('created_at', 0) or 0))
        return jsonify({'status': 'ok', 'items': items})

    @web_app.route('/api/workshop/upload', methods=['POST'])
    def server_workshop_upload():
        data = request.get_json(force=True) or {}
        name = (data.get('name') or '').strip()
        flow = data.get('flow')
        if not name or not flow:
            return jsonify({'status': 'error', 'msg': '名称和流程不能为空'}), 400
        fid = hashlib.md5(f'{name}{time.time()}'.encode()).hexdigest()[:8]
        while fid in workshop_db:
            fid = hashlib.md5(f'{name}{time.time()}{uuid.uuid4()}'.encode()).hexdigest()[:8]
        workshop_db[fid] = {
            'id': fid, 'name': name,
            'author': (data.get('author') or '匿名').strip() or '匿名',
            'description': (data.get('description') or '').strip(),
            'tags': data.get('tags', []), 'flow': flow,
            'created_at': time.time(), 'downloads': 0,
        }
        _save_workshop()
        logger.info('workshop', f'工坊上传: 名称="{name}" · id={fid}')
        return jsonify({'status': 'ok', 'id': fid})

    @web_app.route('/api/workshop/download/<fid>')
    def server_workshop_download(fid):
        item = workshop_db.get(fid)
        if not item:
            return jsonify({'status': 'error', 'msg': '未找到'}), 404
        item['downloads'] = (item.get('downloads', 0) or 0) + 1
        _save_workshop()
        return jsonify({'status': 'ok', 'flow': item['flow']})

    @web_app.route('/api/workshop/delete/<fid>', methods=['POST'])
    def server_workshop_delete(fid):
        if fid in workshop_db:
            name = workshop_db[fid].get('name', fid)
            del workshop_db[fid]
            _save_workshop()
            logger.info('workshop', f'工坊删除: 名称="{name}" · id={fid}')
        return jsonify({'status': 'ok'})

    @web_app.route('/workshop/admin')
    def workshop_admin():
        return render_template_string(_WORKSHOP_ADMIN_HTML.replace('{{ I18N_JS }}', _i18n_js()))

    HAS_WEB = True
except ImportError:
    HAS_WEB = False


running = True
startup_time = time.time()


def main():
    global running
    host = _cfg('bind_host')
    port = int(_cfg('bind_port'))
    name = _cfg('server_name')

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.bind((host, port))
    global TLS_CTX, CERT_PATH, KEY_PATH, TLS_FINGERPRINT
    _tls = _cfg('tls') or {}
    if _tls.get('enabled', True):
        if not tls_util.crypto_available():
            logger.error('tls', '已启用 TLS 但缺少 cryptography —— 拒绝以明文方式启动。')
            logger.error('tls', '  修复：pip install cryptography   或   在 config.json 里显式设 "tls": {"enabled": false}（不推荐）')
            sys.exit(2)
        CERT_PATH = _tls.get('cert_file') or os.path.join(BASE_DIR, 'certs', 'server.crt')
        KEY_PATH = _tls.get('key_file') or os.path.join(BASE_DIR, 'certs', 'server.key')
        if not (os.path.isfile(CERT_PATH) and os.path.isfile(KEY_PATH)):
            TLS_FINGERPRINT = tls_util.generate_self_signed(
                CERT_PATH, KEY_PATH, common_name=_cfg('server_name') or 'yumirror',
                extra_ips=('127.0.0.1',), extra_dns=('localhost',))
            logger.info('tls', f'首次启动已生成自签证书: {CERT_PATH}')
        else:
            TLS_FINGERPRINT = tls_util.fingerprint_of_cert_file(CERT_PATH)
        TLS_CTX = tls_util.server_context(CERT_PATH, KEY_PATH)
        print(f'\n  🔐 传输加密: 已启用 (TLS 1.2+)   证书指纹 SHA256:')
        print(f'     {TLS_FINGERPRINT}')
        print(f'     客户端首次连接会自动固定该指纹；换证书后需要在客户端更新 tls.fingerprint\n')
    else:
        TLS_CTX = None
        logger.warning('tls', '⚠ 传输加密已关闭：文件内容与元数据在局域网内是明文（仅建议调试用）')
        print('\n  ⚠ 传输加密: 已关闭（明文）—— 生产环境请保持 tls.enabled = true\n')

    sock.listen(128)
    sock.settimeout(2)

    print(f"""
╔══════════════════════════════════════════════════════╗
║   🖥  yumirror v3 - 服务端 (Backup Framework)       ║
║  服务名: {name:<42}║
║  绑定:   {host}:{port:<36}║
║  面板:   http://{_cfg('web_host')}:{_cfg('web_port'):<27}║
║  备份:   {BACKUP_DIR:<42}║
║  方案:   {FLOWS_SHARE_DIR:<42}║
║  日志:   {LOGS_DIR:<42}║
╚══════════════════════════════════════════════════════╝
""")

    logger.info('server', '══ 服务端启动 ══')
    logger.info('server', f'服务名={name} · 绑定={host}:{port}')
    logger.info('server', f'备份目录={BACKUP_DIR} · 方案目录={FLOWS_SHARE_DIR}')

    threading.Thread(target=cleanup_task, daemon=True, name='cleanup').start()
    threading.Thread(target=stats_reporter, daemon=True, name='stats').start()

    if HAS_WEB:
        def web_thread():
            wh = _cfg('web_host')
            wp = int(_cfg('web_port'))
            _ptls = _cfg('panel_tls') or {}
            _ssl_ctx = None
            if _ptls.get('enabled'):
                _ssl_ctx = (CERT_PATH, KEY_PATH) if CERT_PATH else (_ptls.get('cert_file'), _ptls.get('key_file'))
            logger.info('server', f'Web 管理面板: {"https" if _ssl_ctx else "http"}://{wh}:{wp}')
            try:
                web_app.run(host=wh, port=wp, debug=False, use_reloader=False, ssl_context=_ssl_ctx)
            except Exception as ex:
                logger.error('server', f'Web 启动失败: {ex}')
        threading.Thread(target=web_thread, daemon=True, name='web').start()

    logger.info('server', '开始监听连接...')

    while running:
        try:
            conn_sock, addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            if not running:
                break
            continue
        if TLS_CTX is not None:
            try:
                conn_sock = tls_util.wrap_server(conn_sock, TLS_CTX)
            except Exception as e:
                # 端口探测、错误的客户端、中间人探测都会走到这里：记为 INFO 级，不刷栈
                logger.info('connection', f'TLS 握手未完成: {addr[0]}:{addr[1]} · {type(e).__name__}')
                try:
                    conn_sock.close()
                except Exception:
                    pass
                continue
        conn = Connection(conn_sock)
        logger.info('connection', f'新连接: {addr[0]}:{addr[1]}' + (' · TLS' if TLS_CTX is not None else ' · 明文'))
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True, name=f'client-{addr[1]}').start()

    logger.info('server', '══ 服务端关闭 ══')
    sock.close()


def sig_handler(s, f):
    global running
    print(f"\n🛑 退出...")
    running = False


if __name__ == '__main__':
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    main()

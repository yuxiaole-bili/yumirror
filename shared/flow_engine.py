#!/usr/bin/env python3
"""
yumirror v3 — 流程引擎（多车道屏障并行）
"""
import os, re, time, json, fnmatch, threading, subprocess, hashlib, shutil, zipfile, csv, operator, base64
import urllib.request, urllib.error
from typing import Any, Callable, Dict, List, Optional


# ============================================================
class BlockRegistry:
    _blocks: Dict[str, dict] = {}

    @classmethod
    def register(cls, tn, label, icon='📦', schema=None, desc=''):
        cls._blocks[tn] = {'type': tn, 'label': label, 'icon': icon,
                            'config_schema': schema or {}, 'description': desc}

    @classmethod
    def get(cls, tn):
        return cls._blocks.get(tn)

    @classmethod
    def all(cls):
        return dict(cls._blocks)


_ENTRIES = [
    ('set_var',         '设置变量',     '📌', {'var_name': {'type': 'string', 'default': ''}, 'value': {'type': 'text', 'default': ''}}, ''),
    ('compare',         '比较判断',     '⚖', {'left': {'type': 'string', 'default': ''}, 'op': {'type': 'select', 'options': ['==', '!=', '>', '<', '>=', '<=', 'contains', 'not_contains', 'matches'], 'default': '=='}, 'right': {'type': 'string', 'default': ''}}, ''),
    ('branch',          '分支判断',     '🔀', {'var': {'type': 'string', 'default': '_compare_result'}}, '按变量真假执行不同子步骤'),
    ('scan_dir',        '扫描目录',     '📂', {'folder': {'type': 'string', 'default': ''}, 'pattern': {'type': 'string', 'default': '*'}, 'recursive': {'type': 'bool', 'default': True}, 'result_var': {'type': 'string', 'default': '_scan_result'}}, ''),
    ('get_file_hash',   '获取文件哈希', '🔑', {'file_path': {'type': 'string', 'default': ''}, 'result_var': {'type': 'string', 'default': '_hash'}}, ''),
    ('get_server_hash', '获取服务端哈希','☁', {'file_path': {'type': 'string', 'default': ''}, 'result_var': {'type': 'string', 'default': '_server_hash'}, 'timeout': {'type': 'number', 'default': 10}}, ''),
    ('get_peer_hash',   '获取同伴哈希', '👥', {'peer_id': {'type': 'string', 'default': ''}, 'file_path': {'type': 'string', 'default': ''}, 'result_var': {'type': 'string', 'default': '_peer_hash'}, 'timeout': {'type': 'number', 'default': 10}}, ''),
    ('get_path_info',   '获取路径信息', '📋', {'source': {'type': 'select', 'options': ['local', 'server', 'peer'], 'default': 'local'}, 'peer_id': {'type': 'string', 'default': ''}, 'file_path': {'type': 'string', 'default': ''}, 'info_type': {'type': 'select', 'options': ['exists', 'size', 'mtime', 'hash', 'count', 'is_file', 'is_dir'], 'default': 'exists'}, 'result_var': {'type': 'string', 'default': '_path_info'}}, ''),
    ('log',             '记录日志',     '📋', {'message': {'type': 'text', 'default': ''}}, ''),
    ('sleep',           '等待延时',     '⏱', {'seconds': {'type': 'number', 'default': 1}}, ''),
    ('http_request',    'HTTP 请求',   '🌐', {'url': {'type': 'string', 'default': ''}, 'method': {'type': 'select', 'options': ['GET', 'POST', 'PUT', 'DELETE'], 'default': 'GET'}, 'headers': {'type': 'text', 'default': '{}'}, 'body': {'type': 'text', 'default': ''}, 'result_var': {'type': 'string', 'default': '_http_result'}, 'timeout': {'type': 'number', 'default': 30}}, ''),
    ('run_command',     '执行命令',     '⚙', {'command': {'type': 'string', 'default': ''}, 'result_var': {'type': 'string', 'default': '_cmd_result'}, 'timeout': {'type': 'number', 'default': 30}}, ''),
    ('write_file',      '写入文件',     '💾', {'file_path': {'type': 'string', 'default': ''}, 'content': {'type': 'text', 'default': ''}, 'mode': {'type': 'select', 'options': ['overwrite', 'append'], 'default': 'overwrite'}}, ''),
    ('delete_file',     '删除文件',     '🗑', {'path': {'type': 'string', 'default': ''}}, '删除本地文件/空目录（在同步目录内安全执行）'),
    ('mirror_from_peer','从同伴镜像',   '🪞', {'peer_id': {'type': 'string', 'default': ''}, 'folder': {'type': 'string', 'default': ''}}, '请求指定同伴与我互相重发快照，实现该目录双向镜像'),
    ('backup_to_server','备份到服务端', '☁', {'file_path': {'type': 'string', 'default': ''}}, '校验文件是否已在服务端留备份（服务端随同步自动落盘）'),
    ('upload_to_server','上传备份到服务端','⬆', {'file_path': {'type': 'string', 'default': '{{_full_path}}'}, 'remote_path': {'type': 'string', 'default': ''}, 'result_var': {'type': 'string', 'default': '_upload_ok'}}, '真正把文件推送到服务端备份目录（不只是查哈希）'),
    ('verify_backup',   '校验备份一致', '🧾', {'file_path': {'type': 'string', 'default': '{{_full_path}}'}, 'remote_path': {'type': 'string', 'default': ''}, 'result_var': {'type': 'string', 'default': '_verify_ok'}}, '本地 SHA256 与服务端备份比对，结果写入 _verify_ok'),
    ('copy_file',       '复制/归档文件','📄', {'src': {'type': 'string', 'default': '{{_full_path}}'}, 'dst_dir': {'type': 'string', 'default': ''}, 'overwrite': {'type': 'bool', 'default': True}, 'result_var': {'type': 'string', 'default': '_copy_ok'}}, '把文件复制到归档目录，可用 @folder:名称 定位'),
    ('archive_zip',     '打包为 ZIP',   '🗜', {'src': {'type': 'string', 'default': '{{_full_path}}'}, 'zip_path': {'type': 'string', 'default': ''}, 'result_var': {'type': 'string', 'default': '_zip_path'}}, '把文件或目录打包进 zip（目标已存在则追加）'),
    ('cleanup_old',     '清理过期文件', '🧹', {'dir': {'type': 'string', 'default': ''}, 'pattern': {'type': 'string', 'default': '*'}, 'days': {'type': 'number', 'default': 30}, 'dry_run': {'type': 'bool', 'default': True}, 'result_var': {'type': 'string', 'default': '_cleanup_count'}}, '删除 N 天前的过期文件；dry_run 默认开启，只列不删'),
    ('foreach',         '遍历列表',     '🔁', {'list_var': {'type': 'string', 'default': '_scan_result'}, 'item_var': {'type': 'string', 'default': '_item'}, 'index_var': {'type': 'string', 'default': '_index'}, 'steps': {'type': 'steps', 'default': []}, 'max_items': {'type': 'number', 'default': 500}}, '对列表变量逐项执行子步骤（配合“扫描目录”使用）'),
    ('manifest',        '生成备份清单', '📑', {'dir': {'type': 'string', 'default': ''}, 'pattern': {'type': 'string', 'default': '*'}, 'out_path': {'type': 'string', 'default': ''}, 'format': {'type': 'select', 'options': ['json', 'csv', 'md'], 'default': 'json'}, 'result_var': {'type': 'string', 'default': '_manifest_path'}}, '扫描目录生成清单（文件名/大小/时间/SHA256）'),
    ('encrypt_file',    '加密文件',     '🔐', {'file_path': {'type': 'string', 'default': '{{_full_path}}'}, 'passphrase_secret': {'type': 'string', 'default': 'backup_passphrase'}, 'passphrase': {'type': 'string', 'default': ''}, 'key_file': {'type': 'string', 'default': '{{_backup_key_file}}'}, 'out_path': {'type': 'string', 'default': ''}, 'delete_source': {'type': 'bool', 'default': False}, 'result_var': {'type': 'string', 'default': '_enc_path'}}, 'AES-256-GCM 分块加密，口令或密钥文件二选一；输出 .enc'),
    ('decrypt_file',    '解密文件',     '🔓', {'file_path': {'type': 'string', 'default': '{{_full_path}}'}, 'passphrase_secret': {'type': 'string', 'default': 'backup_passphrase'}, 'passphrase': {'type': 'string', 'default': ''}, 'key_file': {'type': 'string', 'default': '{{_backup_key_file}}'}, 'out_path': {'type': 'string', 'default': ''}, 'delete_source': {'type': 'bool', 'default': False}, 'result_var': {'type': 'string', 'default': '_dec_path'}}, '解密 .enc 文件（篡改会直接报错）'),
    ('make_key_file',   '生成密钥文件', '🗝', {'path': {'type': 'string', 'default': ''}, 'overwrite': {'type': 'bool', 'default': False}, 'result_var': {'type': 'string', 'default': '_key_file'}}, '生成 32 字节随机密钥文件（0600 权限）'),
    ('list_versions',   '列出版本',     '🕘', {'file_path': {'type': 'string', 'default': '{{_relpath}}'}, 'result_var': {'type': 'string', 'default': '_versions'}}, '列出服务端该文件的历史版本与当前版本'),
    ('snapshot_version','打版本点',     '🔖', {'file_path': {'type': 'string', 'default': '{{_relpath}}'}, 'reason': {'type': 'string', 'default': 'flow'}, 'result_var': {'type': 'string', 'default': '_version_id'}}, '把服务端当前备份归档为历史版本（内容未变也留点）'),
    ('restore_version', '服务端回滚',   '⏪', {'file_path': {'type': 'string', 'default': '{{_relpath}}'}, 'version': {'type': 'string', 'default': 'latest'}, 'result_var': {'type': 'string', 'default': '_restore_ok'}}, '回滚到指定版本：latest / latest-2 / 具体版本号；当前内容会先留档，可撤销'),
    ('fetch_version',   '取回旧版本',   '📥', {'file_path': {'type': 'string', 'default': '{{_relpath}}'}, 'version': {'type': 'string', 'default': 'latest'}, 'out_path': {'type': 'string', 'default': ''}, 'result_var': {'type': 'string', 'default': '_fetched_path'}}, '把服务端历史版本取回到本地（默认写客户端 restored/ 目录），并校验 SHA256'),
]
for _e in _ENTRIES:
    BlockRegistry.register(*_e)


# ============================================================
class FlowContext:
    def __init__(self, event_type='', source_device='', file_path='', relpath='', **kw):
        self.event_type = event_type
        self.source_device = source_device
        self.file_path = file_path
        self.relpath = relpath
        self.vars: Dict[str, Any] = {}
        self.logs: List[str] = []
        self.step_index = 0
        self._jump_to: Optional[int] = None
        self._kw = kw
        self.request_peer_hash: Optional[Callable] = None
        self.request_server_backup_hash: Optional[Callable] = None
        self.request_peer_path_info: Optional[Callable] = None
        self.request_server_path_info: Optional[Callable] = None
        self.request_mirror_from_peer: Optional[Callable] = None
        self.upload_file_to_server: Optional[Callable] = None
        self.version_list: Optional[Callable] = None
        self.version_op: Optional[Callable] = None
        self.secret_resolver: Optional[Callable] = None
        self.folder_resolver: Optional[Callable] = None
        # 「执行命令」积木默认禁用：无鉴权的客户端面板 + 可执行命令 = 远程命令执行
        self.allow_command: bool = False
        self.vars['_event_type'] = event_type
        self.vars['_source_device'] = source_device
        self.vars['_file_path'] = file_path
        self.vars['_relpath'] = relpath
        self.vars['_timestamp'] = int(time.time())
        for k, v in kw.items():
            if not k.startswith('_'):
                self.vars[f'_{k}'] = v
        # 时间戳统一取整数秒：否则模板里 {{_timestamp}} 会渲染成 "1791120372.9158776"，
        # 直接进文件名（演示中实际出现过）
        try:
            self.vars['_timestamp'] = int(float(self.vars.get('_timestamp') or time.time()))
        except Exception:
            self.vars['_timestamp'] = int(time.time())

    def resolve(self, text):
        def _r(m):
            return str(self.vars.get(m.group(1).strip(), m.group(0)))
        s = re.sub(r'\{\{([^}]+)\}\}', _r, str(text) if text else '')
        if '@folder:' in s and self.folder_resolver:
            s = re.sub(r'@folder:([^/\s]+)([^\s]*)',
                       lambda m: self.folder_resolver(m.group(1)) + (m.group(2) or ''), s)
        return s

    def secret(self, name, default=''):
        """按名字取密钥（口令等）：走回调，绝不进 vars，避免被接口/日志回显"""
        if not name:
            return default
        if not self.secret_resolver:
            return default
        try:
            return self.secret_resolver(name) or default
        except Exception:
            return default

    def add_log(self, msg):
        s = self.resolve(msg)
        self.logs.append(s)
        return s

    def clone(self):
        c = FlowContext(self.event_type, self.source_device, self.file_path, self.relpath)
        c.vars = dict(self.vars)
        c.request_peer_hash = self.request_peer_hash
        c.request_server_backup_hash = self.request_server_backup_hash
        c.request_peer_path_info = self.request_peer_path_info
        c.request_server_path_info = self.request_server_path_info
        c.request_mirror_from_peer = self.request_mirror_from_peer
        c.upload_file_to_server = self.upload_file_to_server
        c.version_list = self.version_list
        c.version_op = self.version_op
        c.secret_resolver = self.secret_resolver
        c.folder_resolver = self.folder_resolver
        c.allow_command = self.allow_command
        return c


# ============================================================
class FlowEngine:
    def __init__(self, flow_def: dict):
        self.name = flow_def.get('name', 'Untitled')
        self.steps: List[dict] = flow_def.get('steps', [])
        self.lanes: List[dict] = flow_def.get('queues') or flow_def.get('lanes', [])
        self.merge_at: List[int] = flow_def.get('merge_at', [])
        self.tail: List[dict] = flow_def.get('tail', [])
        self.desc = flow_def.get('description', '')

    def execute(self, ctx: FlowContext) -> FlowContext:
        if self.lanes:
            return self._execute_lanes(ctx)
        else:
            return self._run_steps(ctx, self.steps)

    def _execute_lanes(self, ctx: FlowContext) -> FlowContext:
        lanes = self.lanes
        merge_at = set(self.merge_at or [])
        max_len = max((len(l.get('steps', [])) for l in lanes), default=0)
        ctx.add_log('══ {} 队列并行 ── 最多 {} 步 ══'.format(len(lanes), max_len))
        for li, l in enumerate(lanes):
            ctx.add_log('  队列 [{}]: {} 步'.format(
                l.get('label', l.get('id', 'L{}'.format(li + 1))),
                len(l.get('steps', []))))

        for step_idx in range(max_len):
            step_num = step_idx + 1
            is_merge = step_num in merge_at
            tag = ' 🔒合并' if is_merge else ''
            ctx.add_log('── 步骤 {}{} ──'.format(step_num, tag))

            results = {}
            threads = []
            lock = threading.Lock()

            def _exec_lane_step(lane, li, si):
                lane_steps = lane.get('steps', [])
                if si >= len(lane_steps):
                    with lock:
                        results[li] = {
                            'label': lane.get('label', ''),
                            'logs': [],
                            'vars': {},
                            'skipped': True,
                        }
                    return
                step = lane_steps[si]
                sub = ctx.clone()
                try:
                    self._run_steps(sub, [step])
                    with lock:
                        results[li] = {
                            'label': lane.get('label', ''),
                            'logs': list(sub.logs),
                            'vars': dict(sub.vars),
                            'skipped': False,
                        }
                except Exception as e:
                    with lock:
                        results[li] = {
                            'label': lane.get('label', ''),
                            'logs': sub.logs,
                            'vars': {},
                            'skipped': False,
                            'error': str(e),
                        }

            for li, lane in enumerate(lanes):
                t = threading.Thread(target=_exec_lane_step, args=(lane, li, step_idx), daemon=True)
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            for li in sorted(results.keys()):
                r = results[li]
                prefix = '  {}.{} [{}]'.format(li + 1, step_num, r['label'])
                if r.get('skipped'):
                    ctx.add_log(prefix + ' ⏸ 等待')
                elif r.get('error'):
                    ctx.add_log(prefix + ' ❌ ' + r['error'])
                else:
                    ctx.add_log(prefix + ' →')
                    for m in r['logs']:
                        ctx.add_log('    ' + m)
                ctx.vars.update(r['vars'])

        ctx.add_log('══ 所有队列完成 ══')
        if self.tail:
            ctx.add_log('── 尾步（汇总） ──')
            self._run_steps(ctx, self.tail)
        return ctx

    def _run_steps(self, ctx: FlowContext, steps: List[dict]) -> FlowContext:
        ctx.step_index = 0
        total = len(steps)
        while 0 <= ctx.step_index < total:
            step = steps[ctx.step_index]
            st = step.get('type', '')
            lbl = step.get('label', step.get('id', '步骤{}'.format(ctx.step_index + 1)))
            try:
                h = getattr(self, '_exec_{}'.format(st), None)
                if h:
                    h(ctx, step.get('config', {}))
                else:
                    ctx.add_log('⚠ 未知类型: ' + st)
            except Exception as e:
                ctx.add_log('❌ [{}] 异常: {}'.format(lbl, e))
            ctx.step_index += 1
        return ctx

    # ── 步骤执行器 ──
    def _exec_set_var(self, ctx, cfg):
        n = (cfg.get('var_name') or '').strip()
        v = ctx.resolve(cfg.get('value', ''))
        if n:
            ctx.vars[n] = v
            ctx.add_log('📌 {} = {}'.format(n, str(v)[:80]))

    def _exec_compare(self, ctx, cfg):
        L = ctx.resolve(cfg.get('left', ''))
        op = cfg.get('op', '==')
        R = ctx.resolve(cfg.get('right', ''))
        try:
            lv = float(L) if L.replace('.', '', 1).lstrip('-').isdigit() else L
            rv = float(R) if R.replace('.', '', 1).lstrip('-').isdigit() else R
        except Exception:
            lv, rv = L, R
        if op in ('==', '!=', '>', '<', '>=', '<='):
            # 用 operator 显式比较，替代原先的 eval（避免字符串注入，也避免异常）
            _ops = {'==': operator.eq, '!=': operator.ne, '>': operator.gt,
                    '<': operator.lt, '>=': operator.ge, '<=': operator.le}
            try:
                result = _ops[op](lv, rv)
            except TypeError:
                result = _ops[op](str(lv), str(rv))
        elif op == 'contains':
            result = str(rv) in str(lv)
        elif op == 'not_contains':
            result = str(rv) not in str(lv)
        elif op == 'matches':
            result = bool(fnmatch.fnmatch(str(lv), str(rv)))
        else:
            result = False
        rv = cfg.get('result_var', '_compare_result') or '_compare_result'
        ctx.vars[rv] = result
        ctx.add_log('⚖ {} {} {} = {}'.format(str(L)[:30], op, str(R)[:30], result))

    def _exec_branch(self, ctx, cfg):
        var = cfg.get('var', '_compare_result')
        cond = bool(ctx.vars.get(var))
        steps = cfg.get('true_steps' if cond else 'false_steps', []) or []
        ctx.add_log('🔀 分支判断: {}={} → {}'.format(var, cond, '真分支' if cond else '假分支'))
        if steps:
            sub = ctx.clone()
            self._run_steps(sub, steps)
            ctx.vars.update(sub.vars)
            ctx.logs.extend(sub.logs)

    def _exec_scan_dir(self, ctx, cfg):
        folder = ctx.resolve(cfg.get('folder', ''))
        pat = ctx.resolve(cfg.get('pattern', '*'))
        rec = cfg.get('recursive', True)
        rv = cfg.get('result_var', '_scan_result') or '_scan_result'
        base = folder if folder and os.path.isdir(folder) else (
            ctx._kw.get('full_path') or ctx.vars.get('_full_path', '') or '')
        ctx.add_log('📂 扫描: ' + (base or folder or '?'))
        if not base or not os.path.isdir(base):
            ctx.vars[rv] = []
            ctx.add_log('   ⚠ 目录不存在: ' + (base or '?'))
            return
        ctx.vars['_scan_base'] = base
        try:
            files = []
            walk = os.walk if rec else lambda p: [(p, [], os.listdir(p))]
            for dp, _, fns in walk(base):
                for fn in fns:
                    if fnmatch.fnmatch(fn, pat):
                        files.append(os.path.relpath(os.path.join(dp, fn), base))
            ctx.vars[rv] = files
            ctx.vars['_scan_count'] = len(files)
            ctx.add_log('   ✅ {} 个文件'.format(len(files)))
        except Exception as e:
            ctx.vars[rv] = []
            ctx.add_log('   ❌ ' + str(e))

    def _exec_get_file_hash(self, ctx, cfg):
        fp = ctx.resolve(cfg.get('file_path', '')) or ctx.vars.get('_full_path', '')
        rv = cfg.get('result_var', '_hash') or '_hash'
        if not fp or not os.path.isfile(fp):
            ctx.vars[rv] = ''
            ctx.add_log('🔑 文件不存在: ' + fp)
            return
        try:
            h = hashlib.sha256()
            with open(fp, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
            sha = h.hexdigest()
            ctx.vars[rv] = sha
            ctx.add_log('🔑 本地哈希: {}...'.format(sha[:16]))
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('❌ ' + str(e))

    def _exec_get_server_hash(self, ctx, cfg):
        fp = ctx.resolve(cfg.get('file_path', '')) or ctx.vars.get('_relpath', '')
        rv = cfg.get('result_var', '_server_hash') or '_server_hash'
        ctx.add_log('☁ 服务端哈希: ' + fp)
        if not ctx.request_server_backup_hash:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 未注入')
            return
        try:
            sha = ctx.request_server_backup_hash(fp)
            ctx.vars[rv] = sha or ''
            ctx.add_log('   {}'.format('✅' if sha else '❌'))
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_get_peer_hash(self, ctx, cfg):
        peer = ctx.resolve(cfg.get('peer_id', ''))
        fp = ctx.resolve(cfg.get('file_path', '')) or ctx.vars.get('_relpath', '')
        rv = cfg.get('result_var', '_peer_hash') or '_peer_hash'
        ctx.add_log('👥 同伴哈希: {} → {}'.format(peer or '?', fp))
        if not ctx.request_peer_hash:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 未注入')
            return
        try:
            sha = ctx.request_peer_hash(peer, fp)
            ctx.vars[rv] = sha or ''
            ctx.add_log('   {}'.format('✅' if sha else '❌'))
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_get_path_info(self, ctx, cfg):
        src = cfg.get('source', 'local')
        peer = ctx.resolve(cfg.get('peer_id', ''))
        fp = ctx.resolve(cfg.get('file_path', '')) or ctx.vars.get('_relpath', '')
        it = cfg.get('info_type', 'exists')
        rv = cfg.get('result_var', '_path_info') or '_path_info'
        ctx.add_log('📋 路径信息 [{}] {} → {}'.format(src, fp, it))
        used_path = fp
        try:
            if src == 'local':
                # 配了 file_path 就按它来；只有没配（或它不存在且不是绝对路径）时才回退到触发文件。
                # 之前无条件优先 _full_path，导致「统计目录条目数」这类用法永远拿到触发文件的信息。
                full = fp if (fp and (os.path.isabs(fp) or os.path.exists(fp))) else (
                    ctx.vars.get('_full_path') or fp)
                used_path = full
                if it == 'exists':
                    val = 'true' if os.path.exists(full) else 'false'
                elif it == 'size':
                    val = str(os.path.getsize(full)) if os.path.isfile(full) else '0'
                elif it == 'mtime':
                    val = str(int(os.path.getmtime(full))) if os.path.exists(full) else '0'
                elif it == 'hash':
                    h = hashlib.sha256()
                    with open(full, 'rb') as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            h.update(chunk)
                    val = h.hexdigest()
                elif it == 'is_file':
                    val = 'true' if os.path.isfile(full) else 'false'
                elif it == 'is_dir':
                    val = 'true' if os.path.isdir(full) else 'false'
                elif it == 'count':
                    val = str(len(os.listdir(full))) if os.path.isdir(full) else '0'
                else:
                    val = ''
            elif src == 'server':
                val = (ctx.request_server_path_info and ctx.request_server_path_info(fp, it)) or ''
            elif src == 'peer':
                val = (ctx.request_peer_path_info and ctx.request_peer_path_info(peer, fp, it)) or ''
            else:
                val = ''
            ctx.vars[rv] = val
            note = ''
            if src == 'local':
                note = '　路径=%s' % used_path
            ctx.add_log('   ✅ {} = {}{}'.format(it, str(val)[:60], note[:90]))
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_log(self, ctx, cfg):
        ctx.add_log('📋 ' + ctx.resolve(cfg.get('message', '')))

    def _exec_sleep(self, ctx, cfg):
        s = float(cfg.get('seconds', 1))
        ctx.add_log('⏱ {}s...'.format(s))
        time.sleep(min(s, 60))

    def _exec_http_request(self, ctx, cfg):
        url = ctx.resolve(cfg.get('url', ''))
        method = cfg.get('method', 'GET').upper()
        headers = json.loads(ctx.resolve(cfg.get('headers', '{}')))
        body = ctx.resolve(cfg.get('body', ''))
        rv = cfg.get('result_var', '_http_result') or '_http_result'
        timeout_val = int(cfg.get('timeout', 30))
        ctx.add_log('🌐 {} {}'.format(method, url[:100]))
        try:
            req = urllib.request.Request(
                url, data=body.encode() if body else None,
                headers=headers, method=method
            )
            with urllib.request.urlopen(req, timeout=timeout_val) as resp:
                txt = resp.read().decode('utf-8', errors='replace')[:5000]
                ctx.vars[rv] = txt
                ctx.add_log('   ✅ {}, {} 字节'.format(resp.status, len(txt)))
        except urllib.error.HTTPError as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ HTTP {}'.format(e.code))
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_run_command(self, ctx, cfg):
        rv = cfg.get('result_var', '_cmd_result') or '_cmd_result'
        if not getattr(ctx, 'allow_command', False):
            ctx.vars[rv] = ''
            ctx.add_log('   ⛔ 「执行命令」积木默认禁用（面板无鉴权时等于远程命令执行）')
            ctx.add_log('      如确需使用：client/config.json 里设 "allow_command_block": true')
            return
        cmd = ctx.resolve(cfg.get('command', ''))
        timeout_val = int(cfg.get('timeout', 30))
        ctx.add_log('⚙ ' + cmd[:100])
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout_val)
            out = r.stdout.strip() or r.stderr.strip()
            ctx.vars[rv] = out[:5000]
            ctx.add_log('   ✅ 退出码 {}, {} 字符'.format(r.returncode, len(out)))
        except subprocess.TimeoutExpired:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 超时')
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_write_file(self, ctx, cfg):
        fp = ctx.resolve(cfg.get('file_path', ''))
        content = ctx.resolve(cfg.get('content', ''))
        mode = cfg.get('mode', 'overwrite')
        if not fp:
            ctx.add_log('❌ 缺少 file_path')
            return
        try:
            os.makedirs(os.path.dirname(fp) or '.', exist_ok=True)
            with open(fp, 'w' if mode == 'overwrite' else 'a', encoding='utf-8') as f:
                f.write(content)
            ctx.add_log('💾 已写入 ' + fp)
        except Exception as e:
            ctx.add_log('❌ ' + str(e))

    def _exec_delete_file(self, ctx, cfg):
        fp = ctx.resolve(cfg.get('path', ''))
        if not fp:
            ctx.add_log('❌ 缺少 path')
            return
        try:
            if os.path.isdir(fp):
                if not os.listdir(fp):
                    os.rmdir(fp)
                    ctx.add_log('🗑 已删除空目录 ' + fp)
                else:
                    ctx.add_log('⚠ 目录非空，跳过 ' + fp)
            elif os.path.isfile(fp):
                os.remove(fp)
                ctx.add_log('🗑 已删除文件 ' + fp)
            else:
                ctx.add_log('⚠ 路径不存在: ' + fp)
        except Exception as e:
            ctx.add_log('❌ ' + str(e))

    def _exec_mirror_from_peer(self, ctx, cfg):
        pid = ctx.resolve(cfg.get('peer_id', ''))
        folder = ctx.resolve(cfg.get('folder', ''))
        rv = cfg.get('result_var', '_mirror_ok') or '_mirror_ok'
        ctx.add_log('🪞 请求同伴镜像: peer={} folder={}'.format(pid or '?', folder or '?'))
        if not ctx.request_mirror_from_peer:
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ 未注入 request_mirror_from_peer')
            return
        try:
            ok = ctx.request_mirror_from_peer(pid, folder)
            ctx.vars[rv] = 'true' if ok else 'false'
            ctx.add_log('   ✅ 已发出镜像请求' if ok else '   ❌ 请求失败')
        except Exception as e:
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ ' + str(e))

    def _exec_backup_to_server(self, ctx, cfg):
        fp = ctx.resolve(cfg.get('file_path', ''))
        rv = cfg.get('result_var', '_backup_ok') or '_backup_ok'
        ctx.add_log('☁ 校验服务端备份: ' + (fp or '?'))
        if not ctx.request_server_backup_hash:
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ 未注入 request_server_backup_hash')
            return
        try:
            sha = ctx.request_server_backup_hash(fp) or ''
            ctx.vars[rv] = 'true' if sha else 'false'
            ctx.add_log('   ✅ 服务端已有备份' if sha else '   ⚠ 服务端暂无该文件备份')
        except Exception as e:
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ ' + str(e))

    # ── 备份流程专用执行器 ──

    def _resolve_local(self, ctx, fp):
        """路径解析：绝对路径原样返回；等于 _relpath 时用 _full_path；
        否则尝试按所属同步目录拼接。"""
        fp = ctx.resolve(fp or '')
        if not fp or os.path.isabs(fp):
            return fp
        rel = ctx.vars.get('_relpath') or ''
        if rel and fp == rel:
            full = ctx._kw.get('full_path') or ctx.vars.get('_full_path') or ''
            if full:
                return full
        folder = ctx.vars.get('_folder') or ''
        if folder and ctx.folder_resolver:
            try:
                root = ctx.folder_resolver(folder) or ''
            except Exception:
                root = ''
            if root:
                return os.path.join(root, fp)
        return fp

    @staticmethod
    def _sha256_of(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _exec_upload_to_server(self, ctx, cfg):
        fp = self._resolve_local(ctx, cfg.get('file_path') or '{{_full_path}}')
        rel = ctx.resolve(cfg.get('remote_path', '')) or ctx.vars.get('_relpath', '') or (
            os.path.basename(fp) if fp else '')
        rv = cfg.get('result_var', '_upload_ok') or '_upload_ok'
        ctx.add_log('⬆ 上传备份: ' + (rel or fp or '?'))
        if not fp or not os.path.isfile(fp):
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ 本地文件不存在: ' + (fp or '?'))
            return
        if not ctx.upload_file_to_server:
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ 未注入上传通道（编辑器“试跑”模式不上传，请由客户端事件触发）')
            return
        try:
            res = ctx.upload_file_to_server(fp, rel)
            if isinstance(res, tuple):
                ok, msg = res
            else:
                ok, msg = bool(res), ''
            ctx.vars[rv] = 'true' if ok else 'false'
            if ok:
                ctx.vars['_uploaded_path'] = rel
                ctx.add_log('   ✅ 已上传 {} 字节{}'.format(os.path.getsize(fp),
                                                        (' · ' + str(msg)) if msg else ''))
            else:
                ctx.add_log('   ❌ 上传失败' + ((': ' + str(msg)) if msg else ''))
        except Exception as e:
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ ' + str(e))

    def _exec_verify_backup(self, ctx, cfg):
        fp = self._resolve_local(ctx, cfg.get('file_path') or '{{_full_path}}')
        rel = ctx.resolve(cfg.get('remote_path', '')) or ctx.vars.get('_relpath', '') or (
            os.path.basename(fp) if fp else '')
        rv = cfg.get('result_var', '_verify_ok') or '_verify_ok'
        # 关键：必须对「本次要校验的文件」重新算哈希。
        # 之前优先复用 ctx.vars['_local_hash']，在 foreach 循环里第二次起用的还是上一个文件的哈希，
        # 于是核对的是别的文件（可能把不一致报成一致，也可能把一致报成不一致）。
        local = ''
        if fp and os.path.isfile(fp):
            try:
                local = self._sha256_of(fp)
            except Exception as e:
                ctx.add_log('   ⚠ 计算本地哈希失败: ' + str(e))
        if not local:
            local = ctx.vars.get('_local_hash') or ''      # 兜底：目标不可读时沿用已有结果
        ctx.vars['_local_hash'] = local
        ctx.add_log('🧾 校验备份: ' + (rel or '?'))
        if not ctx.request_server_backup_hash:
            ctx.vars[rv] = False
            ctx.add_log('   ❌ 未注入服务端查询通道')
            return
        remote = ''
        try:
            remote = ctx.request_server_backup_hash(rel) or ''
        except Exception as e:
            ctx.add_log('   ❌ 查询失败: ' + str(e))
        ctx.vars['_server_hash'] = remote
        ok = bool(local) and local == remote
        ctx.vars[rv] = ok
        ctx.add_log('   本地 {} / 服务端 {}'.format(
            (local[:16] + '...') if local else '(空)',
            (remote[:16] + '...') if remote else '(无备份)'))
        ctx.add_log('   ✅ 备份一致' if ok else '   ❌ 备份缺失或不一致')

    def _exec_copy_file(self, ctx, cfg):
        src = self._resolve_local(ctx, cfg.get('src') or '{{_full_path}}')
        dst_dir = ctx.resolve(cfg.get('dst_dir', ''))
        rv = cfg.get('result_var', '_copy_ok') or '_copy_ok'
        overwrite = cfg.get('overwrite', True)
        ctx.add_log('📄 复制: {} → {}'.format(src or '?', dst_dir or '?'))
        if not src or not os.path.isfile(src):
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ 源文件不存在')
            return
        if not dst_dir:
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ 缺少 dst_dir')
            return
        try:
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, os.path.basename(src))
            if os.path.exists(dst) and not overwrite:
                ctx.vars[rv] = 'true'
                ctx.add_log('   ⏭ 已存在，跳过（overwrite=false）')
                return
            shutil.copy2(src, dst)
            ctx.vars[rv] = 'true'
            ctx.vars['_copy_path'] = dst
            ctx.add_log('   ✅ ' + dst)
        except Exception as e:
            ctx.vars[rv] = 'false'
            ctx.add_log('   ❌ ' + str(e))

    def _exec_archive_zip(self, ctx, cfg):
        src = self._resolve_local(ctx, cfg.get('src') or '{{_full_path}}')
        zp = ctx.resolve(cfg.get('zip_path', ''))
        rv = cfg.get('result_var', '_zip_path') or '_zip_path'
        ctx.add_log('🗜 打包: {} → {}'.format(src or '?', zp or '?'))
        if not src or not os.path.exists(src):
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 源不存在')
            return
        if not zp:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 缺少 zip_path')
            return
        if not zp.lower().endswith('.zip'):
            zp += '.zip'
        try:
            os.makedirs(os.path.dirname(zp) or '.', exist_ok=True)
            mode = 'a' if os.path.isfile(zp) else 'w'
            try:
                zf = zipfile.ZipFile(zp, mode, zipfile.ZIP_DEFLATED)
            except zipfile.BadZipFile:
                zf = zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED)
            added = 0
            with zf:
                if os.path.isdir(src):
                    root_name = os.path.basename(os.path.normpath(src))
                    zp_abs = os.path.abspath(zp)
                    for dp, _, fns in os.walk(src):
                        for fn in fns:
                            full = os.path.join(dp, fn)
                            # 不要把输出 zip 自己打进包里：zip 若写在被监听的目录内，
                            # 边写边打包会让文件不断变大（同步也跟着疯跑），必须跳过
                            if os.path.abspath(full) == zp_abs:
                                continue
                            zf.write(full, os.path.join(root_name, os.path.relpath(full, src)))
                            added += 1
                else:
                    zf.write(src, os.path.basename(src))
                    added = 1
            ctx.vars[rv] = zp
            ctx.vars['_zip_count'] = added
            ctx.add_log('   ✅ {} 个条目 · {:.1f} KB'.format(added, os.path.getsize(zp) / 1024.0))
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_cleanup_old(self, ctx, cfg):
        d = ctx.resolve(cfg.get('dir', ''))
        pat = ctx.resolve(cfg.get('pattern', '*')) or '*'
        days = float(cfg.get('days', 30) or 30)
        dry = bool(cfg.get('dry_run', True))
        rv = cfg.get('result_var', '_cleanup_count') or '_cleanup_count'
        ctx.add_log('🧹 清理过期文件: {} · {} 天前 · {}'.format(d or '?', days, '仅预演' if dry else '真删除'))
        if not d or not os.path.isdir(d):
            ctx.vars[rv] = 0
            ctx.add_log('   ⚠ 目录不存在')
            return
        if days <= 0:
            ctx.vars[rv] = 0
            ctx.add_log('   ⚠ days 必须大于 0，已跳过')
            return
        cutoff = time.time() - days * 86400
        victims = []
        try:
            for dp, _, fns in os.walk(d):
                for fn in fns:
                    if not fnmatch.fnmatch(fn, pat):
                        continue
                    full = os.path.join(dp, fn)
                    try:
                        if os.path.getmtime(full) < cutoff:
                            victims.append(full)
                    except OSError:
                        pass
        except Exception as e:
            ctx.vars[rv] = 0
            ctx.add_log('   ❌ ' + str(e))
            return
        deleted = 0
        if not dry:
            for v in victims[:200]:
                try:
                    os.remove(v)
                    deleted += 1
                except OSError:
                    pass
        ctx.vars[rv] = len(victims) if dry else deleted
        for v in victims[:5]:
            ctx.add_log('   · ' + v)
        if len(victims) > 5:
            ctx.add_log('   · … 其余 {} 个'.format(len(victims) - 5))
        if dry:
            ctx.add_log('   🔎 命中 {} 个（预演未删除，确认后把 dry_run 关掉）'.format(len(victims)))
        else:
            ctx.add_log('   ✅ 命中 {} 个，已删除 {} 个'.format(len(victims), deleted))

    def _exec_foreach(self, ctx, cfg):
        lv = cfg.get('list_var', '_scan_result') or '_scan_result'
        iv = cfg.get('item_var', '_item') or '_item'
        xv = cfg.get('index_var', '_index') or '_index'
        steps = cfg.get('steps', []) or []
        max_items = int(cfg.get('max_items', 500) or 500)
        lst = ctx.vars.get(lv)
        if isinstance(lst, str):
            try:
                lst = json.loads(lst)
            except Exception:
                lst = [lst] if lst else []
        if not isinstance(lst, list):
            lst = []
        if not steps:
            ctx.add_log('🔁 列表 {} 有 {} 项，但未配置子步骤'.format(lv, len(lst)))
            return
        total = min(len(lst), max_items)
        ctx.add_log('🔁 遍历 {}: {} 项{}'.format(lv, total, '（已截断）' if len(lst) > max_items else ''))
        ok = 0
        base = ctx.vars.get('_scan_base', '')
        for i, item in enumerate(lst[:total]):
            sub = ctx.clone()
            sub.vars[iv] = item
            sub.vars[xv] = i
            if base:
                sub.vars['_item_path'] = os.path.join(base, str(item))
            self._run_steps(sub, steps)
            ctx.logs.extend(sub.logs)
            for k, v in sub.vars.items():
                if k not in (iv, xv):
                    ctx.vars[k] = v
            if str(sub.vars.get('_upload_ok', '')).lower() == 'true' or sub.vars.get('_verify_ok') is True:
                ok += 1
        ctx.vars['_foreach_total'] = total
        ctx.vars['_foreach_ok'] = ok
        ctx.add_log('🔁 遍历完成: {}/{} 项成功'.format(ok, total))

    # ── 加密积木 ──

    @staticmethod
    def _crypto():
        from . import crypto_util
        return crypto_util

    def _exec_make_key_file(self, ctx, cfg):
        p = ctx.resolve(cfg.get('path', ''))
        rv = cfg.get('result_var', '_key_file') or '_key_file'
        if not p:
            p = os.path.join(ctx.vars.get('_client_dir', '.') or '.', 'keys', 'mirror.key')
        ctx.add_log('🗝 生成密钥文件: ' + p)
        if os.path.isfile(p) and not cfg.get('overwrite', False):
            ctx.vars[rv] = p
            ctx.add_log('   ⏭ 已存在，未覆盖（overwrite=false）')
            return
        try:
            self._crypto().make_key_file(p)
            ctx.vars[rv] = p
            ctx.add_log('   ✅ 已生成（丢了就解不开，请自行备份）')
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_encrypt_file(self, ctx, cfg):
        fp = self._resolve_local(ctx, cfg.get('file_path') or '{{_full_path}}')
        rv = cfg.get('result_var', '_enc_path') or '_enc_path'
        passphrase = ctx.resolve(cfg.get('passphrase', '')) or ctx.secret(cfg.get('passphrase_secret', 'backup_passphrase'))
        key_file = ctx.resolve(cfg.get('key_file', ''))
        out = ctx.resolve(cfg.get('out_path', ''))
        ctx.add_log('🔐 加密: ' + (fp or '?'))
        if not fp or not os.path.isfile(fp):
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 文件不存在')
            return
        if not passphrase and not key_file:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 缺少密钥：请在 client/config.json 里配置 backup_key_file 或 backup_passphrase')
            return
        if out and os.path.isdir(out):
            out = os.path.join(out, os.path.basename(fp) + '.enc')
        try:
            cu = self._crypto()
            info = cu.encrypt_file(fp, out or (fp + '.enc'), passphrase=passphrase,
                                   key_file=key_file,
                                   delete_source=bool(cfg.get('delete_source', False)))
            ctx.vars[rv] = info['path']
            ctx.vars['_enc_size'] = info['size']
            ctx.add_log('   ✅ {} · {} 块 · {:.1f} KB'.format(
                os.path.basename(info['path']), info['chunks'], info['size'] / 1024.0))
            if cfg.get('delete_source', False):
                ctx.add_log('   🗑 已删除明文源文件')
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_decrypt_file(self, ctx, cfg):
        fp = self._resolve_local(ctx, cfg.get('file_path') or '{{_full_path}}')
        rv = cfg.get('result_var', '_dec_path') or '_dec_path'
        passphrase = ctx.resolve(cfg.get('passphrase', '')) or ctx.secret(cfg.get('passphrase_secret', 'backup_passphrase'))
        key_file = ctx.resolve(cfg.get('key_file', ''))
        out = ctx.resolve(cfg.get('out_path', ''))
        ctx.add_log('🔓 解密: ' + (fp or '?'))
        if not fp or not os.path.isfile(fp):
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 文件不存在')
            return
        if not passphrase and not key_file:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 缺少密钥：请在 client/config.json 里配置 backup_key_file 或 backup_passphrase')
            return
        try:
            cu = self._crypto()
            info = cu.decrypt_file(fp, out or None, passphrase=passphrase, key_file=key_file,
                                   delete_source=bool(cfg.get('delete_source', False)))
            ctx.vars[rv] = info['path']
            ctx.add_log('   ✅ {} · {:.1f} KB'.format(
                os.path.basename(info['path']), info['size'] / 1024.0))
            if cfg.get('delete_source', False):
                ctx.add_log('   🗑 已删除密文源文件')
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    # ── 历史版本积木 ──

    def _exec_list_versions(self, ctx, cfg):
        fp = ctx.resolve(cfg.get('file_path', '')) or ctx.vars.get('_relpath', '')
        rv = cfg.get('result_var', '_versions') or '_versions'
        ctx.add_log('🕘 列出版本: ' + (fp or '?'))
        if not ctx.version_list:
            ctx.vars[rv] = []
            ctx.add_log('   ❌ 未注入版本查询通道')
            return
        try:
            info = ctx.version_list(fp) or {}
            vs = info.get('versions') or []
            ctx.vars[rv] = vs
            ctx.vars['_version_count'] = len(vs)
            cur = info.get('current') or {}
            if cur:
                ctx.add_log('   当前: {} 字节 · {}'.format(cur.get('size'), (cur.get('sha256') or '')[:12]))
            for v in vs[:10]:
                ctx.add_log('   · {} · {} 字节 · {} · {}'.format(
                    v.get('id'), v.get('size'),
                    time.strftime('%m-%d %H:%M', time.localtime(v.get('mtime', 0))),
                    v.get('reason', '')))
            if len(vs) > 10:
                ctx.add_log('   · … 其余 {} 个'.format(len(vs) - 10))
            ctx.add_log('   ✅ 共 {} 个历史版本（保留上限 {}）'.format(len(vs), info.get('max_versions')))
        except Exception as e:
            ctx.vars[rv] = []
            ctx.add_log('   ❌ ' + str(e))

    def _exec_snapshot_version(self, ctx, cfg):
        fp = ctx.resolve(cfg.get('file_path', '')) or ctx.vars.get('_relpath', '')
        reason = ctx.resolve(cfg.get('reason', 'flow')) or 'flow'
        rv = cfg.get('result_var', '_version_id') or '_version_id'
        ctx.add_log('🔖 打版本点: {} ({})'.format(fp or '?', reason))
        if not ctx.version_op:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 未注入版本操作通道')
            return
        try:
            r = ctx.version_op('snapshot', fp, 'latest', reason) or {}
            if r.get('ok'):
                ctx.vars[rv] = r.get('version', '')
                ctx.add_log('   ✅ 版本 {} · {} 字节'.format(r.get('version'), r.get('size')))
            else:
                ctx.vars[rv] = ''
                ctx.add_log('   ❌ ' + (r.get('msg') or '打点失败'))
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_restore_version(self, ctx, cfg):
        fp = ctx.resolve(cfg.get('file_path', '')) or ctx.vars.get('_relpath', '')
        ver = ctx.resolve(cfg.get('version', 'latest')) or 'latest'
        rv = cfg.get('result_var', '_restore_ok') or '_restore_ok'
        ctx.add_log('⏪ 回滚: {} → {}'.format(fp or '?', ver))
        if not ctx.version_op:
            ctx.vars[rv] = False
            ctx.add_log('   ❌ 未注入版本操作通道')
            return
        try:
            r = ctx.version_op('restore', fp, ver) or {}
            ctx.vars[rv] = bool(r.get('ok'))
            if r.get('ok'):
                ctx.vars['_restore_version'] = r.get('version', '')
                ctx.add_log('   ✅ 已回滚到 {} · {} 字节 · sha {}'.format(
                    r.get('version'), r.get('size'), (r.get('sha256') or '')[:12]))
                ctx.add_log('   （回滚前的版本已自动留档，可再次回滚撤销）')
            else:
                ctx.add_log('   ❌ ' + (r.get('msg') or '回滚失败'))
        except Exception as e:
            ctx.vars[rv] = False
            ctx.add_log('   ❌ ' + str(e))

    def _exec_fetch_version(self, ctx, cfg):
        fp = ctx.resolve(cfg.get('file_path', '')) or ctx.vars.get('_relpath', '')
        ver = ctx.resolve(cfg.get('version', 'latest')) or 'latest'
        out = ctx.resolve(cfg.get('out_path', ''))
        rv = cfg.get('result_var', '_fetched_path') or '_fetched_path'
        ctx.add_log('📥 取回版本: {} @ {}'.format(fp or '?', ver))
        if not ctx.version_op:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ 未注入版本操作通道')
            return
        try:
            r = ctx.version_op('fetch', fp, ver) or {}
            if not r.get('ok'):
                ctx.vars[rv] = ''
                ctx.add_log('   ❌ ' + (r.get('msg') or '取回失败'))
                return
            data = base64.b64decode(r.get('content_b64') or '')
            if not out:
                name = os.path.basename(str(fp).replace('\\', '/')) or 'restored.bin'
                out = os.path.join(ctx.vars.get('_client_dir', '.') or '.', 'restored',
                                   '{}-{}'.format(ver, name))
            os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
            tmp = out + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, out)
            sha = hashlib.sha256(data).hexdigest()
            expect = r.get('sha256') or ''
            ctx.vars[rv] = out
            ctx.add_log('   ✅ {} · {} 字节 · sha {}'.format(out, len(data), sha[:12]))
            if expect and expect != sha:
                ctx.add_log('   ❌ 校验失败：期望 sha {}'.format(expect[:12]))
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))

    def _exec_manifest(self, ctx, cfg):
        d = ctx.resolve(cfg.get('dir', ''))
        pat = ctx.resolve(cfg.get('pattern', '*')) or '*'
        out = ctx.resolve(cfg.get('out_path', ''))
        fmt = (cfg.get('format', 'json') or 'json').lower()
        rv = cfg.get('result_var', '_manifest_path') or '_manifest_path'
        ctx.add_log('📑 生成清单: {} → {}'.format(d or '?', out or '?'))
        if not d or not os.path.isdir(d):
            ctx.vars[rv] = ''
            ctx.add_log('   ⚠ 目录不存在')
            return
        rows = []
        try:
            for dp, _, fns in os.walk(d):
                for fn in fns:
                    if not fnmatch.fnmatch(fn, pat):
                        continue
                    full = os.path.join(dp, fn)
                    try:
                        stt = os.stat(full)
                        rows.append({'path': os.path.relpath(full, d).replace('\\', '/'),
                                     'size': stt.st_size, 'mtime': int(stt.st_mtime),
                                     'sha256': self._sha256_of(full)})
                    except OSError:
                        pass
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))
            return
        rows.sort(key=lambda r: r['path'])
        if not out:
            out = os.path.join(d, 'manifest.' + ('md' if fmt == 'md' else fmt))
        try:
            os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
            if fmt == 'csv':
                with open(out, 'w', encoding='utf-8-sig', newline='') as f:
                    w = csv.DictWriter(f, fieldnames=['path', 'size', 'mtime', 'sha256'])
                    w.writeheader()
                    w.writerows(rows)
            elif fmt == 'md':
                with open(out, 'w', encoding='utf-8') as f:
                    f.write('# 备份清单\n\n共 {} 个文件\n\n'.format(len(rows)))
                    f.write('| 文件 | 大小 | 修改时间 | SHA256 |\n|---|---|---|---|\n')
                    for r in rows:
                        f.write('| {} | {} | {} | {} |\n'.format(
                            r['path'], r['size'],
                            time.strftime('%Y-%m-%d %H:%M', time.localtime(r['mtime'])),
                            r['sha256'][:16]))
            else:
                with open(out, 'w', encoding='utf-8') as f:
                    json.dump({'generated_at': int(time.time()), 'dir': d, 'count': len(rows),
                               'total_size': sum(r['size'] for r in rows), 'files': rows},
                              f, indent=2, ensure_ascii=False)
            ctx.vars[rv] = out
            ctx.vars['_manifest_count'] = len(rows)
            ctx.add_log('   ✅ {} 个文件 · {:.1f} KB'.format(len(rows), os.path.getsize(out) / 1024.0))
        except Exception as e:
            ctx.vars[rv] = ''
            ctx.add_log('   ❌ ' + str(e))


# ============================================================
FLOW_TEMPLATES = {
    "hash_check": {
        "name": "哈希校验",
        "queues": [
            {
                "id": "q1", "label": "本地校验",
                "steps": [
                    {"id": "b1", "type": "get_file_hash", "label": "计算本地哈希",
                     "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}},
                    {"id": "b2", "type": "log", "label": "记录本地哈希",
                     "config": {"message": "🔑 本地: {{_local_hash}}"}}
                ]
            },
            {
                "id": "q2", "label": "远程校验",
                "steps": [
                    {"id": "r1", "type": "get_server_hash", "label": "获取服务端哈希",
                     "config": {"file_path": "{{_relpath}}", "result_var": "_server_hash"}},
                    {"id": "r2", "type": "log", "label": "记录服务端哈希",
                     "config": {"message": "☁ 服务端: {{_server_hash}}"}}
                ]
            },
            {
                "id": "q3", "label": "同伴校验",
                "steps": [
                    {"id": "p1", "type": "get_peer_hash", "label": "获取同伴哈希",
                     "config": {"peer_id": "", "file_path": "{{_relpath}}", "result_var": "_peer_hash"}},
                    {"id": "p2", "type": "log", "label": "记录同伴哈希",
                     "config": {"message": "👥 同伴: {{_peer_hash}}"}}
                ]
            }
        ],
        "merge_at": [1]
    },
    "backup_verify": {
        "name": "备份验证",
        "queues": [
            {
                "id": "q1", "label": "本地校验",
                "steps": [
                    {"id": "b1", "type": "get_file_hash", "label": "计算本地哈希",
                     "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}},
                    {"id": "b2", "type": "log", "label": "记录本地哈希",
                     "config": {"message": "🔑 本地: {{_local_hash}}"}}
                ]
            },
            {
                "id": "q2", "label": "远程校验",
                "steps": [
                    {"id": "r1", "type": "get_server_hash", "label": "获取服务端哈希",
                     "config": {"file_path": "{{_relpath}}", "result_var": "_server_hash"}},
                    {"id": "r2", "type": "log", "label": "记录服务端哈希",
                     "config": {"message": "☁ 服务端: {{_server_hash}}"}}
                ]
            },
            {
                "id": "q3", "label": "同伴校验",
                "steps": [
                    {"id": "p1", "type": "get_peer_hash", "label": "获取同伴哈希",
                     "config": {"peer_id": "", "file_path": "{{_relpath}}", "result_var": "_peer_hash"}},
                    {"id": "p2", "type": "log", "label": "记录同伴哈希",
                     "config": {"message": "👥 同伴: {{_peer_hash}}"}}
                ]
            }
        ],
        "merge_at": [1]
    },
    "multi_folder_sync": {
        "name": "多目录同步",
        "queues": [
            {
                "id": "q1", "label": "文档目录",
                "steps": [
                    {"id": "d1", "type": "scan_dir", "label": "扫描文档",
                     "config": {"folder": "docs", "pattern": "*.md", "recursive": True, "result_var": "_docs"}},
                    {"id": "d2", "type": "log", "label": "文档统计",
                     "config": {"message": "📄 文档: {{_docs}} 个文件"}}
                ]
            },
            {
                "id": "q2", "label": "图片目录",
                "steps": [
                    {"id": "i1", "type": "scan_dir", "label": "扫描图片",
                     "config": {"folder": "images", "pattern": "*.png", "recursive": True, "result_var": "_images"}},
                    {"id": "i2", "type": "log", "label": "图片统计",
                     "config": {"message": "🖼 图片: {{_images}} 个文件"}}
                ]
            },
            {
                "id": "q3", "label": "配置目录",
                "steps": [
                    {"id": "c1", "type": "scan_dir", "label": "扫描配置",
                     "config": {"folder": "config", "pattern": "*.json", "recursive": False, "result_var": "_configs"}},
                    {"id": "c2", "type": "log", "label": "配置统计",
                     "config": {"message": "⚙ 配置: {{_configs}} 个文件"}}
                ]
            }
        ],
        "merge_at": [1, 2]
    },
    "file_age_cleanup": {
        "name": "旧文件清理",
        "queues": [
            {
                "id": "q1", "label": "检测与清理",
                "steps": [
                    {"id": "a1", "type": "get_path_info", "label": "检查文件修改时间",
                     "config": {"source": "local", "file_path": "{{_relpath}}", "info_type": "mtime", "result_var": "_mtime"}},
                    {"id": "a2", "type": "log", "label": "过期标记",
                     "config": {"message": "⚠ {{_relpath}} 上次修改时间戳: {{_mtime}}"}}
                ]
            }
        ],
        "merge_at": []
    },
    "webhook_notify": {
        "name": "Webhook 通知",
        "queues": [
            {
                "id": "q1", "label": "多渠道通知",
                "steps": [
                    {"id": "w1", "type": "http_request", "label": "钉钉通知",
                     "config": {"url": "https://oapi.dingtalk.com/robot/send", "method": "POST",
                                "headers": "{\"Content-Type\":\"application/json\"}",
                                "body": "{\"msgtype\":\"text\",\"text\":{\"content\":\"文件变更: {{_relpath}}\"}}",
                                "result_var": "_dd_resp", "timeout": 5}},
                    {"id": "w2", "type": "log", "label": "通知完成",
                     "config": {"message": "📡 通知已发送"}}
                ]
            },
            {
                "id": "q2", "label": "日志记录",
                "steps": [
                    {"id": "l1", "type": "write_file", "label": "写入审计日志",
                     "config": {"file_path": "/var/log/mirror/audit.log",
                                "content": "[{{_timestamp}}] {{_event_type}} | {{_relpath}} | {{_source_device}}",
                                "mode": "append"}}
                ]
            }
        ],
        "merge_at": []
    },
    "parallel_search": {
        "name": "并行搜索示例",
        "queues": [
            {
                "id": "q1", "label": "百度搜索",
                "steps": [
                    {"id": "b1", "type": "http_request", "label": "搜索百度",
                     "config": {"url": "https://www.baidu.com/s?wd=test", "method": "GET", "result_var": "_baidu"}},
                    {"id": "b2", "type": "log", "label": "百度完成",
                     "config": {"message": "✅ 百度: {{_baidu}} 字节"}}
                ]
            },
            {
                "id": "q2", "label": "Bing搜索",
                "steps": [
                    {"id": "bi1", "type": "http_request", "label": "搜索Bing",
                     "config": {"url": "https://cn.bing.com/search?q=test", "method": "GET", "result_var": "_bing"}},
                    {"id": "bi2", "type": "log", "label": "Bing完成",
                     "config": {"message": "✅ Bing: {{_bing}} 字节"}}
                ]
            },
            {
                "id": "q3", "label": "本地扫描",
                "steps": [
                    {"id": "l1", "type": "scan_dir", "label": "扫描目录",
                     "config": {"pattern": "*", "result_var": "_scan_result"}},
                    {"id": "l2", "type": "log", "label": "扫描完成",
                     "config": {"message": "✅ 本地: {{_scan_result}} 个文件"}}
                ]
            }
        ],
        "merge_at": []
    },
    "passive_sync": {
        "name": "被动同步",
        "queues": [
            {
                "id": "q1", "label": "主队列",
                "steps": [
                    {"id": "s1", "type": "log", "label": "被动同步",
                     "config": {"message": "📥 被动同步：本地不推送"}}
                ]
            }
        ],
        "merge_at": []
    },
    "full_pipeline": {
        "name": "完整管道",
        "queues": [
            {
                "id": "q1", "label": "哈希检验",
                "steps": [
                    {"id": "h1", "type": "get_file_hash", "label": "本地哈希",
                     "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}},
                    {"id": "h2", "type": "get_server_hash", "label": "服务端哈希",
                     "config": {"file_path": "{{_relpath}}", "result_var": "_server_hash"}}
                ]
            },
            {
                "id": "q2", "label": "信息收集",
                "steps": [
                    {"id": "i1", "type": "get_path_info", "label": "文件大小",
                     "config": {"source": "local", "file_path": "{{_relpath}}", "info_type": "size", "result_var": "_file_size_bytes"}},
                    {"id": "i2", "type": "get_path_info", "label": "是否存在",
                     "config": {"source": "local", "file_path": "{{_relpath}}", "info_type": "exists", "result_var": "_file_exists"}}
                ]
            },
            {
                "id": "q3", "label": "通知发送",
                "steps": [
                    {"id": "n1", "type": "http_request", "label": "发送通知",
                     "config": {"url": "https://example.com/webhook", "method": "POST",
                                "body": "{\"file\":\"{{_relpath}}\",\"hash\":\"{{_local_hash}}\",\"size\":\"{{_file_size_bytes}}\"}",
                                "result_var": "_notify_resp"}},
                    {"id": "n2", "type": "log", "label": "通知结果",
                     "config": {"message": "📤 通知: {{_notify_resp}}"}}
                ]
            }
        ],
        "merge_at": [1, 2]
    },
    "hash_three_way": {
        "name": "三方哈希核验",
        "queues": [
            {"id": "q1", "label": "本地哈希",
             "steps": [
                 {"id": "l1", "type": "get_file_hash", "label": "计算本地哈希",
                  "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}},
                 {"id": "l2", "type": "log", "label": "记录本地",
                  "config": {"message": "🔑 本地: {{_local_hash}}"}}
             ]},
            {"id": "q2", "label": "服务端哈希",
             "steps": [
                 {"id": "s1", "type": "get_server_hash", "label": "获取服务端哈希",
                  "config": {"file_path": "{{_relpath}}", "result_var": "_server_hash", "timeout": 10}},
                 {"id": "s2", "type": "log", "label": "记录服务端",
                  "config": {"message": "☁ 服务端: {{_server_hash}}"}}
             ]},
            {"id": "q3", "label": "同伴哈希",
             "steps": [
                 {"id": "p1", "type": "get_peer_hash", "label": "获取同伴哈希",
                  "config": {"peer_id": "", "file_path": "{{_relpath}}", "result_var": "_peer_hash", "timeout": 10}},
                 {"id": "p2", "type": "log", "label": "记录同伴",
                  "config": {"message": "👥 同伴: {{_peer_hash}}"}}
             ]}
        ],
        "merge_at": [1]
    },
    "backup_health": {
        "name": "备份健康检查",
        "queues": [
            {"id": "q1", "label": "服务端备份",
             "steps": [
                 {"id": "s1", "type": "get_server_hash", "label": "服务端哈希",
                  "config": {"file_path": "{{_relpath}}", "result_var": "_server_hash", "timeout": 10}},
                 {"id": "s2", "type": "get_path_info", "label": "服务端存在性",
                  "config": {"source": "server", "file_path": "{{_relpath}}", "info_type": "exists", "result_var": "_server_exists"}}
             ]},
            {"id": "q2", "label": "本地状态",
             "steps": [
                 {"id": "l1", "type": "get_path_info", "label": "本地存在性",
                  "config": {"source": "local", "file_path": "{{_relpath}}", "info_type": "exists", "result_var": "_local_exists"}},
                 {"id": "l2", "type": "get_path_info", "label": "本地大小",
                  "config": {"source": "local", "file_path": "{{_relpath}}", "info_type": "size", "result_var": "_local_size"}}
             ]}
        ],
        "merge_at": [1],
        "tail": [
            {"id": "t1", "type": "log", "label": "健康汇总",
             "config": {"message": "🩺 本地存在={{_local_exists}} 服务端存在={{_server_exists}} 大小={{_local_size}}"}}
        ]
    },
    "large_file_alert": {
        "name": "大文件告警",
        "queues": [
            {"id": "q1", "label": "大小检测",
             "steps": [
                 {"id": "c1", "type": "get_path_info", "label": "读取文件大小",
                  "config": {"source": "local", "file_path": "{{_relpath}}", "info_type": "size", "result_var": "_size_bytes"}},
                 {"id": "c2", "type": "compare", "label": "是否超限",
                  "config": {"left": "{{_size_bytes}}", "op": ">", "right": "104857600"}},
                 {"id": "c3", "type": "log", "label": "超限提示",
                  "config": {"message": "⚠ 大文件 {{_relpath}} ({{_size_bytes}} 字节) 超限: {{_compare_result}}"}},
                 {"id": "c4", "type": "http_request", "label": "发送告警",
                  "config": {"url": "https://example.com/alert", "method": "POST",
                             "body": "{\"file\":\"{{_relpath}}\",\"size\":\"{{_size_bytes}}\"}",
                             "result_var": "_alert_resp", "timeout": 5}}
             ]}
        ],
        "merge_at": []
    },
    "sync_audit_log": {
        "name": "同步审计日志",
        "queues": [
            {"id": "q1", "label": "审计",
             "steps": [
                 {"id": "a1", "type": "get_file_hash", "label": "本地哈希",
                  "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}},
                 {"id": "a2", "type": "get_server_hash", "label": "服务端哈希",
                  "config": {"file_path": "{{_relpath}}", "result_var": "_server_hash", "timeout": 10}},
                 {"id": "a3", "type": "compare", "label": "一致性比较",
                  "config": {"left": "{{_local_hash}}", "op": "==", "right": "{{_server_hash}}"}},
                 {"id": "a4", "type": "write_file", "label": "写入审计",
                  "config": {"file_path": "./logs/sync_audit.log",
                             "content": "[{{_timestamp}}] {{_event_type}} | {{_relpath}} | 一致={{_compare_result}} | {{_source_device}}",
                             "mode": "append"}},
                 {"id": "a5", "type": "log", "label": "审计完成",
                  "config": {"message": "📝 已记录: {{_relpath}}"}}
             ]}
        ],
        "merge_at": []
    },
    "folder_index_builder": {
        "name": "目录索引生成",
        "queues": [
            {"id": "q1", "label": "扫描",
             "steps": [
                 {"id": "sc1", "type": "scan_dir", "label": "扫描文件",
                  "config": {"folder": "{{_folder_path}}", "pattern": "*", "recursive": True, "result_var": "_files"}}
             ]},
            {"id": "q2", "label": "统计",
             "steps": [
                 {"id": "ct1", "type": "get_path_info", "label": "条目数量",
                  "config": {"source": "local", "file_path": "{{_relpath}}", "info_type": "count", "result_var": "_count"}}
             ]}
        ],
        "merge_at": [1],
        "tail": [
            {"id": "wt1", "type": "write_file", "label": "生成索引",
             "config": {"file_path": "./logs/folder_index.md",
                        "content": "# 目录索引 {{_relpath}}\n条目数: {{_count}}\n扫描数: {{_files}}",
                        "mode": "overwrite"}},
            {"id": "wt2", "type": "log", "label": "索引完成",
             "config": {"message": "📑 索引已生成: {{_relpath}}"}}
        ]
    },
    "webhook_broadcast": {
        "name": "多渠道通知",
        "queues": [
            {"id": "q1", "label": "钉钉",
             "steps": [
                 {"id": "d1", "type": "http_request", "label": "钉钉机器人",
                  "config": {"url": "https://oapi.dingtalk.com/robot/send", "method": "POST",
                             "headers": "{\"Content-Type\":\"application/json\"}",
                             "body": "{\"msgtype\":\"text\",\"text\":{\"content\":\"文件变更: {{_relpath}}\"}}",
                             "result_var": "_dd", "timeout": 5}},
                 {"id": "d2", "type": "log", "label": "钉钉结果",
                  "config": {"message": "📡 钉钉: {{_dd}}"}}
             ]},
            {"id": "q2", "label": "通用Webhook",
             "steps": [
                 {"id": "w1", "type": "http_request", "label": "Webhook",
                  "config": {"url": "https://example.com/webhook", "method": "POST",
                             "body": "{\"event\":\"{{_event_type}}\",\"file\":\"{{_relpath}}\",\"device\":\"{{_source_device}}\"}",
                             "result_var": "_wh", "timeout": 5}},
                 {"id": "w2", "type": "log", "label": "Webhook结果",
                  "config": {"message": "🌐 Webhook: {{_wh}}"}}
             ]}
        ],
        "merge_at": []
    },
    "peer_status_probe": {
        "name": "同伴状态探测",
        "queues": [
            {"id": "q1", "label": "同伴哈希",
             "steps": [
                 {"id": "p1", "type": "get_peer_hash", "label": "请求同伴哈希",
                  "config": {"peer_id": "", "file_path": "{{_relpath}}", "result_var": "_peer_hash", "timeout": 10}},
                 {"id": "p2", "type": "log", "label": "同伴哈希结果",
                  "config": {"message": "👥 同伴: {{_peer_hash}}"}}
             ]},
            {"id": "q2", "label": "同伴存在性",
             "steps": [
                 {"id": "e1", "type": "get_path_info", "label": "同伴文件存在",
                  "config": {"source": "peer", "peer_id": "", "file_path": "{{_relpath}}", "info_type": "exists", "result_var": "_peer_exists"}},
                 {"id": "e2", "type": "log", "label": "存在性结果",
                  "config": {"message": "📋 同伴存在: {{_peer_exists}}"}}
             ]}
        ],
        "merge_at": []
    },
    "cleanup_scan_report": {
        "name": "清理扫描报告",
        "queues": [
            {"id": "q1", "label": "扫描",
             "steps": [
                 {"id": "r1", "type": "scan_dir", "label": "扫描目录",
                  "config": {"folder": "{{_folder_path}}", "pattern": "*.tmp", "recursive": True, "result_var": "_tmp_files"}}
             ]},
            {"id": "q2", "label": "容量",
             "steps": [
                 {"id": "r2", "type": "get_path_info", "label": "目录大小",
                  "config": {"source": "local", "file_path": "{{_relpath}}", "info_type": "size", "result_var": "_dir_size"}}
             ]}
        ],
        "merge_at": [1],
        "tail": [
            {"id": "r3", "type": "write_file", "label": "生成报告",
             "config": {"file_path": "./logs/cleanup_report.txt",
                        "content": "扫描时间: {{_timestamp}}\n临时文件数: {{_tmp_files}}\n目录大小: {{_dir_size}}",
                        "mode": "overwrite"}},
            {"id": "r4", "type": "log", "label": "报告完成",
             "config": {"message": "🧹 报告已生成: {{_relpath}}"}}
        ]
    },
    "command_inspection": {
        "name": "命令巡检",
        "queues": [
            {"id": "q1", "label": "巡检",
             "steps": [
                 {"id": "cmd1", "type": "run_command", "label": "执行命令",
                  "config": {"command": "echo inspect", "result_var": "_cmd_out", "timeout": 15}},
                 {"id": "cmd2", "type": "write_file", "label": "保存输出",
                  "config": {"file_path": "./logs/inspect.txt",
                             "content": "[{{_timestamp}}] 输出:\n{{_cmd_out}}",
                             "mode": "overwrite"}},
                 {"id": "cmd3", "type": "log", "label": "巡检完成",
                  "config": {"message": "⚙ 输出: {{_cmd_out}}"}}
             ]}
        ],
        "merge_at": []
    },
    "mirror_consistency": {
        "name": "镜像一致性",
        "queues": [
            {"id": "q1", "label": "本地",
             "steps": [
                 {"id": "m1", "type": "get_file_hash", "label": "本地哈希",
                  "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}}
             ]},
            {"id": "q2", "label": "服务端",
             "steps": [
                 {"id": "m2", "type": "get_server_hash", "label": "服务端哈希",
                  "config": {"file_path": "{{_relpath}}", "result_var": "_server_hash", "timeout": 10}}
             ]},
            {"id": "q3", "label": "同伴",
             "steps": [
                 {"id": "m3", "type": "get_peer_hash", "label": "同伴哈希",
                  "config": {"peer_id": "", "file_path": "{{_relpath}}", "result_var": "_peer_hash", "timeout": 10}}
             ]}
        ],
        "merge_at": [1],
        "tail": [
            {"id": "m4", "type": "compare", "label": "本地vs服务端",
             "config": {"left": "{{_local_hash}}", "op": "==", "right": "{{_server_hash}}"}},
            {"id": "m5", "type": "log", "label": "一致性结论",
             "config": {"message": "🔍 {{_relpath}} 一致={{_compare_result}} | 本地={{_local_hash}} 服务端={{_server_hash}} 同伴={{_peer_hash}}"}}
        ]
    },
    "manual_mirror": {
        "name": "手动镜像",
        "queues": [
            {"id": "q1", "label": "镜像",
             "steps": [
                 {"id": "m1", "type": "mirror_from_peer", "label": "从同伴镜像",
                  "config": {"peer_id": "", "folder": "@folder:{{_folder}}", "result_var": "_mirror_ok"}},
                 {"id": "m2", "type": "log", "label": "镜像结果",
                  "config": {"message": "🪞 镜像请求已发出: {{_mirror_ok}} | 目录: {{_folder}}"}}
             ]}
        ],
        "merge_at": []
    },
    "auto_backup": {
        "name": "自动备份到服务端",
        "queues": [
            {"id": "q1", "label": "备份校验",
             "steps": [
                 {"id": "b1", "type": "get_file_hash", "label": "本地哈希",
                  "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}},
                 {"id": "b2", "type": "backup_to_server", "label": "校验服务端备份",
                  "config": {"file_path": "{{_relpath}}", "result_var": "_backup_ok"}},
                 {"id": "b3", "type": "compare", "label": "是否已有备份",
                  "config": {"left": "{{_backup_ok}}", "op": "==", "right": "true"}},
                 {"id": "b4", "type": "log", "label": "备份结论",
                  "config": {"message": "☁ {{_relpath}} 服务端备份: {{_backup_ok}} | 本地哈希 {{_local_hash}}"}}
             ]}
        ],
        "merge_at": []
    },
    "cleanup_tmp": {
        "name": "临时文件清理",
        "queues": [
            {"id": "q1", "label": "清理",
             "steps": [
                 {"id": "c1", "type": "scan_dir", "label": "扫描临时文件",
                  "config": {"pattern": "*.tmp", "recursive": True, "result_var": "_tmp"}},
                 {"id": "c2", "type": "get_path_info", "label": "本地哈希",
                  "config": {"source": "local", "file_path": "@folder:{{_folder}}", "info_type": "count", "result_var": "_cnt"}},
                 {"id": "c3", "type": "delete_file", "label": "删除临时文件",
                  "config": {"path": "@folder:{{_folder}}/x.tmp"}},
                 {"id": "c4", "type": "log", "label": "清理完成",
                  "config": {"message": "🧹 清理完成: {{_relpath}}"}}
             ]}
        ],
        "merge_at": []
    },

    # ── 备份流程系列（可在编辑器“模板”下拉里直接选） ──
    "backup_on_change": {
        "name": "① 变更即备份",
        "description": "文件一有变更就推一份到服务端备份，并立刻校验落盘哈希是否一致",
        "steps": [
            {"id": "b1", "type": "log", "label": "开始备份",
             "config": {"message": "🚀 开始备份 {{_relpath}}"}},
            {"id": "b2", "type": "get_file_hash", "label": "计算本地哈希",
             "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}},
            {"id": "b3", "type": "upload_to_server", "label": "上传到服务端",
             "config": {"file_path": "{{_full_path}}", "remote_path": "{{_relpath}}", "result_var": "_upload_ok"}},
            {"id": "b4", "type": "verify_backup", "label": "校验备份一致",
             "config": {"file_path": "{{_full_path}}", "remote_path": "{{_relpath}}", "result_var": "_verify_ok"}},
            {"id": "b5", "type": "branch", "label": "按校验结果分支",
             "config": {"var": "_verify_ok",
                        "true_steps": [{"id": "b5a", "type": "log",
                                        "config": {"message": "✅ 备份完成且校验通过: {{_relpath}}"}}],
                        "false_steps": [{"id": "b5b", "type": "log",
                                         "config": {"message": "❌ 备份校验失败: {{_relpath}}（上传={{_upload_ok}} 服务端={{_server_hash}}）"}}]}}
        ]
    },
    "backup_zip_daily": {
        "name": "② 每日打包归档",
        "description": "把整个同步目录打包成带时间戳的 zip，上传服务端，并生成 Markdown 清单",
        "steps": [
            {"id": "z1", "type": "scan_dir", "label": "统计待归档文件",
             "config": {"folder": "@folder:{{_folder}}", "pattern": "*", "result_var": "_files"}},
            {"id": "z2", "type": "archive_zip", "label": "打包为 zip",
             "config": {"src": "@folder:{{_folder}}", "zip_path": "{{_client_dir}}/archives/工作文档-{{_timestamp}}.zip", "result_var": "_zip_path"}},
            {"id": "z3", "type": "upload_to_server", "label": "上传归档包",
             "config": {"file_path": "{{_zip_path}}", "remote_path": "archives/{{_timestamp}}.zip", "result_var": "_upload_ok"}},
            {"id": "z4", "type": "manifest", "label": "生成归档清单",
             "config": {"dir": "@folder:{{_folder}}", "pattern": "*", "out_path": "{{_client_dir}}/archives/清单-{{_timestamp}}.md", "format": "md", "result_var": "_manifest_path"}},
            {"id": "z5", "type": "log", "label": "汇总",
             "config": {"message": "📦 归档完成: {{_zip_path}} · {{_scan_count}} 个文件在包内"}}
        ]
    },
    "bulk_backup_folder": {
        "name": "③ 整目录批量备份",
        "description": "扫描同步目录并逐个文件上传（首次补齐历史文件、或大改后全量重传）",
        "steps": [
            {"id": "k1", "type": "scan_dir", "label": "扫描目录",
             "config": {"folder": "@folder:{{_folder}}", "pattern": "*", "result_var": "_files"}},
            {"id": "k2", "type": "log", "label": "开始批量上传",
             "config": {"message": "📂 待备份 {{_scan_count}} 个文件"}},
            {"id": "k3", "type": "foreach", "label": "逐文件上传",
             "config": {"list_var": "_files", "max_items": 500, "steps": [
                 {"id": "k3a", "type": "upload_to_server",
                  "config": {"file_path": "{{_item_path}}", "remote_path": "{{_item}}", "result_var": "_upload_ok"}},
                 {"id": "k3b", "type": "log",
                  "config": {"message": "⬆ [{{_index}}] {{_item}} → {{_upload_ok}}"}}
             ]}},
            {"id": "k4", "type": "log", "label": "批量结果",
             "config": {"message": "✅ 批量备份完成: {{_foreach_ok}}/{{_foreach_total}} 成功"}}
        ]
    },
    "backup_manifest_report": {
        "name": "④ 备份清单报告",
        "description": "为同步目录生成含 SHA256 的清单（Markdown + JSON），并把 Markdown 上传留档",
        "steps": [
            {"id": "m1", "type": "manifest", "label": "生成 Markdown 清单",
             "config": {"dir": "@folder:{{_folder}}", "pattern": "*", "out_path": "{{_client_dir}}/manifests/清单-{{_timestamp}}.md", "format": "md", "result_var": "_manifest_md"}},
            {"id": "m2", "type": "manifest", "label": "生成 JSON 清单",
             "config": {"dir": "@folder:{{_folder}}", "pattern": "*", "out_path": "{{_client_dir}}/manifests/清单-{{_timestamp}}.json", "format": "json", "result_var": "_manifest_json"}},
            {"id": "m3", "type": "upload_to_server", "label": "上传清单到服务端",
             "config": {"file_path": "{{_manifest_md}}", "remote_path": "manifests/{{_timestamp}}.md", "result_var": "_upload_ok"}},
            {"id": "m4", "type": "log", "label": "汇总",
             "config": {"message": "📑 清单完成: {{_manifest_count}} 个文件 · {{_manifest_md}}"}}
        ]
    },
    "backup_then_notify": {
        "name": "⑤ 备份并通知",
        "description": "备份并校验后调用 Webhook；成功发完成通知，失败发告警（钉钉/企业微信/Slack 改 URL 即可）",
        "steps": [
            {"id": "n1", "type": "upload_to_server", "label": "上传备份",
             "config": {"file_path": "{{_full_path}}", "remote_path": "{{_relpath}}", "result_var": "_upload_ok"}},
            {"id": "n2", "type": "verify_backup", "label": "校验备份",
             "config": {"file_path": "{{_full_path}}", "remote_path": "{{_relpath}}", "result_var": "_verify_ok"}},
            {"id": "n3", "type": "branch", "label": "成功分支",
             "config": {"var": "_verify_ok",
                        "true_steps": [{"id": "n3a", "type": "http_request", "label": "完成通知",
                                        "config": {"url": "https://example.com/webhook", "method": "POST",
                                                   "headers": "{\"Content-Type\": \"application/json\"}",
                                                   "body": "{\"msg\": \"✅ 备份完成: {{_relpath}}\"}",
                                                   "result_var": "_notify_result"}}],
                        "false_steps": [{"id": "n3b", "type": "http_request", "label": "失败告警",
                                         "config": {"url": "https://example.com/webhook", "method": "POST",
                                                    "headers": "{\"Content-Type\": \"application/json\"}",
                                                    "body": "{\"msg\": \"❌ 备份失败: {{_relpath}}\"}",
                                                    "result_var": "_notify_result"}}]}},
            {"id": "n4", "type": "log", "label": "汇总",
             "config": {"message": "🔔 通知已发送（备份={{_upload_ok}} 校验={{_verify_ok}}）"}}
        ]
    },
    "backup_retention": {
        "name": "⑥ 归档轮转清理",
        "description": "清理超过保留期的历史归档包，默认只预演列出；确认无误后把 dry_run 改成 false",
        "steps": [
            {"id": "r1", "type": "scan_dir", "label": "统计现有归档",
             "config": {"folder": "{{_client_dir}}/archives", "pattern": "*.zip", "result_var": "_archives"}},
            {"id": "r2", "type": "cleanup_old", "label": "找出过期归档（预演）",
             "config": {"dir": "{{_client_dir}}/archives", "pattern": "*.zip", "days": 30, "dry_run": True, "result_var": "_cleanup_count"}},
            {"id": "r3", "type": "manifest", "label": "记录归档现状",
             "config": {"dir": "{{_client_dir}}/archives", "pattern": "*", "out_path": "{{_client_dir}}/archives/index.md", "format": "md", "result_var": "_manifest_path"}},
            {"id": "r4", "type": "log", "label": "汇总",
             "config": {"message": "🧹 现有 {{_scan_count}} 个归档，过期命中 {{_cleanup_count}} 个（预演，未删除）"}}
        ]
    },
    "three_way_guard": {
        "name": "⑦ 三方一致性守护",
        "description": "同时比对本地、服务端备份、同伴副本的哈希，任何一处缺失或不一致都会明确告警",
        "steps": [
            {"id": "g1", "type": "get_file_hash", "label": "本地哈希",
             "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}},
            {"id": "g2", "type": "get_server_hash", "label": "服务端哈希",
             "config": {"file_path": "{{_relpath}}", "result_var": "_server_hash"}},
            {"id": "g3", "type": "get_peer_hash", "label": "同伴哈希",
             "config": {"peer_id": "", "file_path": "{{_relpath}}", "result_var": "_peer_hash"}},
            {"id": "g4", "type": "compare", "label": "本地 vs 服务端",
             "config": {"left": "{{_local_hash}}", "op": "==", "right": "{{_server_hash}}", "result_var": "_server_ok"}},
            {"id": "g5", "type": "compare", "label": "本地 vs 同伴",
             "config": {"left": "{{_local_hash}}", "op": "==", "right": "{{_peer_hash}}", "result_var": "_peer_ok"}},
            {"id": "g6", "type": "branch", "label": "服务端结论",
             "config": {"var": "_server_ok",
                        "true_steps": [{"id": "g6a", "type": "log", "config": {"message": "✅ 服务端备份与本地一致"}}],
                        "false_steps": [{"id": "g6b", "type": "log", "config": {"message": "❌ 服务端备份不一致或缺失（服务端={{_server_hash}}）"}}]}},
            {"id": "g7", "type": "branch", "label": "同伴结论",
             "config": {"var": "_peer_ok",
                        "true_steps": [{"id": "g7a", "type": "log", "config": {"message": "✅ 同伴副本与本地一致"}}],
                        "false_steps": [{"id": "g7b", "type": "log", "config": {"message": "⚠ 同伴副本不一致或离线（同伴={{_peer_hash}}）"}}]}}
        ]
    },
    "backup_health_check": {
        "name": "⑧ 备份健康巡检",
        "description": "逐个文件检查服务端是否已有相同哈希的备份，把缺失清单追加写入报告文件",
        "steps": [
            {"id": "h1", "type": "scan_dir", "label": "扫描同步目录",
             "config": {"folder": "@folder:{{_folder}}", "pattern": "*", "result_var": "_files"}},
            {"id": "h2", "type": "write_file", "label": "写入报告头",
             "config": {"file_path": "{{_client_dir}}/reports/备份巡检-{{_timestamp}}.md", "mode": "overwrite",
                        "content": "# 备份巡检报告\n\n目录: @folder:{{_folder}}\n\n"}},
            {"id": "h3", "type": "foreach", "label": "逐文件体检",
             "config": {"list_var": "_files", "max_items": 500, "steps": [
                 {"id": "h3a", "type": "get_file_hash",
                  "config": {"file_path": "{{_item_path}}", "result_var": "_local_hash"}},
                 {"id": "h3b", "type": "get_server_hash",
                  "config": {"file_path": "{{_item}}", "result_var": "_server_hash"}},
                 {"id": "h3c", "type": "compare",
                  "config": {"left": "{{_local_hash}}", "op": "==", "right": "{{_server_hash}}", "result_var": "_verify_ok"}},
                 {"id": "h3d", "type": "branch",
                  "config": {"var": "_verify_ok",
                             "true_steps": [{"id": "h3e", "type": "log",
                                             "config": {"message": "✅ {{_item}}"}}],
                             "false_steps": [{"id": "h3f", "type": "write_file",
                                              "config": {"file_path": "{{_client_dir}}/reports/备份巡检-{{_timestamp}}.md",
                                                         "mode": "append",
                                                         "content": "- ❌ 缺失/不一致: {{_item}}\n"}}]}}
             ]}},
            {"id": "h4", "type": "log", "label": "汇总",
             "config": {"message": "🩺 巡检完成: {{_foreach_ok}}/{{_foreach_total}} 个文件有完整备份"}}
        ]
    },
    "encrypted_backup": {
        "name": "⑨ 加密备份",
        "description": "先做 AES-256-GCM 加密再上传，服务端只留密文；随后打一个版本点（密钥来自客户端配置，不进流程文件）",
        "steps": [
            {"id": "e1", "type": "get_file_hash", "label": "计算明文哈希",
             "config": {"file_path": "{{_full_path}}", "result_var": "_local_hash"}},
            {"id": "e2", "type": "encrypt_file", "label": "加密文件",
             "config": {"file_path": "{{_full_path}}",
                        "key_file": "{{_backup_key_file}}",
                        "passphrase_secret": "backup_passphrase",
                        "out_path": "{{_client_dir}}/vault/{{_relpath}}.enc",
                        "result_var": "_enc_path"}},
            {"id": "e3", "type": "upload_to_server", "label": "上传密文",
             "config": {"file_path": "{{_enc_path}}", "remote_path": "vault/{{_relpath}}.enc",
                        "result_var": "_upload_ok"}},
            {"id": "e4", "type": "snapshot_version", "label": "打版本点",
             "config": {"file_path": "vault/{{_relpath}}.enc", "reason": "encrypted-backup",
                        "result_var": "_version_id"}},
            {"id": "e5", "type": "log", "label": "汇总",
             "config": {"message": "🔐 加密备份完成: {{_relpath}} → vault/{{_relpath}}.enc · 版本 {{_version_id}}"}}
        ]
    },
    "rollback_latest": {
        "name": "⑩ 一键回滚上一版",
        "description": "列出服务端历史版本：有版本就回滚到上一版并把该版本取回本地留证；没有版本则明确报错",
        "steps": [
            {"id": "r1", "type": "list_versions", "label": "列出版本",
             "config": {"file_path": "{{_relpath}}", "result_var": "_versions"}},
            {"id": "r2", "type": "compare", "label": "有历史版本吗",
             "config": {"left": "{{_version_count}}", "op": ">", "right": "0", "result_var": "_has_version"}},
            {"id": "r3", "type": "branch", "label": "按结果分支",
             "config": {"var": "_has_version",
                        "true_steps": [
                            {"id": "r3a", "type": "restore_version", "label": "服务端回滚",
                             "config": {"file_path": "{{_relpath}}", "version": "latest",
                                        "result_var": "_restore_ok"}},
                            {"id": "r3b", "type": "fetch_version", "label": "取回本地留证",
                             "config": {"file_path": "{{_relpath}}", "version": "latest",
                                        "out_path": "{{_client_dir}}/restored/{{_relpath}}",
                                        "result_var": "_fetched_path"}},
                            {"id": "r3c", "type": "log",
                             "config": {"message": "⏪ 已回滚到上一版并取回: {{_fetched_path}}"}}
                        ],
                        "false_steps": [
                            {"id": "r3d", "type": "log",
                             "config": {"message": "⚠ 还没有历史版本可回滚: {{_relpath}}（服务端覆盖备份时会自动留版本）"}}
                        ]}}
        ]
    },
    "version_audit": {
        "name": "⑪ 版本巡检",
        "description": "扫描同步目录，逐个文件检查服务端留了几个历史版本并汇总",
        "steps": [
            {"id": "v1", "type": "scan_dir", "label": "扫描目录",
             "config": {"folder": "@folder:{{_folder}}", "pattern": "*", "result_var": "_files"}},
            {"id": "v2", "type": "log", "label": "开始巡检",
             "config": {"message": "🕘 巡检 {{_scan_count}} 个文件的版本历史"}},
            {"id": "v3", "type": "foreach", "label": "逐文件查版本",
             "config": {"list_var": "_files", "max_items": 300, "steps": [
                 {"id": "v3a", "type": "list_versions",
                  "config": {"file_path": "{{_item}}", "result_var": "_versions"}},
                 {"id": "v3b", "type": "log",
                  "config": {"message": "· {{_item}} → {{_version_count}} 个历史版本"}}
             ]}},
            {"id": "v4", "type": "log", "label": "汇总",
             "config": {"message": "🕘 版本巡检完成: {{_foreach_total}} 个文件"}}
        ]
    }
}


# 模板分类与说明（编辑器“模板”下拉用；键必须与 FLOW_TEMPLATES 对应）
TEMPLATE_META = {
    "backup_on_change":      ("备份", "变更即上传服务端并校验哈希"),
    "backup_zip_daily":      ("备份", "整目录打包 zip 后上传归档"),
    "bulk_backup_folder":    ("备份", "逐文件批量上传，首次补齐历史文件"),
    "backup_manifest_report": ("备份", "生成含 SHA256 的清单并上传留档"),
    "auto_backup":           ("备份", "变更自动备份（哈希比对）"),
    "full_pipeline":         ("备份", "哈希/路径/服务端/HTTP 全链路流水线示例"),
    "hash_check":            ("校验", "本地、服务端、同伴三处哈希并行比对"),
    "hash_three_way":        ("校验", "三方哈希一致性检查"),
    "backup_verify":         ("校验", "备份后用哈希验证各处副本"),
    "three_way_guard":       ("校验", "三方一致性守护，缺失即告警"),
    "mirror_consistency":    ("同步", "镜像一致性比对"),
    "multi_folder_sync":     ("同步", "多目录并行扫描同步"),
    "manual_mirror":         ("同步", "手动触发同伴镜像"),
    "passive_sync":          ("同步", "被动同步（只记录不推送）"),
    "file_age_cleanup":      ("清理", "按文件年龄清理过期文件"),
    "cleanup_tmp":           ("清理", "清理临时文件"),
    "cleanup_scan_report":   ("清理", "清理扫描并输出报告"),
    "backup_retention":      ("清理", "归档轮转清理（默认预演）"),
    "webhook_notify":        ("通知", "文件变更时推送 Webhook"),
    "webhook_broadcast":     ("通知", "向多个地址广播通知"),
    "large_file_alert":      ("通知", "大文件告警并通知"),
    "backup_then_notify":    ("通知", "备份后通知，失败也告警"),
    "backup_health":         ("巡检", "备份健康检查（存在/大小/哈希）"),
    "backup_health_check":   ("巡检", "逐文件检查服务端备份缺失"),
    "sync_audit_log":        ("巡检", "同步审计日志落盘"),
    "folder_index_builder":  ("巡检", "生成目录索引"),
    "peer_status_probe":     ("巡检", "同伴在线状态探测"),
    "parallel_search":       ("巡检", "多车道并行搜索示例"),
    "command_inspection":    ("系统", "执行命令并巡检输出"),
    "encrypted_backup":      ("备份", "先加密再上传，服务端只存密文"),
    "rollback_latest":       ("版本", "一键回滚到上一版并取回本地"),
    "version_audit":         ("版本", "逐文件检查服务端历史版本数"),
}

# 补充预设工作流（单独模块维护，见 flow_templates_extra.py）
try:
    from .flow_templates_extra import EXTRA_META, EXTRA_TEMPLATES
    FLOW_TEMPLATES.update(EXTRA_TEMPLATES)
    TEMPLATE_META.update(EXTRA_META)
except Exception as _e:      # 缺文件时不影响主功能
    print('⚠ 补充模板加载失败:', _e)

TEMPLATE_CATEGORIES = ['备份', '版本', '校验', '同步', '清理', '通知', '巡检', '系统']


def _count_flow_steps(flow_def):
    """统计流程的步骤总数与用到的积木种类数（含 branch/foreach 子步骤）"""
    found = []

    def walk(steps):
        for s in steps or []:
            if not isinstance(s, dict):
                continue
            found.append(s.get('type', ''))
            cfg = s.get('config') or {}
            walk(cfg.get('true_steps'))
            walk(cfg.get('false_steps'))
            walk(cfg.get('steps'))

    walk(flow_def.get('steps'))
    for lane in (flow_def.get('queues') or flow_def.get('lanes') or []):
        walk(lane.get('steps'))
    walk(flow_def.get('tail'))
    return len(found), len({t for t in found if t})


def list_templates(category=''):
    """内置模板目录：[{key,name,category,description,steps,blocks}]"""
    order = {c: i for i, c in enumerate(TEMPLATE_CATEGORIES)}
    out = []
    for key, tpl in FLOW_TEMPLATES.items():
        cat, desc = TEMPLATE_META.get(key, ('其它', ''))
        if category and cat != category:
            continue
        n, nb = _count_flow_steps(tpl)
        out.append({'key': key, 'name': tpl.get('name', key), 'category': cat,
                    'description': desc or tpl.get('description', ''),
                    'steps': n, 'blocks': nb})
    out.sort(key=lambda x: (order.get(x['category'], 99), x['name']))
    return out


def _validate_step_list(errs, steps, where):
    for i, s in enumerate(steps):
        st = s.get('type', '')
        if not st or not BlockRegistry.get(st):
            errs.append('{} 步骤 {}: 无效类型 "{}"'.format(where, i + 1, st))
            continue
        if st == 'branch':
            cfg = s.get('config') or {}
            _validate_step_list(errs, cfg.get('true_steps', []), '{} 步骤 {} 真分支'.format(where, i + 1))
            _validate_step_list(errs, cfg.get('false_steps', []), '{} 步骤 {} 假分支'.format(where, i + 1))
        if st == 'foreach':
            cfg = s.get('config') or {}
            body = cfg.get('steps', [])
            if not body:
                errs.append('{} 步骤 {}: 遍历列表未配置子步骤'.format(where, i + 1))
            _validate_step_list(errs, body, '{} 步骤 {} 遍历体'.format(where, i + 1))


def validate_flow(flow_def: dict) -> List[str]:
    errs = []
    if not flow_def.get('name', '').strip():
        errs.append('流程名称不能为空')
    has_steps = bool(flow_def.get('steps'))
    has_lanes = bool(flow_def.get('queues') or flow_def.get('lanes'))
    if not has_steps and not has_lanes:
        errs.append('至少需要步骤或多队列之一')
    _validate_step_list(errs, flow_def.get('steps', []), '步骤')
    for li, lane in enumerate(flow_def.get('queues', []) or flow_def.get('lanes', [])):
        if not lane.get('id'):
            errs.append('队列 {}: 缺少 id'.format(li + 1))
        _validate_step_list(errs, lane.get('steps', []), '队列 {}'.format(li + 1))
    _validate_step_list(errs, flow_def.get('tail', []), '尾步')
    return errs

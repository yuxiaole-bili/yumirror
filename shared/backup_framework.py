"""
Backup Framework — 稳定 · 开放 · 可拓展
"""
import os, time, json, threading, traceback
from datetime import datetime
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

from .protocol import MsgType


class LogLevel(Enum):
    INFO = 'INFO'
    WARNING = 'WARNING'
    ERROR = 'ERROR'
    DEBUG = 'DEBUG'


class PipelineLogger:
    def __init__(self, log_dir: str, pipeline_name: str = 'backup', max_files: int = 120):
        self.log_dir = log_dir
        self.pipeline_name = pipeline_name
        self.max_files = max_files
        self._lock = threading.Lock()
        self._current_file: str = ''
        self._listeners: List[Callable[[str, str, str, str], None]] = []
        os.makedirs(log_dir, exist_ok=True)
        self._open_new_file()

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]

    def _open_new_file(self):
        ts = datetime.now().strftime('%Y%m%d%H%M%S')
        self._current_file = os.path.join(self.log_dir, f"{ts}_{self.pipeline_name}.log")

    def _rotate_if_needed(self):
        if os.path.isfile(self._current_file) and os.path.getsize(self._current_file) > 8 * 1024 * 1024:
            self._open_new_file()
            self._cleanup()

    def _cleanup(self):
        files = sorted(
            [f for f in os.listdir(self.log_dir) if f.endswith('.log')],
            key=lambda f: os.path.getmtime(os.path.join(self.log_dir, f))
        )
        while len(files) > self.max_files:
            try:
                os.remove(os.path.join(self.log_dir, files.pop(0)))
            except OSError:
                pass

    def add_listener(self, cb: Callable[[str, str, str, str], None]):
        self._listeners.append(cb)

    def _emit(self, module: str, level: str, message: str):
        ts = self._ts()
        line = f"{ts} [{module}] {level} - {message}"
        with self._lock:
            self._rotate_if_needed()
            try:
                with open(self._current_file, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass
        for cb in self._listeners:
            try:
                cb(module, level, message, ts)
            except Exception:
                pass

    def info(self, module: str, msg: str):    self._emit(module, 'INFO', msg)
    def warning(self, module: str, msg: str):  self._emit(module, 'WARNING', msg)
    def error(self, module: str, msg: str):    self._emit(module, 'ERROR', msg)
    def debug(self, module: str, msg: str):    self._emit(module, 'DEBUG', msg)


class EventType(Enum):
    FILE_RECEIVING = 'file_receiving'
    FILE_RECEIVED = 'file_received'
    FILE_CREATED = 'file_created'
    FILE_MODIFIED = 'file_modified'
    FILE_DELETED = 'file_deleted'
    FILE_RENAMED = 'file_renamed'
    CONFLICT = 'conflict_detected'
    HASH_OK = 'hash_verified'
    SNAPSHOT = 'snapshot'
    FLOW_TRIGGERED = 'flow_triggered'
    PIPELINE_START = 'pipeline_start'
    PIPELINE_END = 'pipeline_end'


@dataclass
class PipelineEvent:
    type: EventType
    folder_name: str = ''
    relpath: str = ''
    file_size: int = 0
    local_sha256: str = ''
    remote_sha256: str = ''
    mtime: float = 0
    full_path: str = ''
    source_device: str = ''
    target_device: str = ''
    src_relpath: str = ''
    dst_relpath: str = ''
    meta: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def clone(self, **overrides) -> 'PipelineEvent':
        d = {k: v for k, v in self.__dict__.items() if not k.startswith('_')}
        d.update(overrides)
        return PipelineEvent(**d)


class PipelineContext:
    def __init__(self, client_id: str = '', server_addr: str = ''):
        self.client_id = client_id
        self.server_addr = server_addr
        self.stats: Dict[str, int] = {'in': 0, 'out': 0, 'conflict': 0, 'error': 0, 'bytes': 0}
        self.vars: Dict[str, Any] = {}
        self.flags: Dict[str, bool] = {}
        self._lock = threading.Lock()

    def inc_stat(self, key: str, delta: int = 1):
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + delta

    def set_flag(self, key: str, value: bool = True):
        with self._lock:
            self.flags[key] = value

    def get_flag(self, key: str, default: bool = False) -> bool:
        return self.flags.get(key, default)


class PipelineHandler(ABC):
    def __init__(self, name: str):
        self.name = name
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    @abstractmethod
    def handle(self, event: PipelineEvent, ctx: PipelineContext, logger: PipelineLogger) -> Optional[PipelineEvent]:
        ...


class BackupPipeline:
    def __init__(self, name: str, logger: PipelineLogger):
        self.name = name
        self.logger = logger
        self.handlers: List[PipelineHandler] = []
        self._lock = threading.Lock()

    def add(self, handler: PipelineHandler, position: int = -1):
        with self._lock:
            if position < 0 or position >= len(self.handlers):
                self.handlers.append(handler)
            else:
                self.handlers.insert(position, handler)
        self.logger.info('pipeline', f'[{self.name}] 注册处理器: {handler.name} (共 {len(self.handlers)} 个)')

    def remove(self, handler_name: str) -> bool:
        with self._lock:
            before = len(self.handlers)
            self.handlers = [h for h in self.handlers if h.name != handler_name]
            if len(self.handlers) < before:
                self.logger.info('pipeline', f'[{self.name}] 移除处理器: {handler_name}')
                return True
        return False

    def get(self, handler_name: str) -> Optional[PipelineHandler]:
        for h in self.handlers:
            if h.name == handler_name:
                return h
        return None

    def run(self, event: PipelineEvent, ctx: PipelineContext) -> PipelineEvent:
        self.logger.info('pipeline',
            f'══ [{self.name}] 管道开始 | type={event.type.value} | '
            f'folder={event.folder_name} | file={event.relpath or "(无)"} ══')
        current = event
        for i, handler in enumerate(self.handlers, 1):
            if not handler.enabled:
                self.logger.info('pipeline', f'  [{i}/{len(self.handlers)}] {handler.name}: ⏸ 已暂停，跳过')
                continue
            t0 = time.time()
            self.logger.info('pipeline', f'  [{i}/{len(self.handlers)}] {handler.name}: → 开始')
            try:
                result = handler.handle(current, ctx, self.logger)
                elapsed = (time.time() - t0) * 1000
                if result is None:
                    self.logger.info('pipeline',
                        f'  [{i}/{len(self.handlers)}] {handler.name}: ⊘ 中断管道 (耗时 {elapsed:.0f}ms)')
                    break
                current = result
                self.logger.info('pipeline',
                    f'  [{i}/{len(self.handlers)}] {handler.name}: ← 完成 (耗时 {elapsed:.0f}ms)')
            except Exception as e:
                elapsed = (time.time() - t0) * 1000
                self.logger.error('pipeline',
                    f'  [{i}/{len(self.handlers)}] {handler.name}: ✕ 异常 → {e} (耗时 {elapsed:.0f}ms)')
                self.logger.debug('pipeline', traceback.format_exc().strip()[:500])
        self.logger.info('pipeline',
            f'══ [{self.name}] 管道结束 | type={event.type.value} | file={event.relpath or "(无)"} ══')
        return current


class FileReceiver(PipelineHandler):
    def __init__(self, get_conn, find_folder):
        super().__init__('file_receiver')
        self._get_conn = get_conn
        self._find_folder = find_folder

    def handle(self, event, ctx, logger):
        if event.type not in (EventType.FILE_RECEIVING,):
            return event
        fc = self._find_folder(event.relpath)
        if not fc:
            logger.error(self.name, f'无法定位文件夹: {event.relpath}')
            return event
        local_base = os.path.abspath(fc['path'])
        full = os.path.join(local_base, event.relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        from shared.sync_core import compute_sha256
        old_sha = compute_sha256(full) if os.path.isfile(full) else ''
        if old_sha:
            logger.info(self.name, f'本地已有文件 | SHA256={old_sha[:16]}... | 大小={os.path.getsize(full)} 字节')
        conn = self._get_conn()
        if not conn:
            logger.error(self.name, '连接已断开，无法接收')
            return event
        logger.info(self.name,
            f'→ 接收文件 | 来源={event.source_device} | '
            f'目标={full} | 期望SHA256={event.remote_sha256[:16] if event.remote_sha256 else "?"}...')
        try:
            from shared.sync_core import FileTransfer
            meta = {'relpath': event.relpath, 'sha256': event.remote_sha256,
                    'size': event.file_size, 'mtime': event.mtime or 0}
            FileTransfer.recv_file(conn, local_base, meta, 'overwrite')
            new_sha = compute_sha256(full) if os.path.isfile(full) else ''
            actual_size = os.path.getsize(full) if os.path.isfile(full) else 0
            ctx.inc_stat('in'); ctx.inc_stat('bytes', actual_size)
            logger.info(self.name,
                f'← 接收完成 | SHA256={new_sha[:16] if new_sha else "?"}... | '
                f'实际大小={actual_size} 字节')
            return event.clone(type=EventType.FILE_RECEIVED, local_sha256=new_sha,
                               file_size=actual_size, full_path=full)
        except Exception as e:
            logger.error(self.name, f'接收失败: {e}')
            ctx.inc_stat('error')
            return event


class FileSender(PipelineHandler):
    def __init__(self, get_conn, find_folder, folder_enabled):
        super().__init__('file_sender')
        self._get_conn = get_conn
        self._find_folder = find_folder
        self._enabled_check = folder_enabled

    def handle(self, event, ctx, logger):
        if event.type not in (EventType.FILE_CREATED, EventType.FILE_MODIFIED):
            return event
        if not self._enabled_check(event.folder_name):
            logger.info(self.name, f'文件夹 [{event.folder_name}] 禁止主动推送，跳过')
            return event
        fc = self._find_folder(event.relpath)
        if not fc:
            logger.error(self.name, f'无法定位文件夹: {event.relpath}')
            return event
        conn = self._get_conn()
        if not conn:
            logger.error(self.name, '连接已断开，无法发送')
            return event
        local_base = os.path.abspath(fc['path'])
        full = os.path.join(local_base, event.relpath)
        if not os.path.isfile(full):
            logger.warning(self.name, f'文件不存在: {full}')
            return event
        actual_size = os.path.getsize(full)
        from shared.sync_core import compute_sha256
        sha = compute_sha256(full) or ''
        logger.info(self.name,
            f'→ 发送文件 | 文件={event.relpath} | SHA256={sha[:16]}... | 大小={actual_size} 字节')
        try:
            from shared.sync_core import FileTransfer
            if FileTransfer.send_file(conn, local_base, event.relpath):
                ctx.inc_stat('out'); ctx.inc_stat('bytes', actual_size)
                logger.info(self.name, f'← 发送完成 | SHA256={sha[:16]}... | 大小={actual_size} 字节')
            else:
                logger.error(self.name, '发送失败')
                ctx.inc_stat('error')
        except Exception as e:
            logger.error(self.name, f'发送异常: {e}')
            ctx.inc_stat('error')
        return event


class DeleteHandler(PipelineHandler):
    def __init__(self, get_conn, find_folder, folder_enabled):
        super().__init__('delete_handler')
        self._get_conn = get_conn
        self._find_folder = find_folder
        self._enabled_check = folder_enabled

    def handle(self, event, ctx, logger):
        if event.type != EventType.FILE_DELETED:
            return event
        fc = self._find_folder(event.relpath)
        if not fc:
            return event
        local_base = os.path.abspath(fc['path'])
        full = os.path.join(local_base, event.relpath)
        if event.source_device == ctx.client_id:
            if not self._enabled_check(event.folder_name):
                return event
            conn = self._get_conn()
            if conn:
                logger.info(self.name, f'→ 通知远端删除: {event.relpath}')
                conn.send(MsgType.FILE_DELETE, event.relpath.encode())
                ctx.inc_stat('out')
        else:
            if os.path.exists(full):
                logger.info(self.name, f'← 执行远端删除: {event.relpath}')
                os.remove(full)
                ctx.inc_stat('in')
        return event


class RenameHandler(PipelineHandler):
    def __init__(self, get_conn, find_folder, folder_enabled):
        super().__init__('rename_handler')
        self._get_conn = get_conn
        self._find_folder = find_folder
        self._enabled_check = folder_enabled

    def handle(self, event, ctx, logger):
        if event.type != EventType.FILE_RENAMED:
            return event
        fc = self._find_folder(event.src_relpath or event.relpath)
        if not fc:
            return event
        local_base = os.path.abspath(fc['path'])
        if event.source_device == ctx.client_id:
            if not self._enabled_check(event.folder_name):
                return event
            conn = self._get_conn()
            if conn:
                payload = event.src_relpath.encode() + b'\x00' + event.dst_relpath.encode()
                logger.info(self.name, f'→ 通知远端重命名: {event.src_relpath} → {event.dst_relpath}')
                conn.send(MsgType.FILE_RENAME, payload)
        else:
            import shutil
            sf = os.path.join(local_base, event.src_relpath)
            df = os.path.join(local_base, event.dst_relpath)
            if os.path.exists(sf):
                os.makedirs(os.path.dirname(df), exist_ok=True)
                logger.info(self.name, f'← 执行远端重命名: {event.src_relpath} → {event.dst_relpath}')
                shutil.move(sf, df)
        return event


class ConflictDetector(PipelineHandler):
    def __init__(self):
        super().__init__('conflict_detector')

    def handle(self, event, ctx, logger):
        if event.type != EventType.FILE_RECEIVED:
            return event
        local = event.local_sha256
        remote = event.remote_sha256
        if not local or not remote:
            return event
        if local != remote:
            logger.warning(self.name,
                f'⚠ 哈希冲突 | 文件={event.relpath} | '
                f'本地SHA256={local[:16]}... | 远端SHA256={remote[:16]}...')
            ctx.inc_stat('conflict')
            return event.clone(type=EventType.CONFLICT)
        else:
            logger.info(self.name, f'✅ 哈希一致 | 文件={event.relpath} | SHA256={local[:16]}...')
            return event.clone(type=EventType.HASH_OK)


class HashVerifier(PipelineHandler):
    def __init__(self, rpc_peer_hash, rpc_server_hash, get_peers):
        super().__init__('hash_verifier')
        self._peer_hash = rpc_peer_hash
        self._server_hash = rpc_server_hash
        self._get_peers = get_peers

    def handle(self, event, ctx, logger):
        if event.type not in (EventType.HASH_OK, EventType.CONFLICT):
            return event
        if self._server_hash:
            logger.info(self.name, f'→ 请求服务端备份哈希 | 文件={event.relpath}')
            t0 = time.time()
            try:
                sha = self._server_hash(event.relpath)
                elapsed = (time.time() - t0) * 1000
                if sha:
                    match = '✅' if sha == event.local_sha256 else '⚠ 不一致'
                    logger.info(self.name, f'← 服务端响应 | SHA256={sha[:32]}... | {match} | 耗时={elapsed:.0f}ms')
                    ctx.vars['server_hash'] = sha
                    ctx.vars['server_match'] = (sha == event.local_sha256)
                else:
                    logger.warning(self.name, f'← 服务端无响应/无备份 | 耗时={elapsed:.0f}ms')
            except Exception as e:
                logger.error(self.name, f'服务端请求异常: {e}')
        peers = self._get_peers() if self._get_peers else []
        for peer in peers:
            if peer == ctx.client_id:
                continue
            logger.info(self.name, f'→ 请求同伴哈希 | 同伴={peer} | 文件={event.relpath}')
            t0 = time.time()
            try:
                sha = self._peer_hash(peer, event.relpath)
                elapsed = (time.time() - t0) * 1000
                if sha:
                    match = '✅' if sha == event.local_sha256 else '⚠ 不一致'
                    logger.info(self.name, f'← 同伴响应 | 同伴={peer} | SHA256={sha[:32]}... | {match} | 耗时={elapsed:.0f}ms')
                else:
                    logger.warning(self.name, f'← 同伴无响应 | 同伴={peer} | 耗时={elapsed:.0f}ms')
            except Exception as e:
                logger.error(self.name, f'同伴请求异常 | 同伴={peer}: {e}')
        return event


class FlowBridge(PipelineHandler):
    def __init__(self, flow_provider, flow_executor):
        super().__init__('flow_bridge')
        self._provider = flow_provider
        self._executor = flow_executor

    def handle(self, event, ctx, logger):
        flow_name = self._provider(event.folder_name)
        if not flow_name:
            return event
        logger.info(self.name, f'→ 触发流程 | 流程="{flow_name}" | 文件夹={event.folder_name} | 文件={event.relpath}')
        try:
            result = self._executor(flow_name, event, ctx, logger)
            if result:
                logger.info(self.name, f'← 流程执行完成 | 流程="{flow_name}"')
                return result
            else:
                logger.info(self.name, f'← 流程执行完成（无修改）')
        except Exception as e:
            logger.error(self.name, f'流程执行异常: {e}')
        return event


class StatsCollector(PipelineHandler):
    def __init__(self, external_stats, stats_lock):
        super().__init__('stats_collector')
        self._ext = external_stats
        self._lock = stats_lock

    def handle(self, event, ctx, logger):
        with self._lock:
            self._ext['in'] = ctx.stats.get('in', 0)
            self._ext['out'] = ctx.stats.get('out', 0)
            self._ext['conflict'] = ctx.stats.get('conflict', 0)
            self._ext['error'] = ctx.stats.get('error', 0)
        return event


class PipelineFactory:
    @staticmethod
    def create_inbound(
        pipeline_name, log_dir, get_conn, find_folder, folder_enabled=None,
        rpc_peer_hash=None, rpc_server_hash=None, get_peers=None,
        flow_provider=None, flow_executor=None,
        external_stats=None, stats_lock=None,
    ):
        logger = PipelineLogger(log_dir, pipeline_name)
        ctx = PipelineContext()
        pipeline = BackupPipeline(pipeline_name, logger)
        pipeline.add(FileReceiver(get_conn, find_folder))
        pipeline.add(ConflictDetector())
        if rpc_server_hash or rpc_peer_hash:
            pipeline.add(HashVerifier(rpc_peer_hash, rpc_server_hash, get_peers))
        if flow_provider and flow_executor:
            pipeline.add(FlowBridge(flow_provider, flow_executor))
        if folder_enabled:
            pipeline.add(DeleteHandler(get_conn, find_folder, folder_enabled))
            pipeline.add(RenameHandler(get_conn, find_folder, folder_enabled))
        if external_stats:
            pipeline.add(StatsCollector(external_stats, stats_lock or threading.Lock()))
        return pipeline, logger, ctx

    @staticmethod
    def create_outbound(
        pipeline_name, log_dir, get_conn, find_folder, folder_enabled,
        external_stats=None, stats_lock=None,
    ):
        logger = PipelineLogger(log_dir, pipeline_name)
        ctx = PipelineContext()
        pipeline = BackupPipeline(pipeline_name, logger)
        pipeline.add(FileSender(get_conn, find_folder, folder_enabled))
        pipeline.add(DeleteHandler(get_conn, find_folder, folder_enabled))
        pipeline.add(RenameHandler(get_conn, find_folder, folder_enabled))
        if external_stats:
            pipeline.add(StatsCollector(external_stats, stats_lock or threading.Lock()))
        return pipeline, logger, ctx

"""
同步核心引擎 v3
"""

import os
import json
import time
import hashlib
import shutil
import fnmatch
import threading
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .protocol import Connection, MsgType

CHUNK_SIZE = 65536


# ============================================================
# 同步模式
# ============================================================
class SyncMode:
    MANUAL_CONFLICT = 1   # 模式1: 冲突时手动确认
    FULL_MIRROR     = 2   # 模式2: 完全镜像
    BACKUP_FALLBACK = 3   # 模式3: 冲突时从备份恢复

    @staticmethod
    def to_string(mode: int) -> str:
        return {1: "手动确认", 2: "完全镜像", 3: "备份恢复"}.get(mode, "未知")


# ============================================================
# 工具函数
# ============================================================
def compute_sha256(filepath: str) -> str | None:
    """计算文件 SHA256"""
    h = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def safe_relpath(path: str, base: str) -> str | None:
    """安全相对路径"""
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return None


def ensure_dir(path: str):
    """确保目录存在"""
    os.makedirs(path, exist_ok=True)


def scan_folder(folder: str, ignore_patterns: list | None = None, need_hash: bool = True) -> dict:
    """扫描文件夹，返回 {relpath: {size, mtime, sha256}}；need_hash=False 时跳过哈希（更快）"""
    result = {}
    if not os.path.isdir(folder):
        return result
    ignore = ignore_patterns or []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs
                   if not any(fnmatch.fnmatch(d, p) for p in ignore)]
        for fname in files:
            full = os.path.join(root, fname)
            rel = safe_relpath(full, folder)
            if rel is None:
                continue
            if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(fname, p)
                   for p in ignore):
                continue
            try:
                stat = os.stat(full)
                entry = {'size': stat.st_size, 'mtime': stat.st_mtime}
                if need_hash:
                    entry['sha256'] = compute_sha256(full)
                else:
                    entry['sha256'] = None
                result[rel] = entry
            except OSError:
                pass
    return result


# ============================================================
# 文件传输
# ============================================================
class FileTransfer:
    """文件传输工具"""

    @staticmethod
    def send_file(conn: Connection, local_dir: str, relpath: str,
                  remote_relpath: str | None = None) -> bool:
        """发送文件

        remote_relpath 用于指定对端保存的相对路径（默认与 relpath 相同）。
        流程引擎的“上传备份到服务端”用它把任意本地文件放到指定备份路径下。
        """
        full = os.path.join(local_dir, relpath)
        if not os.path.isfile(full):
            return False
        try:
            stat = os.stat(full)
            sha = compute_sha256(full)
            meta = json.dumps({
                'relpath': (remote_relpath or relpath),
                'size': stat.st_size,
                'mtime': stat.st_mtime,
                'sha256': sha,
            }).encode('utf-8')
            conn.send(MsgType.FILE_CREATE, meta)
            with open(full, 'rb') as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    conn.send(MsgType.FILE_DATA, chunk)
            conn.send(MsgType.FILE_DATA, b'')   # EOF
            return True
        except Exception as e:
            print(f"  ❌ 发送失败 {relpath}: {e}")
            return False

    @staticmethod
    def recv_file(conn: Connection, local_dir: str, meta: dict,
                  conflict_strategy: str = 'keep_both') -> str | None:
        """接收文件"""
        relpath = meta['relpath']
        size    = meta.get('size', 0)
        mtime   = meta.get('mtime') or time.time()
        full    = os.path.join(local_dir, relpath)
        ensure_dir(os.path.dirname(full))

        if os.path.exists(full):
            local_sha = compute_sha256(full)
            if local_sha == meta.get('sha256'):
                return relpath
            if conflict_strategy == 'keep_both':
                base, ext = os.path.splitext(full)
                backup = f"{base}_conflict_{int(time.time())}{ext}"
                shutil.copy2(full, backup)

        try:
            with open(full, 'wb') as f:
                received = 0
                while received < size:
                    result = conn.recv(timeout=30)
                    if result is None:
                        raise TimeoutError("接收超时")
                    msg_type, _, payload = result
                    if msg_type == MsgType.FILE_DATA:
                        if not payload:
                            break
                        f.write(payload)
                        received += len(payload)
            os.utime(full, (mtime, mtime))
            return relpath
        except Exception as e:
            print(f"  ❌ 接收失败 {relpath}: {e}")
            return None


# ============================================================
# 文件监控
# ============================================================
class SyncEventHandler(FileSystemEventHandler):
    """watchdog 文件事件处理器"""

    def __init__(self, cb_create, cb_modify, cb_delete, cb_rename,
                 ignore_check, local_dir: str):
        super().__init__()
        self._on_create  = cb_create
        self._on_modify  = cb_modify
        self._on_delete  = cb_delete
        self._on_rename  = cb_rename
        self._ignore     = ignore_check
        self._local_dir  = local_dir
        self._paused = threading.Event()
        self._paused.set()
        # 暂停期间（客户端正在处理入站消息）发生的事件不能直接丢：
        # 先缓存，恢复后补发，否则这些文件变更会永久漏同步（实机复现过）
        self._pending = []
        self._pending_lock = threading.Lock()
        self._max_pending = 2000

    def pause(self):
        self._paused.clear()

    def resume(self):
        self._paused.set()
        self._flush_pending()

    @property
    def is_paused(self) -> bool:
        return not self._paused.is_set()

    def _buffer(self, kind, *args):
        with self._pending_lock:
            if len(self._pending) < self._max_pending:
                self._pending.append((kind, args))

    def _flush_pending(self):
        with self._pending_lock:
            pending, self._pending = self._pending, []
        for kind, args in pending:
            try:
                if kind == 'create':
                    self._on_create(*args)
                elif kind == 'modify':
                    self._on_modify(*args)
                elif kind == 'delete':
                    self._on_delete(*args)
                elif kind == 'rename':
                    self._on_rename(*args)
            except Exception:
                pass

    def _rp(self, path: str) -> str | None:
        return safe_relpath(path, self._local_dir)

    def on_created(self, event):
        if event.is_directory:
            return
        rp = self._rp(event.src_path)
        if not (rp and not self._ignore(rp)):
            return
        if self.is_paused:
            self._buffer('create', rp)
            return
        time.sleep(0.15)
        self._on_create(rp)

    def on_modified(self, event):
        if event.is_directory:
            return
        rp = self._rp(event.src_path)
        if not (rp and not self._ignore(rp)):
            return
        if self.is_paused:
            self._buffer('modify', rp)
            return
        time.sleep(0.15)
        self._on_modify(rp)

    def on_deleted(self, event):
        if event.is_directory:
            return
        rp = self._rp(event.src_path)
        if not (rp and not self._ignore(rp)):
            return
        if self.is_paused:
            self._buffer('delete', rp)
            return
        self._on_delete(rp)

    def on_moved(self, event):
        if event.is_directory:
            return
        src = self._rp(event.src_path)
        dst = self._rp(event.dest_path)
        if not (src and dst and not self._ignore(src)):
            return
        if self.is_paused:
            self._buffer('rename', src, dst)
            return
        self._on_rename(src, dst)


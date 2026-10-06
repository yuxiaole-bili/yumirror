#!/usr/bin/env python3
"""
yumirror v3 - 通讯协议常量
"""

import socket
import threading


class MsgType:
    """消息类型枚举"""
    PING        = 0
    PONG        = 1
    HELLO       = 2
    HELLO_ACK   = 3
    BYE         = 4

    FILE_CREATE = 10
    FILE_DELETE = 11
    FILE_RENAME = 12
    FILE_ACK    = 13
    FILE_DELETE_ACK = 14
    FILE_DATA   = 15    # 👈 新增：文件数据块
    FILE_END    = 16    # 👈 新增：文件传输结束

    SNAPSHOT_SEND    = 20
    SNAPSHOT_REQUEST = 21
    SNAPSHOT_COMPARE = 22

    BACKUP_RESTORE = 25
    BACKUP_DATA    = 26

    FILE_HASH_REQUEST  = 30
    FILE_HASH_RESPONSE = 31

    SERVER_BACKUP_HASH_REQUEST  = 32
    SERVER_BACKUP_HASH_RESPONSE = 33

    PATH_INFO_REQUEST  = 34
    PATH_INFO_RESPONSE = 35

    # 历史版本：列出 / 打版本点 / 回滚 / 取回某版本内容
    VERSION_LIST_REQUEST  = 36
    VERSION_LIST_RESPONSE = 37
    VERSION_OP_REQUEST    = 38
    VERSION_OP_RESPONSE   = 39

    FLOW_SHARE_UPLOAD          = 50
    FLOW_SHARE_RESPONSE        = 51
    FLOW_SHARE_DOWNLOAD        = 52
    FLOW_SHARE_DOWNLOAD_RESPONSE = 53

    CONFLICT_ALERT = 40
    CONFLICT_CHOICE = 41

    @classmethod
    def name(cls, mt):
        return {v: k for k, v in cls.__dict__.items() if isinstance(v, int)}.get(mt, f'UNKNOWN({mt})')


class Connection:
    """TCP 消息封装"""
    HEADER_SIZE = 6

    def __init__(self, sock):
        self.sock = sock
        self._buf = b''
        # 收发可能来自多个线程（主循环 + RPC 等待方）：加锁保证一帧不会被两个读者切碎
        self._recv_lock = threading.RLock()

    def send(self, msg_type, payload=b''):
        pl = payload if isinstance(payload, bytes) else payload.encode()
        header = msg_type.to_bytes(2, 'big') + len(pl).to_bytes(4, 'big')
        self.sock.sendall(header + pl)

    def recv(self, timeout=None):
        with self._recv_lock:
            self.sock.settimeout(timeout)
            try:
                while len(self._buf) < self.HEADER_SIZE:
                    data = self.sock.recv(4096)
                    if not data:
                        raise ConnectionError("连接已关闭")
                    self._buf += data

                mt = int.from_bytes(self._buf[:2], 'big')
                length = int.from_bytes(self._buf[2:6], 'big')
                self._buf = self._buf[6:]

                while len(self._buf) < length:
                    data = self.sock.recv(4096)
                    if not data:
                        raise ConnectionError("连接已关闭")
                    self._buf += data

                payload = self._buf[:length]
                self._buf = self._buf[length:]
                return (mt, length, payload)
            except (socket.timeout, TimeoutError):
                return None

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

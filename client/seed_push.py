#!/usr/bin/env python3
"""yumirror v3 - 首次全量播种（把已存在的文件推给服务端备份）

为什么需要它：
    客户端只在 watchdog 捕获到 create/modify 事件时才把文件内容发给服务端，
    启动时发出的 SNAPSHOT 只包含文件名/大小/哈希（供同伴间镜像比对），
    并不携带内容。因此“客户端启动前就已存在”的文件不会进入服务端备份目录。
    本脚本用同一套协议做一次全量推送，把历史文件补齐。

安全性：
    服务端按文件 mtime 做「后写覆盖先写」判定，重复执行本脚本不会破坏已有备份，
    也不会把更旧的版本盖到更新的备份上。

用法：
    python client/seed_push.py                # 推送 config.json 里全部 sync_folders
    python client/seed_push.py 工作文档        # 只推送指定 name 的文件夹
"""
import json
import os
import socket
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from shared.protocol import Connection, MsgType          # noqa: E402
from shared.sync_core import scan_folder, FileTransfer   # noqa: E402

CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')


def load_config():
    if not os.path.isfile(CONFIG_PATH):
        print(f'找不到配置文件: {CONFIG_PATH}')
        print('请先复制 config.example.json 为 config.json 并按需修改。')
        sys.exit(2)
    with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def handshake(cfg):
    host = cfg.get('server_host', '127.0.0.1')
    port = int(cfg.get('server_port', 9999))
    print(f'连接服务端 {host}:{port} ...')
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(10)
    sock.connect((host, port))
    _tls = (cfg.get('tls') or {}) if isinstance(cfg, dict) else {}
    if _tls.get('enabled', True):
        from shared import tls_util
        if not tls_util.crypto_available():
            raise SystemExit('已启用 TLS 但缺少 cryptography：pip install cryptography')
        sock = tls_util.wrap_client(sock, tls_util.client_context(),
                                    _tls.get('server_hostname') or host)
        fp = tls_util.peer_fingerprint(sock)
        pinned = tls_util.normalize_fingerprint(_tls.get('fingerprint'))
        if pinned and pinned != fp:
            raise SystemExit(f'⛔ 服务端证书指纹不匹配（固定 {pinned} ≠ 实际 {fp}），已中止')
        if not pinned:
            print(f'  🔐 TLS 已连接，服务端指纹: {fp}（首次连接，未固定）')
    conn = Connection(sock)
    # 注意：必须用与常驻客户端不同的 client_id。
    # 服务端在同一 client_id 重新注册时会踢掉旧连接（去重逻辑），
    # 若沿用同一个 id，正在运行的客户端会立刻重连并把本脚本踢下线，传输中断。
    conn.send(MsgType.HELLO, json.dumps({
        'client_id': (cfg.get('client_id') or socket.gethostname()) + '-seed',
        'group_id': cfg.get('group_id', 'default'),
        'sync_folders': cfg.get('sync_folders', []),
    }).encode())
    r = conn.recv(timeout=15)
    if not r or r[0] != MsgType.HELLO_ACK:
        raise RuntimeError('握手失败：服务端未返回 HELLO_ACK')
    ack = json.loads(r[2].decode())
    if ack.get('status') != 'ok':
        raise RuntimeError(f"服务端拒绝: {ack.get('reason') or ack}")
    print(f"已连接: {ack.get('server')} · 组=[{ack.get('group_id')}] · 在线同伴={len(ack.get('peers_online', []))}")
    return conn


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ''
    cfg = load_config()
    ignore = cfg.get('ignore_patterns', [])
    folders = [fc for fc in cfg.get('sync_folders', []) if not only or fc.get('name') == only]
    if not folders:
        print('config.json 里没有匹配的 sync_folders')
        return 2

    conn = handshake(cfg)
    total_files = total_bytes = 0
    failed = []
    try:
        for fc in folders:
            lp = os.path.abspath(fc.get('path', ''))
            if not os.path.isdir(lp):
                print(f"  ! 跳过（目录不存在）: {fc.get('name')} -> {lp}")
                continue
            snap = scan_folder(lp, ignore, need_hash=False)
            print(f"  推送 [{fc.get('name')}] {len(snap)} 个文件 <- {lp}")
            for rel in sorted(snap):
                if FileTransfer.send_file(conn, lp, rel):
                    total_files += 1
                    total_bytes += snap[rel].get('size', 0)
                else:
                    failed.append(rel)
                time.sleep(0.01)   # 让服务端线程跟上，避免瞬时打满缓冲区
    finally:
        try:
            conn.send(MsgType.BYE)
            time.sleep(0.2)
        except Exception:
            pass
        conn.close()

    print(f'完成：推送 {total_files} 个文件 / {total_bytes} 字节'
          + (f'，失败 {len(failed)} 个: {failed[:5]}' if failed else ''))
    print('提示：服务端按 mtime 去重，重复执行是安全的。')
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())

"""
SSH 隧道管理器
"""

import os
import time
import socket
import threading
import select

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False
    paramiko = None


class SSHTunnel:
    """SSH 隧道"""

    def __init__(self, remote_host: str, remote_port: int = 22,
                 username: str = None, password: str = None,
                 private_key_path: str = None,
                 private_key_passphrase: str = None,
                 known_hosts_path: str = None):
        if not HAS_PARAMIKO:
            raise ImportError("需要 paramiko: pip install paramiko")

        self.remote_host = remote_host
        self.remote_port = remote_port
        self.username = username
        self.password = password
        self.private_key_path = private_key_path or os.path.expanduser('~/.ssh/id_rsa')
        self.private_key_passphrase = private_key_passphrase
        self.known_hosts_path = known_hosts_path or os.path.expanduser('~/.ssh/known_hosts')

        self._client: paramiko.SSHClient | None = None
        self._transport: paramiko.Transport | None = None
        self._forwarders: list = []
        self._running = False
        self._lock = threading.Lock()
        self._last_error: str | None = None

    # ---- 加载私钥 ----
    def _load_private_key(self):
        key_path = self.private_key_path
        if not key_path or not os.path.isfile(key_path):
            for default in ['~/.ssh/id_ed25519', '~/.ssh/id_rsa', '~/.ssh/id_ecdsa']:
                p = os.path.expanduser(default)
                if os.path.isfile(p):
                    key_path = p
                    break
            else:
                return None
        pw = self.private_key_passphrase
        for kc in [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey]:
            try:
                if pw:
                    return kc.from_private_key_file(key_path, password=pw)
                return kc.from_private_key_file(key_path)
            except Exception:
                continue
        return None

    # ---- 连接 ----
    def connect(self, timeout: float = 15.0) -> bool:
        with self._lock:
            try:
                self._client = paramiko.SSHClient()
                if os.path.isfile(self.known_hosts_path):
                    self._client.load_host_keys(self.known_hosts_path)
                self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                kwargs = {
                    'hostname': self.remote_host,
                    'port': self.remote_port,
                    'username': self.username,
                    'timeout': timeout,
                    'banner_timeout': timeout,
                    'auth_timeout': timeout,
                }
                pkey = self._load_private_key()
                if pkey:
                    kwargs['pkey'] = pkey
                elif self.password:
                    kwargs['password'] = self.password

                self._client.connect(**kwargs)
                self._transport = self._client.get_transport()
                self._running = True
                self._last_error = None
                return True

            except paramiko.AuthenticationException as e:
                self._last_error = f"SSH 认证失败: {e}"
                return False
            except socket.timeout:
                self._last_error = "SSH 连接超时"
                return False
            except Exception as e:
                self._last_error = f"SSH 连接失败: {e}"
                return False

    # ---- 本地端口转发 ----
    def add_local_forward(self, local_port: int,
                          remote_target_host: str,
                          remote_target_port: int) -> threading.Thread:
        if not self._transport or not self._running:
            raise RuntimeError("SSH 未连接")

        def worker():
            srv = None
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(('127.0.0.1', local_port))
                srv.listen(5)
                srv.settimeout(1.0)
                print(f"  🔗 本地转发: 127.0.0.1:{local_port} → "
                      f"{remote_target_host}:{remote_target_port} (SSH)")
                while self._running:
                    try:
                        cs, _ = srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    threading.Thread(
                        target=self._handle_forward,
                        args=(cs, remote_target_host, remote_target_port),
                        daemon=True
                    ).start()
            except Exception as e:
                self._last_error = f"本地转发异常: {e}"
            finally:
                if srv:
                    try:
                        srv.close()
                    except Exception:
                        pass

        t = threading.Thread(target=worker, daemon=True, name=f"lpf-{local_port}")
        t.start()
        self._forwarders.append(t)
        return t

    def _handle_forward(self, client_sock, remote_host, remote_port):
        channel = None
        try:
            channel = self._transport.open_channel(
                'direct-tcpip', (remote_host, remote_port), ('127.0.0.1', 0))
            if channel is None:
                client_sock.close()
                return
            sockets = [client_sock, channel]
            while True:
                try:
                    rlist, _, _ = select.select(sockets, [], [], 1.0)
                except (select.error, ValueError):
                    break
                if not rlist:
                    continue
                for reader in rlist:
                    writer = channel if reader is client_sock else client_sock
                    try:
                        data = reader.recv(65536)
                        if not data:
                            return
                        writer.sendall(data)
                    except (socket.error, OSError):
                        return
        except Exception:
            pass
        finally:
            if channel:
                try:
                    channel.close()
                except Exception:
                    pass
            try:
                client_sock.close()
            except Exception:
                pass

    # ---- 属性 ----
    @property
    def is_connected(self) -> bool:
        if not self._client:
            return False
        try:
            t = self._client.get_transport()
            return t is not None and t.is_active()
        except Exception:
            return False

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ---- 关闭 ----
    def close(self):
        self._running = False
        for t in self._forwarders:
            t.join(timeout=2)
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._forwarders.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


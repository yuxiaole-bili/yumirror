"""传输层 TLS：自签证书自动生成 + 指纹固定（TOFU，类似 SSH 的 known_hosts）

为什么是自签 + 指纹固定：
  局域网里没有公共 CA，服务端自己签一张证书，客户端**第一次连接时记住**它的 SHA256 指纹，
  以后每次连接都核对；指纹对不上就拒绝连接（中间人换了证书一定会对不上）。
  这正是 SSH 第一次问你 yes/no 之后就不再问的道理。

用法（两边都不需要改协议代码，只在 socket 外面套一层）：
  服务端: sock = tls_util.wrap_server(conn_sock, ctx)   # ctx 来自 server_context(cert, key)
  客户端: sock = tls_util.wrap_client(sock, client_context(), host)
          fp  = tls_util.peer_fingerprint(sock)          # 与配置里的固定指纹比对
"""
import hashlib
import os
import socket  # noqa: F401  (类型注解用)
import ssl
import stat

MIN_TLS = ssl.TLSVersion.TLSv1_2


def crypto_available():
    """TLS 依赖 cryptography（生成自签证书、以及加密积木）"""
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


def normalize_fingerprint(fp):
    """把 AA:BB:CC / aabbcc / 带空格 等各种写法统一成大写冒号分隔"""
    if not fp:
        return ''
    hexs = ''.join(c for c in str(fp) if c.lower() in '0123456789abcdef')
    return ':'.join(hexs[i:i + 2].upper() for i in range(0, len(hexs), 2))


def fingerprint_of_der(der):
    return ':'.join('%02X' % b for b in hashlib.sha256(der).digest())


def fingerprint_of_cert_file(path):
    """读 PEM 证书文件算 SHA256 指纹（与 openssl x509 -fingerprint -sha256 一致）"""
    with open(path, 'r', encoding='utf-8') as f:
        pem = f.read()
    der = ssl.PEM_cert_to_DER_cert(pem)
    return fingerprint_of_der(der)


def generate_self_signed(cert_path, key_path, common_name='yumirror',
                         days=3650, extra_ips=(), extra_dns=()):
    """生成 RSA2048 自签证书；私钥文件权限 0600。返回证书指纹。"""
    import datetime as dt
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, (common_name or 'yumirror')[:64]),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'yumirror'),
    ])
    san = []
    for ip in extra_ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    for d in extra_dns:
        san.append(x509.DNSName(d))
    if not san:
        san = [x509.DNSName('localhost')]
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))

    for p in (cert_path, key_path):
        d = os.path.dirname(os.path.abspath(p))
        if d:
            os.makedirs(d, exist_ok=True)
    with open(key_path, 'wb') as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)      # 0600
    except OSError:
        pass
    with open(cert_path, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return fingerprint_of_der(cert.public_bytes(serialization.Encoding.DER))


def server_context(cert_path, key_path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = MIN_TLS
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def client_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = MIN_TLS
    # 自签证书无法做 CA 链校验，身份靠"指纹固定"来保证，所以这里不校验证书链
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def wrap_server(sock, ctx, timeout=15):
    """服务端侧把已 accept 的裸 socket 包成 TLS；握手超时后恢复阻塞模式"""
    sock.settimeout(timeout)
    s = ctx.wrap_socket(sock, server_side=True)
    s.settimeout(None)
    return s


def wrap_client(sock, ctx, server_hostname=None, timeout=15):
    """客户端侧握手（带 SNI）"""
    sock.settimeout(timeout)
    s = ctx.wrap_socket(sock, server_hostname=server_hostname or None)
    return s


def peer_fingerprint(sslsock):
    """取对端证书指纹（用于 TOFU 比对）"""
    try:
        der = sslsock.getpeercert(binary_form=True)
    except Exception:
        return ''
    return fingerprint_of_der(der) if der else ''

#!/usr/bin/env python3
"""yumirror —— 文件级加密工具（供流程积木 encrypt_file / decrypt_file 使用）

设计要点：
- 算法：AES-256-GCM，分块加密，逐块带 16 字节认证标签，能检出任何篡改。
- 密钥：口令（PBKDF2-HMAC-SHA256，20 万次迭代 + 16 字节随机盐）
        或密钥文件（32 字节原始密钥，支持 64 位十六进制写法）。
- 头部参与 AAD：改动盐/随机数/分块大小同样会导致解密失败。
- 落盘原子：先写 .tmp 再 os.replace，中断不会留下半个文件。

文件格式 v1/v2：
    0   6   magic  b'LMENC1'
    6   1   version (1=口令派生, 2=密钥文件)
    7   16  salt
    23  8   nonce_base
    31  4   chunk_size (大端 uint32)
    35  ... 若干块: [uint32 len][密文 len 字节][16 字节 tag]
"""
import hashlib
import os
import secrets
import struct

MAGIC = b'LMENC1'
VERSION_PASSPHRASE = 1
VERSION_RAWKEY = 2
HEADER_SIZE = 35
TAG_SIZE = 16
PBKDF2_ITERATIONS = 200_000
MAX_CHUNK = 64 * 1024 * 1024


class CryptoUnavailable(RuntimeError):
    """未安装 cryptography 时抛出，附可执行的修复建议。"""


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except Exception as e:  # pragma: no cover
        raise CryptoUnavailable(
            '未安装 cryptography，无法使用加密积木。安装：pip install cryptography'
        ) from e


def available() -> bool:
    try:
        _crypto()
        return True
    except CryptoUnavailable:
        return False


def _read_key_file(path):
    with open(path, 'rb') as f:
        raw = f.read()
    if not raw:
        raise ValueError('密钥文件为空: %s' % path)
    text = raw.strip()
    if len(text) == 64:
        try:
            return bytes.fromhex(text.decode('ascii'))
        except Exception:
            pass
    if len(raw) < 32:
        raise ValueError('密钥文件至少需要 32 字节（或 64 位十六进制）: %s' % path)
    return raw[:32]


def _derive(passphrase, salt):
    return hashlib.pbkdf2_hmac('sha256', passphrase.encode('utf-8'), salt, PBKDF2_ITERATIONS, dklen=32)


def is_encrypted(path) -> bool:
    try:
        with open(path, 'rb') as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def _atomic_write(dst, data):
    os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
    tmp = dst + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dst)


def encrypt_file(src, dst=None, passphrase='', key_file='', chunk_size=1024 * 1024,
                 delete_source=False, mtime=None):
    """加密 src → dst（默认 src + '.enc'）。返回 {'path','size','sha256','chunks'}"""
    AESGCM = _crypto()
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    if not passphrase and not key_file:
        raise ValueError('必须提供 passphrase 或 key_file')
    dst = dst or (src + '.enc')
    if chunk_size <= 0 or chunk_size > MAX_CHUNK:
        chunk_size = 1024 * 1024

    salt = secrets.token_bytes(16)
    nonce_base = secrets.token_bytes(8)
    if key_file:
        key = _read_key_file(key_file)
        version = VERSION_RAWKEY
    else:
        key = _derive(passphrase, salt)
        version = VERSION_PASSPHRASE
    header = MAGIC + bytes([version]) + salt + nonce_base + struct.pack('>I', chunk_size)
    aes = AESGCM(key)

    os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
    tmp = dst + '.tmp'
    sha = hashlib.sha256()
    chunks = 0
    total = 0
    try:
        with open(src, 'rb') as fi, open(tmp, 'wb') as fo:
            fo.write(header)
            idx = 0
            while True:
                block = fi.read(chunk_size)
                if not block:
                    break
                nonce = nonce_base + struct.pack('>I', idx)
                ct = aes.encrypt(nonce, block, header)
                fo.write(struct.pack('>I', len(ct)))
                fo.write(ct)
                sha.update(ct)
                total += len(ct)
                idx += 1
                chunks += 1
            fo.flush()
            os.fsync(fo.fileno())
        if mtime is not None:
            try:
                os.utime(tmp, (float(mtime), float(mtime)))
            except OSError:
                pass
        os.replace(tmp, dst)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if delete_source:
        try:
            os.remove(src)
        except OSError:
            pass
    return {'path': dst, 'size': os.path.getsize(dst), 'cipher_size': total,
            'sha256': sha.hexdigest(), 'chunks': chunks, 'version': version}


def decrypt_file(src, dst=None, passphrase='', key_file='', delete_source=False, mtime=None):
    """解密 src → dst（默认去掉 .enc 后缀）。返回 {'path','size','sha256'}"""
    AESGCM = _crypto()
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    if not passphrase and not key_file:
        raise ValueError('必须提供 passphrase 或 key_file')
    if dst is None:
        dst = src[:-4] if src.lower().endswith('.enc') else (src + '.dec')

    with open(src, 'rb') as f:
        header = f.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE or header[:len(MAGIC)] != MAGIC:
            raise ValueError('不是 yumirror 加密文件（magic 不匹配）: %s' % src)
        version = header[6]
        salt = header[7:23]
        nonce_base = header[23:31]
        chunk_size = struct.unpack('>I', header[31:35])[0]
        if chunk_size <= 0 or chunk_size > MAX_CHUNK:
            raise ValueError('头部 chunk_size 异常: %d' % chunk_size)
        if version == VERSION_PASSPHRASE:
            key = _derive(passphrase, salt)
        elif version == VERSION_RAWKEY:
            key = _read_key_file(key_file)
        else:
            raise ValueError('不支持的加密版本: %d' % version)
        aes = AESGCM(key)

        os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
        tmp = dst + '.tmp'
        sha = hashlib.sha256()
        chunks = 0
        try:
            with open(tmp, 'wb') as fo:
                idx = 0
                while True:
                    lb = f.read(4)
                    if not lb:
                        break
                    if len(lb) < 4:
                        raise ValueError('文件被截断（块长度不完整）')
                    ln = struct.unpack('>I', lb)[0]
                    if ln < TAG_SIZE or ln > MAX_CHUNK + TAG_SIZE:
                        raise ValueError('块长度异常: %d' % ln)
                    ct = f.read(ln)
                    if len(ct) < ln:
                        raise ValueError('文件被截断（块数据不完整）')
                    nonce = nonce_base + struct.pack('>I', idx)
                    fo.write(aes.decrypt(nonce, ct, header))
                    sha.update(ct)
                    idx += 1
                    chunks += 1
                fo.flush()
                os.fsync(fo.fileno())
            if mtime is not None:
                try:
                    os.utime(tmp, (float(mtime), float(mtime)))
                except OSError:
                    pass
            os.replace(tmp, dst)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    if delete_source:
        try:
            os.remove(src)
        except OSError:
            pass
    return {'path': dst, 'size': os.path.getsize(dst), 'sha256': sha.hexdigest(), 'chunks': chunks}


def make_key_file(path, size=32):
    """生成一个随机密钥文件（64 位十六进制文本），返回路径"""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='ascii') as f:
        f.write(secrets.token_hex(size))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path

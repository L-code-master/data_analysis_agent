"""密码哈希 —— 直接用 bcrypt，不用 passlib。

为什么弃用 passlib：
  实测 passlib 1.7.4 与 bcrypt 5.0.0 不兼容 ——
  passlib 读取 bcrypt.__about__.__version__（bcrypt 4.1+ 已移除），
  backend 探测失败后，连基本的 hash 都会抛：
      ValueError: password cannot be longer than 72 bytes

  passlib 最后一次发布是 2020 年，已不适配现代 bcrypt。
  直接用 bcrypt 少一层依赖，且行为可预期。

bcrypt 的 72 字节限制：
  bcrypt 算法本身只处理前 72 字节。bcrypt 5.0 起**抛异常**而非静默截断
  （这是好事：静默截断会让长密码的后半段失效）。
  本模块的处理方式：先用 SHA-256 预哈希，再交给 bcrypt ——
  这样任意长度的密码都能安全处理，且不损失熵。
  （这是业界通行做法，见 Django、Werkzeug 的实现）

用法::

    h = hash_password('my-secret-password')
    ok = verify_password('my-secret-password', h)   # True
    ok = verify_password('wrong', h)                 # False
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string

import bcrypt

# bcrypt 成本因子。12 是 2024+ 的推荐值（约 250ms/次）。
# 提高会增强抗暴力破解能力，但登录延迟增加。
DEFAULT_ROUNDS = 12

# bcrypt 硬限制：算法只看前 72 字节
_BCRYPT_MAX_BYTES = 72


def _prepare(password: str) -> bytes:
    """把任意长度密码压到 bcrypt 能处理的范围。

    先 SHA-256 再 base64 —— base64 后固定 44 字节，远小于 72。
    保留全部熵，且不同长度密码不会碰撞。
    """
    if not isinstance(password, str):
        raise TypeError(f'密码必须是 str，得到 {type(password).__name__}')
    digest = hashlib.sha256(password.encode('utf-8')).digest()
    return base64.b64encode(digest)


def hash_password(password: str, rounds: int = DEFAULT_ROUNDS) -> str:
    """生成密码哈希（返回 `$2b$...` 格式字符串，可直接存库）。"""
    if not password:
        raise ValueError('密码不能为空')
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(_prepare(password), salt).decode('ascii')


def verify_password(password: str, hashed: str) -> bool:
    """校验密码。任何异常都返回 False（不向调用方泄露内部细节）。"""
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(_prepare(password), hashed.encode('ascii'))
    except (ValueError, TypeError):
        # 哈希格式非法 / 密码类型错误 —— 一律视为校验失败
        return False


def needs_rehash(hashed: str, rounds: int = DEFAULT_ROUNDS) -> bool:
    """判断已存哈希是否需要用新的成本因子重算（成本因子升级时用）。"""
    try:
        parts = hashed.split('$')
        # $2b$12$... → parts = ['', '2b', '12', '...']
        if len(parts) < 4:
            return True
        return int(parts[2]) < rounds
    except (ValueError, IndexError):
        return True


# ---------------------------------------------------------------------------
# 密码强度校验
# ---------------------------------------------------------------------------

MIN_LENGTH = 10
MAX_LENGTH = 128

_COMMON_PASSWORDS = frozenset({
    'password', 'password1', '12345678', '123456789', '1234567890',
    'qwertyuiop', 'abc123456', 'admin12345', 'iloveyou11', 'welcome123',
    'password123', 'admin123456', 'qwerty123456', '1111111111',
})


def validate_password_strength(password: str,
                               min_length: int = MIN_LENGTH) -> tuple[bool, str]:
    """校验密码强度。返回 (通过, 原因)。"""
    if not password:
        return False, '密码不能为空'
    if len(password) < min_length:
        return False, f'密码至少 {min_length} 位'
    if len(password) > MAX_LENGTH:
        return False, f'密码最长 {MAX_LENGTH} 位'
    if password.lower() in _COMMON_PASSWORDS:
        return False, '密码过于常见，请更换'

    kinds = 0
    if any(c.islower() for c in password):
        kinds += 1
    if any(c.isupper() for c in password):
        kinds += 1
    if any(c.isdigit() for c in password):
        kinds += 1
    if any(c in string.punctuation for c in password):
        kinds += 1

    if kinds < 2:
        return False, '密码需包含字母、数字、符号中的至少两类'
    return True, ''


# ---------------------------------------------------------------------------
# 令牌生成
# ---------------------------------------------------------------------------

def generate_token(nbytes: int = 32) -> str:
    """生成 URL 安全的随机令牌（用于 refresh token / 一次性令牌）。"""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """令牌哈希（入库用）。

    令牌本身是高熵随机串，不需要 bcrypt（慢哈希）——
    用 SHA-256 即可，且校验快。加 pepper 防彩虹表。
    """
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def verify_token(token: str, hashed: str) -> bool:
    """恒定时间比对令牌哈希。"""
    if not token or not hashed:
        return False
    return hmac.compare_digest(hash_token(token), hashed)


def generate_order_no(prefix: str = 'TB') -> str:
    """生成对外订单号：前缀 + 时间戳 + 随机段。"""
    import datetime
    ts = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    rand = secrets.token_hex(3).upper()
    return f'{prefix}{ts}{rand}'
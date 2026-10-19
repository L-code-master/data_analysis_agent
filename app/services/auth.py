"""认证服务 —— 注册 / 登录 / 令牌 / 账号锁定。

安全设计（每条都有对应测试）：
  1. 密码 bcrypt 哈希（cost=12），绝不存明文
  2. Refresh token 存 SHA-256 哈希，原文只回给客户端一次
  3. 连续登录失败 5 次锁定 15 分钟（防爆破）
  4. 登出 / 改密吊销该用户全部 refresh token
  5. 所有失败路径返回同一错误文案（不泄露"邮箱是否存在"）
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import jwt

from app.config import get_settings
from app.services import security as sec
from app.storage import db


class AuthError(Exception):
    """认证失败（对外统一文案，不泄露内部细节）。"""

    def __init__(self, message: str, code: str = 'AUTH_FAILED'):
        super().__init__(message)
        self.code = code
        self.message = message


class EmailExistsError(AuthError):
    def __init__(self):
        super().__init__('该邮箱已被注册', 'EMAIL_EXISTS')


class InvalidCredentialsError(AuthError):
    def __init__(self):
        # 刻意不区分"邮箱不存在"与"密码错误"
        super().__init__('邮箱或密码错误', 'INVALID_CREDENTIALS')


class AccountLockedError(AuthError):
    def __init__(self, until: str):
        super().__init__(f'账号已锁定，请于 {until} 后重试', 'ACCOUNT_LOCKED')


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = 'Bearer'


@dataclass(frozen=True)
class AuthUser:
    """认证后的用户上下文（请求级）。"""
    user_id: str
    tenant_id: str
    email: str
    role: str
    display_name: str = ''

    @property
    def is_owner(self) -> bool:
        return self.role == 'owner'

    @property
    def is_admin(self) -> bool:
        """超级管理员：诊断不限次数（只免额度这一道闸，见 docs/12）。"""
        return self.role == 'admin'


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(d: dt.datetime) -> str:
    return d.isoformat()


def _parse_iso(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------

def create_access_token(user_id: str, tenant_id: str, role: str) -> str:
    """生成 JWT access token。"""
    s = get_settings()
    now = _now()
    payload = {
        'sub': user_id,
        'tid': tenant_id,
        'role': role,
        'iat': int(now.timestamp()),
        'exp': int((now + dt.timedelta(
            minutes=s.jwt_access_token_expire_minutes)).timestamp()),
        'typ': 'access',
        'jti': uuid.uuid4().hex,
    }
    return jwt.encode(payload, s.app_secret_key, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """解析并校验 access token。"""
    s = get_settings()
    try:
        payload = jwt.decode(token, s.app_secret_key, algorithms=[s.jwt_algorithm])
    except jwt.ExpiredSignatureError as e:
        raise AuthError('登录已过期，请重新登录', 'TOKEN_EXPIRED') from e
    except jwt.InvalidTokenError as e:
        raise AuthError('登录凭证无效', 'TOKEN_INVALID') from e

    if payload.get('typ') != 'access':
        raise AuthError('登录凭证类型错误', 'TOKEN_INVALID')
    return payload


def _issue_refresh_token(user_id: str, user_agent: str = '',
                         ip: str = '') -> str:
    """生成 refresh token 并入库（存哈希）。"""
    s = get_settings()
    token = sec.generate_token(32)
    token_hash = sec.hash_token(token)
    expires = _now() + dt.timedelta(days=s.jwt_refresh_token_expire_days)

    with db.transaction() as conn:
        conn.execute(
            'INSERT INTO refresh_tokens (id,user_id,token_hash,expires_at,'
            'user_agent,ip_address,created_at) VALUES (?,?,?,?,?,?,?)',
            (uuid.uuid4().hex, user_id, token_hash, _iso(expires),
             user_agent[:200], ip[:64], _iso(_now())))
    return token


def rotate_refresh_token(refresh_token: str, user_agent: str = '',
                         ip: str = '') -> TokenPair:
    """用 refresh token 换新的令牌对（轮换：旧的立即失效）。"""
    s = get_settings()
    th = sec.hash_token(refresh_token)

    with db.transaction() as conn:
        row = conn.execute(
            'SELECT id,user_id,expires_at,revoked_at FROM refresh_tokens '
            'WHERE token_hash = ?', (th,)).fetchone()
        if row is None:
            raise AuthError('登录凭证无效', 'TOKEN_INVALID')

        rec = dict(row) if not isinstance(row, tuple) else {
            'id': row[0], 'user_id': row[1], 'expires_at': row[2], 'revoked_at': row[3]}
        if rec['revoked_at']:
            raise AuthError('登录凭证已失效，请重新登录', 'TOKEN_REVOKED')
        if _parse_iso(rec['expires_at']) < _now():
            raise AuthError('登录已过期，请重新登录', 'TOKEN_EXPIRED')

        # 轮换：吊销旧的
        conn.execute('UPDATE refresh_tokens SET revoked_at = ? WHERE id = ?',
                     (_iso(_now()), rec['id']))

        urow = conn.execute(
            'SELECT id,tenant_id,email,role,status FROM users WHERE id = ?',
            (rec['user_id'],)).fetchone()
        if urow is None:
            raise AuthError('用户不存在', 'USER_NOT_FOUND')
        u = dict(urow) if not isinstance(urow, tuple) else {
            'id': urow[0], 'tenant_id': urow[1], 'email': urow[2],
            'role': urow[3], 'status': urow[4]}

        if u['status'] != 'active':
            raise AuthError('账号已被禁用', 'ACCOUNT_DISABLED')

    new_refresh = _issue_refresh_token(u['id'], user_agent, ip)
    return TokenPair(
        access_token=create_access_token(u['id'], u['tenant_id'], u['role']),
        refresh_token=new_refresh,
        expires_in=s.jwt_access_token_expire_minutes * 60,
    )


def revoke_all_tokens(user_id: str) -> int:
    """吊销某用户的全部 refresh token（登出 / 改密时调用）。"""
    with db.transaction() as conn:
        cur = conn.execute(
            'UPDATE refresh_tokens SET revoked_at = ? '
            'WHERE user_id = ? AND revoked_at IS NULL',
            (_iso(_now()), user_id))
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# 注册 / 登录
# ---------------------------------------------------------------------------

def _resolve_role(email: str) -> str:
    """命中 SUPER_ADMIN_EMAILS → admin，否则 owner。"""
    s = get_settings()
    return 'admin' if email in s.super_admin_emails else 'owner'


def register(email: str, password: str, tenant_name: str = '',
             contact_phone: str = '', user_agent: str = '',
             ip: str = '') -> tuple[AuthUser, TokenPair]:
    """注册：创建租户 + 用户 + 额度账户，并直接返回登录态。"""
    s = get_settings()
    email = (email or '').strip().lower()
    if not email or '@' not in email:
        raise AuthError('邮箱格式不正确', 'INVALID_EMAIL')

    ok, reason = sec.validate_password_strength(password, s.password_min_length)
    if not ok:
        raise AuthError(reason, 'WEAK_PASSWORD')

    now = _iso(_now())
    tenant_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex

    with db.transaction() as conn:
        exists = conn.execute('SELECT 1 FROM users WHERE email = ?',
                              (email,)).fetchone()
        if exists:
            raise EmailExistsError()

        conn.execute(
            'INSERT INTO tenants (id,name,contact_phone,status,created_at,updated_at)'
            ' VALUES (?,?,?,?,?,?)',
            (tenant_id, tenant_name or email.split('@')[0], contact_phone,
             'active', now, now))
        conn.execute(
            'INSERT INTO users (id,tenant_id,email,password_hash,display_name,'
            'role,email_verified,status,failed_attempts,created_at,updated_at)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (user_id, tenant_id, email, sec.hash_password(password),
             tenant_name or email.split('@')[0], _resolve_role(email),
             0, 'active', 0, now, now))
        # 额度账户（初始 0 次）
        conn.execute(
            'INSERT INTO credit_accounts (id,tenant_id,balance,total_purchased,'
            'total_used,trial_used,cost_used_usd,created_at,updated_at)'
            ' VALUES (?,?,?,?,?,?,?,?,?)',
            (uuid.uuid4().hex, tenant_id, 0, 0, 0, 0, 0.0, now, now))

    user = AuthUser(user_id=user_id, tenant_id=tenant_id, email=email,
                    role=_resolve_role(email),
                    display_name=tenant_name or email.split('@')[0])

    # ★ 注册赠送体验次数（获客钩子，可用 SIGNUP_FREE_CREDITS=0 关闭）。
    # 放在主事务之外：注册成功与否不应受赠送影响；赠送失败也不回滚账号。
    from app.services import billing as _billing
    try:
        _billing.grant_signup_credit(tenant_id)
    except Exception:                       # noqa: BLE001
        pass

    refresh = _issue_refresh_token(user_id, user_agent, ip)
    pair = TokenPair(
        access_token=create_access_token(user_id, tenant_id, user.role),
        refresh_token=refresh,
        expires_in=s.jwt_access_token_expire_minutes * 60,
    )
    return user, pair


def login(email: str, password: str, user_agent: str = '',
          ip: str = '') -> tuple[AuthUser, TokenPair]:
    """登录。失败达阈值则锁定账号。

    实现要点：失败计数的写入必须**先提交再抛异常**。
    早期版本把 UPDATE 和 raise 放在同一个 transaction() 里，
    异常触发 ROLLBACK 把计数一起回滚了 —— 导致锁定永不生效。
    这里改为：先在独立事务里记录失败，提交后再抛错。
    """
    s = get_settings()
    email = (email or '').strip().lower()
    now = _now()

    # ---- 阶段 1：读取用户（只读，不涉及写事务）----
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT id,tenant_id,email,password_hash,display_name,role,status,'
            'failed_attempts,locked_until FROM users WHERE email = ?',
            (email,)).fetchone()

    if row is None:
        # 不泄露"邮箱是否存在"；同时做一次哈希以抹平时间差（防用户枚举）
        sec.verify_password(password, sec.hash_password('dummy-for-timing'))
        raise InvalidCredentialsError()

    u = dict(row) if not isinstance(row, tuple) else {
        'id': row[0], 'tenant_id': row[1], 'email': row[2],
        'password_hash': row[3], 'display_name': row[4], 'role': row[5],
        'status': row[6], 'failed_attempts': row[7], 'locked_until': row[8]}

    # ---- 阶段 2：只读校验（失败不写库）----
    if u['locked_until']:
        until = _parse_iso(u['locked_until'])
        if until > now:
            raise AccountLockedError(until.strftime('%H:%M'))

    if u['status'] != 'active':
        raise AuthError('账号已被禁用', 'ACCOUNT_DISABLED')

    if not sec.verify_password(password, u['password_hash']):
        # ---- 阶段 3：记录失败（独立事务，先提交）----
        fails = (u['failed_attempts'] or 0) + 1
        locked_until = None
        if fails >= s.login_max_attempts:
            locked_until = _iso(now + dt.timedelta(
                minutes=s.login_lockout_minutes))

        with db.transaction() as conn:
            conn.execute(
                'UPDATE users SET failed_attempts = ?, locked_until = ?, '
                'updated_at = ? WHERE id = ?',
                (fails, locked_until, _iso(now), u['id']))
        # 事务已提交，现在才抛错 —— 计数不会被回滚

        if locked_until:
            raise AccountLockedError(_parse_iso(locked_until).strftime('%H:%M'))
        raise InvalidCredentialsError()

    # ---- 阶段 4：成功，清失败计数 ----
    with db.transaction() as conn:
        conn.execute(
            'UPDATE users SET failed_attempts = 0, locked_until = NULL, '
            'last_login_at = ?, updated_at = ? WHERE id = ?',
            (_iso(now), _iso(now), u['id']))

    # 超管提升（容错"先注册后配置 env"的顺序）：命中邮箱且角色还不是 admin
    role = u['role']
    if role != 'admin' and _resolve_role(email) == 'admin':
        role = 'admin'
        with db.transaction() as conn:
            conn.execute('UPDATE users SET role = ?, updated_at = ? WHERE id = ?',
                         ('admin', _iso(now), u['id']))

    user = AuthUser(user_id=u['id'], tenant_id=u['tenant_id'],
                    email=u['email'], role=role,
                    display_name=u['display_name'] or '')
    refresh = _issue_refresh_token(u['id'], user_agent, ip)
    pair = TokenPair(
        access_token=create_access_token(u['id'], u['tenant_id'], u['role']),
        refresh_token=refresh,
        expires_in=s.jwt_access_token_expire_minutes * 60,
    )
    return user, pair


def change_password(user_id: str, old_password: str,
                    new_password: str) -> int:
    """修改密码，并吊销该用户全部 refresh token。返回吊销数量。"""
    s = get_settings()
    ok, reason = sec.validate_password_strength(new_password, s.password_min_length)
    if not ok:
        raise AuthError(reason, 'WEAK_PASSWORD')

    with db.transaction() as conn:
        row = conn.execute('SELECT password_hash FROM users WHERE id = ?',
                           (user_id,)).fetchone()
        if row is None:
            raise AuthError('用户不存在', 'USER_NOT_FOUND')
        ph = row[0] if isinstance(row, tuple) else row['password_hash']

        if not sec.verify_password(old_password, ph):
            raise InvalidCredentialsError()

        conn.execute('UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?',
                     (sec.hash_password(new_password), _iso(_now()), user_id))

    return revoke_all_tokens(user_id)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def get_user(user_id: str) -> AuthUser | None:
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT id,tenant_id,email,role,display_name,status '
            'FROM users WHERE id = ?', (user_id,)).fetchone()
    if row is None:
        return None
    if isinstance(row, tuple):
        uid, tid, email, role, dn, status = row
    else:
        uid, tid, email, role, dn, status = (row['id'], row['tenant_id'],
                                             row['email'], row['role'],
                                             row['display_name'], row['status'])
    if status != 'active':
        return None
    return AuthUser(user_id=uid, tenant_id=tid, email=email, role=role,
                    display_name=dn or '')
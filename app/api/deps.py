"""FastAPI 依赖注入 —— 认证、租户绑定、限额。

★ 核心纪律（见 docs/01-DESIGN.md §3 B）：
    **每个请求必须按 tenant_id 现场构造上下文，绝不复用全局单例。**
    复用单例会串租户 —— 这是最严重的生产事故。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import get_settings
from app.services import auth, ratelimit
from app.services.auth import AuthError, AuthUser


def _client_ip(request: Request) -> str:
    """取客户端 IP（考虑反向代理）。"""
    xff = request.headers.get('x-forwarded-for', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.client.host if request.client else ''


def _user_agent(request: Request) -> str:
    return request.headers.get('user-agent', '')[:200]


def enforce_rate_limit(scope: str, key: str, limit: int,
                       window_seconds: int) -> None:
    """路由层限速入口：超限 → 429（docs/16 P1-7）。

    限速开关与各端点阈值来自 Settings（RATE_LIMIT_ENABLED / RL_*）。
    """
    if not get_settings().rate_limit_enabled:
        return
    try:
        ratelimit.enforce(scope, key, limit, window_seconds)
    except ratelimit.RateLimitExceeded as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={'code': 'RATE_LIMITED', 'message': str(e),
                    'retry_after': e.retry_after},
            headers={'Retry-After': str(e.retry_after)},
        ) from e


async def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthUser:
    """从 Authorization: Bearer <token> 解析当前用户。"""
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='缺少登录凭证',
            headers={'WWW-Authenticate': 'Bearer'})

    token = authorization[7:].strip()
    try:
        payload = auth.decode_access_token(token)
    except AuthError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=e.message) from e

    user = auth.get_user(payload.get('sub', ''))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='用户不存在或已禁用')

    # ★ 关键：每次请求都从库里重新取用户，不信任 token 里的 tenant_id
    #   （token 可能是在用户被移出租户前签发的）
    if user.tenant_id != payload.get('tid'):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='登录凭证已失效，请重新登录')

    # ★ 登出/改密水位（docs/16 P1-18）：无状态 access token 也要能撤销
    if auth.access_token_revoked(payload, user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='登录凭证已失效，请重新登录',
            headers={'WWW-Authenticate': 'Bearer'})

    return user


# 类型别名，便于路由签名简洁
CurrentUser = Annotated[AuthUser, Depends(get_current_user)]

# ★ docs/16 P3-8：require_owner / require_credits 定义后从未被路由使用
#   （各路由内联了自己的检查，P0-1 的核销权限漂移正源于此）—— 已删。
#   额度检查的唯一实现：diagnoses._precheck；管理员豁免见 docs/12。


class RequestContext:
    """请求级上下文。

    ★ 每请求新建 —— 承载该租户的隔离边界。
    """

    __slots__ = ('user', 'tenant_id', 'ip', 'user_agent')

    def __init__(self, user: AuthUser, ip: str, user_agent: str):
        self.user = user
        self.tenant_id = user.tenant_id
        self.ip = ip
        self.user_agent = user_agent


async def get_context(request: Request, user: CurrentUser) -> RequestContext:
    return RequestContext(user, _client_ip(request), _user_agent(request))


Ctx = Annotated[RequestContext, Depends(get_context)]
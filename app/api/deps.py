"""FastAPI 依赖注入 —— 认证、租户绑定、限额。

★ 核心纪律（见 docs/01-DESIGN.md §3 B）：
    **每个请求必须按 tenant_id 现场构造上下文，绝不复用全局单例。**
    复用单例会串租户 —— 这是最严重的生产事故。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.services import auth, billing
from app.services.auth import AuthError, AuthUser


def _client_ip(request: Request) -> str:
    """取客户端 IP（考虑反向代理）。"""
    xff = request.headers.get('x-forwarded-for', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.client.host if request.client else ''


def _user_agent(request: Request) -> str:
    return request.headers.get('user-agent', '')[:200]


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

    return user


# 类型别名，便于路由签名简洁
CurrentUser = Annotated[AuthUser, Depends(get_current_user)]


async def require_owner(user: CurrentUser) -> AuthUser:
    """要求 owner 角色（用于购买、配额管理等敏感操作）。"""
    if not user.is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='需要店铺管理员权限')
    return user


OwnerUser = Annotated[AuthUser, Depends(require_owner)]


async def require_credits(user: CurrentUser) -> AuthUser:
    """要求有可用额度（用于发起诊断）。

    超级管理员放行（docs/12：测试通行证，只免额度这一道闸）。
    """
    if user.is_admin:
        return user
    acc = billing.get_account(user.tenant_id)
    if not acc.can_diagnose:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'INSUFFICIENT_CREDITS',
                'message': f'诊断次数不足（当前 {acc.balance} 次），请先购买',
                'balance': acc.balance,
            })
    return user


CreditUser = Annotated[AuthUser, Depends(require_credits)]


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
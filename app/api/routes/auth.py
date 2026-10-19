"""认证路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import CurrentUser, _client_ip, _user_agent
from app.api.schemas import (
    AuthResponse, ChangePasswordRequest, LoginRequest, RefreshRequest,
    RegisterRequest, TokenResponse, UserResponse,
)
from app.services import auth
from app.services.auth import (
    AccountLockedError, AuthError, EmailExistsError, InvalidCredentialsError,
)

router = APIRouter(prefix='/auth', tags=['认证'])


def _auth_response(user: auth.AuthUser, pair: auth.TokenPair) -> AuthResponse:
    return AuthResponse(
        user=UserResponse(
            user_id=user.user_id, tenant_id=user.tenant_id,
            email=user.email, role=user.role,
            display_name=user.display_name),
        token=TokenResponse(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            token_type=pair.token_type,
            expires_in=pair.expires_in),
    )


def _handle(e: AuthError) -> HTTPException:
    """把 AuthError 映射成 HTTP 状态码。"""
    code_map = {
        'EMAIL_EXISTS': status.HTTP_409_CONFLICT,
        'INVALID_CREDENTIALS': status.HTTP_401_UNAUTHORIZED,
        'ACCOUNT_LOCKED': status.HTTP_423_LOCKED,
        'WEAK_PASSWORD': status.HTTP_400_BAD_REQUEST,
        'INVALID_EMAIL': status.HTTP_400_BAD_REQUEST,
        'ACCOUNT_DISABLED': status.HTTP_403_FORBIDDEN,
        'TOKEN_EXPIRED': status.HTTP_401_UNAUTHORIZED,
        'TOKEN_INVALID': status.HTTP_401_UNAUTHORIZED,
        'TOKEN_REVOKED': status.HTTP_401_UNAUTHORIZED,
    }
    return HTTPException(
        status_code=code_map.get(e.code, status.HTTP_400_BAD_REQUEST),
        detail={'code': e.code, 'message': e.message})


@router.post('/register', response_model=AuthResponse,
             status_code=status.HTTP_201_CREATED, summary='注册')
async def register(req: RegisterRequest, request: Request):
    try:
        user, pair = auth.register(
            req.email, req.password, req.tenant_name, req.contact_phone,
            user_agent=_user_agent(request), ip=_client_ip(request))
    except AuthError as e:
        raise _handle(e) from e
    return _auth_response(user, pair)


@router.post('/login', response_model=AuthResponse, summary='登录')
async def login(req: LoginRequest, request: Request):
    try:
        user, pair = auth.login(
            req.email, req.password,
            user_agent=_user_agent(request), ip=_client_ip(request))
    except AuthError as e:
        raise _handle(e) from e
    return _auth_response(user, pair)


@router.post('/refresh', response_model=TokenResponse, summary='刷新令牌')
async def refresh(req: RefreshRequest, request: Request):
    try:
        pair = auth.rotate_refresh_token(
            req.refresh_token,
            user_agent=_user_agent(request), ip=_client_ip(request))
    except AuthError as e:
        raise _handle(e) from e
    return TokenResponse(
        access_token=pair.access_token, refresh_token=pair.refresh_token,
        token_type=pair.token_type, expires_in=pair.expires_in)


@router.post('/logout', summary='登出（吊销全部令牌）')
async def logout(user: CurrentUser):
    n = auth.revoke_all_tokens(user.user_id)
    return {'message': '已登出', 'revoked_tokens': n}


@router.get('/me', response_model=UserResponse, summary='当前用户')
async def me(user: CurrentUser):
    return UserResponse(
        user_id=user.user_id, tenant_id=user.tenant_id,
        email=user.email, role=user.role, display_name=user.display_name)


@router.post('/change-password', summary='修改密码')
async def change_password(req: ChangePasswordRequest, user: CurrentUser):
    try:
        n = auth.change_password(user.user_id, req.old_password, req.new_password)
    except AuthError as e:
        raise _handle(e) from e
    return {'message': '密码已修改，请重新登录', 'revoked_tokens': n}
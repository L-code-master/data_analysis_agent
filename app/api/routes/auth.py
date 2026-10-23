"""认证路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    CurrentUser, _client_ip, _user_agent, enforce_rate_limit,
)
from app.api.schemas import (
    AuthResponse, ChangePasswordRequest, LoginRequest, RefreshRequest,
    RegisterRequest, TokenResponse, UserResponse, VerifyEmailRequest,
    VerifyEmailResponse,
)
from app.config import get_settings
from app.services import auth
from app.services.auth import (
    AuthError, EmailExistsError, InvalidCredentialsError,
)

router = APIRouter(prefix='/auth', tags=['认证'])

# ★ refresh token 放 httpOnly cookie（docs/16 P2-16）：
#   JS 读不到 → XSS 无法持久接管；SameSite=Lax 防跨站 POST；
#   生产 HTTPS 下带 Secure。access token 仍由前端放内存。
REFRESH_COOKIE = 'daa_refresh'
REFRESH_COOKIE_PATH = '/api/v1/auth'


def _set_refresh_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        key=REFRESH_COOKIE, value=token,
        max_age=s.jwt_refresh_token_expire_days * 86400,
        httponly=True, secure=s.is_production, samesite='lax',
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


def _auth_response(user: auth.AuthUser, pair: auth.TokenPair, *,
                   verification_required: bool = False,
                   verification_sent: bool = False) -> AuthResponse:
    return AuthResponse(
        user=_user_resp(user),
        token=TokenResponse(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            token_type=pair.token_type,
            expires_in=pair.expires_in),
        verification_required=verification_required,
        verification_sent=verification_sent,
    )


def _user_resp(user: auth.AuthUser) -> UserResponse:
    return UserResponse(
        user_id=user.user_id, tenant_id=user.tenant_id,
        email=user.email, role=user.role,
        display_name=user.display_name,
        email_verified=user.email_verified)


def _handle(e: AuthError) -> HTTPException:
    """把 AuthError 映射成 HTTP 状态码。

    ★ P3-16：登录锁定/禁用自 P2-17 起统一走 INVALID_CREDENTIALS(401)，
      原先的 423(非标准码)映射已无触发路径，删除。
    """
    code_map = {
        'EMAIL_EXISTS': status.HTTP_409_CONFLICT,
        'INVALID_CREDENTIALS': status.HTTP_401_UNAUTHORIZED,
        'WEAK_PASSWORD': status.HTTP_400_BAD_REQUEST,
        'INVALID_EMAIL': status.HTTP_400_BAD_REQUEST,
        'ACCOUNT_DISABLED': status.HTTP_403_FORBIDDEN,
        'TOKEN_EXPIRED': status.HTTP_401_UNAUTHORIZED,
        'TOKEN_INVALID': status.HTTP_401_UNAUTHORIZED,
        'TOKEN_REVOKED': status.HTTP_401_UNAUTHORIZED,
        'INVALID_VERIFICATION_TOKEN': status.HTTP_400_BAD_REQUEST,
        'VERIFICATION_TOKEN_EXPIRED': status.HTTP_400_BAD_REQUEST,
        'ALREADY_VERIFIED': status.HTTP_400_BAD_REQUEST,
        'EMAIL_SEND_FAILED': status.HTTP_503_SERVICE_UNAVAILABLE,
        'USER_NOT_FOUND': status.HTTP_404_NOT_FOUND,
    }
    return HTTPException(
        status_code=code_map.get(e.code, status.HTTP_400_BAD_REQUEST),
        detail={'code': e.code, 'message': e.message})


@router.post('/register', response_model=AuthResponse,
             status_code=status.HTTP_201_CREATED, summary='注册')
async def register(req: RegisterRequest, request: Request,
                   response: Response):
    # ★ 每 IP 每小时注册上限（docs/16 P1-7）：每注册一个邮箱 = 1 次免费额度
    #   × 若干次 Agent LLM 循环，不限速等于可刷成本。
    s = get_settings()
    enforce_rate_limit('register', _client_ip(request),
                       s.rl_register_per_hour, 3600)
    try:
        user, pair = auth.register(
            req.email, req.password, req.tenant_name, req.contact_phone,
            user_agent=_user_agent(request), ip=_client_ip(request))
    except AuthError as e:
        raise _handle(e) from e

    _set_refresh_cookie(response, pair.refresh_token)

    sent = False
    if s.email_verification_required and not user.email_verified:
        # SMTP 是阻塞 IO，放线程池；发送失败不影响注册（可重发）
        sent = await run_in_threadpool(auth.send_verification_email, user)
    return _auth_response(user, pair,
                          verification_required=s.email_verification_required,
                          verification_sent=sent)


@router.post('/login', response_model=AuthResponse, summary='登录')
async def login(req: LoginRequest, request: Request, response: Response):
    # 登录限速按 IP（跨账号喷洒防护；账号锁定只管单账号，docs/16 P1-7）
    s = get_settings()
    enforce_rate_limit('login', _client_ip(request),
                       s.rl_login_per_minute, 60)
    try:
        user, pair = auth.login(
            req.email, req.password,
            user_agent=_user_agent(request), ip=_client_ip(request))
    except AuthError as e:
        raise _handle(e) from e
    _set_refresh_cookie(response, pair.refresh_token)
    return _auth_response(user, pair)


@router.post('/refresh', response_model=TokenResponse, summary='刷新令牌')
async def refresh(request: Request, response: Response,
                  req: RefreshRequest | None = None):
    # ★ 优先读取 httpOnly cookie；body 仅兼容旧客户端（P2-16）
    token = (req.refresh_token if req and req.refresh_token else '') \
        or request.cookies.get(REFRESH_COOKIE, '')
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={'code': 'TOKEN_INVALID',
                    'message': '缺少刷新凭证，请重新登录'})
    try:
        pair = auth.rotate_refresh_token(
            token, user_agent=_user_agent(request), ip=_client_ip(request))
    except AuthError as e:
        raise _handle(e) from e
    _set_refresh_cookie(response, pair.refresh_token)
    return TokenResponse(
        access_token=pair.access_token, refresh_token=pair.refresh_token,
        token_type=pair.token_type, expires_in=pair.expires_in)


@router.post('/logout', summary='登出（吊销全部令牌）')
async def logout(user: CurrentUser, response: Response):
    n = auth.revoke_all_tokens(user.user_id)
    _clear_refresh_cookie(response)
    return {'message': '已登出', 'revoked_tokens': n}


@router.post('/verify-email', response_model=VerifyEmailResponse,
             summary='邮箱验证（验证成功后发放注册赠送额度）')
async def verify_email(req: VerifyEmailRequest):
    try:
        user = await run_in_threadpool(auth.verify_email, req.token)
    except AuthError as e:
        raise _handle(e) from e
    return VerifyEmailResponse(
        message='邮箱验证成功，赠送的诊断次数已发放',
        user=_user_resp(user))


@router.post('/resend-verification',
             summary='重新发送邮箱验证邮件')
async def resend_verification(user: CurrentUser, request: Request):
    s = get_settings()
    # 按账号 + IP 限速，防止被当作邮件轰炸入口
    enforce_rate_limit('resend_verify', f'{user.user_id}:{_client_ip(request)}',
                       3, 3600)
    try:
        await run_in_threadpool(auth.resend_verification, user.user_id)
    except AuthError as e:
        raise _handle(e) from e
    return {'message': '验证邮件已重新发送，请查收'}


@router.get('/me', response_model=UserResponse, summary='当前用户')
async def me(user: CurrentUser):
    return _user_resp(user)


@router.post('/change-password', summary='修改密码')
async def change_password(req: ChangePasswordRequest, user: CurrentUser):
    try:
        n = auth.change_password(user.user_id, req.old_password, req.new_password)
    except AuthError as e:
        raise _handle(e) from e
    return {'message': '密码已修改，请重新登录', 'revoked_tokens': n}
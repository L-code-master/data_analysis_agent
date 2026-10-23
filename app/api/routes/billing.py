"""额度与订单路由。

★ 全部为同步 def（非 async）：FastAPI 自动放入线程池执行 ——
  SQLite 写事务遇锁时最长等待 busy_timeout=5s，跑在事件循环里
  会冻结全站请求（docs/16 P0-7）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.api.deps import Ctx, CurrentUser
from app.api.schemas import (
    AccountResponse, CreateOrderRequest, LedgerEntry, OrderResponse, PlanResponse,
)
from app.services import billing

router = APIRouter(prefix='/billing', tags=['额度与订单'])
logger = logging.getLogger(__name__)


def _handle(e: Exception) -> HTTPException:
    if isinstance(e, billing.BillingError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail={'code': e.code, 'message': e.message})
    from app.services.tenant import TenantSuspendedError
    if isinstance(e, TenantSuspendedError):        # P2-20：停用租户
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                             detail={'code': e.code, 'message': str(e)})
    # ★ 未知异常不把内部细节回给客户端（docs/16 P2-14），只进日志
    logger.exception('计费接口未处理异常: %s', e)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                         detail={'code': 'INTERNAL',
                                 'message': '服务内部错误'})


def _account_resp(acc: billing.CreditAccount) -> AccountResponse:
    d = acc.to_dict()
    return AccountResponse(**d)


@router.get('/plans', response_model=list[PlanResponse], summary='套餐清单')
def plans():
    """公开接口（无需登录），供定价页展示。"""
    return [PlanResponse(**p) for p in billing.pricing_plans()]


@router.get('/account', response_model=AccountResponse, summary='我的额度')
def account(ctx: Ctx):
    return _account_resp(billing.get_account(ctx.tenant_id))


@router.get('/orders', response_model=list[OrderResponse], summary='订单列表')
def list_orders(ctx: Ctx, limit: int = 50):
    return [OrderResponse(**o.to_dict())
            for o in billing.list_orders(ctx.tenant_id, limit=limit)]


@router.post('/orders', response_model=OrderResponse,
             status_code=status.HTTP_201_CREATED, summary='创建订单')
def create_order(ctx: Ctx, req: CreateOrderRequest):
    """创建待支付订单。

    当前支付渠道为**人工核销**（BILLING_PROVIDER=manual）：
    客户转账备注订单号 → 管理员在后台确认收款 → 额度入账。
    """
    try:
        order = billing.create_order(
            ctx.tenant_id, req.sku, ctx.user.user_id)
    except Exception as e:
        raise _handle(e) from e
    return OrderResponse(**order.to_dict())


@router.get('/orders/{order_no}', response_model=OrderResponse,
            summary='查询订单')
def get_order(ctx: Ctx, order_no: str):
    o = billing.get_order_by_no(order_no)
    if o is None or o.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={'code': 'ORDER_NOT_FOUND',
                                    'message': f'订单不存在: {order_no}'})
    return OrderResponse(**o.to_dict())


@router.post('/orders/{order_no}/cancel', response_model=OrderResponse,
             summary='取消订单')
def cancel_order(ctx: Ctx, order_no: str):
    o = billing.get_order_by_no(order_no)
    if o is None or o.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={'code': 'ORDER_NOT_FOUND',
                                    'message': f'订单不存在: {order_no}'})
    try:
        canceled = billing.cancel_order(order_no)
    except Exception as e:
        raise _handle(e) from e
    return OrderResponse(**canceled.to_dict())


@router.post('/orders/{order_no}/proof', summary='上传付款凭证（P2-19）')
async def upload_proof(ctx: Ctx, order_no: str, request: Request,
                       file: UploadFile = File(...)):
    """把转账截图/PDF 挂到 pending 订单，供平台管理员核销时核对。

    jpg/png/webp/pdf，≤5MB。重复上传覆盖（以最新凭证为准）。
    """
    # Content-Length 预检（同 P1-14：不整包读内存后才报错）
    declared = request.headers.get('content-length')
    if declared and int(declared) > billing.PROOF_MAX_BYTES + 8192:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={'code': 'BAD_PROOF_SIZE',
                    'message': '凭证超过 5MB 上限'})
    content = await file.read(billing.PROOF_MAX_BYTES + 1)
    if len(content) > billing.PROOF_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={'code': 'BAD_PROOF_SIZE',
                    'message': '凭证超过 5MB 上限'})
    try:
        res = await run_in_threadpool(
            billing.attach_pay_proof, order_no, ctx.tenant_id,
            file.filename or 'proof', content)
    except Exception as e:
        raise _handle(e) from e
    return res


@router.get('/orders/{order_no}/proof', summary='下载付款凭证')
def download_proof(ctx: Ctx, order_no: str):
    """订单归属方或平台管理员可下载；无凭证返回 404 NO_PROOF。"""
    try:
        path, media = billing.get_pay_proof(order_no, ctx.tenant_id,
                                            is_admin=ctx.user.is_admin)
    except Exception as e:
        raise _handle(e) from e
    return FileResponse(path, media_type=media,
                        filename=f'proof-{order_no}{Path(path).suffix}')


@router.get('/ledger', response_model=list[LedgerEntry], summary='额度流水')
def ledger(ctx: Ctx, limit: int = 100):
    """额度增减明细（客服对账用）。"""
    return [LedgerEntry(**e) for e in billing.list_ledger(ctx.tenant_id, limit)]


@router.get('/usage', summary='用量与成本')
def usage(ctx: Ctx):
    """Token 与成本汇总。reasoning_tokens 单独统计（推理模型必需）。"""
    return {
        'usage': billing.usage_summary(ctx.tenant_id),
        'account': _account_resp(billing.get_account(ctx.tenant_id)).model_dump(),
    }


# ---------------------------------------------------------------------------
# 人工核销（管理员接口）
# ---------------------------------------------------------------------------

@router.post('/orders/{order_no}/confirm', response_model=AccountResponse,
             summary='确认收款（人工核销，仅平台管理员）')
def confirm_payment(ctx: Ctx, order_no: str,
                    provider_trade_no: str = ''):
    """平台管理员确认收到款项 → 订单转 paid → 额度入账。

    ★ 仅限平台管理员（role=admin，SUPER_ADMIN_EMAILS 白名单）。
      租户 owner 不能核销任何订单 —— 包括自己租户的（docs/16 P0-1：
      自助核销 = 免费无限拿额度）。管理员跨租户操作是人工核销的本意。
      支付宝商户号就绪后，改为由回调接口自动调用 billing.mark_paid()。
    """
    if not ctx.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={'code': 'FORBIDDEN',
                                    'message': '仅平台管理员可确认收款'})

    o = billing.get_order_by_no(order_no)
    if o is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={'code': 'ORDER_NOT_FOUND',
                                    'message': f'订单不存在: {order_no}'})

    try:
        acc = billing.mark_paid(order_no,
                                provider_trade_no=provider_trade_no,
                                reviewed_by=ctx.user.user_id)
    except Exception as e:
        raise _handle(e) from e
    return _account_resp(acc)
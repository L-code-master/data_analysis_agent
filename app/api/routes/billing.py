"""额度与订单路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import Ctx, CurrentUser
from app.api.schemas import (
    AccountResponse, CreateOrderRequest, LedgerEntry, OrderResponse, PlanResponse,
)
from app.services import billing

router = APIRouter(prefix='/billing', tags=['额度与订单'])


def _handle(e: Exception) -> HTTPException:
    if isinstance(e, billing.BillingError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail={'code': e.code, 'message': e.message})
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                         detail={'code': 'INTERNAL', 'message': str(e)})


def _account_resp(acc: billing.CreditAccount) -> AccountResponse:
    d = acc.to_dict()
    return AccountResponse(**d)


@router.get('/plans', response_model=list[PlanResponse], summary='套餐清单')
async def plans():
    """公开接口（无需登录），供定价页展示。"""
    return [PlanResponse(**p) for p in billing.pricing_plans()]


@router.get('/account', response_model=AccountResponse, summary='我的额度')
async def account(ctx: Ctx):
    return _account_resp(billing.get_account(ctx.tenant_id))


@router.get('/orders', response_model=list[OrderResponse], summary='订单列表')
async def list_orders(ctx: Ctx, limit: int = 50):
    return [OrderResponse(**o.to_dict())
            for o in billing.list_orders(ctx.tenant_id, limit=limit)]


@router.post('/orders', response_model=OrderResponse,
             status_code=status.HTTP_201_CREATED, summary='创建订单')
async def create_order(ctx: Ctx, req: CreateOrderRequest):
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
async def get_order(ctx: Ctx, order_no: str):
    o = billing.get_order_by_no(order_no)
    if o is None or o.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={'code': 'ORDER_NOT_FOUND',
                                    'message': f'订单不存在: {order_no}'})
    return OrderResponse(**o.to_dict())


@router.post('/orders/{order_no}/cancel', response_model=OrderResponse,
             summary='取消订单')
async def cancel_order(ctx: Ctx, order_no: str):
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


@router.get('/ledger', response_model=list[LedgerEntry], summary='额度流水')
async def ledger(ctx: Ctx, limit: int = 100):
    """额度增减明细（客服对账用）。"""
    return [LedgerEntry(**e) for e in billing.list_ledger(ctx.tenant_id, limit)]


@router.get('/usage', summary='用量与成本')
async def usage(ctx: Ctx):
    """Token 与成本汇总。reasoning_tokens 单独统计（推理模型必需）。"""
    return {
        'usage': billing.usage_summary(ctx.tenant_id),
        'account': _account_resp(billing.get_account(ctx.tenant_id)).model_dump(),
    }


# ---------------------------------------------------------------------------
# 人工核销（管理员接口）
# ---------------------------------------------------------------------------

@router.post('/orders/{order_no}/confirm', response_model=AccountResponse,
             summary='确认收款（人工核销）')
async def confirm_payment(ctx: Ctx, order_no: str,
                          provider_trade_no: str = ''):
    """管理员确认收到款项 → 订单转 paid → 额度入账。

    ⚠️ 生产环境应限制为管理员角色；当前单租户 MVP 允许 owner 操作。
    支付宝商户号就绪后，改为由回调接口自动调用 billing.mark_paid()。
    """
    if not ctx.user.is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={'code': 'FORBIDDEN',
                                    'message': '需要店铺管理员权限'})

    o = billing.get_order_by_no(order_no)
    if o is None or o.tenant_id != ctx.tenant_id:
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
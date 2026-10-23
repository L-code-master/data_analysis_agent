"""平台管理员接口（docs/12 / docs/16 P1-3）。

只做「平台级」操作，独立于租户 owner：
  - 查看待补偿退款（诊断失败但退款未成功）
  - 重试退款 / 核销（不退款）

鉴权：role=admin（SUPER_ADMIN_EMAILS 白名单），与 P0-1 核销权限一致。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.api.deps import Ctx
from app.config import get_settings
from app.services import billing
from app.storage import db

router = APIRouter(prefix='/admin', tags=['平台管理'])
logger = logging.getLogger(__name__)


def _require_admin(ctx: Ctx) -> None:
    if not ctx.user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={'code': 'FORBIDDEN', 'message': '仅平台管理员可访问'})


def _handle(e: Exception) -> HTTPException:
    if isinstance(e, billing.BillingError):
        code = (status.HTTP_404_NOT_FOUND
                if e.code == 'REFUND_PENDING_NOT_FOUND'
                else status.HTTP_400_BAD_REQUEST)
        return HTTPException(status_code=code,
                             detail={'code': e.code, 'message': e.message})
    logger.exception('管理端未处理异常: %s', e)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                         detail={'code': 'INTERNAL',
                                 'message': '服务内部错误'})


@router.get('/health', summary='系统健康详情（管理员）')
def health_detail(ctx: Ctx):
    """DB 路径/表清单/沙箱配置等运维详情，仅平台管理员可见。"""
    _require_admin(ctx)
    cfg = get_settings()
    try:
        dbh = db.health_check()
    except Exception as e:                      # noqa: BLE001
        dbh = {'ok': False, 'error': str(e)}
    return {
        'status': 'ok' if dbh.get('ok') else 'degraded',
        'database': dbh,
        'sandbox': {
            'network_isolated': cfg.sandbox_network_isolated,
            'mode': cfg.sandbox_mode,
        },
    }


@router.get('/refunds', summary='待补偿退款列表')
def list_refunds(ctx: Ctx, status: str = '', limit: int = 100):
    _require_admin(ctx)
    return {'items': billing.list_refund_pending(limit=limit, status=status)}


@router.post('/refunds/{pending_id}/retry', summary='重试补偿退款')
def retry_refund(ctx: Ctx, pending_id: str):
    _require_admin(ctx)
    try:
        return billing.retry_refund_pending(pending_id)
    except Exception as e:                      # noqa: BLE001
        raise _handle(e) from e


@router.post('/refunds/{pending_id}/writeoff', summary='核销补偿退款（不退款）')
def writeoff_refund(ctx: Ctx, pending_id: str, note: str = ''):
    _require_admin(ctx)
    try:
        return billing.writeoff_refund_pending(pending_id, note=note)
    except Exception as e:                      # noqa: BLE001
        raise _handle(e) from e


# ---------------------------------------------------------------------------
# 租户管理（docs/16 P2-20：tenants.status 此前零消费方）
# ---------------------------------------------------------------------------

@router.get('/tenants', summary='租户清单（含磁盘目录有无）')
def list_tenants(ctx: Ctx, status: str = '', limit: int = 200):
    """平台管理员查看租户与状态；配合 POST /status 停用/恢复。"""
    _require_admin(ctx)
    from app.services import tenant as tn
    return {'items': tn.list_tenants(limit=limit, status=status)}


@router.post('/tenants/{tenant_id}/status', summary='停用/恢复租户')
def set_tenant_status(ctx: Ctx, tenant_id: str, status: str):
    """status: active | suspended | closed。

    停用后该租户的上传/诊断/对话/下单全部 403（docs/16 P2-20）；
    已有订单的核销与退款不受影响。
    """
    _require_admin(ctx)
    from app.services import tenant as tn
    try:
        return tn.set_tenant_status(tenant_id, status)
    except tn.TenantPathError as e:
        # 参数 status 遮蔽了 fastapi.status 模块，这里用数字字面量
        raise HTTPException(status_code=400,
                            detail={'code': 'BAD_TENANT_STATUS',
                                    'message': str(e)}) from e
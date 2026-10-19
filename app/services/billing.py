"""额度与订单服务 —— 按次付费模型。

模型（见 docs/06-定价方案.md）：
    体验价 ¥49/1次（每人限 1 次）· 标准价 ¥199/1次 · 5次包 ¥799/5次

设计原则：
  1. 额度增减全部走 credit_ledger 留痕（客服对账的依据）
  2. 扣减用条件 UPDATE（balance >= n）防超扣，绝不出现负余额
  3. 体验价限购由**数据库唯一索引**强制，不靠应用层判断
  4. 订单金额一律用「分」存储，避免浮点误差
  5. 支付回调幂等（payment_events 唯一索引）
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.services import security as sec
from app.storage import db


class BillingError(Exception):
    def __init__(self, message: str, code: str = 'BILLING_ERROR'):
        super().__init__(message)
        self.code = code
        self.message = message


class InsufficientCreditsError(BillingError):
    def __init__(self, balance: int):
        super().__init__(
            f'诊断次数不足（当前余额 {balance} 次），请先购买',
            'INSUFFICIENT_CREDITS')
        self.balance = balance


class TrialAlreadyUsedError(BillingError):
    def __init__(self):
        super().__init__('体验价每人限购一次', 'TRIAL_ALREADY_USED')


class OrderNotFoundError(BillingError):
    def __init__(self, order_no: str):
        super().__init__(f'订单不存在: {order_no}', 'ORDER_NOT_FOUND')


@dataclass(frozen=True)
class CreditAccount:
    tenant_id: str
    balance: int
    total_purchased: int
    total_used: int
    trial_used: bool
    cost_used_usd: float

    @property
    def can_diagnose(self) -> bool:
        return self.balance > 0

    @property
    def trial_available(self) -> bool:
        return not self.trial_used

    def to_dict(self) -> dict:
        s = get_settings()
        return {
            'tenant_id': self.tenant_id,
            'balance': self.balance,
            'total_purchased': self.total_purchased,
            'total_used': self.total_used,
            'trial_used': self.trial_used,
            'trial_available': self.trial_available,
            'cost_used_usd': round(self.cost_used_usd, 4),
            'prices': {
                'trial': s.price_trial_cents,
                'single': s.price_single_cents,
                'pack5': s.price_pack5_cents,
            },
        }


@dataclass(frozen=True)
class Order:
    id: str
    order_no: str
    tenant_id: str
    sku: str
    quantity: int
    amount_cents: int
    status: str
    provider: str
    created_at: str
    paid_at: str = ''
    provider_trade_no: str = ''

    @property
    def amount_yuan(self) -> float:
        return self.amount_cents / 100

    def to_dict(self) -> dict:
        return {
            'order_no': self.order_no,
            'sku': self.sku,
            'quantity': self.quantity,
            'amount_cents': self.amount_cents,
            'amount_yuan': self.amount_yuan,
            'status': self.status,
            'provider': self.provider,
            'created_at': self.created_at,
            'paid_at': self.paid_at,
        }


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _row_get(row, name: str, idx: int):
    """兼容 sqlite3.Row 与 tuple。"""
    if hasattr(row, 'keys'):
        return row[name]
    return row[idx]


# ---------------------------------------------------------------------------
# 账户
# ---------------------------------------------------------------------------

def get_account(tenant_id: str) -> CreditAccount:
    """取额度账户。不存在则创建（注册时本应已创建，这里兜底）。"""
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT tenant_id,balance,total_purchased,total_used,trial_used,'
            'cost_used_usd FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()

    if row is None:
        now = _now()
        with db.transaction() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO credit_accounts (id,tenant_id,balance,'
                'total_purchased,total_used,trial_used,cost_used_usd,'
                'created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex, tenant_id, 0, 0, 0, 0, 0.0, now, now))
        return CreditAccount(tenant_id, 0, 0, 0, False, 0.0)

    return CreditAccount(
        tenant_id=_row_get(row, 'tenant_id', 0),
        balance=_row_get(row, 'balance', 1) or 0,
        total_purchased=_row_get(row, 'total_purchased', 2) or 0,
        total_used=_row_get(row, 'total_used', 3) or 0,
        trial_used=bool(_row_get(row, 'trial_used', 4)),
        cost_used_usd=_row_get(row, 'cost_used_usd', 5) or 0.0,
    )


def _ledger(conn, tenant_id: str, delta: int, balance_after: int,
            reason: str, order_id: str = '', diagnosis_id: str = '',
            note: str = '') -> None:
    """记一笔额度流水。"""
    conn.execute(
        'INSERT INTO credit_ledger (id,tenant_id,order_id,diagnosis_id,delta,'
        'balance_after,reason,note,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
        (uuid.uuid4().hex, tenant_id, order_id or None,
         diagnosis_id or None, delta, balance_after, reason, note, _now()))


# ---------------------------------------------------------------------------
# 订单
# ---------------------------------------------------------------------------

def create_order(tenant_id: str, sku: str, user_id: str = '') -> Order:
    """创建待支付订单。

    体验价限购在这里做**前置友好提示**，真正的强约束靠数据库唯一索引
    （idx_orders_trial_once），因为应用层判断有并发漏洞。
    """
    s = get_settings()
    if sku not in ('trial', 'single', 'pack5'):
        raise BillingError(f'未知的商品: {sku!r}', 'INVALID_SKU')

    if sku == 'trial' and s.trial_once_per_user:
        acc = get_account(tenant_id)
        if acc.trial_used:
            raise TrialAlreadyUsedError()

    amount = s.price_for_sku(sku)
    quantity = {'trial': 1, 'single': 1, 'pack5': 5}[sku]
    order_id = uuid.uuid4().hex
    order_no = sec.generate_order_no()
    now = _now()

    try:
        with db.transaction() as conn:
            conn.execute(
                'INSERT INTO orders (id,tenant_id,user_id,order_no,sku,quantity,'
                'amount_cents,currency,status,provider,created_at,updated_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (order_id, tenant_id, user_id or None, order_no, sku, quantity,
                 amount, 'CNY', 'pending', s.billing_provider, now, now))
    except Exception as e:
        # 唯一索引拦截重复体验价
        if 'trial' in str(e).lower() or 'UNIQUE' in str(e).upper():
            raise TrialAlreadyUsedError() from e
        raise

    return Order(id=order_id, order_no=order_no, tenant_id=tenant_id, sku=sku,
                 quantity=quantity, amount_cents=amount, status='pending',
                 provider=s.billing_provider, created_at=now)


def get_order_by_no(order_no: str) -> Order | None:
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT id,order_no,tenant_id,sku,quantity,amount_cents,status,'
            'provider,created_at,paid_at,provider_trade_no '
            'FROM orders WHERE order_no = ?', (order_no,)).fetchone()
    if row is None:
        return None
    return Order(
        id=_row_get(row, 'id', 0),
        order_no=_row_get(row, 'order_no', 1),
        tenant_id=_row_get(row, 'tenant_id', 2),
        sku=_row_get(row, 'sku', 3),
        quantity=_row_get(row, 'quantity', 4) or 1,
        amount_cents=_row_get(row, 'amount_cents', 5) or 0,
        status=_row_get(row, 'status', 6),
        provider=_row_get(row, 'provider', 7),
        created_at=_row_get(row, 'created_at', 8) or '',
        paid_at=_row_get(row, 'paid_at', 9) or '',
        provider_trade_no=_row_get(row, 'provider_trade_no', 10) or '',
    )


def list_orders(tenant_id: str, limit: int = 50) -> list[Order]:
    with db.get_conn() as conn:
        rows = conn.execute(
            'SELECT id,order_no,tenant_id,sku,quantity,amount_cents,status,'
            'provider,created_at,paid_at,provider_trade_no '
            'FROM orders WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?',
            (tenant_id, int(limit))).fetchall()
    return [Order(
        id=_row_get(r, 'id', 0), order_no=_row_get(r, 'order_no', 1),
        tenant_id=_row_get(r, 'tenant_id', 2), sku=_row_get(r, 'sku', 3),
        quantity=_row_get(r, 'quantity', 4) or 1,
        amount_cents=_row_get(r, 'amount_cents', 5) or 0,
        status=_row_get(r, 'status', 6), provider=_row_get(r, 'provider', 7),
        created_at=_row_get(r, 'created_at', 8) or '',
        paid_at=_row_get(r, 'paid_at', 9) or '',
        provider_trade_no=_row_get(r, 'provider_trade_no', 10) or '',
    ) for r in rows]


# ---------------------------------------------------------------------------
# 支付确认
# ---------------------------------------------------------------------------

def mark_paid(order_no: str, *, provider_trade_no: str = '',
              reviewed_by: str = '', pay_proof_ref: str = '',
              amount_cents: int | None = None) -> CreditAccount:
    """确认收款 → 订单转 paid → 额度入账。

    幂等：已支付的订单重复调用直接返回当前账户，不重复加额度。

    Args:
        amount_cents: 第三方回调金额。**必须与订单金额一致**，
                      不一致则拒绝（防篡改）。
    """
    s = get_settings()
    now = _now()

    with db.transaction() as conn:
        row = conn.execute(
            'SELECT id,tenant_id,sku,quantity,amount_cents,status '
            'FROM orders WHERE order_no = ?', (order_no,)).fetchone()
        if row is None:
            raise OrderNotFoundError(order_no)

        oid = _row_get(row, 'id', 0)
        tenant_id = _row_get(row, 'tenant_id', 1)
        sku = _row_get(row, 'sku', 2)
        quantity = _row_get(row, 'quantity', 3) or 1
        order_amount = _row_get(row, 'amount_cents', 4) or 0
        status = _row_get(row, 'status', 5)

        # ---- 幂等：已支付直接返回 ----
        if status == 'paid':
            acc_row = conn.execute(
                'SELECT tenant_id,balance,total_purchased,total_used,'
                'trial_used,cost_used_usd FROM credit_accounts WHERE tenant_id = ?',
                (tenant_id,)).fetchone()
            return CreditAccount(
                tenant_id=tenant_id,
                balance=_row_get(acc_row, 'balance', 1) or 0,
                total_purchased=_row_get(acc_row, 'total_purchased', 2) or 0,
                total_used=_row_get(acc_row, 'total_used', 3) or 0,
                trial_used=bool(_row_get(acc_row, 'trial_used', 4)),
                cost_used_usd=_row_get(acc_row, 'cost_used_usd', 5) or 0.0)

        if status != 'pending':
            raise BillingError(f'订单状态为 {status}，无法确认收款',
                               'ORDER_NOT_PENDING')

        # ---- 金额校验（防篡改）----
        if amount_cents is not None and amount_cents != order_amount:
            raise BillingError(
                f'回调金额不匹配：订单 {order_amount} 分，回调 {amount_cents} 分',
                'AMOUNT_MISMATCH')

        # ---- 订单转 paid ----
        conn.execute(
            'UPDATE orders SET status = ?, paid_at = ?, provider_trade_no = ?, '
            'reviewed_by = ?, reviewed_at = ?, pay_proof_ref = ?, updated_at = ? '
            'WHERE id = ?',
            ('paid', now, provider_trade_no or None,
             reviewed_by or None, now if reviewed_by else None,
             pay_proof_ref or None, now, oid))

        # ---- 额度入账（用条件 UPDATE 保证原子性）----
        conn.execute(
            'UPDATE credit_accounts SET balance = balance + ?, '
            'total_purchased = total_purchased + ?, '
            'trial_used = CASE WHEN ? = ? THEN 1 ELSE trial_used END, '
            'updated_at = ? WHERE tenant_id = ?',
            (quantity, quantity, sku, 'trial', now, tenant_id))

        acc_row = conn.execute(
            'SELECT tenant_id,balance,total_purchased,total_used,trial_used,'
            'cost_used_usd FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()

        balance_after = _row_get(acc_row, 'balance', 1) or 0
        _ledger(conn, tenant_id, quantity, balance_after, 'purchase',
                order_id=oid, note=f'{sku} +{quantity}次')

    return CreditAccount(
        tenant_id=tenant_id, balance=balance_after,
        total_purchased=_row_get(acc_row, 'total_purchased', 2) or 0,
        total_used=_row_get(acc_row, 'total_used', 3) or 0,
        trial_used=bool(_row_get(acc_row, 'trial_used', 4)),
        cost_used_usd=_row_get(acc_row, 'cost_used_usd', 5) or 0.0)


def cancel_order(order_no: str) -> Order:
    """取消待支付订单。已支付的不可取消（需走退款流程）。"""
    with db.transaction() as conn:
        row = conn.execute('SELECT id,status FROM orders WHERE order_no = ?',
                           (order_no,)).fetchone()
        if row is None:
            raise OrderNotFoundError(order_no)
        status = _row_get(row, 'status', 1)
        if status != 'pending':
            raise BillingError(f'订单状态为 {status}，无法取消', 'ORDER_NOT_PENDING')
        conn.execute('UPDATE orders SET status = ?, updated_at = ? WHERE id = ?',
                     ('canceled', _now(), _row_get(row, 'id', 0)))

    o = get_order_by_no(order_no)
    if o is None:
        raise OrderNotFoundError(order_no)
    return o


# ---------------------------------------------------------------------------
# 额度扣减
# ---------------------------------------------------------------------------

def consume_credit(tenant_id: str, diagnosis_id: str,
                   note: str = '') -> CreditAccount:
    """扣 1 次额度（诊断开始时调用）。

    Raises:
        InsufficientCreditsError: 余额不足
    """
    now = _now()
    with db.transaction() as conn:
        # 条件 UPDATE：余额不足时影响 0 行 —— 从根上杜绝负余额
        cur = conn.execute(
            'UPDATE credit_accounts SET balance = balance - 1, '
            'total_used = total_used + 1, updated_at = ? '
            'WHERE tenant_id = ? AND balance >= 1',
            (now, tenant_id))

        if not cur.rowcount:
            row = conn.execute(
                'SELECT balance FROM credit_accounts WHERE tenant_id = ?',
                (tenant_id,)).fetchone()
            balance = (_row_get(row, 'balance', 0) or 0) if row else 0
            raise InsufficientCreditsError(balance)

        acc_row = conn.execute(
            'SELECT tenant_id,balance,total_purchased,total_used,trial_used,'
            'cost_used_usd FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()

        balance_after = _row_get(acc_row, 'balance', 1) or 0
        _ledger(conn, tenant_id, -1, balance_after, 'consume',
                diagnosis_id=diagnosis_id, note=note or '诊断消耗 1 次')

    return CreditAccount(
        tenant_id=tenant_id, balance=balance_after,
        total_purchased=_row_get(acc_row, 'total_purchased', 2) or 0,
        total_used=_row_get(acc_row, 'total_used', 3) or 0,
        trial_used=bool(_row_get(acc_row, 'trial_used', 4)),
        cost_used_usd=_row_get(acc_row, 'cost_used_usd', 5) or 0.0)


def grant_signup_credit(tenant_id: str, note: str = '注册赠送体验诊断') -> int:
    """注册后赠送体验次数（ SIGNUP_FREE_CREDITS，默认 1，可配 0 关闭）。

    返回实际赠送的次数。注意：只加 balance，不加 total_purchased
    （赠予不算"购买"，账目口径分开）。
    """
    s = get_settings()
    n = s.signup_free_credits
    if n <= 0:
        return 0

    now = _now()
    with db.transaction() as conn:
        row = conn.execute(
            'SELECT balance FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()
        if row is None:
            return 0
        balance = _row_get(row, 'balance', 0) or 0
        conn.execute(
            'UPDATE credit_accounts SET balance = balance + ?, updated_at = ? '
            'WHERE tenant_id = ?', (n, now, tenant_id))
        conn.execute(
            'INSERT INTO credit_ledger (id,tenant_id,order_id,diagnosis_id,delta,'
            'balance_after,reason,note,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (uuid.uuid4().hex, tenant_id, None, None, n, balance + n,
             'grant', note, now))
    return n


def record_admin_usage(tenant_id: str, diagnosis_id: str,
                       note: str = '管理员免费诊断') -> None:
    """管理员免费诊断的审计留痕：delta=0，不动 balance / total_used。

    账目纪律（docs/12 §3）：测试数据永不污染经营数据 —— 月底看流水时
    "真实客户消耗 N 次 + 管理员测试 M 次" 一目了然。
    """
    now = _now()
    with db.transaction() as conn:
        row = conn.execute(
            'SELECT balance FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()
        if row is None:
            return
        balance = _row_get(row, 'balance', 0) or 0
        conn.execute(
            'INSERT INTO credit_ledger (id,tenant_id,order_id,diagnosis_id,delta,'
            'balance_after,reason,note,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (uuid.uuid4().hex, tenant_id, None, diagnosis_id or None, 0,
             balance, 'admin_test', note, now))


def refund_credit(tenant_id: str, diagnosis_id: str, note: str = '') -> CreditAccount:
    """退还 1 次额度（诊断失败时调用）。"""
    now = _now()
    with db.transaction() as conn:
        conn.execute(
            'UPDATE credit_accounts SET balance = balance + 1, '
            'total_used = MAX(total_used - 1, 0), updated_at = ? '
            'WHERE tenant_id = ?', (now, tenant_id))
        acc_row = conn.execute(
            'SELECT tenant_id,balance,total_purchased,total_used,trial_used,'
            'cost_used_usd FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()
        balance_after = _row_get(acc_row, 'balance', 1) or 0
        _ledger(conn, tenant_id, 1, balance_after, 'refund',
                diagnosis_id=diagnosis_id, note=note or '诊断失败退还 1 次')

    return CreditAccount(
        tenant_id=tenant_id, balance=balance_after,
        total_purchased=_row_get(acc_row, 'total_purchased', 2) or 0,
        total_used=_row_get(acc_row, 'total_used', 3) or 0,
        trial_used=bool(_row_get(acc_row, 'trial_used', 4)),
        cost_used_usd=_row_get(acc_row, 'cost_used_usd', 5) or 0.0)


def record_cost(tenant_id: str, cost_usd: float, model: str = '',
                input_tokens: int = 0, output_tokens: int = 0,
                reasoning_tokens: int = 0, diagnosis_id: str = '') -> float:
    """记录 LLM 成本，返回累计成本。

    推理模型的 reasoning token 必须单独记录 —— 否则成本不可解释。
    """
    now = _now()
    with db.transaction() as conn:
        conn.execute(
            'INSERT INTO usage_records (id,tenant_id,diagnosis_id,model,'
            'input_tokens,output_tokens,reasoning_tokens,cost_usd,created_at)'
            ' VALUES (?,?,?,?,?,?,?,?,?)',
            (uuid.uuid4().hex, tenant_id, diagnosis_id or None, model,
             input_tokens, output_tokens, reasoning_tokens, cost_usd, now))
        conn.execute(
            'UPDATE credit_accounts SET cost_used_usd = cost_used_usd + ?, '
            'updated_at = ? WHERE tenant_id = ?',
            (cost_usd, now, tenant_id))
        row = conn.execute(
            'SELECT cost_used_usd FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()

    return (_row_get(row, 'cost_used_usd', 0) or 0.0) if row else cost_usd


def check_cost_quota(tenant_id: str) -> tuple[bool, str]:
    """检查租户月度成本是否超限。"""
    s = get_settings()
    acc = get_account(tenant_id)
    if acc.cost_used_usd >= s.tenant_monthly_cost_usd:
        return False, (f'本月算力额度已用完'
                       f'（{acc.cost_used_usd:.2f}/{s.tenant_monthly_cost_usd:.2f} USD）')
    return True, ''


# ---------------------------------------------------------------------------
# 流水查询（客服对账）
# ---------------------------------------------------------------------------

def list_ledger(tenant_id: str, limit: int = 100) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            'SELECT delta,balance_after,reason,note,created_at '
            'FROM credit_ledger WHERE tenant_id = ? '
            'ORDER BY created_at DESC LIMIT ?', (tenant_id, int(limit))).fetchall()
    return [{
        'delta': _row_get(r, 'delta', 0),
        'balance_after': _row_get(r, 'balance_after', 1),
        'reason': _row_get(r, 'reason', 2),
        'note': _row_get(r, 'note', 3) or '',
        'created_at': _row_get(r, 'created_at', 4),
    } for r in rows]


def usage_summary(tenant_id: str) -> dict:
    """用量汇总（给工作台展示）。"""
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens),0) AS it, '
            'COALESCE(SUM(output_tokens),0) AS ot, '
            'COALESCE(SUM(reasoning_tokens),0) AS rt, '
            'COALESCE(SUM(cost_usd),0) AS cost '
            'FROM usage_records WHERE tenant_id = ?', (tenant_id,)).fetchone()
    return {
        'calls': _row_get(row, 'n', 0) or 0,
        'input_tokens': _row_get(row, 'it', 1) or 0,
        'output_tokens': _row_get(row, 'ot', 2) or 0,
        'reasoning_tokens': _row_get(row, 'rt', 3) or 0,
        'cost_usd': round(_row_get(row, 'cost', 4) or 0.0, 4),
    }


def pricing_plans() -> list[dict]:
    """给前端的套餐清单。"""
    s = get_settings()
    return [
        {'sku': 'trial', 'name': '体验价', 'price_cents': s.price_trial_cents,
         'price_yuan': s.price_trial_cents / 100, 'quantity': 1,
         'per_unit_yuan': s.price_trial_cents / 100,
         'badge': '每人限 1 次', 'desc': '先试一次，看看值不值'},
        {'sku': 'single', 'name': '标准价', 'price_cents': s.price_single_cents,
         'price_yuan': s.price_single_cents / 100, 'quantity': 1,
         'per_unit_yuan': s.price_single_cents / 100,
         'badge': '', 'desc': '常规单次诊断'},
        {'sku': 'pack5', 'name': '5 次包', 'price_cents': s.price_pack5_cents,
         'price_yuan': s.price_pack5_cents / 100, 'quantity': 5,
         'per_unit_yuan': s.price_pack5_cents / 100 / 5,
         'badge': f'省 ¥{(s.price_single_cents*5 - s.price_pack5_cents)/100:.0f}',
         'desc': f'有效期 {s.pack5_valid_days} 天，适合持续经营诊断'},
    ]
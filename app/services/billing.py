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
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.services import security as sec
from app.storage import db

logger = logging.getLogger(__name__)


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
    next_expiry_at: str = ''      # 最早到期的可用批次（'' = 无到期批次）

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
            'next_expiry_at': self.next_expiry_at,
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
    """取额度账户。不存在则创建（注册时本应已创建，这里兜底）。

    ★ 读之前先同步批次状态：到期额度在这里作废并记 expire 流水
      （docs/16 P1-6），所以任何账户 API 都会看到最新余额。
    """
    _sync_credit_state(tenant_id)

    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT tenant_id,balance,total_purchased,total_used,trial_used,'
            'cost_used_usd FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()

        if row is None:
            now = _now()
            with db.transaction() as conn2:
                conn2.execute(
                    'INSERT OR IGNORE INTO credit_accounts (id,tenant_id,'
                    'balance,total_purchased,total_used,trial_used,'
                    'cost_used_usd,created_at,updated_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, tenant_id, 0, 0, 0, 0, 0.0, now, now))
            return CreditAccount(tenant_id, 0, 0, 0, False, 0.0)

        return _account_from_conn(conn, tenant_id)


def _ledger(conn, tenant_id: str, delta: int, balance_after: int,
            reason: str, order_id: str = '', diagnosis_id: str = '',
            note: str = '', expires_at: str = '', lot_id: str = '') -> None:
    """记一笔额度流水（含批次溯源：expires_at / lot_id）。"""
    conn.execute(
        'INSERT INTO credit_ledger (id,tenant_id,order_id,diagnosis_id,delta,'
        'balance_after,reason,expires_at,lot_id,note,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (uuid.uuid4().hex, tenant_id, order_id or None,
         diagnosis_id or None, delta, balance_after, reason,
         expires_at or None, lot_id or None, note, _now()))


_ACCOUNT_COLS = ('tenant_id,balance,total_purchased,total_used,'
                 'trial_used,cost_used_usd')


def _account_from_conn(conn, tenant_id: str) -> CreditAccount:
    """在已有事务/连接上读账户（避免事务中途另开连接造成读旧值）。"""
    row = conn.execute(
        f'SELECT {_ACCOUNT_COLS} FROM credit_accounts WHERE tenant_id = ?',
        (tenant_id,)).fetchone()
    if row is None:
        return CreditAccount(tenant_id, 0, 0, 0, False, 0.0)
    return CreditAccount(
        tenant_id=_row_get(row, 'tenant_id', 0),
        balance=_row_get(row, 'balance', 1) or 0,
        total_purchased=_row_get(row, 'total_purchased', 2) or 0,
        total_used=_row_get(row, 'total_used', 3) or 0,
        trial_used=bool(_row_get(row, 'trial_used', 4)),
        cost_used_usd=_row_get(row, 'cost_used_usd', 5) or 0.0,
        next_expiry_at=_next_expiry(conn, tenant_id))


def _next_expiry(conn, tenant_id: str) -> str:
    row = conn.execute(
        'SELECT MIN(expires_at) FROM credit_lots '
        'WHERE tenant_id = ? AND remaining > 0 AND expires_at IS NOT NULL',
        (tenant_id,)).fetchone()
    return str(row[0]) if row and row[0] else ''


# ---------------------------------------------------------------------------
# 额度批次（FIFO 过期，docs/16 P1-6）
# ---------------------------------------------------------------------------

def _expires_at(days: int | None) -> str | None:
    """到期时间（None/<=0 = 永久有效）。"""
    if days is None or int(days) <= 0:
        return None
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(days=int(days))).isoformat()


def _insert_lot(conn, tenant_id: str, qty: int, *, source: str,
                source_ref: str = '', expires_at: str | None = None,
                note: str = '') -> str:
    """新建一个额度批次。"""
    lid = uuid.uuid4().hex
    now = _now()
    conn.execute(
        'INSERT INTO credit_lots (id,tenant_id,source,source_ref,granted,'
        'remaining,expires_at,note,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (lid, tenant_id, source, source_ref or None, qty, qty,
         expires_at, note, now, now))
    return lid


def _lots_remaining(conn, tenant_id: str) -> int:
    row = conn.execute(
        'SELECT COALESCE(SUM(remaining), 0) FROM credit_lots '
        'WHERE tenant_id = ? AND remaining > 0', (tenant_id,)).fetchone()
    return int(row[0] or 0) if row else 0


def _ensure_lots(conn, tenant_id: str) -> None:
    """让批次余额与账户余额对齐（存量库补登 / 账目漂移修正）。"""
    acc = conn.execute(
        'SELECT balance FROM credit_accounts WHERE tenant_id = ?',
        (tenant_id,)).fetchone()
    if acc is None:
        return
    balance = int(_row_get(acc, 'balance', 0) or 0)
    lots_sum = _lots_remaining(conn, tenant_id)
    if balance == lots_sum:
        return

    s = get_settings()
    if balance > lots_sum:
        # 旧库没有批次记录 / 直接改过 balance → 补一个历史批次
        qty = balance - lots_sum
        _insert_lot(conn, tenant_id, qty, source='legacy',
                    expires_at=_expires_at(s.credit_expire_days),
                    note='历史余额补登（无批次记录）')
        logger.info('额度批次补登 tenant=%s qty=%d', tenant_id, qty)
        return

    # lots_sum > balance：从「最晚到期」的批次开始回收多余部分
    excess = lots_sum - balance
    rows = conn.execute(
        'SELECT id,remaining,expires_at FROM credit_lots '
        'WHERE tenant_id = ? AND remaining > 0 '
        'ORDER BY expires_at IS NULL ASC, expires_at DESC, created_at DESC',
        (tenant_id,)).fetchall()
    for row in rows:
        rem = int(_row_get(row, 'remaining', 1) or 0)
        take = min(excess, rem)
        if take:
            conn.execute(
                'UPDATE credit_lots SET remaining = remaining - ?, '
                'updated_at = ? WHERE id = ?', (take, _now(),
                                                _row_get(row, 'id', 0)))
            excess -= take
        if excess <= 0:
            break
    logger.warning('额度批次多于账户余额，已回收 %d 次 tenant=%s',
                   lots_sum - balance, tenant_id)


def _expire_due_lots(conn, tenant_id: str) -> int:
    """把已到期批次归零、扣减余额并写 expire 流水。返回作废次数。"""
    now = _now()
    rows = conn.execute(
        'SELECT id,remaining,source,expires_at FROM credit_lots '
        'WHERE tenant_id = ? AND remaining > 0 '
        'AND expires_at IS NOT NULL AND expires_at <= ? '
        'ORDER BY expires_at', (tenant_id, now)).fetchall()
    if not rows:
        return 0

    acc = conn.execute(
        'SELECT balance FROM credit_accounts WHERE tenant_id = ?',
        (tenant_id,)).fetchone()
    balance = int(_row_get(acc, 'balance', 0) or 0) if acc else 0
    total = sum(int(_row_get(r, 'remaining', 1) or 0) for r in rows)
    running = balance
    for r in rows:
        rem = int(_row_get(r, 'remaining', 1) or 0)
        lid = _row_get(r, 'id', 0)
        expires_at = _row_get(r, 'expires_at', 3)
        source = _row_get(r, 'source', 2)
        conn.execute(
            'UPDATE credit_lots SET remaining = 0, updated_at = ? WHERE id = ?',
            (now, lid))
        running = max(running - rem, 0)
        _ledger(conn, tenant_id, -rem, running, 'expire',
                note=f'额度到期作废: {source} {rem} 次',
                expires_at=str(expires_at or ''), lot_id=lid)

    new_balance = max(balance - total, 0)
    if new_balance != balance - total:
        logger.warning('到期额度超过账户余额：tenant=%s balance=%d expired=%d',
                       tenant_id, balance, total)
    conn.execute(
        'UPDATE credit_accounts SET balance = ?, updated_at = ? '
        'WHERE tenant_id = ?', (new_balance, now, tenant_id))
    logger.info('额度到期作废 tenant=%s count=%d', tenant_id, total)
    return total


def _sync_credit_state(tenant_id: str) -> None:
    """读账户前的轻量同步：只在「有到期批次」或「批次与余额不一致」时写库。"""
    with db.get_conn() as conn:
        acc = conn.execute(
            'SELECT balance FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()
        if acc is None:
            return
        balance = int(_row_get(acc, 'balance', 0) or 0)
        lots_sum = _lots_remaining(conn, tenant_id)
        due = conn.execute(
            'SELECT 1 FROM credit_lots WHERE tenant_id = ? AND remaining > 0 '
            'AND expires_at IS NOT NULL AND expires_at <= ? LIMIT 1',
            (tenant_id, _now())).fetchone()
        if balance == lots_sum and due is None:
            return

    with db.transaction() as conn:
        _ensure_lots(conn, tenant_id)
        _expire_due_lots(conn, tenant_id)
        _ensure_lots(conn, tenant_id)


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
    except sqlite3.IntegrityError as e:
        # ★ docs/16 P2-11：只有「体验价限购」索引（idx_orders_trial_once）
        #   的冲突才定性为限购。原先按 'trial'/'UNIQUE' 子串匹配，任何
        #   唯一冲突（如订单号碰撞）都会被误报成"体验价限购一次"。
        if sku == 'trial' and 'idx_orders_trial_once' in str(e):
            raise TrialAlreadyUsedError() from e
        raise BillingError(f'订单创建失败：{e}', 'ORDER_CREATE_FAILED') from e

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
            return _account_from_conn(conn, tenant_id)

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
        cur = conn.execute(
            'UPDATE credit_accounts SET balance = balance + ?, '
            'total_purchased = total_purchased + ?, '
            'trial_used = CASE WHEN ? = ? THEN 1 ELSE trial_used END, '
            'updated_at = ? WHERE tenant_id = ?',
            (quantity, quantity, sku, 'trial', now, tenant_id))
        if not cur.rowcount:
            # ★ docs/16 P2-11：账户行缺失（数据异常）必须显式报错 ——
            #   原先继续执行会在后续 _row_get(None) 上 TypeError 500。
            #   抛错会连带回滚上面的订单 UPDATE，订单保持 pending，
            #   修好账户后可重新核销。
            raise BillingError(
                f'额度账户不存在（tenant={tenant_id}），请联系客服核对账目',
                'ACCOUNT_MISSING')

        # ★ 购买产生一个额度批次（5 次包 180 天，其余 365 天）
        lot_days = s.pack5_valid_days if sku == 'pack5' \
            else s.credit_expire_days
        lot_expires = _expires_at(lot_days)
        lot_id = _insert_lot(
            conn, tenant_id, quantity, source='purchase', source_ref=oid,
            expires_at=lot_expires, note=f'{sku} +{quantity}次')

        acc_row = conn.execute(
            'SELECT tenant_id,balance,total_purchased,total_used,trial_used,'
            'cost_used_usd FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()

        balance_after = _row_get(acc_row, 'balance', 1) or 0
        _ledger(conn, tenant_id, quantity, balance_after, 'purchase',
                order_id=oid, note=f'{sku} +{quantity}次',
                expires_at=lot_expires or '', lot_id=lot_id)

        return _account_from_conn(conn, tenant_id)


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
    """扣 1 次额度（诊断开始时调用），按**最早到期优先**扣减批次。

    Raises:
        InsufficientCreditsError: 余额不足
    """
    now = _now()
    with db.transaction() as conn:
        # 先同步批次：到期额度作废 / 存量库补登，再做扣减
        _ensure_lots(conn, tenant_id)
        _expire_due_lots(conn, tenant_id)
        _ensure_lots(conn, tenant_id)

        # 选「最早到期」的可用批次（NULL = 永久有效，排最后）
        lot = conn.execute(
            'SELECT id,expires_at FROM credit_lots '
            'WHERE tenant_id = ? AND remaining > 0 '
            'AND (expires_at IS NULL OR expires_at > ?) '
            'ORDER BY expires_at IS NULL ASC, expires_at ASC, '
            'created_at ASC LIMIT 1',
            (tenant_id, now)).fetchone()

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

        lot_id = ''
        lot_expires = ''
        if lot is not None:
            lot_id = str(_row_get(lot, 'id', 0))
            lot_expires = str(_row_get(lot, 'expires_at', 1) or '')
            conn.execute(
                'UPDATE credit_lots SET remaining = remaining - 1, '
                'updated_at = ? WHERE id = ? AND remaining > 0',
                (now, lot_id))
        else:
            # 批次缺失属于账目异常：不阻断用户诊断，但必须留痕
            logger.warning('扣额度时无可用批次 tenant=%s diagnosis=%s',
                           tenant_id, diagnosis_id)

        acc = _account_from_conn(conn, tenant_id)
        _ledger(conn, tenant_id, -1, acc.balance, 'consume',
                diagnosis_id=diagnosis_id, note=note or '诊断消耗 1 次',
                expires_at=lot_expires, lot_id=lot_id)

    return acc


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

        # ★ 幂等：注册赠送只能发生一次（邮箱验证后调用也不会重复发）
        existing = conn.execute(
            "SELECT 1 FROM credit_ledger WHERE tenant_id = ? "
            "AND reason = 'grant' AND note = ? LIMIT 1",
            (tenant_id, note)).fetchone()
        if existing is not None:
            return 0

        balance = _row_get(row, 'balance', 0) or 0
        expires_at = _expires_at(s.credit_expire_days)
        lot_id = _insert_lot(conn, tenant_id, n, source='grant',
                             expires_at=expires_at, note=note)
        conn.execute(
            'UPDATE credit_accounts SET balance = balance + ?, updated_at = ? '
            'WHERE tenant_id = ?', (n, now, tenant_id))
        _ledger(conn, tenant_id, n, balance + n, 'grant', note=note,
                expires_at=expires_at or '', lot_id=lot_id)
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
    """退还 1 次额度（诊断失败时调用）。

    ★ 幂等（docs/16 P1-2）：
      1. 必须存在该诊断的 consume 流水，否则拒绝 —— 防止「没扣过却退」；
      2. 已退过的诊断直接返回当前账户，不再加余额 —— 防止重放膨胀余额；
      3. 数据库层另有部分唯一索引 `idx_ledger_diag_once` 兜底。

    Raises:
        BillingError: code='NO_CONSUME_RECORD'（该诊断没有扣费记录）
    """
    s = get_settings()
    now = _now()
    with db.transaction() as conn:
        consume = conn.execute(
            "SELECT expires_at,lot_id FROM credit_ledger "
            "WHERE tenant_id = ? AND diagnosis_id = ? "
            "AND reason = 'consume' LIMIT 1",
            (tenant_id, diagnosis_id)).fetchone()
        if consume is None:
            raise BillingError(
                f'诊断 {diagnosis_id} 没有可退还的扣费记录',
                'NO_CONSUME_RECORD')
        consume_exp = str(_row_get(consume, 'expires_at', 0) or '')
        consume_lot = str(_row_get(consume, 'lot_id', 1) or '')

        refunded = conn.execute(
            "SELECT 1 FROM credit_ledger WHERE tenant_id = ? "
            "AND diagnosis_id = ? AND reason = 'refund' LIMIT 1",
            (tenant_id, diagnosis_id)).fetchone()
        if refunded is not None:
            return _account_from_conn(conn, tenant_id)

        # ★ 退款回原批次（docs/16 P1-6）：保留原有效期；
        #   若原批次已不存在/已过期，则新开一个按配置有效期的批次。
        lot_id = ''
        lot_expires = ''
        restored = False
        if consume_lot:
            lot = conn.execute(
                'SELECT id,expires_at FROM credit_lots '
                'WHERE id = ? AND tenant_id = ?',
                (consume_lot, tenant_id)).fetchone()
            if lot is not None:
                row_exp = str(_row_get(lot, 'expires_at', 1) or '')
                if not row_exp or row_exp > now:
                    conn.execute(
                        'UPDATE credit_lots SET remaining = remaining + 1, '
                        'updated_at = ? WHERE id = ?', (now, consume_lot))
                    lot_id = consume_lot
                    lot_expires = row_exp
                    restored = True
        if not restored:
            lot_expires = (consume_exp if consume_exp and consume_exp > now
                           else (_expires_at(s.credit_expire_days) or ''))
            lot_id = _insert_lot(
                conn, tenant_id, 1, source='refund',
                source_ref=diagnosis_id, expires_at=lot_expires or None,
                note='诊断失败退还 1 次')

        pre = conn.execute(
            'SELECT total_used FROM credit_accounts WHERE tenant_id = ?',
            (tenant_id,)).fetchone()
        used_before = int(_row_get(pre, 'total_used', 0) or 0) if pre else 0
        if used_before < 1:
            # 有 consume 流水但累计使用为 0 —— 账目已漂移，记录而不是用
            # MAX(...,0) 把它悄悄抹平（docs/16 P1-2）
            logger.warning('额度账目漂移：tenant=%s diagnosis=%s total_used=%d',
                           tenant_id, diagnosis_id, used_before)
        conn.execute(
            'UPDATE credit_accounts SET balance = balance + 1, '
            'total_used = total_used - 1, updated_at = ? '
            'WHERE tenant_id = ?', (now, tenant_id))
        acc = _account_from_conn(conn, tenant_id)
        _ledger(conn, tenant_id, 1, acc.balance, 'refund',
                diagnosis_id=diagnosis_id, note=note or '诊断失败退还 1 次',
                expires_at=lot_expires, lot_id=lot_id)

    return acc


def _refunds_within_24h(tenant_id: str) -> int:
    since = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(hours=24)).isoformat()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM credit_ledger WHERE tenant_id = ? "
            "AND reason = 'refund' AND created_at >= ?",
            (tenant_id, since)).fetchone()
    return int(row[0] or 0) if row else 0


def _schedule_refund(tenant_id: str, diagnosis_id: str, reason: str,
                     note: str = '', status: str = 'pending') -> None:
    """把一次失败的退款落库，等启动对账/后台重试（docs/16 P1-3）。"""
    now = _now()
    try:
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT id,attempts FROM refund_pending WHERE tenant_id = ? "
                "AND diagnosis_id = ? AND status IN ('pending','manual_review') "
                "ORDER BY created_at DESC LIMIT 1",
                (tenant_id, diagnosis_id)).fetchone()
            if row is not None:
                conn.execute(
                    'UPDATE refund_pending SET attempts = attempts + 1, '
                    'last_error = ?, status = ?, updated_at = ? WHERE id = ?',
                    (reason[:500], status, now, _row_get(row, 'id', 0)))
            else:
                conn.execute(
                    'INSERT INTO refund_pending (id,tenant_id,diagnosis_id,'
                    'reason,attempts,last_error,status,note,created_at,updated_at)'
                    ' VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, tenant_id, diagnosis_id, reason[:500],
                     1, reason[:500], status, note[:500], now, now))
    except Exception:                       # noqa: BLE001
        # 最后兜底：落库都失败时至少进日志，绝不静默
        logger.exception('退款补偿记录写入失败 tenant=%s diagnosis=%s: %s',
                         tenant_id, diagnosis_id, reason)


def request_refund(tenant_id: str, diagnosis_id: str,
                   note: str = '') -> CreditAccount:
    """诊断失败后的退款入口：永不抛异常，失败则落补偿表。

    风控（docs/16 P1-7）：同一租户 24h 内自动退款超过阈值时转人工，
    防止「构造永远失败的数据」刷 CPU/IO 后自动全额退。
    """
    s = get_settings()
    if s.max_auto_refunds_per_day > 0 \
            and _refunds_within_24h(tenant_id) >= s.max_auto_refunds_per_day:
        _schedule_refund(tenant_id, diagnosis_id,
                         '24h 自动退款次数超限，转人工审核',
                         note=note, status='manual_review')
        return get_account(tenant_id)

    try:
        return refund_credit(tenant_id, diagnosis_id, note=note)
    except BillingError as e:
        if e.code == 'NO_CONSUME_RECORD':
            # 没扣过费就没什么可退的（例如 admin 免费诊断）
            return get_account(tenant_id)
        _schedule_refund(tenant_id, diagnosis_id, e.message, note=note)
        return get_account(tenant_id)
    except Exception as e:                  # noqa: BLE001
        _schedule_refund(tenant_id, diagnosis_id,
                         f'{type(e).__name__}: {e}', note=note)
        return get_account(tenant_id)


def process_pending_refunds(limit: int = 100) -> dict:
    """重试补偿表里的待退款（启动对账 + 可被后台任务调用）。"""
    max_attempts = 5
    stats = {'resolved': 0, 'failed': 0, 'manual_review': 0}
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id,tenant_id,diagnosis_id,attempts,note FROM refund_pending "
            "WHERE status = 'pending' ORDER BY created_at LIMIT ?",
            (int(limit),)).fetchall()

    for r in rows:
        rid = _row_get(r, 'id', 0)
        tid = _row_get(r, 'tenant_id', 1)
        did = _row_get(r, 'diagnosis_id', 2)
        attempts = int(_row_get(r, 'attempts', 3) or 0)
        try:
            refund_credit(tid, did, note='对账补退款')
            _close_pending(rid, 'resolved', '')
            stats['resolved'] += 1
        except BillingError as e:
            if e.code == 'NO_CONSUME_RECORD':
                _close_pending(rid, 'resolved', '无扣费记录，无需退款')
                stats['resolved'] += 1
                continue
            status = 'manual_review' if attempts + 1 >= max_attempts else 'pending'
            _update_pending_failure(rid, attempts + 1, e.message, status)
            stats[status if status in stats else 'failed'] += 1
        except Exception as e:              # noqa: BLE001
            status = 'manual_review' if attempts + 1 >= max_attempts else 'pending'
            _update_pending_failure(rid, attempts + 1,
                                    f'{type(e).__name__}: {e}', status)
            stats[status if status in stats else 'failed'] += 1
    return stats


# ---------------------------------------------------------------------------
# 补偿退款管理端（docs/16 P1-3 + P1-7）
# ---------------------------------------------------------------------------

def list_refund_pending(limit: int = 100, status: str = '') -> list[dict]:
    """列出待补偿退款（管理员接口用）。"""
    sql = ('SELECT id,tenant_id,diagnosis_id,reason,attempts,last_error,'
           'status,note,created_at,updated_at,resolved_at '
           'FROM refund_pending')
    params: list[Any] = []
    if status:
        sql += ' WHERE status = ?'
        params.append(status)
    sql += ' ORDER BY created_at DESC LIMIT ?'
    params.append(int(limit))
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{
        'id': _row_get(r, 'id', 0),
        'tenant_id': _row_get(r, 'tenant_id', 1),
        'diagnosis_id': _row_get(r, 'diagnosis_id', 2),
        'reason': _row_get(r, 'reason', 3) or '',
        'attempts': int(_row_get(r, 'attempts', 4) or 0),
        'last_error': _row_get(r, 'last_error', 5) or '',
        'status': _row_get(r, 'status', 6),
        'note': _row_get(r, 'note', 7) or '',
        'created_at': _row_get(r, 'created_at', 8),
        'updated_at': _row_get(r, 'updated_at', 9),
        'resolved_at': _row_get(r, 'resolved_at', 10) or '',
    } for r in rows]


def _get_pending(pending_id: str):
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT id,tenant_id,diagnosis_id,status,attempts '
            'FROM refund_pending WHERE id = ?', (pending_id,)).fetchone()
    return row


def retry_refund_pending(pending_id: str) -> dict:
    """管理员重试一笔补偿退款。"""
    row = _get_pending(pending_id)
    if row is None:
        raise BillingError('补偿记录不存在', 'REFUND_PENDING_NOT_FOUND')
    tenant_id = _row_get(row, 'tenant_id', 1)
    diagnosis_id = _row_get(row, 'diagnosis_id', 2)
    status = _row_get(row, 'status', 3)
    attempts = int(_row_get(row, 'attempts', 4) or 0)
    if status not in ('pending', 'manual_review'):
        raise BillingError('该补偿记录已处理', 'REFUND_PENDING_CLOSED')

    try:
        refund_credit(tenant_id, diagnosis_id, note='管理端重试退款')
    except BillingError as e:
        if e.code == 'NO_CONSUME_RECORD':
            _close_pending(pending_id, 'resolved', '无扣费记录，无需退款')
            return {'id': pending_id, 'status': 'resolved'}
        _update_pending_failure(
            pending_id, attempts + 1, e.message,
            'manual_review' if attempts + 1 >= 5 else 'pending')
        raise BillingError(f'重试退款失败: {e.message}',
                           'REFUND_RETRY_FAILED') from e
    except Exception as e:                  # noqa: BLE001
        _update_pending_failure(pending_id, attempts + 1,
                                f'{type(e).__name__}: {e}', 'manual_review')
        raise BillingError(f'重试退款失败: {e}',
                           'REFUND_RETRY_FAILED') from e

    _close_pending(pending_id, 'resolved', '管理端重试成功')
    return {'id': pending_id, 'status': 'resolved'}


def writeoff_refund_pending(pending_id: str, note: str = '') -> dict:
    """管理员核销：不再退款，只标记已处理（留审计说明）。"""
    row = _get_pending(pending_id)
    if row is None:
        raise BillingError('补偿记录不存在', 'REFUND_PENDING_NOT_FOUND')
    if _row_get(row, 'status', 3) == 'resolved':
        raise BillingError('该补偿记录已处理', 'REFUND_PENDING_CLOSED')
    _close_pending(pending_id, 'resolved', note or '管理员核销（不退款）')
    return {'id': pending_id, 'status': 'resolved'}


def _close_pending(pending_id: str, status: str, reason: str) -> None:
    now = _now()
    with db.transaction() as conn:
        conn.execute(
            'UPDATE refund_pending SET status = ?, last_error = ?, '
            'resolved_at = ?, updated_at = ? WHERE id = ?',
            (status, reason[:500] or None,
             now if status == 'resolved' else None, now, pending_id))


def _update_pending_failure(pending_id: str, attempts: int, error: str,
                            status: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            'UPDATE refund_pending SET attempts = ?, last_error = ?, '
            'status = ?, updated_at = ? WHERE id = ?',
            (attempts, error[:500], status, _now(), pending_id))


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


def monthly_cost_usd(tenant_id: str) -> float:
    """当月（本机时区自然月）LLM 成本合计。

    ★ 熔断必须按月统计（docs/16 P1-4）：`credit_accounts.cost_used_usd`
      是终身累计值，拿它比较月上限会让付费用户在累计达标后被永久 402。
      月界按本机时区计算（国内部署即北京时间），再换算成 UTC 与
      usage_records.created_at（UTC ISO）比较。
    """
    now_local = dt.datetime.now().astimezone()
    month_start = (now_local.replace(day=1, hour=0, minute=0, second=0,
                                     microsecond=0)
                   .astimezone(dt.timezone.utc).isoformat())
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT COALESCE(SUM(cost_usd), 0) FROM usage_records '
            'WHERE tenant_id = ? AND created_at >= ?',
            (tenant_id, month_start)).fetchone()
    return float(row[0] or 0.0) if row else 0.0


def check_cost_quota(tenant_id: str) -> tuple[bool, str]:
    """检查租户**本月**成本是否超限（docs/16 P1-4）。"""
    s = get_settings()
    month_cost = monthly_cost_usd(tenant_id)
    if month_cost >= s.tenant_monthly_cost_usd:
        return False, (f'本月算力额度已用完'
                       f'（{month_cost:.2f}/{s.tenant_monthly_cost_usd:.2f} USD）')
    return True, ''


# ---------------------------------------------------------------------------
# 流水查询（客服对账）
# ---------------------------------------------------------------------------

def list_ledger(tenant_id: str, limit: int = 100) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            'SELECT delta,balance_after,reason,note,created_at,expires_at '
            'FROM credit_ledger WHERE tenant_id = ? '
            'ORDER BY created_at DESC LIMIT ?', (tenant_id, int(limit))).fetchall()
    return [{
        'delta': _row_get(r, 'delta', 0),
        'balance_after': _row_get(r, 'balance_after', 1),
        'reason': _row_get(r, 'reason', 2),
        'note': _row_get(r, 'note', 3) or '',
        'created_at': _row_get(r, 'created_at', 4),
        'expires_at': _row_get(r, 'expires_at', 5) or '',
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
         # ★ 批次化有效期已实现（docs/16 P1-6）
         'desc': (f'有效期 {s.pack5_valid_days} 天，适合持续经营诊断'
                  if s.pack5_valid_days > 0 else '额度长期有效，适合持续经营诊断')},
    ]
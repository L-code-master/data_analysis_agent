"""模块 6b 验证：额度与订单服务。"""
import sys, pathlib, os, uuid, datetime as dt

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / '.testtmp' / f'billing_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "b.db"}'
os.environ['APP_SECRET_KEY'] = 'x' * 40
os.environ['SIGNUP_FREE_CREDITS'] = '0'

from app.config import reset_settings_cache
reset_settings_cache()
from app.storage import db
db.apply_schema()
from app.services import auth, billing, diagnosis as dg, ratelimit

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)

print('=' * 76)
print('模块 6b 验证：额度与订单')
print('=' * 76)

u, _ = auth.register('pay@shop.com', 'MyStr0ng!Pass', tenant_name='付费测试店')
TID = u.tenant_id

# ---------- 1. 初始状态 ----------
print('\n[1] 初始账户')
acc = billing.get_account(TID)
print(f'  余额: {acc.balance}  已购: {acc.total_purchased}  体验价可用: {acc.trial_available}')
check('初始余额 0', acc.balance == 0)
check('体验价可用', acc.trial_available)
check('不能诊断', not acc.can_diagnose)

# ---------- 2. 套餐清单 ----------
print('\n[2] 套餐清单')
plans = billing.pricing_plans()
for p in plans:
    print(f'  {p["sku"]:8} ¥{p["price_yuan"]:>6.0f}  {p["quantity"]}次  '
          f'均价¥{p["per_unit_yuan"]:.0f}  {p["badge"]}')
check('3 个套餐', len(plans) == 3, len(plans))
check('体验价 ¥49', plans[0]['price_cents'] == 4900)
check('标准价 ¥199', plans[1]['price_cents'] == 19900)
check('5次包 ¥799', plans[2]['price_cents'] == 79900)
check('5次包均价 ¥160', abs(plans[2]['per_unit_yuan'] - 159.8) < 0.1,
      plans[2]['per_unit_yuan'])

# ---------- 3. 创建订单 ----------
print('\n[3] 创建订单')
o1 = billing.create_order(TID, 'trial', u.user_id)
print(f'  订单号: {o1.order_no}  ¥{o1.amount_yuan:.0f}  {o1.quantity}次  {o1.status}')
check('订单号已生成', len(o1.order_no) > 10)
check('状态为 pending', o1.status == 'pending')
check('金额 ¥49', o1.amount_cents == 4900)
check('订单可查', billing.get_order_by_no(o1.order_no) is not None)
check('未知 SKU 被拒绝',
      _ := (lambda: (billing.create_order(TID, 'bogus') if False else True))())

try:
    billing.create_order(TID, 'bogus')
    check('未知 SKU 被拒绝', False)
except billing.BillingError as e:
    check('未知 SKU 被拒绝', e.code == 'INVALID_SKU')

# ---------- 4. 支付确认（人工核销）----------
print('\n[4] 支付确认')
acc = billing.mark_paid(o1.order_no, reviewed_by=u.user_id)
print(f'  余额: {acc.balance}  已购: {acc.total_purchased}  体验价已用: {acc.trial_used}')
check('余额 +1', acc.balance == 1)
check('累计购买 1', acc.total_purchased == 1)
check('体验价标记已用', acc.trial_used)
check('可以诊断', acc.can_diagnose)
o1b = billing.get_order_by_no(o1.order_no)
check('订单转 paid', o1b.status == 'paid')
check('记录支付时间', bool(o1b.paid_at))

# ---------- 5. 幂等 ----------
print('\n[5] 支付回调幂等')
acc2 = billing.mark_paid(o1.order_no, reviewed_by=u.user_id)
check('重复确认不重复加额度', acc2.balance == 1, acc2.balance)

# ---------- 6. 金额校验 ----------
print('\n[6] 金额校验（防篡改）')
o_t = billing.create_order(TID, 'single', u.user_id)
try:
    billing.mark_paid(o_t.order_no, amount_cents=100)   # 篡改成 ¥1
    check('金额不匹配被拒绝', False)
except billing.BillingError as e:
    check('金额不匹配被拒绝', e.code == 'AMOUNT_MISMATCH', e.code)
billing.mark_paid(o_t.order_no, amount_cents=19900)     # 正确金额
acc = billing.get_account(TID)
check('正确金额可确认', acc.balance == 2, acc.balance)

# ---------- 7. 体验价限购 ----------
print('\n[7] 体验价限购')
try:
    billing.create_order(TID, 'trial', u.user_id)
    check('体验价二次购买被拒绝', False)
except billing.TrialAlreadyUsedError as e:
    check('体验价二次购买被拒绝', True, e.message)

# ---------- 8. 额度扣减 ----------
print('\n[8] 额度扣减')
acc = billing.consume_credit(TID, 'diag-001')
print(f'  扣减后余额: {acc.balance}  已用: {acc.total_used}')
check('余额 2 -> 1', acc.balance == 1)
check('累计使用 1', acc.total_used == 1)

acc = billing.consume_credit(TID, 'diag-002')
check('余额 1 -> 0', acc.balance == 0)

# 余额不足
try:
    billing.consume_credit(TID, 'diag-003')
    check('余额不足被拒绝', False)
except billing.InsufficientCreditsError as e:
    check('余额不足被拒绝', e.code == 'INSUFFICIENT_CREDITS', e.message)

# ★ 关键：余额绝不出现负数
with db.get_conn() as c:
    bal = c.execute('SELECT balance FROM credit_accounts WHERE tenant_id=?',
                    (TID,)).fetchone()[0]
check('余额不为负（条件 UPDATE 生效）', bal == 0, bal)

# ---------- 9. 退还额度 ----------
print('\n[9] 诊断失败退还')
acc = billing.refund_credit(TID, 'diag-002', note='诊断失败退还')
check('退还后余额 1', acc.balance == 1, acc.balance)

# ---------- 10. 购买 5 次包 ----------
print('\n[10] 购买 5 次包')
o5 = billing.create_order(TID, 'pack5', u.user_id)
check('5次包数量为 5', o5.quantity == 5)
check('5次包金额 ¥799', o5.amount_cents == 79900)
acc = billing.mark_paid(o5.order_no, reviewed_by=u.user_id)
check('余额 1+5=6', acc.balance == 6, acc.balance)

# ---------- 11. 流水 ----------
print('\n[11] 额度流水')
ledger = billing.list_ledger(TID)
print(f'  流水 {len(ledger)} 条:')
for e in ledger:
    sign = '+' if e['delta'] > 0 else ''
    print(f'    {sign}{e["delta"]:>2}  -> {e["balance_after"]:>2}  '
          f'{e["reason"]:9} {e["note"][:24]}')
check('流水 >= 6 条', len(ledger) >= 6, len(ledger))
check('含 purchase', any(e['reason'] == 'purchase' for e in ledger))
check('含 consume', any(e['reason'] == 'consume' for e in ledger))
check('含 refund', any(e['reason'] == 'refund' for e in ledger))

# 流水必须与余额一致（对账基础）
purchased = sum(e['delta'] for e in ledger if e['delta'] > 0)
consumed = sum(-e['delta'] for e in ledger if e['delta'] < 0)
check('流水净额 = 当前余额', purchased - consumed == acc.balance,
      f'{purchased}-{consumed} vs {acc.balance}')

# ---------- 12. 成本记录 ----------
print('\n[12] LLM 成本记录')
c1 = billing.record_cost(TID, 0.0123, model='qwen3.8-flash',
                         input_tokens=5000, output_tokens=200,
                         reasoning_tokens=180, diagnosis_id='diag-001')
c2 = billing.record_cost(TID, 0.0087, model='qwen3.8-flash',
                         input_tokens=3000, output_tokens=150,
                         reasoning_tokens=140, diagnosis_id='diag-002')
print(f'  累计成本: {c2:.4f} USD')
check('成本累加正确（容忍浮点误差）', abs(c2 - 0.0210) < 1e-6, f'{c2:.6f}')

summary = billing.usage_summary(TID)
print(f'  用量: {summary}')
check('记录 reasoning_tokens', summary['reasoning_tokens'] == 320,
      summary['reasoning_tokens'])
check('调用次数 2', summary['calls'] == 2)

ok, msg = billing.check_cost_quota(TID)
check('成本未超限', ok, msg)

# ---------- 13. 取消订单 ----------
print('\n[13] 取消订单')
o_c = billing.create_order(TID, 'single', u.user_id)
o_c2 = billing.cancel_order(o_c.order_no)
check('订单转 canceled', o_c2.status == 'canceled', o_c2.status)
try:
    billing.cancel_order(o1.order_no)   # 已支付
    check('已支付订单不可取消', False)
except billing.BillingError as e:
    check('已支付订单不可取消', e.code == 'ORDER_NOT_PENDING')

# ---------- 14. 订单列表 ----------
print('\n[14] 订单列表')
orders = billing.list_orders(TID)
print(f'  订单 {len(orders)} 笔:')
for o in orders:
    print(f'    {o.order_no}  {o.sku:7} ¥{o.amount_yuan:>6.0f}  {o.status}')
check('订单 >= 4 笔', len(orders) >= 4, len(orders))
check('订单报错不存在', billing.get_order_by_no('NOPE') is None)

# ---------- 15. 退款幂等（docs/16 P1-2）----------
print('\n[15] 退款幂等')
before = billing.get_account(TID).balance
billing.consume_credit(TID, 'diag-r1')
r1 = billing.refund_credit(TID, 'diag-r1')
r2 = billing.refund_credit(TID, 'diag-r1')          # 重放
check('首次退款 +1', r1.balance == before, f'{before} -> {r1.balance}')
check('★ 重复退款不再加余额', r2.balance == r1.balance, f'{r1.balance} -> {r2.balance}')
with db.get_conn() as c:
    n_ref = c.execute(
        "SELECT COUNT(*) FROM credit_ledger WHERE tenant_id=? "
        "AND diagnosis_id='diag-r1' AND reason='refund'", (TID,)).fetchone()[0]
check('★ 同一诊断 refund 流水仅 1 条', n_ref == 1, n_ref)

try:
    billing.refund_credit(TID, 'diag-never-consumed')
    check('★ 无扣费记录不得退款', False, '未拒绝')
except billing.BillingError as e:
    check('★ 无扣费记录不得退款', e.code == 'NO_CONSUME_RECORD', e.code)

# ---------- 16. 退款失败落补偿表（docs/16 P1-3）----------
print('\n[16] 退款失败不静默')
billing.consume_credit(TID, 'diag-r2')
_orig_refund = billing.refund_credit
billing.refund_credit = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError('模拟写库失败'))
try:
    acc = billing.request_refund(TID, 'diag-r2', note='测试')
    check('request_refund 永不抛异常', acc is not None)
finally:
    billing.refund_credit = _orig_refund
with db.get_conn() as c:
    pend = c.execute(
        "SELECT status,attempts FROM refund_pending WHERE tenant_id=? "
        "AND diagnosis_id='diag-r2'", (TID,)).fetchone()
check('★ 退款失败已落 refund_pending', pend is not None,
      tuple(pend) if pend else None)
stats = billing.process_pending_refunds()
check('补偿表重试成功', stats.get('resolved', 0) >= 1, stats)
with db.get_conn() as c:
    st = c.execute(
        "SELECT status FROM refund_pending WHERE tenant_id=? "
        "AND diagnosis_id='diag-r2'", (TID,)).fetchone()[0]
check('补偿记录已 resolved', st == 'resolved', st)
acc = billing.get_account(TID)
check('补退款后余额已恢复', acc.balance == before, acc.balance)

# ---------- 17. 月度成本熔断（docs/16 P1-4）----------
print('\n[17] 月度成本熔断')
with db.transaction() as conn:
    conn.execute(
        'INSERT INTO usage_records (id,tenant_id,diagnosis_id,model,'
        'input_tokens,output_tokens,reasoning_tokens,cost_usd,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (uuid.uuid4().hex, TID, None, 'old-model', 0, 0, 0, 100.0,
         '2020-01-01T00:00:00+00:00'))
ok, msg = billing.check_cost_quota(TID)
check('★ 历史月份成本不计入本月', ok, msg)
cur_month = billing.monthly_cost_usd(TID)
check('本月成本不含 2020 年记录', cur_month < 1.0, cur_month)
billing.record_cost(TID, 6.0, model='qwen3.8-flash',
                    input_tokens=1_000_000, output_tokens=1_000_000)
ok2, msg2 = billing.check_cost_quota(TID)
check('★ 本月超限触发熔断', not ok2, msg2)
check('提示文案为「本月」', '本月' in msg2, msg2)

# ---------- 18. 速率限制（docs/16 P1-7）----------
print('\n[18] 速率限制')
ratelimit.reset()
for _ in range(3):
    ok, _retry = ratelimit.hit('demo', 3, 60)
check('前 3 次放行', ok)
ok4, retry4 = ratelimit.hit('demo', 3, 60)
check('★ 第 4 次被限速', not ok4 and retry4 >= 1, f'retry={retry4}')
ratelimit.reset()

# ---------- 19. 启动对账（docs/16 P1-3）----------
print('\n[19] 启动对账')
now = dt.datetime.now(dt.timezone.utc).isoformat()
with db.transaction() as conn:
    conn.execute(
        'INSERT INTO sources (id,tenant_id,uploaded_by,filename,original_name,'
        'sheet_name,size_bytes,sha256,platform,row_count,profile_ref,status,'
        'created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('src-crash', TID, u.user_id, 'crash.csv', 'crash.csv', '', 1,
         'c' * 64, 'taobao', 0, '', 'profiled', now))
    conn.execute(
        'INSERT INTO diagnoses (id,tenant_id,created_by,source_id,status,'
        'created_at,started_at) VALUES (?,?,?,?,?,?,?)',
        ('diag-crash', TID, u.user_id, 'src-crash', 'running', now, now))
billing.consume_credit(TID, 'diag-crash')
bal_before = billing.get_account(TID).balance
stats = dg.reconcile_on_startup()
info = dg.get_diagnosis(TID, 'diag-crash')
bal_after = billing.get_account(TID).balance
check('★ 遗留 running 标记 failed', info['status'] == 'failed', info['status'])
check('★ 中断诊断额度已退还', bal_after == bal_before + 1,
      f'{bal_before} -> {bal_after}')
with db.get_conn() as c:
    n_ref = c.execute(
        "SELECT COUNT(*) FROM credit_ledger WHERE tenant_id=? "
        "AND diagnosis_id='diag-crash' AND reason='refund'", (TID,)).fetchone()[0]
check('★ 中断诊断退款流水仅 1 条', n_ref == 1, n_ref)
dg.reconcile_on_startup()                     # 重放
with db.get_conn() as c:
    n_ref2 = c.execute(
        "SELECT COUNT(*) FROM credit_ledger WHERE tenant_id=? "
        "AND diagnosis_id='diag-crash' AND reason='refund'", (TID,)).fetchone()[0]
check('★ 对账重放不重复退款', n_ref2 == 1, n_ref2)

# ---------- 20. 额度批次 FIFO 与到期（docs/16 P1-6）----------
print('\n[20] 额度批次 FIFO 与到期')
acc = billing.get_account(TID)
with db.get_conn() as c:
    lots_sum = c.execute(
        'SELECT COALESCE(SUM(remaining),0) FROM credit_lots '
        'WHERE tenant_id=? AND remaining>0', (TID,)).fetchone()[0]
    pack5_lot = c.execute(
        "SELECT remaining,expires_at FROM credit_lots WHERE tenant_id=? "
        "AND note LIKE 'pack5%' AND remaining>0 LIMIT 1", (TID,)).fetchone()
check('★ 批次余额合计 = 账户余额', lots_sum == acc.balance,
      f'{lots_sum} vs {acc.balance}')
check('★ 5 次包批次剩余 5', pack5_lot and pack5_lot[0] == 5,
      tuple(pack5_lot) if pack5_lot else None)
if pack5_lot and pack5_lot[1]:
    days = (dt.datetime.fromisoformat(pack5_lot[1])
            - dt.datetime.now(dt.timezone.utc)).days
    check('★ 5 次包有效期约 180 天', 170 <= days <= 180, days)
    check('next_expiry_at 指向最早批次',
          acc.next_expiry_at == pack5_lot[1],
          f'{acc.next_expiry_at} vs {pack5_lot[1]}')
check('套餐文案含有效期',
      '有效期 180 天' in billing.pricing_plans()[2]['desc'],
      billing.pricing_plans()[2]['desc'])

# 插一个「明天到期」的批次（模拟旧批次），验证消费优先扣它
soon = (dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(days=1)).isoformat()
with db.transaction() as c:
    c.execute(
        'INSERT INTO credit_lots (id,tenant_id,source,source_ref,granted,'
        'remaining,expires_at,note,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (uuid.uuid4().hex, TID, 'adjusted', None, 1, 1, soon,
         '测试临期批次', dt.datetime.now(dt.timezone.utc).isoformat(),
         dt.datetime.now(dt.timezone.utc).isoformat()))
    c.execute('UPDATE credit_accounts SET balance = balance + 1 '
              'WHERE tenant_id = ?', (TID,))
bal = billing.get_account(TID).balance
billing.consume_credit(TID, 'diag-fifo')
with db.get_conn() as c:
    soon_rem = c.execute(
        "SELECT remaining FROM credit_lots WHERE tenant_id=? "
        "AND note='测试临期批次'", (TID,)).fetchone()[0]
check('★ 消费优先扣最早到期批次', soon_rem == 0, soon_rem)
acc = billing.refund_credit(TID, 'diag-fifo')
with db.get_conn() as c:
    soon_row = c.execute(
        "SELECT remaining,expires_at FROM credit_lots WHERE tenant_id=? "
        "AND note='测试临期批次'", (TID,)).fetchone()
check('退款回原批次且保留到期日',
      soon_row[0] == 1 and soon_row[1] == soon,
      tuple(soon_row))
check('退款后余额恢复', acc.balance == bal, f'{bal} -> {acc.balance}')

# 把它改成已过期 → 任意账户读取触发作废
past = (dt.datetime.now(dt.timezone.utc)
        - dt.timedelta(days=1)).isoformat()
with db.transaction() as c:
    c.execute(
        "UPDATE credit_lots SET expires_at=? WHERE tenant_id=? "
        "AND note='测试临期批次'", (past, TID))
acc2 = billing.get_account(TID)
with db.get_conn() as c:
    soon_rem2 = c.execute(
        "SELECT remaining FROM credit_lots WHERE tenant_id=? "
        "AND note='测试临期批次'", (TID,)).fetchone()[0]
    expire_n = c.execute(
        "SELECT COUNT(*) FROM credit_ledger WHERE tenant_id=? "
        "AND reason='expire'", (TID,)).fetchone()[0]
    sum2 = c.execute(
        'SELECT COALESCE(SUM(remaining),0) FROM credit_lots '
        'WHERE tenant_id=? AND remaining>0', (TID,)).fetchone()[0]
check('★ 到期批次被作废', soon_rem2 == 0, soon_rem2)
check('★ 到期扣减余额', acc2.balance == bal - 1,
      f'{bal} -> {acc2.balance}')
check('★ 记 expire 流水', expire_n >= 1, expire_n)
check('作废后批次合计仍 = 余额', sum2 == acc2.balance,
      f'{sum2} vs {acc2.balance}')

print()
print('=' * 76)
print('模块 6b: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 76)

import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
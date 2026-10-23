"""模块 6f：并发与互斥防护（docs/16 批次 1 · P0-6 / P0-7）。

覆盖：
  1. P0-6  同类型多数据源 → 立即 400（不再静默互相覆盖）
  2. P0-7  同租户已有进行中诊断 → 409
  3. P0-7  全局并发上限耗尽 → 503
  4. P0-7  两个并发 run_diagnosis 被租户锁串行化 → 双双成功
  5. P0-7  诊断进行中查数问答（形态 A）不 500（等锁后正常回答）
"""
import io
import csv
import os
import pathlib
import sys
import threading
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = ROOT / '.testtmp' / f'conc_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "conc.db"}'
os.environ['TENANTS_ROOT'] = str(TMP / 'tenants')
os.environ['APP_SECRET_KEY'] = 'test-secret-key-for-jwt-signing-32chars-minimum'
os.environ['SIGNUP_FREE_CREDITS'] = '1'
os.environ['LLM_API_KEY'] = ''            # 形态 A 不需要 LLM

sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient         # noqa: E402

from app.config import reset_settings_cache       # noqa: E402
reset_settings_cache()
from app.main import app                          # noqa: E402
from app.storage import db                        # noqa: E402
from app.services import sample_data              # noqa: E402

db.apply_schema()
client = TestClient(app)
fails: list[str] = []


def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)


print('=' * 78)
print('模块 6f：并发与互斥防护（P0-6 / P0-7）')
print('=' * 78)

# ---------------------------------------------------------------------------
print('\n[0] 准备：注册 + 充值 + 样例上传')
r = client.post('/api/v1/auth/register', json={
    'email': 'conc@test.com', 'password': 'ConcTest!2026x', 'tenant_name': '并发店'})
check('注册 201', r.status_code == 201, r.status_code)
TENANT_ID = r.json()['user']['tenant_id']
USER_ID = r.json()['user']['user_id']
H = {'Authorization': f'Bearer {r.json()["token"]["access_token"]}'}

# 充值（走服务层 mark_paid —— API confirm 仅平台管理员可调，见模块 6d/e）
from app.services import billing as billing_svc    # noqa: E402
o = client.post('/api/v1/billing/orders', headers=H, json={'sku': 'pack5'})
check('创建订单', o.status_code == 201, o.status_code)
billing_svc.mark_paid(o.json()['order_no'], reviewed_by='test')
acc = client.get('/api/v1/billing/account', headers=H).json()
check('余额 6（1 赠送 + 5 购买）', acc['balance'] == 6, acc['balance'])

xlsx = sample_data.build_xlsx()
r = client.post('/api/v1/sources', headers=H,
                files={'file': ('样例.xlsx', xlsx,
                                'application/vnd.openxmlformats-'
                                'officedocument.spreadsheetml.sheet')})
srcs = r.json()
check('上传拆分 6 源', r.status_code == 201 and len(srcs) == 6, len(srcs))
IDS = [s['source_id'] for s in srcs]
for s in srcs:
    p = client.get(f'/api/v1/sources/{s["source_id"]}/profile', headers=H).json()
    m = {x['target_field']: x['source_column'] for x in p['mappings']}
    client.post(f'/api/v1/sources/{s["source_id"]}/mapping', headers=H,
                json={'mapping': m})

# ---------------------------------------------------------------------------
print('\n[1] P0-6：同类型多数据源立即拒绝（不再静默互相覆盖）')


def orders_csv_v2() -> bytes:
    """第二份订单表（内容不同 → sha256 去重不会合并）。"""
    headers = ['订单编号', '子订单编号', '订单创建时间', '买家付款时间', '发货时间',
               '确认收货时间', '订单状态', '退款状态', '买家会员名', '商品ID',
               '商品标题', '商家编码', 'SKU ID', 'sku属性', '单价', '购买数量',
               '买家实际支付金额', '商品金额', '运费', '优惠金额', '商家实收金额',
               '收货省份']
    rows = [['TBX1', 'TBX1-1', '2026-09-10 10:00:00', '2026-09-10 10:05:00',
             '2026-09-10 18:00:00', '2026-09-13 12:00:00', '交易成功', '无退款',
             'b9', 'ITEM-Z', '测试衬衫', 'MC-Z', 'SKU-Z', '白;L', '90.00', '2',
             '180.00', '180.00', '0.00', '0.00', '180.00', '上海']]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode('utf-8')


r = client.post('/api/v1/sources', headers=H,
                files={'file': ('订单2.csv', orders_csv_v2(), 'text/csv')})
check('上传第二份订单表 201', r.status_code == 201, r.status_code)
if r.status_code == 201:
    second_orders_id = r.json()[0]['source_id']
    r = client.post('/api/v1/diagnoses', headers=H, json={
        'source_ids': IDS + [second_orders_id],
        'time_range': '2026-09-01~2026-09-28'})
    check('★ 同类型重复 → 400', r.status_code == 400, r.status_code)
    check('★ 错误码 DUPLICATE_SOURCE_TYPE',
          r.json().get('code') == 'DUPLICATE_SOURCE_TYPE', r.json().get('code'))
    acc = client.get('/api/v1/billing/account', headers=H).json()
    check('★ 未扣额度（余额仍 6）', acc['balance'] == 6, acc['balance'])

# ---------------------------------------------------------------------------
print('\n[2] P0-7：同租户已有进行中诊断 → 409')
with db.transaction() as conn:
    conn.execute(
        "INSERT INTO diagnoses (id,tenant_id,created_by,source_id,status,"
        "progress_phase,created_at,started_at) VALUES (?,?,?,?,?,?,?,?)",
        ('synthetic-running', TENANT_ID, USER_ID, IDS[0], 'running', 'etl',
         '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00'))
r = client.post('/api/v1/diagnoses', headers=H, json={
    'source_ids': IDS, 'time_range': '2026-09-01~2026-09-28'})
check('★ 已有进行中诊断 → 409', r.status_code == 409, r.status_code)
check('★ 错误码 DIAGNOSIS_RUNNING',
      r.json().get('code') == 'DIAGNOSIS_RUNNING', r.json().get('code'))
with db.transaction() as conn:
    conn.execute("DELETE FROM diagnoses WHERE id = 'synthetic-running'")

# ---------------------------------------------------------------------------
print('\n[3] P0-7：全局并发上限耗尽 → 503')
from app.api.routes import diagnoses as diag_routes    # noqa: E402
held = []
for _ in range(diag_routes._MAX_RUNNING_DIAGNOSES):
    if diag_routes._diag_slots.acquire(blocking=False):
        held.append(True)
check('测试占满全部并发槽', len(held) == diag_routes._MAX_RUNNING_DIAGNOSES)
r = client.post('/api/v1/diagnoses', headers=H, json={
    'source_ids': IDS, 'time_range': '2026-09-01~2026-09-28'})
check('★ 并发上限 → 503', r.status_code == 503, r.status_code)
check('★ 错误码 SYSTEM_BUSY', r.json().get('code') == 'SYSTEM_BUSY')
for _ in held:
    diag_routes._diag_slots.release()

# ---------------------------------------------------------------------------
print('\n[4] P0-7：并发 run_diagnosis 被租户锁串行化 → 双双成功')
from app.services import diagnosis as dg              # noqa: E402

results: list = []


def run_one(tag: str) -> None:
    try:
        res = dg.run_diagnosis(TENANT_ID, USER_ID, IDS,
                               time_range_text='2026-09-01~2026-09-28',
                               diagnosis_id=f'conc-{tag}')
        results.append((tag, res.status, None))
    except Exception as e:                            # noqa: BLE001
        results.append((tag, None, f'{type(e).__name__}: {e}'))


t1 = threading.Thread(target=run_one, args=('a',))
t2 = threading.Thread(target=run_one, args=('b',))
t1.start()
t2.start()
t1.join(timeout=180)
t2.join(timeout=180)
check('两个线程都已结束', not t1.is_alive() and not t2.is_alive())
for tag, st, err in results:
    check(f'★ 诊断 {tag} 成功（串行化，无写冲突）', st == 'succeeded',
          err or st)
check('两次都成功', len([1 for _t, s, _e in results if s == 'succeeded']) == 2)

# ---------------------------------------------------------------------------
print('\n[5] P0-7：诊断进行中，查数问答不 500（等锁后正常回答）')
r = client.post('/api/v1/chat/sessions', headers=H, params={'title': '并发问答'})
SID = r.json()['session_id']

r = client.post('/api/v1/diagnoses', headers=H, json={
    'source_ids': IDS, 'time_range': '2026-09-01~2026-09-28'})
check('发起诊断 202', r.status_code == 202, r.status_code)
DID = r.json()['diagnosis_id']

# 诊断刚起步、大概率正持有租户锁 —— 此时提问：要么等锁、要么先到，
# 两条路径都必须正常返回而不是 500（TestClient 走进程内传输，无超时）
r = client.post(f'/api/v1/chat/sessions/{SID}/ask', headers=H,
                json={'question': '这个月毛利率多少'})
check('★ 诊断期间查数问答 200', r.status_code == 200, r.status_code)
check('★ 走 qa 路由', r.json().get('route') == 'qa', r.json().get('route'))

# 等诊断收尾
for _ in range(120):
    r = client.get(f'/api/v1/diagnoses/{DID}', headers=H)
    if r.status_code == 200 and r.json()['status'] in ('succeeded', 'failed'):
        break
    time.sleep(0.4)
check('诊断最终成功', r.json().get('status') == 'succeeded', r.json().get('status'))

# ---------------------------------------------------------------------------
print('\n[6] P2-30：并发扣减最后 1 次额度 —— 恰好一人成功，余额不透支')
r = client.post('/api/v1/auth/register', json={
    'email': 'race@test.com', 'password': 'RaceTest!2026x', 'tenant_name': '并发扣减店'})
check('注册 201', r.status_code == 201, r.status_code)
RACE_TID = r.json()['user']['tenant_id']
acc = billing_svc.get_account(RACE_TID)
check('新租户余额 1（注册赠送）', acc.balance == 1, acc.balance)

consume_ok: list = []
consume_rejected: list = []


def try_consume(i: int) -> None:
    try:
        billing_svc.consume_credit(RACE_TID, f'race-consume-{i}')
        consume_ok.append(i)
    except billing_svc.InsufficientCreditsError:
        consume_rejected.append(i)
    except Exception as e:                        # noqa: BLE001
        consume_rejected.append(f'意外异常:{type(e).__name__}:{e}')


threads = [threading.Thread(target=try_consume, args=(i,)) for i in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)
check('线程全部结束', not any(t.is_alive() for t in threads))
check('★ 恰好 1 次扣减成功', len(consume_ok) == 1, consume_ok)
check('★ 其余 7 次余额不足被拒', len(consume_rejected) == 7, consume_rejected)
check('★ 无意外异常（仅 InsufficientCredits）',
      all(isinstance(x, int) for x in consume_rejected), consume_rejected)
acc = billing_svc.get_account(RACE_TID)
check('★ 余额 0（不透支不透负）', acc.balance == 0, acc.balance)
with db.get_conn() as conn:
    n_consume = conn.execute(
        "SELECT COUNT(*) FROM credit_ledger WHERE tenant_id = ? "
        "AND reason = 'consume'", (RACE_TID,)).fetchone()[0]
check('★ 流水恰好 1 条 consume', n_consume == 1, n_consume)

# ---------------------------------------------------------------------------
print('\n[7] P2-30：并发重放退款 —— 只落一次 refund，余额不膨胀')
o = billing_svc.create_order(RACE_TID, 'single')
billing_svc.mark_paid(o.order_no, reviewed_by='test')
acc = billing_svc.get_account(RACE_TID)
check('充值后余额 1（[6] 已消耗赠送额度）', acc.balance == 1, acc.balance)

billing_svc.consume_credit(RACE_TID, 'race-refund-diag')     # → 余额 0

refunded: list = []


def try_refund() -> None:
    try:
        billing_svc.refund_credit(RACE_TID, 'race-refund-diag')
        refunded.append(True)
    except Exception as e:                        # noqa: BLE001
        refunded.append(f'{type(e).__name__}: {e}')


threads = [threading.Thread(target=try_refund) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)
check('线程全部结束', not any(t.is_alive() for t in threads))
check('★ 4 次并发退款均正常返回', all(x is True for x in refunded), refunded)
acc = billing_svc.get_account(RACE_TID)
check('★ 余额回到 1（只退一次不膨胀）', acc.balance == 1, acc.balance)
with db.get_conn() as conn:
    n_refund = conn.execute(
        "SELECT COUNT(*) FROM credit_ledger WHERE tenant_id = ? "
        "AND reason = 'refund' AND diagnosis_id = 'race-refund-diag'",
        (RACE_TID,)).fetchone()[0]
check('★ 流水恰好 1 条 refund（幂等）', n_refund == 1, n_refund)

print()
print('=' * 78)
print('模块 6f: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)

import shutil                                   # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)

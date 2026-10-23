"""模块 10：超级管理员（免额度限制，docs/12）。

断言计划见 docs/12 §5。核心：
  admin 只免额度这一道闸 —— 不扣余额、留 delta=0 审计痕；
  普通用户行为完全不变。
"""
import os
import sys
import pathlib
import tempfile
import time
import uuid
import datetime as dt

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = pathlib.Path(__file__).resolve().parents[1]
# ★ 用工作区内临时目录：DSH 沙箱下 %TEMP% 无法创建 SQLite 库
TMP = ROOT / '.testtmp' / f'admin_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "admin.db"}'
os.environ['TENANTS_ROOT'] = str(TMP / 'tenants')
os.environ['APP_SECRET_KEY'] = 'test-secret-key-for-jwt-signing-32chars-minimum'
os.environ['SIGNUP_FREE_CREDITS'] = '1'
os.environ['SUPER_ADMIN_EMAILS'] = 'boss@test.com, dev@test.com'

sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient         # noqa: E402

from app.config import reset_settings_cache       # noqa: E402
from app.main import app                          # noqa: E402
from app.storage import db                        # noqa: E402

db.apply_schema()
client = TestClient(app)
fails: list[str] = []


def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)


print('=' * 78)
print('模块 10：超级管理员（免额度限制）')
print('=' * 78)

# ---------------------------------------------------------------------------
print('\n[1] env 配置后注册 → admin')
r = client.post('/api/v1/auth/register', json={
    'email': 'boss@test.com', 'password': 'AdminTest!2026', 'tenant_name': '总管理'})
check('注册 201', r.status_code == 201, r.status_code)
u = r.json()['user']
check('★ role=admin', u['role'] == 'admin', u['role'])
ADMIN_TOKEN = r.json()['token']['access_token']
H_ADMIN = {'Authorization': f'Bearer {ADMIN_TOKEN}'}

# JWT 里的 role
import jwt as pyjwt                                # noqa: E402
from app.config import get_settings                # noqa: E402
payload = pyjwt.decode(ADMIN_TOKEN, get_settings().app_secret_key,
                        algorithms=['HS256'])
check('JWT 携带 role=admin', payload.get('role') == 'admin', payload.get('role'))

# /auth/me
r = client.get('/api/v1/auth/me', headers=H_ADMIN)
check('me 返回 admin', r.json().get('role') == 'admin')

# ---------------------------------------------------------------------------
print('\n[2] 余额为 0 也能发起诊断（require_credits 放行）')

# 把赠送的 1 次先用掉：上传最小订单 → 诊断一次（走 admin 免扣路径）
import io as _io
import csv as _csv

def _csv_bytes(rows, header):
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode('utf-8')

orders = _csv_bytes(
    [['TB1', 'TB1-1', '2026-10-01 10:00:00', '2026-10-01 10:05:00',
      '2026-10-01 18:00:00', '2026-10-04 12:00:00', '交易成功', '无退款',
      'b1', 'ITEM-A', '连衣裙', 'MC-A', 'SKU-1', '红;M', '200.00', '1',
      '200.00', '200.00', '0.00', '0.00', '200.00', '浙江']],
    ['订单编号', '子订单编号', '订单创建时间', '买家付款时间', '发货时间',
     '确认收货时间', '订单状态', '退款状态', '买家会员名', '商品ID', '商品标题',
     '商家编码', 'SKU ID', 'sku属性', '单价', '购买数量', '买家实际支付金额',
     '商品金额', '运费', '优惠金额', '商家实收金额', '收货省份'])

# ★ 采购成本批次：缺少它会被 QC-06b（P1-8）拦截，诊断失败并退款
purchase = _csv_bytes(
    [['B1', 'MC-A', 'ITEM-A', '2026-09-01', '2026-09-10', '100',
      '80.00', '400.00', '0.00', '甲', 'CNY']],
    ['批次号', '商家编码', '商品ID', '采购日期', '到仓日期', '数量',
     '采购单价', '运费', '其他成本', '供应商', '币种'])

r = client.post('/api/v1/sources', headers=H_ADMIN,
                files={'file': ('订单.csv', orders, 'text/csv')})
check('上传订单 201', r.status_code == 201, r.status_code)
sid = r.json()[0]['source_id']
r = client.post('/api/v1/sources', headers=H_ADMIN,
                files={'file': ('采购.csv', purchase, 'text/csv')})
psid = r.json()[0]['source_id']

# 确认映射
for _sid in (sid, psid):
    r = client.get(f'/api/v1/sources/{_sid}/profile', headers=H_ADMIN)
    prof = r.json()
    mapping = {m['target_field']: m['source_column'] for m in prof['mappings']}
    client.post(f'/api/v1/sources/{_sid}/mapping', headers=H_ADMIN,
                json={'mapping': mapping})

# 第一次诊断（还有 1 次赠送额度，但 admin 不扣）
r = client.post('/api/v1/diagnoses', headers=H_ADMIN,
                json={'source_ids': [sid, psid],
                      'time_range': '2026-10-01~2026-10-04'})
check('★ admin 发起诊断 202（不受额度检查拦截）', r.status_code == 202, r.status_code)
DID1 = r.json()['diagnosis_id']
for _ in range(80):
    r = client.get(f'/api/v1/diagnoses/{DID1}', headers=H_ADMIN)
    if r.json().get('status') in ('succeeded', 'failed'):
        break
    time.sleep(0.3)
print(f'  第一次诊断终态: {r.json().get("status")}')

# ---------------------------------------------------------------------------
print('\n[3] 账目纪律：不动余额，留 delta=0 审计痕')
r = client.get('/api/v1/billing/account', headers=H_ADMIN)
acc = r.json()
check('★ balance 不变（赠送 1 次仍在）', acc['balance'] == 1, acc['balance'])
check('★ total_used 不增', acc['total_used'] == 0, acc['total_used'])

r = client.get('/api/v1/billing/ledger', headers=H_ADMIN)
ledger = r.json()
admin_rows = [x for x in ledger if x['reason'] == 'admin_test']
check('★ ledger 有 admin_test 记录', len(admin_rows) >= 1, len(admin_rows))
check('★ admin_test 的 delta=0',
      all(x['delta'] == 0 for x in admin_rows))

# 把余额清零后再诊断一次：admin 依然 202（这才是"不限次"的直接验证）
from app.services import billing as _billing      # noqa: E402
with db.transaction() as conn:
    conn.execute('UPDATE credit_accounts SET balance = 0 WHERE tenant_id = ?',
                 (u['tenant_id'],))
r = client.post('/api/v1/diagnoses', headers=H_ADMIN,
                json={'source_ids': [sid, psid]})
check('★ 余额 0 时 admin 仍 202', r.status_code == 202, r.status_code)
DID2 = r.json()['diagnosis_id']
for _ in range(80):
    r = client.get(f'/api/v1/diagnoses/{DID2}', headers=H_ADMIN)
    if r.json().get('status') in ('succeeded', 'failed'):
        break
    time.sleep(0.3)
print(f'  第二次诊断终态: {r.json().get("status")}')
r = client.get('/api/v1/billing/account', headers=H_ADMIN)
check('余额仍为 0（未扣成负数）', r.json()['balance'] == 0, r.json()['balance'])

# 审计日志
with db.get_conn() as conn:
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='admin_free_diagnosis'"
    ).fetchone()[0]
check('audit_logs 记录 admin_free_diagnosis', n >= 1, n)

# ---------------------------------------------------------------------------
print('\n[4] 普通用户行为不变（回归）')
r = client.post('/api/v1/auth/register', json={
    'email': 'normal@test.com', 'password': 'NormalTest!2026', 'tenant_name': '普通店'})
nu = r.json()['user']
check('普通用户 role=owner', nu['role'] == 'owner', nu['role'])
NORMAL_TOKEN = r.json()['token']['access_token']
H_N = {'Authorization': f'Bearer {NORMAL_TOKEN}'}

# 用光赠送额度（先诊断一次）
r = client.post('/api/v1/sources', headers=H_N,
                files={'file': ('订单.csv', orders, 'text/csv')})
nsid = r.json()[0]['source_id']
r = client.post('/api/v1/sources', headers=H_N,
                files={'file': ('采购.csv', purchase, 'text/csv')})
npsid = r.json()[0]['source_id']
for _sid in (nsid, npsid):
    r = client.get(f'/api/v1/sources/{_sid}/profile', headers=H_N)
    nm = {m['target_field']: m['source_column'] for m in r.json()['mappings']}
    client.post(f'/api/v1/sources/{_sid}/mapping', headers=H_N,
                json={'mapping': nm})
r = client.post('/api/v1/diagnoses', headers=H_N,
                json={'source_ids': [nsid, npsid]})
check('普通用户有额度时 202', r.status_code == 202, r.status_code)
NDID = r.json()['diagnosis_id']
for _ in range(80):
    r = client.get(f'/api/v1/diagnoses/{NDID}', headers=H_N)
    if r.json().get('status') in ('succeeded', 'failed'):
        break
    time.sleep(0.3)

r = client.post('/api/v1/diagnoses', headers=H_N,
                json={'source_ids': [nsid, npsid]})
check('★ 普通用户额度用完后 402（行为不变）', r.status_code == 402, r.status_code)
check('错误码 INSUFFICIENT_CREDITS',
      r.json().get('code') == 'INSUFFICIENT_CREDITS')

# ---------------------------------------------------------------------------
print('\n[4.5] 补偿退款管理端（docs/16 P1-3）')
now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
pid = uuid.uuid4().hex
with db.transaction() as conn:
    conn.execute(
        'INSERT INTO refund_pending (id,tenant_id,diagnosis_id,reason,attempts,'
        'last_error,status,note,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (pid, nu['tenant_id'], NDID, '测试补偿', 0, '', 'pending', '',
         now_iso, now_iso))
r = client.get('/api/v1/admin/refunds', headers=H_ADMIN)
check('管理员可查看补偿列表', r.status_code == 200, r.status_code)
check('列表包含补偿记录',
      any(x['id'] == pid for x in r.json().get('items', [])), r.text[:100])
r = client.get('/api/v1/admin/refunds', headers=H_N)
check('★ 非管理员（租户 owner）访问补偿列表 403',
      r.status_code == 403, r.status_code)

bal_before_admin = client.get('/api/v1/billing/account', headers=H_N).json()['balance']
r = client.post(f'/api/v1/admin/refunds/{pid}/retry', headers=H_ADMIN)
check('管理员重试退款 200', r.status_code == 200, r.text[:100])
check('补偿记录已 resolved', r.json().get('status') == 'resolved', r.json())
bal_after_admin = client.get('/api/v1/billing/account', headers=H_N).json()['balance']
check('★ 重试后额度已退还', bal_after_admin == bal_before_admin + 1,
      f'{bal_before_admin} -> {bal_after_admin}')

# 无法退款（无 consume）的记录 → 核销
pid2 = uuid.uuid4().hex
with db.transaction() as conn:
    conn.execute(
        'INSERT INTO refund_pending (id,tenant_id,diagnosis_id,reason,attempts,'
        'last_error,status,note,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (pid2, nu['tenant_id'], 'diag-manual-x', '人工核销测试', 2, '',
         'manual_review', '', now_iso, now_iso))
r = client.post(f'/api/v1/admin/refunds/{pid2}/writeoff',
                headers=H_ADMIN, params={'note': '测试核销'})
check('管理员核销 200', r.status_code == 200, r.text[:100])
check('核销后 resolved', r.json().get('status') == 'resolved', r.json())
with db.get_conn() as conn:
    st = conn.execute('SELECT status FROM refund_pending WHERE id=?',
                      (pid2,)).fetchone()[0]
check('核销状态已落库', st == 'resolved', st)

# ---------------------------------------------------------------------------
print('\n[5] 顺序容错：先注册（owner）→ 后配置 env → 登录提升')
# dev@test.com 在 env 里，但用"未配置"的 settings 先注册
os.environ['SUPER_ADMIN_EMAILS'] = ''              # 模拟当时未配置
reset_settings_cache()
r = client.post('/api/v1/auth/register', json={
    'email': 'dev@test.com', 'password': 'DevTest!2026x', 'tenant_name': '后提权'})
check('未配置时注册为 owner', r.json()['user']['role'] == 'owner',
      r.json()['user']['role'])

os.environ['SUPER_ADMIN_EMAILS'] = 'dev@test.com'  # 后来配置了
reset_settings_cache()
r = client.post('/api/v1/auth/login', json={
    'email': 'dev@test.com', 'password': 'DevTest!2026x'})
check('★ 再登录自动提升为 admin', r.json()['user']['role'] == 'admin',
      r.json()['user']['role'])

# ---------------------------------------------------------------------------
print('\n[6] env 未配置 = 功能整体关闭')
os.environ['SUPER_ADMIN_EMAILS'] = ''
reset_settings_cache()
r = client.post('/api/v1/auth/register', json={
    'email': 'noadmin@test.com', 'password': 'NoAdmin!2026x', 'tenant_name': '无管理'})
check('默认注册均为 owner（功能关闭）', r.json()['user']['role'] == 'owner')

print()
print('=' * 78)
print('模块 10: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)
sys.exit(1 if fails else 0)

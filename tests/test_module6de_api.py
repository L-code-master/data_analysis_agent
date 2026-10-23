"""模块 6d/6e 验证：FastAPI 端到端 API 测试。

覆盖：健康检查 → 注册 → 登录 → 上传 → 画像 → 映射确认
      → 购买 → 发起诊断 → SSE 进度 → 结果 → 报告 → 越权防护
"""
import sys, pathlib, os, io, csv, json, time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / '.testtmp' / f'api_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "api.db"}'
os.environ['APP_SECRET_KEY'] = 'test-secret-key-32-chars-minimum-ok!'
os.environ['SIGNUP_FREE_CREDITS'] = '0'
os.environ['TENANTS_ROOT'] = str(TMP / 'tenants')
os.environ['APP_ENV'] = 'development'
os.environ['APP_DEBUG'] = 'true'
os.environ['SUPER_ADMIN_EMAILS'] = 'platform@admin.com'
os.environ['MAX_UPLOAD_MB'] = '1'          # 便于验证上传大小前置限制（P1-14）

from app.config import reset_settings_cache
reset_settings_cache()

from fastapi.testclient import TestClient
from app.main import create_app

app = create_app()
# ★ 必须用上下文管理器：否则 lifespan（建表 + 启动自检）不会执行
client = TestClient(app)
client.__enter__()

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)

print('=' * 78)
print('模块 6d/6e 验证：FastAPI 端到端')
print('=' * 78)

# ===========================================================================
print('\n[1] 健康检查（无需登录）')
r = client.get('/api/v1/health')
print(f'  HTTP {r.status_code}')
h = r.json()
print(f'  status={h["status"]} version={h["version"]}')
print(f'  db tables={h["database"].get("tables")} journal={h["database"].get("journal_mode")}')
print(f'  sandbox={h["sandbox"]}')
check('健康检查 200', r.status_code == 200)
check('状态 ok', h['status'] == 'ok')
check('★ 公开 health 不泄露 DB/沙箱详情（P2-13）',
      not h.get('database') and not h.get('sandbox'), h)

# ===========================================================================
print('\n[2] 未认证访问被拒绝')
for path in ['/api/v1/auth/me', '/api/v1/sources', '/api/v1/billing/account']:
    r = client.get(path)
    check(f'{path} 未登录返回 401', r.status_code == 401, r.status_code)

r = client.get('/api/v1/auth/me', headers={'Authorization': 'Bearer garbage'})
check('非法 token 返回 401', r.status_code == 401, r.status_code)
r = client.get('/api/v1/auth/me', headers={'Authorization': 'NotBearer xxx'})
check('错误认证方案返回 401', r.status_code == 401, r.status_code)

# ===========================================================================
print('\n[3] 注册')
r = client.post('/api/v1/auth/register', json={
    'email': 'boss@shop.com', 'password': 'MyStr0ng!Pass',
    'tenant_name': '测试旗舰店'})
print(f'  HTTP {r.status_code}')
check('注册返回 201', r.status_code == 201, r.status_code)
data = r.json()
TOKEN = data['token']['access_token']
REFRESH = data['token']['refresh_token']
TENANT = data['user']['tenant_id']
print(f'  tenant={TENANT[:12]}... role={data["user"]["role"]}')
check('返回 access token', bool(TOKEN))
check('返回 refresh token', bool(REFRESH))
check('角色 owner', data['user']['role'] == 'owner')

H = {'Authorization': f'Bearer {TOKEN}'}

# 重复注册
r = client.post('/api/v1/auth/register', json={
    'email': 'boss@shop.com', 'password': 'AnotherPass1!'})
check('重复邮箱返回 409', r.status_code == 409, r.status_code)
check('错误码 EMAIL_EXISTS', r.json().get('code') == 'EMAIL_EXISTS')

# 弱密码
r = client.post('/api/v1/auth/register', json={
    'email': 'weak@shop.com', 'password': '123'})
check('弱密码返回 400', r.status_code == 400, r.status_code)

# ===========================================================================
print('\n[4] 登录与当前用户')
r = client.post('/api/v1/auth/login', json={
    'email': 'boss@shop.com', 'password': 'MyStr0ng!Pass'})
check('登录成功', r.status_code == 200, r.status_code)
_set_cookie = r.headers.get('set-cookie', '').lower()
check('★ refresh 走 httpOnly cookie（P2-16）',
      'daa_refresh=' in _set_cookie and 'httponly' in _set_cookie,
      r.headers.get('set-cookie', '')[:120])
check('cookie 限定了路径与 SameSite',
      'path=/api/v1/auth' in _set_cookie and 'samesite=lax' in _set_cookie)

r = client.post('/api/v1/auth/login', json={
    'email': 'boss@shop.com', 'password': 'WrongPass1!'})
check('错误密码返回 401', r.status_code == 401, r.status_code)

r = client.get('/api/v1/auth/me', headers=H)
check('获取当前用户', r.status_code == 200 and r.json()['email'] == 'boss@shop.com')

# 刷新令牌
r = client.post('/api/v1/auth/refresh', json={'refresh_token': REFRESH})
check('刷新令牌成功', r.status_code == 200, r.status_code)
NEW_REFRESH = r.json()['refresh_token']
r = client.post('/api/v1/auth/refresh', json={'refresh_token': REFRESH})
check('旧 refresh 失效', r.status_code == 401, r.status_code)
check('★ 错误码 TOKEN_REVOKED', r.json().get('code') == 'TOKEN_REVOKED',
      r.text[:80])
# ★ 重放检测：旧 token 被重放后，同族的新 token 也应被整族吊销
r = client.post('/api/v1/auth/refresh', json={'refresh_token': NEW_REFRESH})
check('★ 同族 refresh 被连带吊销', r.status_code == 401, r.status_code)
REFRESH = NEW_REFRESH

# ===========================================================================
print('\n[5] 套餐清单（公开）')
r = client.get('/api/v1/billing/plans')
check('套餐清单 200', r.status_code == 200)
plans = r.json()
for p in plans:
    print(f'  {p["sku"]:8} ¥{p["price_yuan"]:>6.0f}  {p["quantity"]}次  {p["badge"]}')
check('3 个套餐', len(plans) == 3)

# ===========================================================================
print('\n[6] 初始额度')
r = client.get('/api/v1/billing/account', headers=H)
check('额度接口 200', r.status_code == 200)
acc = r.json()
print(f'  balance={acc["balance"]} trial_available={acc["trial_available"]}')
check('初始余额 0', acc['balance'] == 0)
check('体验价可用', acc['trial_available'] is True)

# ===========================================================================
print('\n[7] 上传与识别')

def orders_csv() -> bytes:
    headers = ['订单编号','子订单编号','订单创建时间','买家付款时间','发货时间',
               '确认收货时间','订单状态','退款状态','买家会员名','商品ID',
               '商品标题','商家编码','SKU ID','sku属性','单价','购买数量',
               '买家实际支付金额','商品金额','运费','优惠金额','商家实收金额',
               '收货省份']
    rows = [
        ['TB001','TB001-1','2026-10-01 10:00:00','2026-10-01 10:05:00',
         '2026-10-01 18:00:00','2026-10-04 12:00:00','交易成功','无退款','b1',
         'ITEM-A','秋季连衣裙','MC-A','SKU-1','红;M','200.00','30',
         '6,000.00','5,900.00','100.00','0.00','6,000.00','浙江'],
        ['TB002','TB002-1','2026-10-02 09:00:00','2026-10-02 09:03:00',
         '2026-10-02 17:00:00','','交易成功','无退款','b2',
         'ITEM-B','半身裙','MC-B','SKU-2','蓝;L','150.00','24',
         '3,600.00','3,540.00','60.00','0.00','3,600.00','广东'],
    ]
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(['订单导出报表']); w.writerow(['导出时间：2026-10-19'])
    w.writerow(headers); w.writerows(rows)
    return buf.getvalue().encode('gbk')

def traffic_csv() -> bytes:
    headers = ['日期','访客数','浏览量','支付买家数','支付件数','支付金额',
               '下单买家数','下单件数','下单金额','跳失率','平均停留时长',
               '加购人数','收藏人数']
    rows = [
        ['2026-10-01','1,000','2,500','20','30','6,000.00','25','32','6,400.00','45.0%','65','120','60'],
        ['2026-10-02','900','2,200','15','24','3,600.00','18','26','3,900.00','46.0%','62','100','50'],
    ]
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(headers); w.writerows(rows)
    return b'\xef\xbb\xbf' + buf.getvalue().encode('utf-8')

def account_csv() -> bytes:
    headers = ['流水号','记账日期','交易时间','订单编号','业务类型',
               '收支类型','金额','余额','备注','对方']
    rows = [
        ['F001','2026-10-01','','TB001','天猫佣金','支出','-300.00','9,700.00','','淘宝'],
        ['F002','2026-10-01','','TB001','技术服务费','支出','-120.00','9,580.00','','淘宝'],
        ['F003','2026-10-02','','TB002','天猫佣金','支出','-180.00','9,400.00','','淘宝'],
        ['F004','2026-10-02','','TB002','技术服务费','支出','-72.00','9,328.00','','淘宝'],
    ]
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(headers); w.writerows(rows)
    return buf.getvalue().encode('utf-8')

def purchase_csv() -> bytes:
    headers = ['批次号','商家编码','商品ID','采购日期','到仓日期','数量',
               '采购单价','运费','其他成本','供应商','币种']
    rows = [
        ['B1','MC-A','ITEM-A','2026-09-01','2026-09-10','100','80.00','400.00','0.00','甲','CNY'],
        ['B2','MC-B','ITEM-B','2026-09-05','2026-09-15','100','60.00','300.00','0.00','乙','CNY'],
    ]
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(headers); w.writerows(rows)
    return buf.getvalue().encode('utf-8')

uploads = {}
for name, content, ctype in [
        ('订单导出.csv', orders_csv(), 'text/csv'),
        ('流量.csv', traffic_csv(), 'text/csv'),
        ('账房.csv', account_csv(), 'text/csv'),
        ('采购成本.csv', purchase_csv(), 'text/csv')]:
    r = client.post('/api/v1/sources', headers=H,
                    files={'file': (name, content, ctype)})
    if r.status_code != 201:
        check(f'上传 {name}', False, f'HTTP {r.status_code}: {r.text[:100]}')
        continue
    d = r.json()[0]          # ★ 多表返回列表；单文件取第一项
    uploads[name] = d
    print(f'  {name:16} -> {d["source_type"]:16} {d["row_count"]} 行  '
          f'id={d["source_id"][:8]}')
    check(f'上传 {name}', True)

check('上传 4 个文件', len(uploads) == 4, len(uploads))
check('订单识别正确',
      uploads.get('订单导出.csv', {}).get('source_type') == 'trade_orders')

# 非法上传
r = client.post('/api/v1/sources', headers=H,
                files={'file': ('evil.exe', b'MZ\x90', 'application/octet-stream')})
check('拒绝 .exe 上传', r.status_code == 400, r.status_code)

r = client.post('/api/v1/sources', headers=H,
                files={'file': ('noise.csv', b'name,age\nTom,30\n', 'text/csv')})
check('拒绝无法识别的文件', r.status_code == 422, r.status_code)

# ★ 大文件前置拒绝（docs/16 P1-14；本测试 MAX_UPLOAD_MB=1）
_big = b'date,visitors\n' + b'2026-10-01,1\n' * 120_000   # ≈ 1.3 MB
r = client.post('/api/v1/sources', headers=H,
                files={'file': ('big.csv', _big, 'text/csv')})
check('★ 超大文件返回 413', r.status_code == 413, r.status_code)
check('错误码 FILE_TOO_LARGE', r.json().get('code') == 'FILE_TOO_LARGE',
      r.text[:100])

# ===========================================================================
print('\n[8] 数据画像')
SID = uploads['订单导出.csv']['source_id']
r = client.get(f'/api/v1/sources/{SID}/profile', headers=H)
check('画像接口 200', r.status_code == 200)
prof = r.json()
print(f'  {prof["display_name"]}  {prof["row_count"]} 行  {prof["column_count"]} 列')
print(f'  映射 {len(prof["mappings"])} 个  需确认={prof["needs_confirmation"]}')
check('返回列结构', prof['column_count'] == 22, prof['column_count'])
check('返回映射建议', len(prof['mappings']) >= 20)
# ★ 画像不含数据行
body = json.dumps(prof, ensure_ascii=False)
check('★ 画像不含数据行', 'TB001' not in body and '6,000.00' not in body)

# ===========================================================================
print('\n[9] 确认映射')
mapping = {m['target_field']: m['source_column'] for m in prof['mappings']}
r = client.post(f'/api/v1/sources/{SID}/mapping', headers=H,
                json={'mapping': mapping})
check('确认映射 200', r.status_code == 200, r.text[:120])
print(f'  {r.json()["message"]}')

r = client.post(f'/api/v1/sources/{SID}/mapping', headers=H,
                json={'mapping': {'order_id': '订单编号'}})
check('缺必填映射返回 400', r.status_code == 400, r.status_code)

# 其余数据源也确认
for name, d in uploads.items():
    if name == '订单导出.csv':
        continue
    p = client.get(f'/api/v1/sources/{d["source_id"]}/profile', headers=H).json()
    m = {x['target_field']: x['source_column'] for x in p['mappings']}
    client.post(f'/api/v1/sources/{d["source_id"]}/mapping', headers=H,
                json={'mapping': m})
print('  其余 3 个数据源映射已确认')

# ===========================================================================
print('\n[10] 额度不足时拒绝诊断')
ALL_IDS = [d['source_id'] for d in uploads.values()]
r = client.post('/api/v1/diagnoses', headers=H,
                json={'source_ids': ALL_IDS, 'time_range': '2026-10-01~2026-10-02'})
print(f'  HTTP {r.status_code}  {r.json()}')
check('额度不足返回 402', r.status_code == 402, r.status_code)
check('错误码 INSUFFICIENT_CREDITS',
      r.json().get('code') == 'INSUFFICIENT_CREDITS')

# ===========================================================================
print('\n[11] 购买 → 人工核销（仅平台管理员）')
r = client.post('/api/v1/billing/orders', headers=H, json={'sku': 'pack5'})
check('创建订单 201', r.status_code == 201, r.status_code)
order = r.json()
print(f'  订单 {order["order_no"]}  ¥{order["amount_yuan"]:.0f}  {order["quantity"]}次')
check('订单金额 ¥799', order['amount_cents'] == 79900)
check('订单 5 次', order['quantity'] == 5)

# ★ P0-1 负向测试：租户 owner 自助核销必须被拒（否则 = 免费无限拿额度）
r = client.post(f'/api/v1/billing/orders/{order["order_no"]}/confirm', headers=H)
check('★ owner 自助核销被拒 403', r.status_code == 403, r.status_code)
check('★ 错误码 FORBIDDEN', r.json().get('code') == 'FORBIDDEN')
acc = client.get('/api/v1/billing/account', headers=H).json()
check('★ 核销被拒后余额仍为 0', acc['balance'] == 0, acc['balance'])

# 平台管理员（SUPER_ADMIN_EMAILS 白名单）核销
r = client.post('/api/v1/auth/register', json={
    'email': 'platform@admin.com', 'password': 'PlatformAdmin!2026',
    'tenant_name': '平台管理'})
check('注册管理员 201', r.status_code == 201, r.status_code)
check('★ role=admin', r.json()['user']['role'] == 'admin',
      r.json()['user'].get('role'))
ADMIN_TOKEN = r.json()['token']['access_token']
H_ADMIN = {'Authorization': f'Bearer {ADMIN_TOKEN}'}

r = client.get('/api/v1/admin/health', headers=H_ADMIN)
check('管理员可看运维详情', r.status_code == 200, r.status_code)
if r.status_code == 200:
    hd = r.json()
    check('详情含数据库与沙箱',
          hd.get('database', {}).get('tables', 0) >= 15
          and hd.get('sandbox', {}).get('network_isolated') is True, hd)

# ★ P2-19：付款凭证通道 —— owner 上传、归属方/管理员可下载
proof_png = b'\x89PNG\r\n\x1a\n' + b'0' * 64      # 假 PNG 内容（只测流转）
r = client.post(f'/api/v1/billing/orders/{order["order_no"]}/proof',
                headers=H, files={'file': ('转账截图.png', proof_png, 'image/png')})
check('★ 上传凭证 200', r.status_code == 200, r.text[:100])
proof_ref = r.json().get('pay_proof_ref', '')
check('★ 返回凭证引用（png）', proof_ref.endswith('.png'), proof_ref)

r = client.get(f'/api/v1/billing/orders/{order["order_no"]}/proof', headers=H)
check('★ owner 下载凭证 200', r.status_code == 200, r.status_code)
check('★ Content-Type image/png',
      r.headers.get('content-type', '').startswith('image/png'),
      r.headers.get('content-type', '')[:30])
r = client.get(f'/api/v1/billing/orders/{order["order_no"]}/proof',
               headers=H_ADMIN)
check('★ 管理员可下载凭证（核销审计）', r.status_code == 200, r.status_code)

# 类型/大小/越权负向
r = client.post(f'/api/v1/billing/orders/{order["order_no"]}/proof',
                headers=H, files={'file': ('x.exe', b'MZ', 'application/x-msdownload')})
check('★ 非法类型被拒 400', r.status_code == 400
      and r.json().get('code') == 'BAD_PROOF_TYPE', r.status_code)
r = client.post(f'/api/v1/billing/orders/{order["order_no"]}/proof',
                headers=H,
                files={'file': ('big.png', b'x' * (5 * 1024 * 1024 + 2), 'image/png')})
check('★ 超 5MB 被拒 413', r.status_code == 413
      and r.json().get('code') == 'BAD_PROOF_SIZE', r.status_code)

r = client.post(f'/api/v1/billing/orders/{order["order_no"]}/confirm',
                headers=H_ADMIN)
check('管理员核销 200', r.status_code == 200, r.text[:120])
acc = r.json()
print(f'  核销后余额: {acc["balance"]}')
check('余额 5', acc['balance'] == 5, acc['balance'])

# ===========================================================================
print('\n[12] 发起诊断（异步）')
r = client.post('/api/v1/diagnoses', headers=H,
                json={'source_ids': ALL_IDS, 'time_range': '2026-10-01~2026-10-02'})
print(f'  HTTP {r.status_code}')
check('发起诊断返回 202', r.status_code == 202, r.status_code)
d0 = r.json()
DID = d0['diagnosis_id']
print(f'  diagnosis_id={DID[:12]}... status={d0["status"]}')
check('返回 diagnosis_id', bool(DID))

# ---- 消费 SSE 进度 ----
print('\n[13] SSE 进度流')
events = []
with client.stream('GET', f'/api/v1/diagnoses/{DID}/events', headers=H) as resp:
    check('SSE 连接 200', resp.status_code == 200, resp.status_code)
    check('Content-Type 为 event-stream',
          'text/event-stream' in resp.headers.get('content-type', ''),
          resp.headers.get('content-type'))
    check('★ 设置 X-Accel-Buffering: no（防 Nginx 缓冲）',
          resp.headers.get('x-accel-buffering') == 'no',
          resp.headers.get('x-accel-buffering'))
    for line in resp.iter_lines():
        if line.startswith('data: '):
            try:
                ev = json.loads(line[6:])
                events.append(ev)
            except json.JSONDecodeError:
                pass
        if events and events[-1].get('type') in ('done', 'error'):
            break

print(f'  收到 {len(events)} 个事件')
for e in events[:6]:
    print(f'    {e.get("type"):9} {e.get("phase","")!s:12} {str(e.get("message",""))[:36]}')
check('收到 ready 事件', any(e.get('type') == 'ready' for e in events))
check('收到 progress 事件', any(e.get('type') == 'progress' for e in events))
check('收到 done 事件', any(e.get('type') == 'done' for e in events))
check('★ 事件含单调 seq（去重依据）',
      all(e.get('seq', 0) > 0 for e in events if e.get('type') == 'progress'))

# ★ P2-24：两个订阅者各自拿到同一事件（旧实现互相抢队列）
from app.api.routes import diagnoses as _dr              # noqa: E402
_dr._drop_queue('sse-test')
_q1 = _dr._subscribe('sse-test')
_q2 = _dr._subscribe('sse-test')
_dr._emit('sse-test', {'type': 'progress', 'phase': 'x', 'message': 'm'})
_e1, _e2 = _q1.get_nowait(), _q2.get_nowait()
check('★ 双订阅者都收到事件',
      _e1.get('seq') == _e2.get('seq') == 1,
      (_e1.get('seq'), _e2.get('seq')))
_dr._drop_queue('sse-test')
check('订阅/历史可清理', 'sse-test' not in _dr._progress
      and 'sse-test' not in _dr._history)
# TTL 兜底清理
_dr._subscribe('ttl-test')
with _dr._lock:
    _dr._last_activity['ttl-test'] = (
        time.monotonic() - _dr._PROGRESS_TTL_SECONDS - 1)
_dr._sweep_progress()
check('★ TTL 清理无活动进度', 'ttl-test' not in _dr._progress)

# ===========================================================================
print('\n[14] 诊断结果')
# 等待任务落库
for _ in range(50):
    r = client.get(f'/api/v1/diagnoses/{DID}', headers=H)
    if r.status_code == 200 and r.json()['status'] in ('succeeded', 'failed'):
        break
    time.sleep(0.1)

check('查询结果 200', r.status_code == 200)
res = r.json()
print(f'  status={res["status"]}  quality_passed={res["quality_passed"]}')
print(f'  指标 {len(res["metrics"])} 个:')
for m in res['metrics']:
    print(f'    {m["metric_id"]:5} {m["metric_name"]:16} = {m["value"]}')
check('诊断成功', res['status'] == 'succeeded', res.get('error'))
check('质量门通过', res['quality_passed'])
check('有指标结果', len(res['metrics']) >= 5, len(res['metrics']))
check('有口径脚注', bool(res['footnote']))

mvals = {m['metric_id']: m['value'] for m in res['metrics']}
check('M01 访客数 = 1900', mvals.get('M01') == 1900, mvals.get('M01'))
check('M06 支付金额 = 9600', mvals.get('M06') == 9600.0, mvals.get('M06'))
check('M13 毛利率有值', mvals.get('M13') is not None, mvals.get('M13'))

# ===========================================================================
print('\n[15] 报告')
r = client.get(f'/api/v1/diagnoses/{DID}/report', headers=H)
check('报告 200', r.status_code == 200, r.status_code)
check('Content-Type 为 HTML',
      'text/html' in r.headers.get('content-type', ''), r.headers.get('content-type'))
check('★ 报告带严格 CSP（P1-17）',
      "default-src 'none'" in r.headers.get('content-security-policy', ''),
      r.headers.get('content-security-policy', '')[:80])
check('报告 nosniff',
      r.headers.get('x-content-type-options') == 'nosniff')
html = r.text
check('报告含 HTML 结构', '<!DOCTYPE html>' in html)
check('报告含指标', '访客数' in html or '净销售额' in html)
check('报告含口径声明', '口径说明' in html)
print(f'  HTML {len(html)} 字节')

r = client.get(f'/api/v1/diagnoses/{DID}/report.json', headers=H)
check('报告 JSON 200', r.status_code == 200)

# ===========================================================================
print('\n[16] 额度与流水')
r = client.get('/api/v1/billing/account', headers=H)
acc = r.json()
print(f'  balance={acc["balance"]}  used={acc["total_used"]}')
check('诊断扣了 1 次（5→4）', acc['balance'] == 4, acc['balance'])

r = client.get('/api/v1/billing/ledger', headers=H)
ledger = r.json()
print(f'  流水 {len(ledger)} 条:')
for e in ledger[:5]:
    sign = '+' if e['delta'] > 0 else ''
    print(f'    {sign}{e["delta"]:>2} -> {e["balance_after"]:>2}  {e["reason"]}')
check('流水含 purchase', any(e['reason'] == 'purchase' for e in ledger))
check('流水含 consume', any(e['reason'] == 'consume' for e in ledger))

r = client.get('/api/v1/billing/usage', headers=H)
check('用量接口 200', r.status_code == 200)

# ===========================================================================
print('\n[17] ★ 越权防护（跨租户）')
r = client.post('/api/v1/auth/register', json={
    'email': 'other@shop.com', 'password': 'OtherStr0ng!1',
    'tenant_name': '另一家店'})
check('创建第二租户', r.status_code == 201)
TOKEN2 = r.json()['token']['access_token']
H2 = {'Authorization': f'Bearer {TOKEN2}'}

# B 想读 A 的数据源
r = client.get(f'/api/v1/sources/{SID}/profile', headers=H2)
check('★ 跨租户读画像被拒', r.status_code == 404, r.status_code)

# B 想读 A 的诊断
r = client.get(f'/api/v1/diagnoses/{DID}', headers=H2)
check('★ 跨租户读诊断被拒', r.status_code == 404, r.status_code)

# B 想读 A 的报告
r = client.get(f'/api/v1/diagnoses/{DID}/report', headers=H2)
check('★ 跨租户读报告被拒', r.status_code == 404, r.status_code)

# B 想订阅 A 的 SSE
r = client.get(f'/api/v1/diagnoses/{DID}/events', headers=H2)
check('★ 跨租户订阅 SSE 被拒', r.status_code == 404, r.status_code)

# B 想看运维详情（平台管理员专属）
r = client.get('/api/v1/admin/health', headers=H2)
check('★ 非管理员看运维详情 403', r.status_code == 403, r.status_code)

# B 想查 A 的订单
r = client.get(f'/api/v1/billing/orders/{order["order_no"]}', headers=H2)
check('★ 跨租户查订单被拒', r.status_code == 404, r.status_code)

# B 想确认 A 的订单（白嫖额度）——非管理员一律 403
r = client.post(f'/api/v1/billing/orders/{order["order_no"]}/confirm', headers=H2)
check('★ 非管理员确认收款被拒', r.status_code == 403, r.status_code)

# B 用 A 的数据源发起诊断
r = client.post('/api/v1/diagnoses', headers=H2,
                json={'source_ids': [SID], 'time_range': ''})
check('★ 跨租户用他人数据源被拒', r.status_code == 402 or r.status_code == 404,
      r.status_code)

# 独立额度
r = client.get('/api/v1/billing/account', headers=H2)
check('★ 两租户额度独立', r.json()['balance'] == 0, r.json()['balance'])

# ===========================================================================
print('\n[17.5] cookie 刷新（不带 body）')
r = client.post('/api/v1/auth/login', json={
    'email': 'boss@shop.com', 'password': 'MyStr0ng!Pass'})
check('重新登录 200', r.status_code == 200, r.status_code)
r = client.post('/api/v1/auth/refresh')          # 无 body，仅 cookie
check('★ 仅凭 cookie 可刷新', r.status_code == 200, r.status_code)
check('返回新 access token', bool(r.json().get('access_token')))

# ===========================================================================
print('\n[18] 登出')
r = client.post('/api/v1/auth/logout', headers=H)
check('登出 200', r.status_code == 200)
check('★ 登出清除 refresh cookie', 'daa_refresh' not in client.cookies,
      dict(client.cookies))
r = client.post('/api/v1/auth/refresh', json={'refresh_token': REFRESH})
check('登出后 refresh 失效', r.status_code == 401, r.status_code)
# ★ P1-18：登出后旧 access token 也必须失效（token_valid_after 水位）
r = client.get('/api/v1/auth/me', headers=H)
check('★ 登出后旧 access token 失效 401', r.status_code == 401,
      r.status_code)

print()
print('=' * 78)
print('模块 6d/6e: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)

import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
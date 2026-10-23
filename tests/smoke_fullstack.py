"""全栈冒烟测试：通过前端代理走一遍完整用户流程。

模拟浏览器行为：注册 → 购买 → 确认收款 → 上传 → 画像 → 映射
→ 诊断(含 SSE) → 报告。全部经由 http://127.0.0.1:5173/api/v1（Vite 代理）。
"""
import io, csv, json, time, sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = 'http://127.0.0.1:5173/api/v1'   # ★ 走前端代理，验证代理链
fails = []


def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)


def req(method, path, token=None, json_body=None, raw=None, ctype=None):
    url = BASE + path
    data = None
    headers = {}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers['Content-Type'] = 'application/json'
    elif raw is not None:
        data = raw
        headers['Content-Type'] = ctype or 'application/octet-stream'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            return resp.status, (json.loads(body) if body.strip().startswith(('{', '[')) else body)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


print('=' * 74)
print('全栈冒烟测试（经 http://127.0.0.1:5173 代理）')
print('=' * 74)

# ---------- 1. 健康检查 ----------
print('\n[1] 健康检查')
st, h = req('GET', '/health')
print(f'  HTTP {st}  status={h.get("status")}  tables={h.get("database",{}).get("tables")}')
check('健康检查通过', st == 200 and h.get('status') == 'ok')

# ---------- 2. 登录/注册固定冒烟租户 ----------
# ★ P2-20：原每次冒烟用时间戳邮箱注册新租户（+1 租户/次，目录只增不减）。
#   改为固定邮箱：首次注册，之后登录同一租户；断言改为相对余额。
print('\n[2] 冒烟租户就绪（固定，重复复用）')
SMOKE_EMAIL = 'smoke-main@shop.com'
st, r = req('POST', '/auth/register', json_body={
    'email': SMOKE_EMAIL, 'password': 'SmokeTest!2026', 'tenant_name': '冒烟测试店'})
if st == 409:                                     # 之前冒烟已注册 → 登录
    st, r = req('POST', '/auth/login', json_body={
        'email': SMOKE_EMAIL, 'password': 'SmokeTest!2026'})
check('冒烟租户就绪（注册或登录）', st in (200, 201), f'HTTP {st}')
TOKEN = r['token']['access_token']
TENANT_ID = r['user']['tenant_id']
print(f'  租户 {TENANT_ID[:12]}…')

# 基准余额（相对断言用）
st, acc0 = req('GET', '/billing/account', TOKEN)
BALANCE0 = acc0['balance']
print(f'  当前余额 {BALANCE0}')

# ---------- 3. 购买 ----------
print('\n[3] 购买 5 次包（人工核销需平台管理员）')
st, o = req('POST', '/billing/orders', TOKEN, json_body={'sku': 'pack5'})
check('创建订单', st == 201, f'HTTP {st}')
print(f'  订单 {o["order_no"]}  ¥{o["amount_yuan"]:.0f}  {o["quantity"]}次')

# ★ P0-1：owner 自助核销必须被拒
st, _ = req('POST', f'/billing/orders/{o["order_no"]}/confirm', TOKEN)
check('★ owner 自助核销被拒 403', st == 403, f'HTTP {st}')

# 平台管理员核销（SUPER_ADMIN_EMAILS 白名单邮箱，见 .env；
# 用冒烟专用管理员：首次注册，后续冒烟用同一密码登录）
ADMIN_EMAIL, ADMIN_PASS = 'smoke-admin@shop.com', 'SmokeAdmin!2026'
st, r = req('POST', '/auth/register', json_body={
    'email': ADMIN_EMAIL, 'password': ADMIN_PASS, 'tenant_name': '平台管理'})
if st == 409:                                     # 之前冒烟已注册 → 登录
    st, r = req('POST', '/auth/login', json_body={
        'email': ADMIN_EMAIL, 'password': ADMIN_PASS})
check('管理员就绪（注册或登录）', st in (200, 201) and
      r.get('user', {}).get('role') == 'admin', f'HTTP {st} role={r.get("user", {}).get("role") if isinstance(r, dict) else "?"}')
ADMIN_TOKEN = r['token']['access_token']

st, acc = req('POST', f'/billing/orders/{o["order_no"]}/confirm', ADMIN_TOKEN)
check('管理员确认收款', st == 200, f'HTTP {st}')
print(f'  余额 {acc["balance"]}（原 {BALANCE0} + 购买 5）')
check('额度到账（+5）', acc['balance'] == BALANCE0 + 5, acc['balance'])

# ---------- 4. 上传 ----------
print('\n[4] 上传 6 个文件')

def csv_bytes(rows, header, preamble=None, enc='utf-8', bom=False):
    buf = io.StringIO()
    w = csv.writer(buf)
    for p in (preamble or []):
        w.writerow([p])
    w.writerow(header)
    w.writerows(rows)
    b = buf.getvalue().encode(enc)
    return (b'\xef\xbb\xbf' + b) if bom else b

FILES = {
    '订单导出.csv': csv_bytes(
        [['TB001','TB001-1','2026-10-01 10:00:00','2026-10-01 10:05:00','2026-10-01 18:00:00',
          '2026-10-04 12:00:00','交易成功','无退款','b1','ITEM-A','连衣裙','MC-A','SKU-1',
          '红;M','200.00','30','6,000.00','5,900.00','100.00','0.00','6,000.00','浙江'],
         ['TB002','TB002-1','2026-10-02 09:00:00','2026-10-02 09:03:00','2026-10-02 17:00:00',
          '','交易成功','无退款','b2','ITEM-B','半身裙','MC-B','SKU-2','蓝;L','150.00','24',
          '3,600.00','3,540.00','60.00','0.00','3,600.00','广东']],
        ['订单编号','子订单编号','订单创建时间','买家付款时间','发货时间','确认收货时间',
         '订单状态','退款状态','买家会员名','商品ID','商品标题','商家编码','SKU ID',
         'sku属性','单价','购买数量','买家实际支付金额','商品金额','运费','优惠金额',
         '商家实收金额','收货省份'],
        preamble=['订单导出报表','导出时间：2026-10-19'], enc='gbk'),

    '流量.csv': csv_bytes(
        [['2026-10-01','1,000','2,500','20','30','6,000.00','25','32','6,400.00','45.0%','65','120','60'],
         ['2026-10-02','900','2,200','15','24','3,600.00','18','26','3,900.00','46.0%','62','100','50']],
        ['日期','访客数','浏览量','支付买家数','支付件数','支付金额','下单买家数','下单件数',
         '下单金额','跳失率','平均停留时长','加购人数','收藏人数'], bom=True),

    '账房.csv': csv_bytes(
        [['F001','2026-10-01','','TB001','天猫佣金','支出','-300.00','9,700.00','','淘宝'],
         ['F002','2026-10-01','','TB001','技术服务费','支出','-120.00','9,580.00','','淘宝'],
         ['F003','2026-10-02','','TB002','天猫佣金','支出','-180.00','9,400.00','','淘宝'],
         ['F004','2026-10-02','','TB002','技术服务费','支出','-72.00','9,328.00','','淘宝']],
        ['流水号','记账日期','交易时间','订单编号','业务类型','收支类型','金额','余额','备注','对方']),

    '采购成本.csv': csv_bytes(
        [['B1','MC-A','ITEM-A','2026-09-01','2026-09-10','100','80.00','400.00','0.00','甲','CNY'],
         ['B2','MC-B','ITEM-B','2026-09-05','2026-09-15','100','60.00','300.00','0.00','乙','CNY']],
        ['批次号','商家编码','商品ID','采购日期','到仓日期','数量','采购单价','运费',
         '其他成本','供应商','币种']),

    '退款.csv': csv_bytes(
        [['RF001','TB002','TB002-1','2026-10-02 20:00:00','2026-10-03 10:00:00','退款成功',
          '仅退款','ITEM-A','MC-A','2','400.00','不想要了','']],
        ['退款编号','订单编号','子订单编号','退款申请时间','退款成功时间','退款状态',
         '退款类型','商品ID','商家编码','退款数量','退款金额','退款原因','退款阶段']),

    '广告.csv': csv_bytes(
        [['2026-10-01','直通车','C1','计划A','ITEM-A','50,000','800','1.60%','600.00','0.75',
          '40','44','7,000.00','8,000.00','90','30'],
         ['2026-10-02','万相台','C2','计划B','ITEM-B','30,000','500','1.67%','400.00','0.80',
          '20','22','3,000.00','3,400.00','50','20']],
        ['日期','渠道','计划ID','计划名称','商品ID','展现量','点击量','点击率','花费',
         '平均点击花费','成交笔数','成交件数','成交金额','总成交金额','加购数','收藏数']),
}

uploaded = []
for name, content in FILES.items():
    boundary = '----smoke' + str(int(time.time() * 1000))
    body = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f'Content-Type: text/csv\r\n\r\n'
    ).encode() + content + f'\r\n--{boundary}--\r\n'.encode()
    st, r = req('POST', '/sources', TOKEN, raw=body,
                ctype=f'multipart/form-data; boundary={boundary}')
    if st == 201:
        # ★ 多表返回列表（每个 CSV 是单元素列表）
        for item in r:
            uploaded.append(item)
        first = r[0]
        print(f'  {name:14} -> {first["source_type"]:16} {first["row_count"]} 行')
        check(f'上传 {name}', True)
    else:
        check(f'上传 {name}', False, f'HTTP {st}: {str(r)[:80]}')

check('6 个文件全部成功', len(uploaded) == 6, len(uploaded))

# ---------- 5. 画像 + 映射 ----------
print('\n[5] 画像与映射确认')
for s in uploaded:
    st, prof = req('GET', f'/sources/{s["source_id"]}/profile', TOKEN)
    if st != 200:
        check(f'画像 {s["source_type"]}', False, f'HTTP {st}')
        continue
    mapping = {m['target_field']: m['source_column'] for m in prof['mappings']}
    low = [m for m in prof['mappings'] if m['needs_review']]
    print(f'  {prof["display_name"][:18]:20} {len(prof["mappings"])} 字段'
          f'{"  ⚠ 低置信 " + str(len(low)) if low else ""}')
    st2, res = req('POST', f'/sources/{s["source_id"]}/mapping', TOKEN,
                   json_body={'mapping': mapping})
    check(f'确认映射 {s["source_type"]}', st2 == 200, f'HTTP {st2}')

# ---------- 6. 诊断 ----------
print('\n[6] 发起诊断')
ids = [s['source_id'] for s in uploaded]
st, d = req('POST', '/diagnoses', TOKEN,
            json_body={'source_ids': ids, 'time_range': '2026-10-01~2026-10-02'})
check('发起诊断 202', st == 202, f'HTTP {st}')
DID = d['diagnosis_id']
print(f'  diagnosis_id = {DID[:16]}…')

# ---------- 7. SSE ----------
print('\n[7] SSE 进度流')
events = []
url = f'{BASE}/diagnoses/{DID}/events'
r = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + TOKEN,
                                         'Accept': 'text/event-stream'})
try:
    with urllib.request.urlopen(r, timeout=120) as resp:
        print(f'  Content-Type: {resp.headers.get("content-type")}')
        for raw in resp:
            line = raw.decode('utf-8', errors='replace').strip()
            if line.startswith('data: '):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
            if events and events[-1].get('type') in ('done', 'error'):
                break
except Exception as e:
    print(f'  SSE 异常: {type(e).__name__}: {e}')

print(f'  收到 {len(events)} 个事件:')
for e in events:
    if e.get('type') == 'progress':
        print(f'    progress  {e.get("phase"):10} {str(e.get("message",""))[:40]}')
    else:
        print(f'    {e.get("type")}')
check('收到进度事件', any(e.get('type') == 'progress' for e in events),
      f'{len([e for e in events if e.get("type")=="progress"])} 个')
check('收到完成事件', any(e.get('type') == 'done' for e in events))

# ---------- 8. 结果 ----------
print('\n[8] 诊断结果')
for _ in range(60):
    st, res = req('GET', f'/diagnoses/{DID}', TOKEN)
    if st == 200 and res['status'] in ('succeeded', 'failed'):
        break
    time.sleep(0.3)

print(f'  status={res["status"]}  quality_passed={res["quality_passed"]}')
check('诊断成功', res['status'] == 'succeeded', res.get('error', ''))
print(f'  指标 {len(res["metrics"])} 个:')
for m in res['metrics']:
    print(f'    {m["metric_id"]:5} {m["metric_name"]:16} = {m["value"]}')
check('有指标结果', len(res['metrics']) >= 6, len(res['metrics']))

mvals = {m['metric_id']: m['value'] for m in res['metrics']}
check('M01 访客数 = 1900', mvals.get('M01') == 1900, mvals.get('M01'))
check('M06 支付金额 = 9600', mvals.get('M06') == 9600.0, mvals.get('M06'))
check('M07 净销售额 = 9200', mvals.get('M07') == 9200.0, mvals.get('M07'))
check('M17 退款率 = 0.0417',
      mvals.get('M17') is not None and abs(mvals['M17'] - 400/9600) < 1e-4,
      mvals.get('M17'))

# ---------- 9. 报告 ----------
print('\n[9] HTML 报告')
st, html = req('GET', f'/diagnoses/{DID}/report', TOKEN)
check('报告 200', st == 200, f'HTTP {st}')
if isinstance(html, str):
    check('含 HTML 结构', '<!DOCTYPE html>' in html)
    check('含指标名', '访客数' in html or '净销售额' in html)
    check('含口径声明', '口径说明' in html)
    print(f'  HTML {len(html)} 字节')

# ---------- 10. 额度 ----------
print('\n[10] 额度')
st, acc = req('GET', '/billing/account', TOKEN)
print(f'  余额 {acc["balance"]}  已用 {acc["total_used"]}')
check('扣了 1 次（购买后余额 -1）',
      acc['balance'] == BALANCE0 + 4, acc['balance'])
st, ledger = req('GET', '/billing/ledger', TOKEN)
check('有流水记录', len(ledger) >= 2, len(ledger))
check('流水行带主键 id（P3-22）', all(e.get('id') for e in ledger))

print()
print('=' * 74)
print('全栈冒烟: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 74)
sys.exit(1 if fails else 0)
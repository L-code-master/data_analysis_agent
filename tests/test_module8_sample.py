"""模块 8：样例数据全流程验证（演示漏斗 = 付费转化路径）。

验证的正是"新用户从注册到看到报告"的完整旅程：
  注册（赠 1 次） → 下载样例 Excel → 上传（多表自动拆分）
  → 映射无需确认（置信度 1.0） → 诊断 → 趋势图 → HTML 报告 → 余额清零
"""
import sys, pathlib, os, json, time, io

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / '.testtmp' / f'sample_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "s.db"}'
os.environ['APP_SECRET_KEY'] = 'x' * 40
os.environ['TENANTS_ROOT'] = str(TMP / 'tenants')
# ★ 不设 SIGNUP_FREE_CREDITS → 默认赠送 1 次（本测试就是要验证它）

from app.config import reset_settings_cache
reset_settings_cache()
from app.storage import db
db.apply_schema()

from fastapi.testclient import TestClient
from app.main import create_app

app = create_app()
client = TestClient(app)
client.__enter__()

from app.services import sample_data

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)

print('=' * 78)
print('模块 8：样例数据全流程（演示漏斗）')
print('=' * 78)

# ===========================================================================
print('\n[1] 样例数据生成器')
files = sample_data.build_files()
print(f'  build_files: {len(files)} 份')
for name, content in files:
    print(f'    {name:20} {len(content):,} bytes')
check('生成 6 份数据', len(files) == 6)

xlsx = sample_data.build_xlsx()
print(f'  build_xlsx: {len(xlsx):,} bytes')
check('生成单个 Excel', len(xlsx) > 10000, f'{len(xlsx):,} bytes')

info = sample_data.story_summary()
check('故事摘要含周期', '2026-09' in info['period'], info['period'])
check('故事摘要含剧情', '转化率' in info['story'])

# ===========================================================================
print('\n[2] 样例 API（公开，无需登录）')
r = client.get('/api/v1/samples')
check('GET /samples 200', r.status_code == 200, r.status_code)
d = r.json()
print(f'  period={d["period"]}  sheets={len(d["sheets"])}')
check('含下载链接', d.get('download_url') == '/api/v1/samples/download')
check('含使用步骤', len(d.get('usage', [])) >= 3)

r = client.get('/api/v1/samples/download')
check('GET /samples/download 200', r.status_code == 200, r.status_code)
check('Content-Type 为 xlsx',
      'spreadsheetml' in r.headers.get('content-type', ''),
      r.headers.get('content-type', '')[:50])
check('Content-Disposition 含附件',
      'attachment' in r.headers.get('content-disposition', ''))
sample_bytes = r.content
# xlsx 含 zip 时间戳，不做字节级比较；验证可解析且 sheet 数一致
import io as _io
from openpyxl import load_workbook as _lw
_wb = _lw(_io.BytesIO(sample_bytes), read_only=True)
check('下载内容有效（7 个工作表）', len(_wb.sheetnames) == 7,
      str(_wb.sheetnames))
_wb.close()

# ===========================================================================
print('\n[3] 注册（应赠送 1 次）')
r = client.post('/api/v1/auth/register', json={
    'email': f'demo{int(time.time())}@shop.com',
    'password': 'DemoTest!2026', 'tenant_name': '演示店铺'})
check('注册 201', r.status_code == 201, r.status_code)
TOKEN = r.json()['token']['access_token']
H = {'Authorization': f'Bearer {TOKEN}'}

r = client.get('/api/v1/billing/account', headers=H)
acc = r.json()
print(f'  注册后余额: {acc["balance"]}')
check('★ 注册赠送 1 次', acc['balance'] == 1, acc['balance'])
check('赠送不算购买（total_purchased=0）', acc['total_purchased'] == 0,
      acc['total_purchased'])

r = client.get('/api/v1/billing/ledger', headers=H)
led = r.json()
check('流水含 grant 记录',
      any(e['reason'] == 'grant' for e in led), led)

# ===========================================================================
print('\n[4] 上传样例 Excel（多表自动拆分）')
r = client.post('/api/v1/sources', headers=H,
                files={'file': ('样例数据.xlsx', sample_bytes,
                                'application/vnd.openxmlformats-officedocument'
                                '.spreadsheetml.sheet')})
check('上传 201', r.status_code == 201, r.status_code)
sources = r.json()
print(f'  拆分出 {len(sources)} 个数据源:')
for s in sources:
    print(f'    {s["sheet_name"]:12} {s["source_type"]:18} {s["row_count"]:>4} 行')
check('★ 拆分出 6 个数据源', len(sources) == 6, len(sources))
types = {s['source_type'] for s in sources}
check('含全部 6 种类型',
      types == {'trade_orders', 'sycm_traffic', 'account_flow',
                'refunds', 'ad_reports', 'purchase_batches'}, types)
check('"说明"表被跳过',
      all(s['source_type'] for s in sources))

# 重复上传 → 去重
r2 = client.post('/api/v1/sources', headers=H,
                 files={'file': ('样例数据.xlsx', sample_bytes,
                                 'application/octet-stream')})
sources2 = r2.json()
check('重复上传去重（ID 不变）',
      {s['source_id'] for s in sources2} == {s['source_id'] for s in sources})

# ===========================================================================
print('\n[5] 画像（样例数据应全部高置信，无需确认）')
needs_confirm = []
for s in sources:
    r = client.get(f'/api/v1/sources/{s["source_id"]}/profile', headers=H)
    check(f'画像 {s["sheet_name"]}', r.status_code == 200)
    prof = r.json()
    low = [m for m in prof['mappings'] if m['needs_review']]
    missing = prof['missing_required']
    print(f'    {s["sheet_name"]:12} {len(prof["mappings"])} 字段  '
          f'低置信 {len(low)}  缺失 {len(missing)}')
    if prof['needs_confirmation']:
        needs_confirm.append(s['sheet_name'])
check('★ 全部数据源无需人工确认', len(needs_confirm) == 0, needs_confirm)

# ===========================================================================
print('\n[5b] CSV 闭环（docs/16 P2-4：单 BOM，列匹配不降级）')
_bom = b'\xef\xbb\xbf'
for name, content in files:
    check(f'{name} 仅一个 BOM',
          content.startswith(_bom) and not content[3:].startswith(_bom))

r = client.post('/api/v1/auth/register', json={
    'email': f'csv{int(time.time())}@shop.com',
    'password': 'CsvTest!2026', 'tenant_name': 'CSV 店'})
check('CSV 店注册 201', r.status_code == 201, r.status_code)
CH = {'Authorization': f'Bearer {r.json()["token"]["access_token"]}'}
r = client.post('/api/v1/sources', headers=CH,
                files={'file': ('订单明细.csv', files[0][1], 'text/csv')})
check('CSV 上传 201', r.status_code == 201, r.status_code)
csv_src = r.json()[0]
r = client.get(f'/api/v1/sources/{csv_src["source_id"]}/profile', headers=CH)
check('CSV 画像 200', r.status_code == 200, r.status_code)
prof = r.json()
# 双 BOM 时代首列 '订单编号' 带 U+FEFF 前缀 → 匹配降级 → needs_confirmation
check('★ CSV 无缺失必填字段', prof['missing_required'] == [],
      prof.get('missing_required'))
check('★ CSV 无需人工确认（BOM 不再破坏首列匹配）',
      prof['needs_confirmation'] is False)

# ===========================================================================
print('\n[6] 发起诊断（用全部 6 个数据源）')
ids = [s['source_id'] for s in sources]
r = client.post('/api/v1/diagnoses', headers=H,
                json={'source_ids': ids, 'time_range': '2026-09-01~2026-09-28'})
check('发起 202', r.status_code == 202, r.status_code)
DID = r.json()['diagnosis_id']
print(f'  diagnosis_id = {DID[:16]}…')

# 消费 SSE
print('\n[7] SSE 进度')
events = []
with client.stream('GET', f'/api/v1/diagnoses/{DID}/events', headers=H) as resp:
    for line in resp.iter_lines():
        if line.startswith('data: '):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
        if events and events[-1].get('type') in ('done', 'error'):
            break
print(f'  收到 {len(events)} 个事件')
check('收到进度', any(e.get('type') == 'progress' for e in events))
check('收到 done', any(e.get('type') == 'done' for e in events))
if any(e.get('type') == 'error' for e in events):
    err = [e for e in events if e.get('type') == 'error'][0]
    print(f'  ERROR: {err.get("message")}')

# ===========================================================================
print('\n[8] 诊断结果')
for _ in range(60):
    r = client.get(f'/api/v1/diagnoses/{DID}', headers=H)
    res = r.json()
    if res['status'] in ('succeeded', 'failed'):
        break
    time.sleep(0.3)

check('诊断成功', res['status'] == 'succeeded', res.get('error', ''))
print(f'  指标 {len(res["metrics"])} 个:')
for m in res['metrics']:
    v = m['value']
    if m['unit'] == 'percent' and isinstance(v, (int, float)):
        v = f'{v * 100:.2f}%'
    print(f'    {m["metric_id"]:5} {m["metric_name"]:16} = {v}')
check('至少 8 个指标', len(res['metrics']) >= 8, len(res['metrics']))

# 故事验证：第 3 周的转化率应显著低于第 1 周
trend = res.get('trend') or []
check('★ 含趋势数据', len(trend) >= 14, f'{len(trend)} 天')
if trend:
    w1 = [t for t in trend if t['date'] <= '2026-09-07']
    w3 = [t for t in trend if '2026-09-15' <= t['date'] <= '2026-09-21']
    cvr1 = sum(t['visitors'] for t in w1) and sum(
        t['net_sales'] for t in w1) / (sum(t['visitors'] for t in w1) * 168)
    cvr3 = sum(t['visitors'] for t in w3) and sum(
        t['net_sales'] for t in w3) / (sum(t['visitors'] for t in w3) * 168)
    print(f'  周1 净销售/访客 ≈ ¥{cvr1:.2f}，周3 ≈ ¥{cvr3:.2f}')
    check('★ 故事可读：周3 人均产出低于周1', cvr3 < cvr1, f'{cvr3:.2f} < {cvr1:.2f}')

# ===========================================================================
print('\n[9] HTML 报告')
r = client.get(f'/api/v1/diagnoses/{DID}/report', headers=H)
check('报告 200', r.status_code == 200, r.status_code)
html = r.text
check('含统计卡片', 'cards' in html or '支付金额' in html)
check('★ 含趋势 SVG', '<svg' in html, f'{len(html):,} bytes')
check('含指标', '净销售额' in html or '毛利率' in html)
check('含口径声明', '口径说明' in html)
check('含打印样式', '@media print' in html)
check('报告大小合理', 3000 < len(html) < 60000, f'{len(html):,} bytes')

r = client.get(f'/api/v1/diagnoses/{DID}/report.json', headers=H)
rj = r.json()
check('报告 JSON 含趋势', 'trend' in rj, list(rj.keys()))
check('报告 JSON 含时间范围', 'time_range' in rj)

# ===========================================================================
print('\n[9.5] v1.2 归因区块（商品拆解 / 异常日 / 周对比 / 口径对比）')
prods = rj.get('products') or []
check('★ 商品拆解：2 个商品', len(prods) == 2, len(prods))
if len(prods) == 2:
    a = next((p for p in prods if p['merchant_code'] == 'MC-A'), None)
    b = next((p for p in prods if p['merchant_code'] == 'MC-B'), None)
    check('★ 连衣裙（MC-A）被标记重点关注', bool(a) and a['flags'] == ['退款率偏高'],
          a['flags'] if a else 'missing')
    check('★ MC-A 主要退款原因 = QUALITY（前缀映射）',
          bool(a) and a['top_refund_reason'] == 'QUALITY',
          a['top_refund_reason'] if a else '')
    check('半身裙（MC-B）原因 = SIZE', bool(b) and b['top_refund_reason'] == 'SIZE',
          b['top_refund_reason'] if b else '')
    check('商品含占比与毛利率', bool(a) and a['share'] > 0.5
          and a['margin_rate'] is not None)

anoms = rj.get('anomaly_days') or []
check('★ 异常日 ≥3 天', len(anoms) >= 3, len(anoms))
w3 = [a for a in anoms if '2026-09-15' <= a['date'] <= '2026-09-21']
check('★ 第 3 周有异常日（故事被识别）', len(w3) >= 2, len(w3))
if anoms:
    check('异常日含特征与建议', all(a.get('features') and a.get('note')
                                     for a in anoms))
check('HTML 含异常标记线', 'stroke="#d46b08"' in html)

weekly = rj.get('weekly') or {}
wk = weekly.get('weeks') or []
check('周对比 ≥4 周', len(wk) >= 4, len(wk))
check('★ biggest 来自完整周（残周排除）',
      weekly.get('biggest') is not None
      and all(w.get('days') is not None for w in wk))

cc = rj.get('caliber_compare') or {}
check('★ 口径对比：M13/M14 并排', len(cc.get('items') or []) == 2)
if cc.get('items'):
    fin = cc['items'][0]['value']
    pro = cc['items'][1]['value']
    check('财务口径 > 推广口径（差异=广告占比）', fin > pro and cc['diff'] > 0)
cal = rj.get('caliber') or {}
check('★ 口径快照（registry 版本）', bool(cal.get('registry_version')),
      cal.get('registry_version'))
check('HTML 含 口径对比/异常日/商品/周对比',
      all(k in html for k in ('口径对比', '异常日定位', '商品维度拆解', '周度对比')))

# ===========================================================================
print('\n[10] 额度清零（赠试用完）')
r = client.get('/api/v1/billing/account', headers=H)
acc = r.json()
print(f'  余额 {acc["balance"]}  已用 {acc["total_used"]}')
check('★ 赠送的 1 次已用完', acc['balance'] == 0, acc['balance'])
check('total_used = 1', acc['total_used'] == 1, acc['total_used'])
check('total_purchased 仍为 0', acc['total_purchased'] == 0)

# 再诊断 → 应被拒
r = client.post('/api/v1/diagnoses', headers=H, json={'source_ids': ids})
check('额度用完后拒绝（402）', r.status_code == 402, r.status_code)
check('错误码正确', r.json().get('code') == 'INSUFFICIENT_CREDITS')

print()
print('=' * 78)
print('模块 8: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)

client.__exit__(None, None, None)
import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
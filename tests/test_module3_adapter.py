"""模块 3 验证：淘宝适配器。

生成仿真的千牛导出文件（含真实的中文列名、GBK 编码、表头前置行），
验证：识别 → 读取 → 映射建议 → 应用 → 校验 全链路。
"""
import sys, pathlib, csv, io

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapters import taobao
from app.adapters.taobao_fields import get_spec, spec_summary

TMP = ROOT / '.testtmp' / f'adapters_{__import__("os").getpid()}'
TMP.mkdir(parents=True, exist_ok=True)

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond: fails.append(label)

print('=' * 76)
print('模块 3 验证：淘宝适配器')
print('=' * 76)

# ---------- 0. 规格清单 ----------
print('\n[0] 数据源规格')
specs = spec_summary()
for s in specs:
    print(f'  {s["key"]:18} {s["display_name"]:24} {s["field_count"]:2} 字段 '
          f'({len(s["required_fields"])} 必填)')
check('7 种数据源规格', len(specs) == 7, len(specs))

# ---------- 1. 生成仿真千牛导出文件 ----------
print('\n[1] 生成仿真导出文件')

# ① 订单 CSV（GBK 编码 + 前 2 行说明行，模拟真实千牛导出）
orders_path = TMP / '订单导出.csv'
order_headers = [
    '订单编号', '子订单编号', '订单创建时间', '买家付款时间', '发货时间',
    '确认收货时间', '订单状态', '退款状态', '买家会员名', '商品ID',
    '商品标题', '商家编码', 'SKU ID', 'sku属性', '单价', '购买数量',
    '买家实际支付金额', '商品金额', '运费', '优惠金额', '商家实收金额',
    '收货省份',
]
order_rows = [
    ['TB20261001001', 'TB20261001001-1', '2026-10-01 10:00:00', '2026-10-01 10:05:00',
     '2026-10-01 18:00:00', '2026-10-04 12:00:00', '交易成功', '无退款', 'buyer_a',
     'ITEM-A', '秋季连衣裙', 'MC-A', 'SKU-1', '红色;M', '200.00', '30',
     '6,000.00', '5,900.00', '100.00', '0.00', '6,000.00', '浙江'],
    ['TB20261002001', 'TB20261002001-1', '2026-10-02 09:00:00', '2026-10-02 09:03:00',
     '2026-10-02 17:00:00', '', '交易成功', '退款成功', 'buyer_b',
     'ITEM-A', '秋季连衣裙', 'MC-A', 'SKU-1', '红色;M', '200.00', '24',
     '4,800.00', '4,720.00', '80.00', '0.00', '4,800.00', '江苏'],
]
buf = io.StringIO()
w = csv.writer(buf)
w.writerow(['订单导出报表'])          # 表头前置行 1
w.writerow(['导出时间：2026-10-18'])   # 表头前置行 2
w.writerow(order_headers)
w.writerows(order_rows)
orders_path.write_bytes(buf.getvalue().encode('gbk'))
print(f'  订单文件: {orders_path.name} ({orders_path.stat().st_size} bytes, GBK)')

# ② 生意参谋流量 CSV（UTF-8-BOM，表头在第 1 行）
traffic_path = TMP / '生意参谋_流量.csv'
traffic_headers = ['日期', '访客数', '浏览量', '支付买家数', '支付件数',
                   '支付金额', '下单买家数', '下单件数', '下单金额', '跳失率',
                   '平均停留时长', '加购人数', '收藏人数']
traffic_rows = [
    ['2026-10-01', '1,000', '2,500', '20', '30', '6,000.00', '25', '32', '6,400.00', '45.0%', '65', '120', '60'],
    ['2026-10-02', '950', '2,300', '16', '24', '4,800.00', '19', '26', '5,100.00', '47.0%', '62', '105', '55'],
]
buf2 = io.StringIO()
w2 = csv.writer(buf2)
w2.writerow(traffic_headers)
w2.writerows(traffic_rows)
traffic_path.write_bytes(b'\xef\xbb\xbf' + buf2.getvalue().encode('utf-8'))
print(f'  流量文件: {traffic_path.name} (UTF-8-BOM)')

# ③ 账房收支明细（表头在第 0 行）
account_path = TMP / '账房_收支明细.csv'
account_headers = ['流水号', '记账日期', '交易时间', '订单编号', '业务类型',
                   '收支类型', '金额', '余额', '备注', '对方']
account_rows = [
    ['F001', '2026-10-01', '', 'TB20261001001', '天猫佣金', '支出', '-300.00', '9,700.00', '', '淘宝'],
    ['F002', '2026-10-01', '', 'TB20261001001', '技术服务费', '支出', '-120.00', '9,580.00', '', '淘宝'],
    ['F003', '2026-10-02', '', 'TB20261002001', '天猫佣金', '支出', '-240.00', '9,340.00', '', '淘宝'],
    ['F004', '2026-10-02', '', '', '未知费用类型XYZ', '支出', '-50.00', '9,290.00', '', '淘宝'],
]
buf3 = io.StringIO()
w3 = csv.writer(buf3)
w3.writerow(account_headers)
w3.writerows(account_rows)
account_path.write_text(buf3.getvalue(), encoding='utf-8')
print(f'  账房文件: {account_path.name} (UTF-8)')

# ④ 一个无关文件（测试识别不会乱猜）
noise_path = TMP / '无关文件.csv'
noise_path.write_text('姓名,年龄,城市\n张三,30,杭州\n', encoding='utf-8')

# ---------- 2. 识别 ----------
print('\n[2] 文件类型识别（两步式：先读后识别）')
prepared = {}
for label, p in (('订单', orders_path), ('流量', traffic_path), ('账房', account_path)):
    pr = taobao.prepare(p)
    prepared[label] = pr
    sn = pr.sniff
    print(f'  {label}: 表头行={pr.read.header_row_index} 列数={len(pr.read.headers)} '
          f'行数={pr.read.row_count}')
    print(f'        识别 -> {sn.spec.key if sn.spec else "未知"} (score={sn.score:.2f})')
    print(f'        候选 -> {sn.candidates[:3]}')
    check(f'{label}识别正确', sn.spec is not None and sn.confident)

check('订单 -> trade_orders', prepared['订单'].spec.key == 'trade_orders',
      prepared['订单'].spec.key)
check('流量 -> sycm_traffic', prepared['流量'].spec.key == 'sycm_traffic',
      prepared['流量'].spec.key)
check('账房 -> account_flow', prepared['账房'].spec.key == 'account_flow',
      prepared['账房'].spec.key)

pr_noise = taobao.prepare(noise_path)
check('无关文件识别为低分', pr_noise.sniff.score < 0.2,
      f'score={pr_noise.sniff.score:.2f}')

# 表头行定位
check('订单表头定位到第 2 行（跳过 2 行前置）',
      prepared['订单'].read.header_row_index == 2,
      prepared['订单'].read.header_row_index)
check('流量表头定位到第 0 行',
      prepared['流量'].read.header_row_index == 0,
      prepared['流量'].read.header_row_index)

# ---------- 3. 编码探测 ----------
print('\n[3] 编码探测')
enc_orders = taobao.detect_encoding(orders_path)
enc_traffic = taobao.detect_encoding(traffic_path)
print(f'  订单文件 -> {enc_orders}')
print(f'  流量文件 -> {enc_traffic}')
check('GBK 文件被正确识别', enc_orders.lower() in ('gbk', 'gb18030', 'gb2312'), enc_orders)
check('UTF-8-BOM 被正确识别', enc_traffic == 'utf-8-sig', enc_traffic)

# 读出的中文是否正确
r = taobao.read_file(orders_path)
check('GBK 中文未乱码', '订单编号' in r.headers, r.headers[0])

# ---------- 4. 字段映射 ----------
print('\n[4] 字段映射建议（订单）')
pr_orders = prepared['订单']
r = pr_orders.read
spec = pr_orders.spec
mappings = pr_orders.mappings
print(f'  映射 {len(mappings)}/{spec.field_count} 个字段')
for m in mappings[:8]:
    flag = '⚠' if m.needs_review else ' '
    print(f'    {flag} {m.source_column:20} -> {m.target_field:18} '
          f'{m.confidence:.2f} ({m.confidence_label})')
if len(mappings) > 8:
    print(f'    ... 其余 {len(mappings)-8} 个')

check('映射数量 >= 18', len(mappings) >= 18, len(mappings))
check('订单号映射正确',
      any(m.target_field == 'order_id' and m.source_column == '订单编号' for m in mappings))
check('商家编码映射正确',
      any(m.target_field == 'merchant_code' and m.source_column == '商家编码' for m in mappings))
missing = pr_orders.missing_required
check('必填字段齐全', len(missing) == 0, missing)
check('prepare 判定为 ok', pr_orders.ok, pr_orders.spec.key if pr_orders.spec else 'None')

# ---------- 5. 应用映射 ----------
print('\n[5] 应用映射 -> 标准行')
rows, errs = taobao.apply_mapping(r, mappings, spec)
print(f'  生成 {len(rows)} 行, {len(errs)} 个错误')
if rows:
    r0 = rows[0]
    print(f'  第一行关键字段:')
    for k in ('order_id', 'pay_date' if 'pay_date' in r0 else 'pay_time',
              'merchant_code', 'quantity', 'pay_amount', 'create_time'):
        if k in r0:
            print(f'    {k:16} = {r0[k]!r}')
check('生成 2 行', len(rows) == 2, len(rows))
check('无错误', len(errs) == 0, [e.message for e in errs])
check('金额去千分位', rows[0].get('pay_amount') == 6000.0, rows[0].get('pay_amount'))
check('数量为整数', rows[0].get('quantity') == 30, rows[0].get('quantity'))
check('时间归一化为 ISO', str(rows[0].get('create_time', '')).startswith('2026-10-01'),
      rows[0].get('create_time'))
check('空确认收货时间为 None', rows[1].get('confirm_time') is None,
      rows[1].get('confirm_time'))

# ---------- 6. 数值归一化细节 ----------
print('\n[6] 数值/日期归一化')
cases = [
    ('1,234.56', 1234.56), ('¥1,234', 1234), ('45.0%', 0.45),
    ('(123.45)', -123.45), ('1.5万', 15000), ('-300.00', -300.0),
    ('--', None), ('', None), ('2,000', 2000),
]
for raw, expect in cases:
    got = taobao._coerce('pay_amount', raw)
    ok = (got == expect) if expect is not None else (got is None)
    print(f'    {raw!r:12} -> {got!r:12} (expect {expect!r})')
    if not ok: fails.append(f'归一化 {raw!r}')

dt_cases = [
    ('2026-10-01 10:05:00', '2026-10-01 10:05:00'),
    ('2026/10/01', '2026-10-01'),
    ('2026.10.01', '2026-10-01'),
    ('2026年10月1日', '2026-10-01'),
    ('2026年10月1日 14:30', '2026-10-01 14:30:00'),
]
for raw, expect in dt_cases:
    got = taobao._normalize_datetime(raw)
    print(f'    {raw!r:24} -> {got!r:24} (expect {expect!r})')
    if got != expect: fails.append(f'日期 {raw!r}')

# ---------- 7. 列名归一化 ----------
print('\n[7] 列名归一化')
norm_cases = [
    ('支付金额（元）', '支付金额元'),
    (' 访客数 ', '访客数'),
    ('支付金额(元)', '支付金额元'),
    ('SKU ID', 'skuid'),
    ('商品ID', '商品id'),
]
for raw, expect in norm_cases:
    got = taobao.normalize_column(raw)
    ok = got == expect
    print(f'    {raw!r:18} -> {got!r:16} (expect {expect!r})')
    if not ok: fails.append(f'归一化列名 {raw!r}')

# ---------- 8. 缺失必填字段会被报错 ----------
print('\n[8] 缺失必填字段检测')
partial_headers = ['日期', '访客数']   # 缺浏览量、支付买家数、支付金额
sp = get_spec('sycm_traffic')
pm = taobao.suggest(partial_headers, sp)
miss = taobao.missing_required(pm, sp)
print(f'  缺失必填: {miss}')
check('检测到缺失必填字段', len(miss) >= 3, miss)

# ---------- 9. 指纹 ----------
print('\n[9] 指纹')
fp1 = taobao.schema_fingerprint(traffic_headers)
fp2 = taobao.schema_fingerprint(list(reversed(traffic_headers)))
check('列序不同指纹相同（同结构可复用映射）', fp1 == fp2, f'{fp1[:16]}')
fp3 = taobao.schema_fingerprint(traffic_headers + ['新增列'])
check('列不同指纹不同', fp1 != fp3)
h1 = taobao.file_sha256(orders_path)
check('文件哈希稳定', h1 == taobao.file_sha256(orders_path), h1[:16])

# ---------- 10. 数值清洗失败可追溯（docs/16 P1-10）----------
print('\n[10] 数值清洗失败记录')
bad: dict[str, dict] = {}
v = taobao.coerce_for_table('stg_trade_orders', 'pay_amount', 'abc', bad)
check('非法金额置 NULL', v is None, v)
check('★ 失败值被收集（BAD_AMOUNT）',
      bad.get('pay_amount', {}).get('count') == 1, bad)
check('收集了原文示例', 'abc' in bad.get('pay_amount', {}).get('samples', []),
      bad.get('pay_amount', {}).get('samples'))
taobao.coerce_for_table('stg_trade_orders', 'pay_amount', '--', bad)
check('空值不算清洗失败', bad['pay_amount']['count'] == 1, bad['pay_amount'])
v2 = taobao.coerce_for_table('stg_trade_orders', 'pay_amount', '1,234.56', bad)
check('正常金额转换正确', v2 == 1234.56, v2)
check('死三元已修复（float 解析失败返回 None）',
      taobao._coerce_typed('float', 'abc') is None
      and taobao._coerce_typed('int', 'abc') is None)

# ---------- 11. 行数上限截断告警（docs/16 P1-11）----------
print('\n[11] 行数上限截断')
trunc_path = TMP / 'truncated.csv'
tb = io.StringIO()
tw = csv.writer(tb)
tw.writerow(traffic_headers)
for i in range(5):
    tw.writerow(['2026-10-0%d' % (i + 1), str(100 + i)] + ['0'] * 11)
trunc_path.write_bytes(tb.getvalue().encode('utf-8'))
_orig_max = taobao.MAX_READ_ROWS
taobao.MAX_READ_ROWS = 3
try:
    rr = taobao.read_file(trunc_path)
    check('★ 截断标记为 True', rr.truncated, rr.truncated)
    check('★ 截断产生 warning',
          any('上限' in w for w in rr.warnings), rr.warnings)
    prep = taobao.prepare(trunc_path)
    check('★ 截断要求人工确认', prep.needs_confirmation(),
          prep.spec.key if prep.spec else None)
finally:
    taobao.MAX_READ_ROWS = _orig_max

print()
print('=' * 76)
print('模块 3: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 76)

import shutil
shutil.rmtree(TMP, ignore_errors=True)
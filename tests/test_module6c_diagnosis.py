"""模块 6c-2/3/4 验证：上传 → 画像 → 确认映射 → 执行诊断 → 报告。"""
import sys, pathlib, os, io, csv, json

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / '.testtmp' / f'diag_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "d.db"}'
os.environ['APP_SECRET_KEY'] = 'x' * 40
os.environ['SIGNUP_FREE_CREDITS'] = '0'
os.environ['TENANTS_ROOT'] = str(TMP / 'tenants')

from app.config import reset_settings_cache
reset_settings_cache()
from app.storage import db
db.apply_schema()
from app.services import auth, billing, diagnosis as dg, tenant as tn

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)

print('=' * 78)
print('模块 6c-2/3/4 验证：诊断服务')
print('=' * 78)

# ---------- 准备：注册 + 充值 ----------
u, _ = auth.register('diag@shop.com', 'MyStr0ng!Pass', tenant_name='诊断测试店')
TID = u.tenant_id
o = billing.create_order(TID, 'pack5', u.user_id)
billing.mark_paid(o.order_no, reviewed_by=u.user_id)
print(f'\n[0] 准备完成：租户={TID[:10]}... 额度={billing.get_account(TID).balance}')

# ---------- 构造淘宝订单 CSV ----------
def make_orders_csv() -> bytes:
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
         '2026-10-02 17:00:00','','交易成功','退款成功','b2',
         'ITEM-A','秋季连衣裙','MC-A','SKU-1','红;M','200.00','24',
         '4,800.00','4,720.00','80.00','0.00','4,800.00','江苏'],
        ['TB003','TB003-1','2026-10-03 09:00:00','2026-10-03 09:05:00','',
         '','交易成功','无退款','b3',
         'ITEM-B','半身裙','MC-B','SKU-2','蓝;L','150.00','24',
         '3,600.00','3,540.00','60.00','0.00','3,600.00','广东'],
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['订单导出报表'])
    w.writerow(['导出时间：2026-10-19'])
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode('gbk')

def make_traffic_csv() -> bytes:
    headers = ['日期','访客数','浏览量','支付买家数','支付件数','支付金额',
               '下单买家数','下单件数','下单金额','跳失率','平均停留时长',
               '加购人数','收藏人数']
    rows = [
        ['2026-10-01','1,000','2,500','20','30','6,000.00','25','32','6,400.00','45.0%','65','120','60'],
        ['2026-10-02','950','2,300','16','24','4,800.00','19','26','5,100.00','47.0%','62','105','55'],
        ['2026-10-03','800','2,000','12','18','3,600.00','15','20','3,900.00','40.0%','60','90','45'],
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return b'\xef\xbb\xbf' + buf.getvalue().encode('utf-8')

def make_account_csv() -> bytes:
    headers = ['流水号','记账日期','交易时间','订单编号','业务类型',
               '收支类型','金额','余额','备注','对方']
    rows = [
        ['F001','2026-10-01','','TB001','天猫佣金','支出','-300.00','9,700.00','','淘宝'],
        ['F002','2026-10-01','','TB001','技术服务费','支出','-120.00','9,580.00','','淘宝'],
        ['F003','2026-10-02','','TB002','天猫佣金','支出','-240.00','9,340.00','','淘宝'],
        ['F004','2026-10-02','','TB002','技术服务费','支出','-96.00','9,244.00','','淘宝'],
        ['F005','2026-10-03','','TB003','天猫佣金','支出','-180.00','9,064.00','','淘宝'],
        ['F006','2026-10-03','','TB003','技术服务费','支出','-72.00','8,992.00','','淘宝'],
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode('utf-8')

def make_refunds_csv() -> bytes:
    headers = ['退款编号','订单编号','子订单编号','退款申请时间','退款成功时间',
               '退款状态','退款类型','商品ID','商家编码','退款数量',
               '退款金额','退款原因','退款阶段']
    rows = [
        ['RF001','TB002','TB002-1','2026-10-02 20:00:00','2026-10-03 10:00:00',
         '退款成功','仅退款','ITEM-A','MC-A','2','400.00','不想要了',''],
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode('utf-8')

def make_ads_csv() -> bytes:
    headers = ['日期','渠道','计划ID','计划名称','商品ID','展现量','点击量',
               '点击率','花费','平均点击花费','成交笔数','成交件数',
               '成交金额','总成交金额','加购数','收藏数']
    rows = [
        ['2026-10-01','直通车','C1','计划A','ITEM-A','50,000','800','1.60%',
         '600.00','0.75','40','44','7,000.00','8,000.00','90','30'],
        ['2026-10-02','直通车','C1','计划A','ITEM-A','48,000','760','1.58%',
         '700.00','0.92','30','33','5,200.00','6,000.00','80','25'],
        ['2026-10-03','万相台','C2','计划B','ITEM-B','30,000','500','1.67%',
         '400.00','0.80','20','22','3,000.00','3,400.00','50','20'],
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode('utf-8')

def make_purchase_csv() -> bytes:
    headers = ['批次号','商家编码','商品ID','采购日期','到仓日期','数量',
               '采购单价','运费','其他成本','供应商','币种']
    rows = [
        ['B1','MC-A','ITEM-A','2026-09-01','2026-09-10','100','80.00','400.00','0.00','供应商甲','CNY'],
        ['B2','MC-B','ITEM-B','2026-09-05','2026-09-15','100','60.00','300.00','0.00','供应商乙','CNY'],
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode('utf-8')

# ===========================================================================
print('\n[1] 上传与识别')
src_orders = dg.register_source(TID, u.user_id, '订单导出.csv', make_orders_csv())[0]
print(f'  订单: type={src_orders.source_type} rows={src_orders.row_count} '
      f'id={src_orders.id[:10]}')
check('识别为 trade_orders', src_orders.source_type == 'trade_orders')
check('解析出 3 行', src_orders.row_count == 3, src_orders.row_count)
check('状态 profiled', src_orders.status == 'profiled')

src_traffic = dg.register_source(TID, u.user_id, '生意参谋_流量.csv', make_traffic_csv())[0]
print(f'  流量: type={src_traffic.source_type} rows={src_traffic.row_count}')
check('识别为 sycm_traffic', src_traffic.source_type == 'sycm_traffic')
check('解析出 3 行', src_traffic.row_count == 3)

src_account = dg.register_source(TID, u.user_id, '账房_收支明细.csv', make_account_csv())[0]
print(f'  账房: type={src_account.source_type} rows={src_account.row_count}')
check('识别为 account_flow', src_account.source_type == 'account_flow')

src_refunds = dg.register_source(TID, u.user_id, '退款明细.csv', make_refunds_csv())[0]
print(f'  退款: type={src_refunds.source_type} rows={src_refunds.row_count}')
check('识别为 refunds', src_refunds.source_type == 'refunds')

src_ads = dg.register_source(TID, u.user_id, '直通车报表.csv', make_ads_csv())[0]
print(f'  广告: type={src_ads.source_type} rows={src_ads.row_count}')
check('识别为 ad_reports', src_ads.source_type == 'ad_reports')

src_purchase = dg.register_source(TID, u.user_id, '采购成本.csv', make_purchase_csv())[0]
print(f'  成本: type={src_purchase.source_type} rows={src_purchase.row_count}')
check('识别为 purchase_batches', src_purchase.source_type == 'purchase_batches')

ALL_SOURCES = [src_orders.id, src_traffic.id, src_account.id,
               src_refunds.id, src_ads.id, src_purchase.id]

# 无法识别的文件
try:
    dg.register_source(TID, u.user_id, 'random.csv', '姓名,年龄\n张三,30\n'.encode('utf-8'))
    check('无法识别的文件被拒绝', False)
except dg.SourceTypeUnknownError as e:
    check('无法识别的文件被拒绝', True, e.message[:50])

# 非法扩展名
try:
    dg.register_source(TID, u.user_id, 'evil.exe', b'MZ\x90\x00')
    check('非法扩展名被拒绝', False)
except dg.DiagnosisError as e:
    check('非法扩展名被拒绝', e.code == 'UPLOAD_REJECTED', e.message[:40])

# 内容去重
src_dup = dg.register_source(TID, u.user_id, '订单导出_副本.csv', make_orders_csv())[0]
check('相同内容去重（同一 source_id）', src_dup.id == src_orders.id)

# ===========================================================================
print('\n[2] 数据画像（只含结构，不含数据行）')
prof = dg.profile_source(TID, src_orders.id)
print(f'  类型: {prof.display_name}')
print(f'  行数: {prof.row_count}  列数: {len(prof.columns)}')
print(f'  映射: {len(prof.mappings)} 个')
print(f'  需确认: {prof.needs_confirmation}')
for m in prof.mappings[:5]:
    flag = '⚠' if m['needs_review'] else ' '
    print(f'    {flag} {m["source_column"]:20} -> {m["target_field"]:18} {m["confidence"]:.2f}')
check('返回列结构', len(prof.columns) == 22, len(prof.columns))
check('生成映射建议', len(prof.mappings) >= 20, len(prof.mappings))
check('无缺失必填', len(prof.missing_required) == 0, prof.missing_required)
# ★ 画像不得含数据行
blob = json.dumps(prof.to_dict(), ensure_ascii=False)
check('★ 画像不含数据行（无 TB001）', 'TB001' not in blob)
check('★ 画像不含金额明细（无 6000）', '6000' not in blob.replace('"', ''))
check('含 schema 指纹', len(prof.schema_fingerprint) == 32, prof.schema_fingerprint)

# ===========================================================================
print('\n[3] 确认字段映射')
mapping = {m['target_field']: m['source_column'] for m in prof.mappings}
res = dg.confirm_mapping(TID, u.user_id, src_orders.id, mapping)
print(f'  {res["message"]}')
check('确认成功', res['confirmed_fields'] >= 20, res['confirmed_fields'])

# 缺必填
try:
    dg.confirm_mapping(TID, u.user_id, src_orders.id, {'order_id': '订单编号'})
    check('缺必填被拒绝', False)
except dg.DiagnosisError as e:
    check('缺必填被拒绝', e.code == 'MISSING_REQUIRED_MAPPING', e.message[:50])

# 未知字段
try:
    bad = dict(mapping); bad['bogus_field'] = 'x'
    dg.confirm_mapping(TID, u.user_id, src_orders.id, bad)
    check('未知目标字段被拒绝', False)
except dg.DiagnosisError as e:
    check('未知目标字段被拒绝', e.code == 'BAD_MAPPING')

# ★ 同结构免二次确认
prof2 = dg.profile_source(TID, src_orders.id)
already = [m for m in prof2.mappings if m.get('confirmed')]
check('★ 同结构映射已复用（免二次确认）', len(already) >= 20, len(already))

# 其余数据源也确认
for sid in (src_traffic.id, src_account.id, src_refunds.id,
            src_ads.id, src_purchase.id):
    p2 = dg.profile_source(TID, sid)
    m2 = {m['target_field']: m['source_column'] for m in p2.mappings}
    dg.confirm_mapping(TID, u.user_id, sid, m2)
print('  流量/账房/退款/广告/成本 映射也已确认')

# ===========================================================================
print('\n[4] 执行诊断')
before = billing.get_account(TID).balance
print(f'  诊断前余额: {before}')

phases = []
res = dg.run_diagnosis(TID, u.user_id, ALL_SOURCES,
                       time_range_text='2026-10-01~2026-10-03',
                       progress=lambda p, m: phases.append(p))
after = billing.get_account(TID).balance
print(f'  诊断后余额: {after}')
print(f'  状态: {res.status}  耗时: {res.duration_ms}ms')
print(f'  阶段: {sorted(set(phases))}')
print(f'  指标 {len(res.metrics)} 个:')
for m in res.metrics:
    print(f'    {m["metric_id"]:5} {m["metric_name"]:16} = {m["value"]}')
print(f'  告警 {len(res.warnings)} 条')

check('诊断成功', res.status == 'succeeded')
check('★ 扣了 1 次额度', after == before - 1, f'{before} -> {after}')
check('有指标结果', len(res.metrics) > 0, len(res.metrics))
check('质量门通过', res.quality_passed)
check('有报告产物', bool(res.report_ref))
check('有口径脚注', bool(res.footnote))
check('阶段有 importing', 'importing' in phases)
check('阶段有 quality', 'quality' in phases)

# ---------- 数值正确性 ----------
mvals = {m['metric_id']: m['value'] for m in res.metrics}
print(f'\n  指标值明细: {mvals}')
check('M01 访客数 = 2750', mvals.get('M01') == 2750, mvals.get('M01'))
check('M03 支付转化率 ≈ 0.0175',
      mvals.get('M03') is not None and abs(mvals['M03'] - 48/2750) < 1e-4,
      mvals.get('M03'))
check('M05 客单价 = 300', mvals.get('M05') == 300.0, mvals.get('M05'))
check('M06 支付金额 = 14400', mvals.get('M06') == 14400.0, mvals.get('M06'))
check('M07 净销售额 = 14000', mvals.get('M07') == 14000.0, mvals.get('M07'))
check('M13 毛利率有值', mvals.get('M13') is not None, mvals.get('M13'))
check('M17 退款率 ≈ 0.0278',
      mvals.get('M17') is not None and abs(mvals['M17'] - 400/14400) < 1e-5,
      mvals.get('M17'))

# ===========================================================================
print('\n[5] 报告生成')
html = dg.render_report_html(TID, res.diagnosis_id)
print(f'  HTML 长度: {len(html)} 字节')
check('生成 HTML', len(html) > 500, len(html))
check('含 html 标签', '<!DOCTYPE html>' in html)
check('含指标名', '访客数' in html or '净销售额' in html)
check('含口径声明', '口径说明' in html)
check('★ 含待确认提示', '待确认' in html)
check('中文编码正确', '经营诊断报告' in html)

rpt = dg.get_report_json(TID, res.diagnosis_id)
check('报告 JSON 可取', 'metrics' in rpt)
check('报告含数据源信息', rpt.get('source', {}).get('name') == '订单导出.csv')

# ===========================================================================
print('\n[6] 查询接口')
d = dg.get_diagnosis(TID, res.diagnosis_id)
check('可查诊断状态', d['status'] == 'succeeded')
check('状态含 report_ref', bool(d['report_ref']))

lst = dg.list_diagnoses(TID)
check('诊断列表非空', len(lst) >= 1, len(lst))

srcs = dg.list_sources(TID)
print(f'  数据源 {len(srcs)} 个')
check('数据源列表非空', len(srcs) >= 3, len(srcs))

try:
    dg.get_diagnosis('other_tenant', res.diagnosis_id)
    check('★ 跨租户查诊断被拒绝', False, '未拦截！')
except dg.DiagnosisError:
    check('★ 跨租户查诊断被拒绝', True)

# ===========================================================================
print('\n[7] 失败路径：额度退还')
# 构造一个必填字段缺失的场景
bad = dg.register_source(TID, u.user_id, '残缺订单.csv',
                         '订单编号,订单状态\nTB999,交易成功\n'.encode('utf-8'))[0]
before2 = billing.get_account(TID).balance
try:
    dg.run_diagnosis(TID, u.user_id, [bad.id])
    check('缺映射时诊断失败', False)
except dg.DiagnosisError as e:
    check('缺映射时诊断失败', True, e.code)
after2 = billing.get_account(TID).balance
check('★ 失败退还额度', after2 == before2, f'{before2} -> {after2}')

# ===========================================================================
print('\n[8] 额度耗尽')
# 把余额清零
cur = billing.get_account(TID).balance
for _ in range(cur):
    billing.consume_credit(TID, f'burn-{_}')
print(f'  余额已清零（消耗 {cur} 次）')
try:
    dg.run_diagnosis(TID, u.user_id, [src_orders.id])
    check('额度耗尽时拒绝诊断', False)
except billing.InsufficientCreditsError as e:
    check('额度耗尽时拒绝诊断', True, e.message[:40])

# ===========================================================================
print('\n[9] 租户目录隔离')
p = tn.tenant_paths(TID)
import os as _os
print(f'  租户目录: {p.root.name}')
files = []
for root, _d, names in _os.walk(p.root):
    for n in names:
        files.append(_os.path.relpath(_os.path.join(root, n), p.root))
print(f'  文件 {len(files)} 个:')
for f in sorted(files)[:8]:
    print(f'    {f}')
check('inbox 有上传文件', any('inbox' in f for f in files))
check('artifacts 有产物', any('artifacts' in f for f in files))
check('有 semantic.duckdb', any('semantic.duckdb' in f for f in files))

usage = tn.tenant_usage(TID)
print(f'  磁盘用量: {usage["files"]} 文件 / {usage["mb"]} MB')

# ===========================================================================
print('\n[10] 报告 XSS 转义（docs/16 P1-17）')
evil = '<img src=x onerror=alert(1)>'
evil_data = {
    'diagnosis_id': 'abc123', 'generated_at': '2026-10-23T00:00:00',
    'time_range': None,
    'source': {'name': evil, 'file_count': 1, 'row_count': 1,
               'files': [{'name': evil, 'rows': 1}]},
    'metrics': [{'metric_id': 'M01', 'metric_name': evil, 'value': evil,
                 'unit': 'count', 'formula': evil, 'uncertainty': 'low'}],
    'trend': [], 'anomaly_days': [],
    'products': [{'item_id': '1', 'merchant_code': evil, 'title': evil,
                  'pay_amount': 100, 'share': 1.0, 'net_sales': 90,
                  'quantity': 1, 'refund_amount': 0, 'refund_rate': 0,
                  'gross_margin': 50, 'margin_rate': 0.5,
                  'top_refund_reason': evil, 'flags': []}],
    'weekly': {}, 'caliber_compare': {},
    'caliber': {'registry_version': evil},
    'footnote': evil, 'warnings': [evil],
}
from app.services.report_render import build_html    # noqa: E402
evil_html = build_html(evil_data)
check('★ 动态文本已转义（无原始 <img）', '<img' not in evil_html)
check('★ 保留可见的转义文本', '&lt;img' in evil_html)
check('★ 无 <script> 注入', '<script' not in evil_html.lower())

print()
print('=' * 78)
print('模块 6c: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)

import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
"""模块 4 验证：SQL 渲染器 + 时间窗解析（DuckDB 真实执行）。

端到端验证：
    自然语言时间 → DateRange → SQL → 在真实 DuckDB 上执行 → 数值正确
"""
import sys, pathlib, datetime as dt

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import duckdb
from app.semantic.registry import SemanticRegistry
from app.semantic.renderer import SQLRenderer, RenderError, metric_catalog
from app.semantic.timerange import (
    parse_range, comparison_range, DateRange, TimeRangeError, CompareMode,
)

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)

# ===========================================================================
print('=' * 78)
print('模块 4 验证：SQL 渲染器 + 时间窗')
print('=' * 78)

# ---------- 建库 + 灌数据 ----------
con = duckdb.connect(':memory:')
con.execute((ROOT / 'semantic' / 'schema.sql').read_text(encoding='utf-8'))

con.execute("""
INSERT INTO stg_sycm_traffic
 (stat_date,stat_scope,item_id,item_title,visitors,page_views,pay_buyers,pay_items,
  pay_amount,order_buyers,order_items,order_amount,bounce_rate,stay_time,
  source_channel,cart_buyers,favorite_buyers) VALUES
 ('2026-10-01','SHOP','','',1000,2500,20,30,6000,25,32,6400,NULL,65,'TOTAL',120,60),
 ('2026-10-02','SHOP','','', 950,2300,16,24,4800,19,26,5100,NULL,62,'TOTAL',105,55),
 ('2026-10-03','SHOP','','', 800,2000,12,18,3600,15,20,3900,NULL,60,'TOTAL', 90,45);

INSERT INTO stg_trade_orders
 (order_id,sub_order_id,create_time,pay_time,ship_time,confirm_time,order_status,
  refund_status,buyer_nick,item_id,item_title,merchant_code,sku_id,sku_props,price,
  quantity,pay_amount,goods_amount,shipping_fee,discount,seller_income,receiver_province) VALUES
 ('TB001','TB001-1','2026-10-01 10:00:00','2026-10-01 10:05:00','2026-10-01 18:00:00',
  '2026-10-04 12:00:00','交易成功','无退款','b','ITEM-A','连衣裙','MC-A','SKU-1','红;M',
  200.0,30,6000.0,5900.0,100.0,0.0,6000.0,'浙江'),
 ('TB002','TB002-1','2026-10-02 09:00:00','2026-10-02 09:03:00','2026-10-02 17:00:00',
  NULL,'交易成功','退款成功','b','ITEM-A','连衣裙','MC-A','SKU-1','红;M',
  200.0,24,4800.0,4720.0,80.0,0.0,4800.0,'江苏'),
 ('TB003','TB003-1','2026-10-03 09:00:00','2026-10-03 09:05:00',NULL,
  NULL,'交易成功','无退款','b','ITEM-B','半身裙','MC-B','SKU-2','蓝;L',
  150.0,24,3600.0,3540.0,60.0,0.0,3600.0,'广东');

INSERT INTO stg_refunds
 (refund_id,order_id,sub_order_id,apply_time,refund_success_time,refund_status,
  refund_type,item_id,merchant_code,quantity,refund_amount,refund_reason,refund_stage) VALUES
 ('RF001','TB002','TB002-1','2026-10-02 20:00:00','2026-10-03 10:00:00','退款成功',
  '仅退款','ITEM-A','MC-A',2,400.0,'不想要了',NULL);

INSERT INTO stg_account_flow
 (flow_id,posted_date,trade_time,order_id,biz_type,income_expense,amount,balance,remark,counterparty) VALUES
 ('F001','2026-10-01',NULL,'TB001','天猫佣金','支出',300.0,9700.0,'','淘宝'),
 ('F002','2026-10-01',NULL,'TB001','技术服务费','支出',120.0,9580.0,'','淘宝'),
 ('F003','2026-10-02',NULL,'TB002','天猫佣金','支出',240.0,9340.0,'','淘宝'),
 ('F004','2026-10-02',NULL,'TB002','技术服务费','支出',96.0,9244.0,'','淘宝'),
 ('F005','2026-10-03',NULL,'TB003','天猫佣金','支出',180.0,9064.0,'','淘宝'),
 ('F006','2026-10-03',NULL,'TB003','技术服务费','支出',72.0,8992.0,'','淘宝');

INSERT INTO stg_ad_reports
 (stat_date,channel,campaign_id,campaign_name,item_id,impressions,clicks,ctr,spend,cpc,
  attributed_orders,attributed_items,attributed_amount,attributed_gmv,cart_adds,favorites) VALUES
 ('2026-10-01','直通车','C1','计划A','ITEM-A',50000,800,0.016,600.0,0.75,40,44,7000.0,8000.0,90,30),
 ('2026-10-02','直通车','C1','计划A','ITEM-A',48000,760,0.0158,700.0,0.92,30,33,5200.0,6000.0,80,25),
 ('2026-10-03','万相台','C2','计划B','ITEM-B',30000,500,0.0167,400.0,0.80,20,22,3000.0,3400.0,50,20);

INSERT INTO stg_purchase_batches
 (batch_id,merchant_code,item_id,purchase_date,arrival_date,quantity,unit_cost,
  freight_total,other_cost,supplier,currency) VALUES
 ('B1','MC-A','ITEM-A','2026-09-01','2026-09-10',100,80.0,400.0,0.0,'供应商甲','CNY'),
 ('B2','MC-B','ITEM-B','2026-09-05','2026-09-15',100,60.0,300.0,0.0,'供应商乙','CNY');

INSERT INTO dim_fee_mapping VALUES ('天猫佣金','COMMISSION',''),('技术服务费','TECH_SERVICE','');
""")

etl = (ROOT / 'semantic' / 'schema.sql').read_text(encoding='utf-8')
con.execute(etl[etl.index('-- ===== S5 ETL'):etl.index('-- ===== S6 指标视图')])
print('\n[0] 测试数据就绪（3 天 2 商品）')

reg = SemanticRegistry(con)
reg.load()
r = SQLRenderer(reg)

# ===========================================================================
# 1. 时间窗解析
# ===========================================================================
print('\n[1] 时间窗解析')
BASE = dt.date(2026, 10, 15)   # 周三

cases = [
    ('今天', dt.date(2026, 10, 15), dt.date(2026, 10, 15)),
    ('昨天', dt.date(2026, 10, 14), dt.date(2026, 10, 14)),
    ('本周', dt.date(2026, 10, 12), dt.date(2026, 10, 18)),   # 周一~周日
    ('上周', dt.date(2026, 10, 5),  dt.date(2026, 10, 11)),
    ('本月', dt.date(2026, 10, 1),  dt.date(2026, 10, 31)),
    ('上月', dt.date(2026, 9, 1),   dt.date(2026, 9, 30)),
    ('本季度', dt.date(2026, 10, 1), dt.date(2026, 12, 31)),
    ('上季度', dt.date(2026, 7, 1),  dt.date(2026, 9, 30)),
    ('今年', dt.date(2026, 1, 1),   dt.date(2026, 12, 31)),
    ('去年', dt.date(2025, 1, 1),   dt.date(2025, 12, 31)),
    ('最近7天', dt.date(2026, 10, 9), dt.date(2026, 10, 15)),
    ('最近30天', dt.date(2026, 9, 16), dt.date(2026, 10, 15)),
    ('2026-10', dt.date(2026, 10, 1), dt.date(2026, 10, 31)),
    ('2026-10-01', dt.date(2026, 10, 1), dt.date(2026, 10, 1)),
    ('2026-10-01~2026-10-03', dt.date(2026, 10, 1), dt.date(2026, 10, 3)),
    ('2026年', dt.date(2026, 1, 1), dt.date(2026, 12, 31)),
]
for text, exp_s, exp_e in cases:
    try:
        tr = parse_range(text, today=BASE)
        ok = (tr.start == exp_s and tr.end == exp_e)
        print(f'  [{"PASS" if ok else "FAIL"}] {text:22} -> {tr.start} ~ {tr.end} '
              f'({tr.days}天) expect {exp_s}~{exp_e}')
        if not ok:
            fails.append(f'时间窗 {text}')
    except TimeRangeError as e:
        print(f'  [FAIL] {text:22} 解析失败: {e}')
        fails.append(f'时间窗 {text}')

# 非法输入
for bad in ['上周五的下午', '', '随便什么', '2026-13-01']:
    try:
        parse_range(bad, today=BASE)
        print(f'  [FAIL] 非法输入 {bad!r} 未报错')
        fails.append(f'非法时间 {bad}')
    except (TimeRangeError, ValueError):
        print(f'  [PASS] 非法输入 {bad!r} 被拒绝')

# 对比区间
print('\n[2] 对比区间')
tr = parse_range('2026-10-01~2026-10-03', today=BASE)
prev = comparison_range(tr, CompareMode.PREVIOUS_PERIOD)
print(f'  本期: {tr.start} ~ {tr.end} ({tr.days}天)')
print(f'  环比: {prev.start} ~ {prev.end} ({prev.days}天)')
check('环比区间等长', prev.days == tr.days, f'{prev.days} vs {tr.days}')
check('环比紧邻本期', prev.end == tr.start - dt.timedelta(days=1))
yoy = comparison_range(tr, CompareMode.YEAR_OVER_YEAR)
print(f'  同比: {yoy.start} ~ {yoy.end}')
check('同比年份 -1', yoy.start.year == 2025, yoy.start)

# ===========================================================================
# 3. 渲染 + 真实执行
# ===========================================================================
print('\n[3] 渲染 + DuckDB 真实执行')

def run(label, **kw):
    q = r.render(**kw)
    rows = con.execute(q.sql, q.params).fetchall()
    return q, rows

# --- 单指标，无维度 ---
print('\n  ── M01 访客数（无维度）──')
q, rows = run('M01', metric_id='M01', time_range=None)
print(f'  SQL: {q.sql}')
print(f'  结果: {rows}')
check('M01 合计 = 2750', rows[0][0] == 2750, rows[0][0])

# --- 按日期分组 ---
print('\n  ── M01 按日期 ──')
q, rows = run('M01d', metric_id='M01', dimensions=('date',), time_range=None)
print(f'  SQL:\n    ' + q.sql.replace('\n', '\n    '))
print(f'  列: {q.columns}')
for row in rows:
    print(f'    {row}')
check('按日期返回 3 行', len(rows) == 3, len(rows))
check('列名含 date/m01', 'date' in q.columns and 'm01' in q.columns, q.columns)

# --- 比率指标 ---
print('\n  ── M03 支付转化率（比率）──')
q, rows = run('M03', metric_id='M03', time_range=None)
val = rows[0][0]
print(f'  SQL: {q.sql}')
print(f'  值: {val:.6f}')
check('M03 = 48/2750', abs(val - 48/2750) < 1e-9, val)

# --- 时间窗过滤 ---
print('\n  ── M06 支付金额（2026-10-01~02）──')
tr2 = parse_range('2026-10-01~2026-10-02', today=BASE)
q, rows = run('M06', metric_id='M06', time_range=tr2)
print(f'  SQL:\n    ' + q.sql.replace('\n', '\n    '))
print(f'  参数: {q.params}')
print(f'  值: {rows[0][0]}')
check('M06 两天合计 = 10800', rows[0][0] == 10800.0, rows[0][0])

# --- 多指标并列 ---
print('\n  ── 多指标并列（同视图）──')
q, rows = run('multi', metric_id='M10', metric_ids=('M11', 'M13'),
              dimensions=('date',), time_range=None)
print(f'  列: {q.columns}')
for row in rows:
    print(f'    {row}')
check('返回 3 个指标列', len([c for c in q.columns if c.startswith('m')]) == 3, q.columns)

# --- 维度过滤 ---
print('\n  ── 按 item_id 过滤 ──')
q, rows = run('filter', metric_id='M07', dimensions=('item_id',),
              time_range=None, filters={'item_id': 'ITEM-A'})
print(f'  结果: {rows}')
check('过滤返回 1 行', len(rows) == 1, len(rows))
check('ITEM-A 净销售 = 10400', rows[0][1] == 10400.0, rows[0][1])

# --- 跨视图指标 M17 ---
print('\n  ── M17 退款率（跨视图）──')
q, rows = run('M17', metric_id='M17', time_range=None)
print(f'  SQL:\n    ' + q.sql.replace('\n', '\n    '))
print(f'  参数: {q.params}')
print(f'  值: {rows[0][0]:.6f}')
expected = 400.0 / 14400.0
check(f'M17 = 400/14400 = {expected:.6f}', abs(rows[0][0] - expected) < 1e-9, rows[0][0])

# ===========================================================================
# 4. 安全：维度越权必须拒绝
# ===========================================================================
print('\n[4] 安全校验')

# 4a 白名单外维度
try:
    r.render(metric_id='M13', dimensions=('campaign_id',))
    check('M13 拒绝 campaign_id（不在白名单）', False, '未拒绝！')
except RenderError as e:
    check('M13 拒绝 campaign_id（不在白名单）', True, str(e)[:60])

# 4b 真正允许的维度
try:
    q = r.render(metric_id='M15', dimensions=('campaign_id',), time_range=None)
    check('M15 允许 campaign_id', True)
except RenderError as e:
    check('M15 允许 campaign_id', False, str(e)[:60])

# 4c 非法标识符（注入尝试）
for evil in ('date; DROP TABLE x', 'date--', "1=1", 'a b'):
    try:
        r.render(metric_id='M01', dimensions=(evil,))
        check(f'拒绝非法标识符 {evil!r}', False, '未拒绝！')
    except RenderError:
        check(f'拒绝非法标识符 {evil!r}', True)

# 4d 过滤字段越权
try:
    r.render(metric_id='M01', filters={'evil_col': 'x'})
    check('拒绝非白名单过滤字段', False, '未拒绝！')
except RenderError:
    check('拒绝非白名单过滤字段', True)

# 4e 跨视图并列应拒绝
try:
    r.render(metric_id='M01', metric_ids=('M06',))
    check('拒绝跨视图并列（M01+M06）', False, '未拒绝！')
except RenderError as e:
    check('拒绝跨视图并列（M01+M06）', True, str(e)[:60])

# 4f 未知指标
try:
    r.render(metric_id='M99')
    check('拒绝未知指标', False)
except RenderError:
    check('拒绝未知指标', True)

# ===========================================================================
# 5. 脚注与告警
# ===========================================================================
print('\n[5] 口径脚注与告警')
q = r.render(metric_id='M13', dimensions=('date',),
             time_range=parse_range('2026-10-01~2026-10-02', today=BASE))
print(f'  脚注: {q.footnote}')
print(f'  告警 ({len(q.warnings)} 条):')
for w in q.warnings:
    print(f'    - {w}')
check('脚注含口径说明', '财务口径' in q.footnote)
check('脚注含时间范围', '2026-10-01' in q.footnote)
check('告警含待确认项（M13 为 high）', len(q.warnings) > 0, len(q.warnings))

# 已核实口径的指标不应有告警
q2 = r.render(metric_id='M01', time_range=None)
check('M01（low 级）无告警', len(q2.warnings) == 0, q2.warnings)

# ===========================================================================
# 6. 指标目录
# ===========================================================================
print('\n[6] 指标目录（给前端/Agent）')
cat = metric_catalog(reg)
print(f'  指标数: {len(cat)}')
for c in cat[:3]:
    print(f'    {c["metric_id"]} {c["metric_name"]:16} dims={c["dimensions"]} '
          f'uncertainty={c["uncertainty_level"]}')
check('目录含 16 个指标', len(cat) == 16, len(cat))
check('目录含维度信息', all('dimensions' in c for c in cat))

print()
print('=' * 78)
print('模块 4: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)
sys.exit(1 if fails else 0)
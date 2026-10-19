"""DuckDB 端到端验证：灌入淘宝仿真数据 → ETL → 质量校验 → 指标查询。

这是语义层的完整功能验证（此前只用 SQLite 代经验证过公式）。
"""
import sys, pathlib
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import duckdb

ROOT = pathlib.Path(__file__).resolve().parents[1]
con = duckdb.connect(':memory:')
con.execute((ROOT / 'semantic' / 'schema.sql').read_text(encoding='utf-8'))

print('=' * 78)
print('DuckDB 端到端验证：淘宝数据 → ETL → 质量门 → 指标')
print('=' * 78)

# ---------- 灌入仿真数据 ----------
print('\n[1] 灌入淘宝仿真数据')
con.execute("""
INSERT INTO stg_sycm_traffic
 (stat_date, stat_scope, item_id, item_title, visitors, page_views,
  pay_buyers, pay_items, pay_amount, order_buyers, order_items, order_amount,
  bounce_rate, stay_time, source_channel, cart_buyers, favorite_buyers) VALUES
 ('2026-10-01','SHOP','','',1000,2500,20,30,6000,25,32,6400,NULL,65,'TOTAL',120,60),
 ('2026-10-02','SHOP','','', 950,2300,16,24,4800,19,26,5100,NULL,62,'TOTAL',105,55);
""")

con.execute("""
INSERT INTO stg_trade_orders
 (order_id, sub_order_id, create_time, pay_time, ship_time, confirm_time,
  order_status, refund_status, buyer_nick, item_id, item_title, merchant_code,
  sku_id, sku_props, price, quantity, pay_amount, goods_amount, shipping_fee,
  discount, seller_income, receiver_province) VALUES
 ('TB001','TB001-1','2026-10-01 10:00:00','2026-10-01 10:05:00','2026-10-01 18:00:00',
  '2026-10-04 12:00:00','交易成功','无退款','buyer_a','ITEM-A','连衣裙','MC-A','SKU-1',
  '红;M',200.0,30,6000.0,5900.0,100.0,0.0,6000.0,'浙江'),
 ('TB002','TB002-1','2026-10-02 09:00:00','2026-10-02 09:03:00','2026-10-02 17:00:00',
  NULL,'交易成功','退款成功','buyer_b','ITEM-A','连衣裙','MC-A','SKU-1',
  '红;M',200.0,24,4800.0,4720.0,80.0,0.0,4800.0,'江苏');
""")

con.execute("""
INSERT INTO stg_refunds
 (refund_id, order_id, sub_order_id, apply_time, refund_success_time,
  refund_status, refund_type, item_id, merchant_code, quantity,
  refund_amount, refund_reason, refund_stage) VALUES
 ('RF001','TB002','TB002-1','2026-10-02 20:00:00','2026-10-03 10:00:00',
  '退款成功','仅退款','ITEM-A','MC-A',2,400.0,'不想要了',NULL);
""")

con.execute("""
INSERT INTO stg_account_flow
 (flow_id, posted_date, trade_time, order_id, biz_type, income_expense,
  amount, balance, remark, counterparty) VALUES
 ('F001','2026-10-01',NULL,'TB001','天猫佣金','支出',300.0,9700.0,'','淘宝'),
 ('F002','2026-10-01',NULL,'TB001','技术服务费','支出',120.0,9580.0,'','淘宝'),
 ('F003','2026-10-02',NULL,'TB002','天猫佣金','支出',240.0,9340.0,'','淘宝'),
 ('F004','2026-10-02',NULL,'TB002','技术服务费','支出',96.0,9244.0,'','淘宝');
""")

con.execute("""
INSERT INTO stg_ad_reports
 (stat_date, channel, campaign_id, campaign_name, item_id, impressions,
  clicks, ctr, spend, cpc, attributed_orders, attributed_items,
  attributed_amount, attributed_gmv, cart_adds, favorites) VALUES
 ('2026-10-01','直通车','C1','计划A','ITEM-A',50000,800,0.016,600.0,0.75,40,44,7000.0,8000.0,90,30),
 ('2026-10-02','直通车','C1','计划A','ITEM-A',48000,760,0.0158,700.0,0.92,30,33,5200.0,6000.0,80,25);
""")

con.execute("""
INSERT INTO stg_purchase_batches
 (batch_id, merchant_code, item_id, purchase_date, arrival_date, quantity,
  unit_cost, freight_total, other_cost, supplier, currency) VALUES
 ('B1','MC-A','ITEM-A','2026-09-01','2026-09-10',100,80.0,400.0,0.0,'供应商甲','CNY');
""")

# 费用映射（真实环境需按账房实际业务类型补全）
con.execute("""
INSERT INTO dim_fee_mapping VALUES
 ('天猫佣金','COMMISSION','交易佣金'),
 ('技术服务费','TECH_SERVICE','天猫技术服务费');
""")

con.execute("""
INSERT INTO dim_item VALUES ('ITEM-A','MC-A','秋季连衣裙','SKU-1','红色;M','女装','自有','active','2026-08-01');
INSERT INTO dim_shop VALUES ('SHOP01','测试旗舰店','淘宝','女装',NULL);
INSERT INTO dim_refund_reason VALUES ('不想要了','OTHER');
""")
print('    已灌入 2 天数据')

# ---------- 重跑 ETL ----------
print('\n[2] 重跑 ETL（S5 节，幂等）')
etl = (ROOT / 'semantic' / 'schema.sql').read_text(encoding='utf-8')
i5 = etl.index('-- ===== S5 ETL')
i6 = etl.index('-- ===== S6 指标视图')
s5 = etl[i5:i6]
con.execute(s5)
print('    ETL 完成')

# ---------- 质量校验 ----------
print('\n[3] 数据质量校验（门1）')
i65 = etl.index('-- ===== S6.5 数据质量校验')
i7 = etl.index('-- ===== S7 指标注册')
con.execute(etl[i65:i7])
rows = con.execute("""
    SELECT rule_id, severity, passed, message
    FROM sem_quality_result WHERE run_id='v1-full-refresh' ORDER BY rule_id
""").fetchall()
for rid, sev, passed, msg in rows:
    mark = '✓' if passed else ('✗' if sev == 'ERROR' else '⚠')
    print(f'    {mark} [{rid}] {sev:5} {msg[:60]}')

blockers = con.execute("""
    SELECT COUNT(*) FROM sem_quality_result
    WHERE run_id='v1-full-refresh' AND severity='ERROR' AND passed=false
""").fetchone()[0]
print(f'    阻塞级失败: {blockers}')
assert blockers == 0, f'质量门未通过: {blockers} 项'

# ---------- 指标查询 ----------
print('\n[4] 指标计算结果')

def show(title, query, expect=None):
    rows = con.execute(query).fetchall()
    print(f'\n    ── {title} ──')
    for r in rows:
        print(f'      {r}')
    return rows

show('M01 访客数 / M03 支付转化率 / M05 客单价',
     "SELECT stat_date, visitors, pay_buyers, ROUND(pay_cvr,4) AS pay_cvr, "
     "ROUND(avg_order_value,2) AS aov FROM v_traffic_daily ORDER BY stat_date")

show('M06 支付金额 / M07 净销售额',
     "SELECT stat_date, pay_amount, refund_amount, net_sales "
     "FROM v_sales_daily ORDER BY stat_date")

show('M10-M14 利润链',
     "SELECT stat_date, net_sales, cogs, commission, tech_service, "
     "gross_margin, ROUND(gross_margin_rate,4) AS gm_rate, margin_after_ads "
     "FROM v_profit_daily ORDER BY stat_date")

show('M15 ROI / M16 TACOS',
     "SELECT t.stat_date, t.ad_spend, t.net_sales, ROUND(t.tacos,4) AS tacos "
     "FROM v_tacos_daily t ORDER BY stat_date")

show('M18 退款',
     "SELECT stat_date, reason_category, refund_count, refund_amount "
     "FROM v_refund_daily ORDER BY stat_date")

# ---------- 断言 ----------
print('\n[5] 断言')
fails = []
def assert_eq(label, actual, expected, tol=1e-4):
    ok = actual is not None and abs(float(actual) - float(expected)) < tol
    print(f'    [{"PASS" if ok else "FAIL"}] {label:40} {actual}  (expect {expected})')
    if not ok:
        fails.append(label)

import datetime as dt
D1, D2 = dt.date(2026, 10, 1), dt.date(2026, 10, 2)

t = {r[0]: r for r in con.execute(
    'SELECT stat_date, visitors, pay_cvr, avg_order_value FROM v_traffic_daily').fetchall()}
assert_eq('10-01 支付转化率 20/1000', t[D1][2], 0.02)
assert_eq('10-01 客单价 6000/20', t[D1][3], 300.0)

s = {r[0]: r for r in con.execute(
    'SELECT stat_date, net_sales, refund_amount FROM v_sales_daily').fetchall()}
assert_eq('10-01 净销售', s[D1][1], 6000.0)
assert_eq('10-02 退款', s[D2][2], 400.0)
assert_eq('10-02 净销售 4800-400', s[D2][1], 4400.0)

p = {r[0]: r for r in con.execute(
    'SELECT stat_date, cogs, gross_margin, gross_margin_rate FROM v_profit_daily').fetchall()}
assert_eq('10-01 COGS 30×84', p[D1][1], 2520.0)
assert_eq('10-01 毛利 6000-2520-300-120', p[D1][2], 3060.0)
assert_eq('10-01 毛利率', p[D1][3], 0.51)
assert_eq('10-02 毛利率', p[D2][3], 2048.0 / 4400.0)

r = con.execute('SELECT tacos FROM v_tacos_daily WHERE stat_date = ?', [D1]).fetchone()[0]
assert_eq('10-01 TACOS 600/6000', r, 0.1)

# 溯源
tr = con.execute("SELECT metric_id, component_cogs FROM v_metric_trace LIMIT 1").fetchone()
print(f'    [{"PASS" if tr else "FAIL"}] 溯源视图可查 metric_id={tr[0] if tr else None}')
if not tr:
    fails.append('溯源视图')

print()
print('=' * 78)
print('✅ 端到端全部通过' if not fails else f'❌ {len(fails)} 项失败: {fails}')
print('=' * 78)
sys.exit(1 if fails else 0)
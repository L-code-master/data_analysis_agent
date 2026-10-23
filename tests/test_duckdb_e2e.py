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

# ===========================================================================
print('\n[6] ★ 多商品广告费分摊对数（docs/16 P0-4）')
# 场景：同一天两个商品（A: 6000 / B: 1000，当日净销售 7000），
#       两条广告渠道（500 + 300 = 当日总花费 800）。
# 旧视图缺陷：日级广告总额直接放在每个商品行 → SUM 把 800 扣成 1600。
c2 = duckdb.connect(':memory:')
c2.execute((ROOT / 'semantic' / 'schema.sql').read_text(encoding='utf-8'))
c2.execute("""
INSERT INTO stg_trade_orders
 (order_id, sub_order_id, create_time, pay_time, ship_time, confirm_time,
  order_status, refund_status, buyer_nick, item_id, item_title, merchant_code,
  sku_id, sku_props, price, quantity, pay_amount, goods_amount, shipping_fee,
  discount, seller_income, receiver_province) VALUES
 ('TB001','TB001-1','2026-10-01 10:00:00','2026-10-01 10:05:00','2026-10-01 18:00:00',
  '2026-10-04 12:00:00','交易成功','无退款','buyer_a','ITEM-A','连衣裙','MC-A','SKU-1',
  '红;M',200.0,30,6000.0,5900.0,100.0,0.0,6000.0,'浙江'),
 ('TB003','TB003-1','2026-10-01 11:00:00','2026-10-01 11:05:00','2026-10-01 19:00:00',
  '2026-10-04 12:00:00','交易成功','无退款','buyer_c','ITEM-B','半身裙','MC-B','SKU-9',
  '黑;S',100.0,10,1000.0,1000.0,0.0,0.0,1000.0,'上海');
""")
c2.execute("""
INSERT INTO stg_ad_reports
 (stat_date, channel, campaign_id, campaign_name, item_id, impressions,
  clicks, ctr, spend, cpc, attributed_orders, attributed_items,
  attributed_amount, attributed_gmv, cart_adds, favorites) VALUES
 ('2026-10-01','直通车','C1','计划A','ITEM-A',50000,800,0.016,500.0,0.75,40,44,7000.0,8000.0,90,30),
 ('2026-10-01','万相台','C2','计划B','ITEM-B',30000,500,0.0167,300.0,0.80,20,22,3000.0,3400.0,50,20);
""")
c2.execute("""
INSERT INTO stg_account_flow
 (flow_id, posted_date, trade_time, order_id, biz_type, income_expense,
  amount, balance, remark, counterparty) VALUES
 ('F001','2026-10-01',NULL,'TB001','天猫佣金','支出',300.0,9700.0,'','淘宝'),
 ('F002','2026-10-01',NULL,'TB001','技术服务费','支出',120.0,9580.0,'','淘宝');
""")
c2.execute("""
INSERT INTO stg_purchase_batches
 (batch_id, merchant_code, item_id, purchase_date, arrival_date, quantity,
  unit_cost, freight_total, other_cost, supplier, currency) VALUES
 ('B1','MC-A','ITEM-A','2026-09-01','2026-09-10',100,80.0,400.0,0.0,'甲','CNY'),
 ('B2','MC-B','ITEM-B','2026-09-05','2026-09-15',50,40.0,0.0,0.0,'乙','CNY');
""")
c2.execute("""
INSERT INTO dim_fee_mapping VALUES
 ('天猫佣金','COMMISSION','交易佣金'), ('技术服务费','TECH_SERVICE','天猫技术服务费');
INSERT INTO dim_item VALUES ('ITEM-A','MC-A','连衣裙','SKU-1','红色;M','女装','自有','active','2026-08-01');
INSERT INTO dim_item VALUES ('ITEM-B','MC-B','半身裙','SKU-9','黑色;S','女装','自有','active','2026-08-01');
INSERT INTO dim_shop VALUES ('SHOP01','测试旗舰店','淘宝','女装',NULL);
INSERT INTO dim_refund_reason VALUES ('不想要了','OTHER');
""")
c2.execute(s5)                                   # ETL

# 手工对数：A 占 6/7、B 占 1/7；广告 800 = 500+300
ad_a, ad_b = 800 * 6000 / 7000, 800 * 1000 / 7000
rows = c2.execute(
    'SELECT item_id, ad_spend, margin_after_ads FROM v_profit_daily '
    "WHERE stat_date = DATE '2026-10-01' ORDER BY item_id").fetchall()
assert_eq('★ A 行广告分摊 800×6/7', rows[0][1], ad_a)
assert_eq('★ B 行广告分摊 800×1/7', rows[1][1], ad_b)
tot_ad = c2.execute(
    'SELECT SUM(ad_spend) FROM v_profit_daily '
    "WHERE stat_date = DATE '2026-10-01'").fetchone()[0]
assert_eq('★ 当日广告合计 = 800（旧视图会算成 1600）', tot_ad, 800.0)
tot_margin = c2.execute(
    'SELECT SUM(margin_after_ads) FROM v_profit_daily '
    "WHERE stat_date = DATE '2026-10-01'").fetchone()[0]
# 7000 - (2520+400) - 300 - 120 - 800 = 2860
assert_eq('★ 推广口径毛利合计 = 2860', tot_margin, 2860.0)
m14 = c2.execute(
    'SELECT SUM(margin_after_ads) / NULLIF(SUM(net_sales), 0) '
    "FROM v_profit_daily WHERE stat_date = DATE '2026-10-01'").fetchone()[0]
assert_eq('★ M14 口径 2860/7000', m14, 2860.0 / 7000.0)

# 升级补丁 U2 幂等重放后数字不变（存量租户库走这条路径拿到修复）
sys.path.insert(0, str(ROOT))
from app.services.diagnosis import _apply_semantic_upgrades   # noqa: E402
_apply_semantic_upgrades(c2)
tot_ad2 = c2.execute(
    'SELECT SUM(ad_spend) FROM v_profit_daily '
    "WHERE stat_date = DATE '2026-10-01'").fetchone()[0]
assert_eq('★ U2 升级重放后广告合计仍 = 800', tot_ad2, 800.0)

# ===========================================================================
print('\n[7] ★ COGS 缺批次不得静默按 0（docs/16 P1-8）')
c3 = duckdb.connect(':memory:')
c3.execute((ROOT / 'semantic' / 'schema.sql').read_text(encoding='utf-8'))
c3.execute("""
INSERT INTO stg_trade_orders
 (order_id, sub_order_id, create_time, pay_time, ship_time, confirm_time,
  order_status, refund_status, buyer_nick, item_id, item_title, merchant_code,
  sku_id, sku_props, price, quantity, pay_amount, goods_amount, shipping_fee,
  discount, seller_income, receiver_province) VALUES
 ('TB900','TB900-1','2026-10-01 10:00:00','2026-10-01 10:05:00','2026-10-01 18:00:00',
  '2026-10-04 12:00:00','交易成功','无退款','b','ITEM-X','无成本批次商品','MC-X',
  'SKU-X','红;M',100.0,10,1000.0,1000.0,0.0,0.0,1000.0,'浙江');
-- ★ 流量口径归一化反例：中文店铺级/商品级 + 合计/分渠道
INSERT INTO stg_sycm_traffic
 (stat_date, stat_scope, item_id, item_title, visitors, page_views, pay_buyers,
  pay_items, pay_amount, order_buyers, order_items, order_amount,
  bounce_rate, stay_time, source_channel, cart_buyers, favorite_buyers) VALUES
 ('2026-10-01','店铺级','','',1000,2500,10,10,1000,10,10,1000,NULL,60,'合计',10,5),
 ('2026-10-01','店铺级','','', 600,1500, 6, 6, 600, 6, 6, 600,NULL,60,'手淘搜索',6,3),
 ('2026-10-01','商品级','ITEM-X','', 500,1200, 5, 5, 500, 5, 5, 500,NULL,60,'合计',5,2);
""")
c3.execute(s5)
from app.services.diagnosis import _record_bad_amounts  # noqa: E402
_record_bad_amounts(c3, {'stg_trade_orders': {
    'pay_amount': {'count': 2, 'samples': ['abc', '12x']}}})
c3.execute(etl[etl.index('-- ===== S6.5 数据质量校验'):etl.index('-- ===== S7 指标注册')])

bad_rows = c3.execute(
    "SELECT message FROM dwd_etl_errors WHERE error_code = 'BAD_AMOUNT'"
).fetchall()
check_bad = len(bad_rows) == 1 and 'pay_amount' in bad_rows[0][0]
print(f'    [{"PASS" if check_bad else "FAIL"}] ★ 数值清洗失败落 BAD_AMOUNT: '
      f'{bad_rows}')
if not check_bad:
    fails.append('BAD_AMOUNT')
qc10 = c3.execute(
    "SELECT passed FROM sem_quality_result WHERE rule_id = 'QC-10'").fetchone()
check_qc10 = bool(qc10) and qc10[0] is False
print(f'    [{"PASS" if check_qc10 else "FAIL"}] ★ QC-10 汇总 WARN')
if not check_qc10:
    fails.append('QC-10')

scopes = {r[0] for r in c3.execute(
    'SELECT DISTINCT stat_scope FROM dwd_traffic_daily').fetchall()}
chans = {r[0] for r in c3.execute(
    'SELECT DISTINCT source_channel FROM dwd_traffic_daily').fetchall()}
check_scopes = scopes == {'SHOP', 'ITEM'}
print(f'    [{"PASS" if check_scopes else "FAIL"}] 流量口径归一化 scope={scopes}')
if not check_scopes:
    fails.append('流量口径归一化')
check_chans = chans == {'TOTAL', '手淘搜索'}
print(f'    [{"PASS" if check_chans else "FAIL"}] 渠道归一化 channel={chans}')
if not check_chans:
    fails.append('渠道归一化')

shop_total = c3.execute(
    "SELECT SUM(visitors) FROM v_traffic_daily "
    "WHERE stat_scope = 'SHOP' AND source_channel = 'TOTAL'").fetchone()[0]
assert_eq('★ 店铺 TOTAL 访客 = 1000（不叠加分渠道）', shop_total, 1000)

cogs_x = c3.execute(
    "SELECT cogs FROM v_profit_daily WHERE item_id = 'ITEM-X'").fetchone()[0]
check_cogs = cogs_x is None
print(f'    [{"PASS" if check_cogs else "FAIL"}] ★ 缺批次 COGS 为 NULL（不是 0）: '
      f'{cogs_x}')
if not check_cogs:
    fails.append('COGS 缺批次')

qc6b = c3.execute(
    "SELECT passed FROM sem_quality_result WHERE rule_id = 'QC-06b'").fetchone()
check_qc = bool(qc6b) and qc6b[0] is False
print(f'    [{"PASS" if check_qc else "FAIL"}] ★ QC-06b 拦截缺批次')
if not check_qc:
    fails.append('QC-06b')

warn = c3.execute(
    "SELECT COUNT(*) FROM dwd_etl_errors "
    "WHERE error_code = 'COGS_MISSING_BATCH' AND severity = 'WARN'").fetchone()[0]
assert_eq('缺批次已落 ETL 错误表（WARN）', warn, 1)

# ===========================================================================
print('\n[8] ★ 语义层版本化迁移（docs/16 P1-12）')
from app.services.diagnosis import _migrate_semantic_db, SEMANTIC_SCHEMA_VERSION  # noqa: E402

# 模拟「存量旧库」：无版本记录 + 旧 metric_spec + 旧维度白名单
c4 = duckdb.connect(':memory:')
c4.execute((ROOT / 'semantic' / 'schema.sql').read_text(encoding='utf-8'))
c4.execute('DELETE FROM sem_schema_version')
c4.execute("UPDATE sem_metric_registry SET metric_spec = "
           "'{\"agg\":\"SUM\",\"expr\":\"visitors\",\"unit\":\"count\"}' "
           "WHERE metric_id = 'M01'")
c4.execute("INSERT INTO sem_metric_dimension VALUES "
           "('M01','source_channel',NULL,NULL,1)")
ver = _migrate_semantic_db(c4)
check_ver = ver == SEMANTIC_SCHEMA_VERSION == 4
print(f'    [{"PASS" if check_ver else "FAIL"}] 迁移到 v{ver}')
if not check_ver:
    fails.append('语义层版本')
spec = c4.execute(
    "SELECT metric_spec FROM sem_metric_registry WHERE metric_id='M01'"
).fetchone()[0]
check_spec = 'base_filter' in spec and 'TOTAL' in spec
print(f'    [{"PASS" if check_spec else "FAIL"}] M01 老规格已升级出 base_filter')
if not check_spec:
    fails.append('M01 规格迁移')
dim_left = c4.execute(
    "SELECT COUNT(*) FROM sem_metric_dimension "
    "WHERE metric_id='M01' AND dimension_name='source_channel'").fetchone()[0]
assert_eq('M01 source_channel 维度已移除', dim_left, 0)
ver2 = _migrate_semantic_db(c4)                   # 幂等重放
check_idem = ver2 == SEMANTIC_SCHEMA_VERSION
print(f'    [{"PASS" if check_idem else "FAIL"}] 迁移幂等（重复调用无变化）')
if not check_idem:
    fails.append('迁移幂等')

print()
print('=' * 78)
print('✅ 端到端全部通过' if not fails else f'❌ {len(fails)} 项失败: {fails}')
print('=' * 78)
sys.exit(1 if fails else 0)
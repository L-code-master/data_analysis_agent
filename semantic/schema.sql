-- ============================================================================
-- 淘宝店铺经营诊断 · 语义层 v1.0（建表 + ETL + 视图 + 指标注册 + 质量校验）
-- ============================================================================
-- 适用运行时：DuckDB 1.1+（推荐，可直接读 Excel/CSV）
--             PostgreSQL 14+（TEXT 存 JSON 的列升级为 jsonb）
--
-- 平台：淘宝 / 天猫（千牛工作台导出）
--
-- 数据来源与导出路径（已核实）：
--   ① 生意参谋（流量与转化）  → stg_sycm_traffic
--      路径：千牛工作台 → 生意参谋 → 流量 / 商品 / 交易
--   ② 已卖出的宝贝（订单）      → stg_trade_orders
--      路径：千牛工作台 → 交易 → 已卖出的宝贝 → 批量导出
--   ③ 退款/售后报表            → stg_refunds
--      路径：千牛工作台 → 退款售后 → 退款管理 → 导出
--   ④ 账房/收支明细（费用真账）  → stg_account_flow
--      路径：千牛工作台 → 财务 → 账房 → 收支明细 → 导出Excel
--   ⑤ 直通车/万相台报表        → stg_ad_reports
--      路径：万相台无界版 / 直通车 → 报表 → 导出
--   ⑥ 采购/成本（自录）        → stg_purchase_batches
--   ⑦ 库存（自录或千牛库存）    → stg_inventory
--
-- ---------------------------------------------------------------------------
-- ⚠️ 重要：本文件包含两类标注，请在开发与访谈时优先处理
-- ---------------------------------------------------------------------------
--   [UNCERTAIN-n]  口径/字段不确定 —— 需要在访谈中向卖家确认，
--                  或在接入真实导出文件后核对。**不要当成已知事实使用。**
--   [ASSUMPTION-n] 为了 v1.0 能跑通而做的假设 —— 可能错，已在
--                  caliber_rules 中显式声明，Agent 回答时会带脚注提示。
--
--   全部标注清单见文件末尾「附录 A：不确定性清单」。
-- ---------------------------------------------------------------------------
-- 设计原则：
--   P1 分层：STG（原样落表）→ DWD（统一口径事实）→ DIM（维表）→ SEM（语义层）
--   P2 口径 = 数据：口径规则存 caliber_rules(JSON)，访谈后 UPDATE 即可
--   P3 维度白名单：由 sem_metric_dimension 单表控制
--   P4 全量刷新：DELETE+INSERT 重建 DWD，可重复执行
--   P5 不静默修补：数据问题落地为错误或质量失败，绝不用默认值兜底
--   P6 不确定即标注：宁可在报告里写"此口径待确认"，也不猜一个数出来
-- ============================================================================


-- ===== S1 STG 层：千牛导出文件原样落表（不做清洗，字段名对齐导出文件） =====

-- ① 生意参谋 · 流量（日粒度）
--    来源：生意参谋 → 流量 → 流量看板/商品效果 → 下载
--    [UNCERTAIN-1] 生意参谋不同版本（基础版/专业版）导出的列不完全一致；
--                  本表按「流量看板」常见列设计，实际以你手上的导出文件为准。
CREATE TABLE IF NOT EXISTS stg_sycm_traffic (
    stat_date        DATE,        -- 统计日期
    stat_scope       TEXT,        -- 统计范围：店铺 / 商品 / 单商品（生意参谋可切）
    item_id          TEXT,        -- 商品ID（stat_scope=商品 时有值）
    item_title       TEXT,
    visitors         INTEGER,     -- 访客数（去重人数）
    page_views       INTEGER,     -- 浏览量（不去重）
    pay_buyers       INTEGER,     -- 支付买家数
    pay_items        INTEGER,     -- 支付件数
    pay_amount       DOUBLE,      -- 支付金额（未剔除退款！见 M06 口径）
    order_buyers     INTEGER,     -- 下单买家数
    order_items      INTEGER,     -- 下单件数
    order_amount     DOUBLE,      -- 下单金额
    bounce_rate      DOUBLE,      -- 跳失率
    stay_time        DOUBLE,      -- 平均停留时长（秒）
    -- 流量来源拆分（生意参谋「流量来源」表，可选）
    source_channel   TEXT,        -- 手淘搜索/手淘推荐/购物车/直通车/...
    cart_buyers      INTEGER,     -- 加购人数
    favorite_buyers  INTEGER      -- 收藏人数
);

-- ② 已卖出的宝贝 · 订单（子订单粒度）
--    [UNCERTAIN-2] 「已卖出的宝贝」导出的列名与「订单报表」下载的列名略有差异
--                  （如「买家实际支付金额」vs「实收款」）。本表按常见列设计，
--                  接入真实文件后需核对【字段映射】。
CREATE TABLE IF NOT EXISTS stg_trade_orders (
    order_id           TEXT,      -- 主订单号
    sub_order_id       TEXT,      -- 子订单号（一个订单含多商品时有多个）
    create_time        TIMESTAMP, -- 订单创建时间（下单时间）
    pay_time           TIMESTAMP, -- 买家付款时间（★ 支付口径的归属时间）
    ship_time          TIMESTAMP, -- 发货时间
    confirm_time       TIMESTAMP, -- 确认收货时间
    order_status       TEXT,      -- 交易成功/交易关闭/等待买家付款/...
    refund_status      TEXT,      -- 退款状态（淘宝订单表自带，与独立退款表可能不一致）
    buyer_nick         TEXT,      -- 买家会员名（脱敏）
    item_id            TEXT,      -- 商品ID
    item_title         TEXT,
    merchant_code      TEXT,      -- 商家编码（= 你的 SKU 编码）★ 成本匹配靠它
    sku_id             TEXT,      -- SKU ID
    sku_props          TEXT,      -- SKU 属性（颜色:红;尺码:M）
    price              DOUBLE,    -- 单价
    quantity           INTEGER,   -- 购买数量
    -- 金额字段（淘宝口径，务必看清「是否含运费」「是否含优惠」）
    pay_amount         DOUBLE,    -- 买家实际支付金额（通常含运费）
    goods_amount       DOUBLE,    -- 商品金额（通常不含运费）
    shipping_fee       DOUBLE,    -- 运费
    discount           DOUBLE,    -- 优惠金额（店铺优惠+平台优惠）
    seller_income      DOUBLE,    -- 商家实收金额 [UNCERTAIN-3] 部分导出无此列
    buyer_message      TEXT,
    seller_memo        TEXT,
    receiver_province  TEXT       -- 收货省份（可用于地域维度）
);

-- ③ 退款 / 售后
CREATE TABLE IF NOT EXISTS stg_refunds (
    refund_id          TEXT,
    order_id           TEXT,
    sub_order_id       TEXT,
    apply_time         TIMESTAMP, -- 退款申请时间 ★ 退款口径的归属时间
    refund_success_time TIMESTAMP,-- 退款成功时间
    refund_status      TEXT,      -- 退款成功/退款关闭/等待卖家同意/...
    refund_type        TEXT,      -- 仅退款/退货退款/换货
    item_id            TEXT,
    merchant_code      TEXT,
    quantity           INTEGER,
    refund_amount      DOUBLE,    -- 退款金额（可能含运费）
    refund_reason      TEXT,      -- 退款原因（买家填写）
    refund_stage       TEXT       -- 售中退款 / 售后退款 [UNCERTAIN-4] 部分导出无此列
);

-- ④ 账房 · 收支明细（费用真账）
--    这是淘宝的「Settlement」等价物 —— 平台费用以实际扣款为准
CREATE TABLE IF NOT EXISTS stg_account_flow (
    flow_id            TEXT,
    posted_date        DATE,      -- 入账日期（账房记账日）★ 费用归属时间
    trade_time         TIMESTAMP, -- 交易时间（可能为空）
    order_id           TEXT,      -- 关联订单号（可能为空，如月度服务费）
    biz_type           TEXT,      -- 业务类型（见下方 [UNCERTAIN-5]）
    income_expense     TEXT,      -- 收入 / 支出
    amount             DOUBLE,    -- 金额（正负号按导出文件，ETL 统一为「支出为正」）
    balance            DOUBLE,    -- 账户余额（用于对账）
    remark             TEXT,      -- 备注
    counterparty       TEXT       -- 对方（如「淘宝」「支付宝」）
);

-- ⑤ 广告报表（直通车 / 万相台）
--    [UNCERTAIN-6] 直通车与万相台的报表列不同；万相台主打「成交金额」，
--                  直通车主打「总成交金额/总成交笔数」。归因窗口也可能不同。
CREATE TABLE IF NOT EXISTS stg_ad_reports (
    stat_date          DATE,
    channel            TEXT,      -- 直通车 / 万相台 / 引力魔方 / 淘宝客
    campaign_id        TEXT,
    campaign_name      TEXT,
    item_id            TEXT,
    impressions        INTEGER,   -- 展现量
    clicks             INTEGER,   -- 点击量
    ctr                DOUBLE,    -- 点击率
    spend              DOUBLE,    -- 花费 ★
    cpc                DOUBLE,    -- 平均点击花费
    -- 成交数据（注意归因窗口不同）
    attributed_orders  INTEGER,   -- 成交笔数
    attributed_items   INTEGER,   -- 成交件数
    attributed_amount  DOUBLE,    -- 成交金额
    attributed_gmv     DOUBLE,    -- 总成交金额（含自然流量间接成交）
    cart_adds          INTEGER,   -- 加购数
    favorites          INTEGER    -- 收藏数
);

-- ⑥ 采购 / 成本（自录，成本核算源头）
CREATE TABLE IF NOT EXISTS stg_purchase_batches (
    batch_id            TEXT,
    merchant_code       TEXT,     -- 商家编码（与订单的 merchant_code 对应）★
    item_id             TEXT,
    purchase_date       DATE,
    arrival_date        DATE,     -- 到仓日期
    quantity            INTEGER,
    unit_cost           DOUBLE,   -- 采购单价
    freight_total       DOUBLE,   -- 该批次总运费（头程/快递）
    other_cost          DOUBLE,   -- 其他成本（包装/质检）
    supplier            TEXT,
    currency            TEXT DEFAULT 'CNY'   -- 淘宝为人民币，保留列以备多平台
);

-- ⑦ 库存
CREATE TABLE IF NOT EXISTS stg_inventory (
    stat_date          DATE,
    item_id            TEXT,
    merchant_code      TEXT,
    sku_id             TEXT,
    sellable_qty       INTEGER,   -- 可售库存
    locked_qty         INTEGER,   -- 锁定库存（未发货订单占用）
    inbound_qty        INTEGER    -- 在途库存
);


-- ===== S2 DWD 层：统一口径的事实表 =====

-- 流量日汇总（店铺级 + 商品级）
CREATE TABLE IF NOT EXISTS dwd_traffic_daily (
    stat_date       DATE,
    stat_scope      TEXT,          -- SHOP / ITEM
    item_id         TEXT,          -- SHOP 时为 ''
    source_channel  TEXT DEFAULT 'TOTAL',  -- TOTAL 或具体渠道
    visitors        INTEGER,
    page_views      INTEGER,
    pay_buyers      INTEGER,
    pay_items       INTEGER,
    pay_amount      DOUBLE,        -- 支付金额（★ 未剔除退款，淘宝口径）
    order_buyers    INTEGER,
    order_items     INTEGER,
    order_amount    DOUBLE,
    cart_buyers     INTEGER,
    favorite_buyers INTEGER
);

-- 订单行（子订单粒度，人民币，无需汇率折算）
CREATE TABLE IF NOT EXISTS dwd_order_line (
    order_line_id      TEXT,       -- sub_order_id（唯一）
    order_id           TEXT,
    order_date         DATE,       -- 下单日（create_time）
    pay_date           DATE,       -- 付款日（pay_time）★ 支付口径归属
    confirm_date       DATE,       -- 确认收货日（可能为空）
    item_id            TEXT,
    merchant_code      TEXT,
    sku_id             TEXT,
    quantity           INTEGER,
    -- 金额（拆分清晰，避免口径混淆）
    pay_amount         DOUBLE,     -- 买家实际支付（含运费）
    goods_amount       DOUBLE,     -- 商品金额（不含运费）
    shipping_fee       DOUBLE,
    discount           DOUBLE,
    seller_income      DOUBLE,     -- 商家实收 [UNCERTAIN-3]
    is_closed          BOOLEAN,    -- 交易关闭（未付款/已取消）
    is_success         BOOLEAN,    -- 交易成功（确认收货）
    refund_status      TEXT,       -- 订单表自带退款状态
    receiver_province  TEXT
);

-- 退款行
CREATE TABLE IF NOT EXISTS dwd_refund_line (
    refund_line_id     TEXT,
    order_id           TEXT,
    sub_order_id       TEXT,
    apply_date         DATE,       -- 退款申请日 ★ 退款归属
    success_date       DATE,
    item_id            TEXT,
    merchant_code      TEXT,
    quantity           INTEGER,
    refund_amount      DOUBLE,
    refund_status      TEXT,
    refund_reason      TEXT,
    refund_stage       TEXT        -- BEFORE_SHIP / AFTER_SHIP [UNCERTAIN-4]
);

-- 平台费用行（账房）
CREATE TABLE IF NOT EXISTS dwd_platform_fee (
    fee_line_id        TEXT,
    posted_date        DATE,       -- 费用归属日
    order_id           TEXT,
    fee_category       TEXT,       -- COMMISSION / TECH_SERVICE / AD_FEE /
                                   -- SHIPPING_SUBSIDY / REFUND_ADJ / OTHER
    biz_type_raw       TEXT,       -- 原始业务类型（保留以便追溯）
    amount             DOUBLE,     -- 支出为正（ETL 统一符号）
    balance            DOUBLE,
    remark             TEXT
);

-- 广告日汇总
CREATE TABLE IF NOT EXISTS dwd_ad_daily (
    stat_date          DATE,
    channel            TEXT,
    campaign_id        TEXT,
    campaign_name      TEXT,
    item_id            TEXT,
    impressions        INTEGER,
    clicks             INTEGER,
    spend              DOUBLE,     -- 花费
    attributed_orders  INTEGER,
    attributed_amount  DOUBLE,     -- 成交金额（窗口见 M15 口径）
    cart_adds          INTEGER,
    favorites          INTEGER
);

-- 成本批次
CREATE TABLE IF NOT EXISTS dwd_cost_batch (
    batch_id           TEXT,
    merchant_code      TEXT,
    item_id            TEXT,
    arrival_date       DATE,
    quantity           INTEGER,
    unit_cost          DOUBLE,
    freight_per_unit   DOUBLE,
    other_per_unit     DOUBLE,
    total_unit_cost    DOUBLE      -- unit_cost + freight_per_unit + other_per_unit
);

-- 库存快照
CREATE TABLE IF NOT EXISTS dwd_inventory_snapshot (
    snapshot_date   DATE,
    item_id         TEXT,
    merchant_code   TEXT,
    sku_id          TEXT,
    sellable_qty    INTEGER,
    locked_qty      INTEGER,
    inbound_qty     INTEGER
);

-- ETL 异常落地（不静默修补）
CREATE TABLE IF NOT EXISTS dwd_etl_errors (
    error_id     TEXT,
    run_id       TEXT,
    stage        TEXT,
    severity     TEXT,        -- ERROR / WARN
    error_code   TEXT,        -- MISSING_DIM / BAD_AMOUNT / UNMAPPED_BIZ_TYPE ...
    message      TEXT,
    ref_table    TEXT,
    ref_key      TEXT,
    created_at   TIMESTAMP
);


-- ===== S3 DIM 层：维表 =====

CREATE TABLE IF NOT EXISTS dim_date (
    date          DATE PRIMARY KEY,
    year          INTEGER,
    quarter       INTEGER,
    month         INTEGER,
    week          INTEGER,        -- ISO 周
    day_of_week   INTEGER,        -- 1=周一 ... 7=周日
    is_weekend    BOOLEAN,
    is_promo_day  BOOLEAN DEFAULT FALSE,   -- 大促标记
    promo_name    TEXT            -- 618 / 双11 / 双12 / 年货节
);

-- 商品维表（淘宝：商品ID 为最细粒度，商家编码为成本匹配键）
CREATE TABLE IF NOT EXISTS dim_item (
    item_id         TEXT,
    merchant_code   TEXT,          -- 商家编码（SKU 级）
    item_title      TEXT,
    sku_id          TEXT,
    sku_props       TEXT,
    category        TEXT,          -- 类目
    brand           TEXT,
    status          TEXT DEFAULT 'active',
    launch_date     DATE
);

-- 店铺维表（淘宝单店，保留结构以支持多店）
CREATE TABLE IF NOT EXISTS dim_shop (
    shop_id         TEXT,
    shop_name       TEXT,
    shop_type       TEXT,          -- 淘宝 / 天猫
    category_main   TEXT,          -- 主营类目
    tz_note         TEXT DEFAULT '北京时间（Asia/Shanghai）'
);

-- 退款原因维表（归因诊断用）
CREATE TABLE IF NOT EXISTS dim_refund_reason (
    reason_text     TEXT,
    reason_category TEXT           -- QUALITY / SIZE / LOGISTICS / DESCRIPTION / OTHER
);
-- 种子：常见退款原因前缀（匹配规则 = 最长前缀命中，见 v_refund_daily）
INSERT INTO dim_refund_reason VALUES
    ('质量问题', 'QUALITY'),
    ('假冒', 'QUALITY'),
    ('尺寸不符', 'SIZE'),
    ('尺码', 'SIZE'),
    ('大小不符', 'SIZE'),
    ('物流', 'LOGISTICS'),
    ('未收到货', 'LOGISTICS'),
    ('描述不符', 'DESCRIPTION'),
    ('与描述不符', 'DESCRIPTION'),
    ('图片不符', 'DESCRIPTION');

-- 费用类型维表（把账房的杂乱 biz_type 归一到 fee_category）
-- [UNCERTAIN-5] 账房 biz_type 的实际取值需按真实导出文件补全映射表
CREATE TABLE IF NOT EXISTS dim_fee_mapping (
    biz_type_raw    TEXT,
    fee_category    TEXT,
    note            TEXT
);


-- ===== S4 SEM 层：指标注册表（口径 = 数据） =====

CREATE TABLE IF NOT EXISTS sem_metric_registry (
    metric_id           TEXT PRIMARY KEY,
    metric_name         TEXT,
    category            TEXT,
    priority            TEXT,

    aliases             TEXT,      -- JSON：卖家口语别名
    business_def        TEXT,      -- 给卖家看的一句话定义
    formula_view        TEXT,      -- 指标视图（SQL 真源）
    formula_expression  TEXT,      -- 人类可读公式（给卖家看）

    -- ★ 机器可执行规格（SQL 渲染器的唯一输入）JSON：
    --   {"agg":"SUM|AVG|NONE", "expr":"SQL 表达式", "unit":"percent|currency|count|ratio",
    --    "needs_base_dims":true}   -- 比率型指标：分母需按同一维度集聚合
    metric_spec         TEXT,

    time_grain          TEXT,      -- JSON：["day","week","month"]
    caliber_rules       TEXT,      -- JSON：口径规则 ★
    footnote_template   TEXT,      -- 回答时自动附带 ★
    quality_rules       TEXT,      -- JSON：质量校验规则 ID
    uncertainty         TEXT,      -- JSON：本指标的不确定项 ★★ 新增

    status              TEXT DEFAULT 'draft',   -- draft / confirmed
    source_tables       TEXT,
    version             TEXT DEFAULT 'v1.0',
    owner               TEXT,
    created_at          TIMESTAMP,
    updated_at          TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sem_metric_dimension (
    metric_id      TEXT,
    dimension_name TEXT,
    dim_table      TEXT,
    dim_key        TEXT,
    enabled        INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sem_quality_result (
    result_id    TEXT,
    run_id       TEXT,
    metric_id    TEXT,
    rule_id      TEXT,
    severity     TEXT,
    passed       BOOLEAN,
    expected     TEXT,
    actual       TEXT,
    message      TEXT,
    checked_at   TIMESTAMP
);


-- ===== S5 ETL：STG → DWD（全量刷新，可重复执行） =====

DELETE FROM dwd_etl_errors WHERE run_id = 'v1-full-refresh';

-- S5.1 流量
DELETE FROM dwd_traffic_daily;
INSERT INTO dwd_traffic_daily
SELECT stat_date,
       COALESCE(NULLIF(stat_scope, ''), 'SHOP'),
       COALESCE(item_id, ''),
       COALESCE(NULLIF(source_channel, ''), 'TOTAL'),
       visitors, page_views, pay_buyers, pay_items, pay_amount,
       order_buyers, order_items, order_amount,
       cart_buyers, favorite_buyers
FROM stg_sycm_traffic;

-- S5.2 订单行
-- 金额口径说明（[ASSUMPTION-1]）：
--   goods_amount 缺失时用 pay_amount - shipping_fee 回推，并在错误表记 WARN
DELETE FROM dwd_order_line;
INSERT INTO dwd_order_line
SELECT COALESCE(NULLIF(sub_order_id, ''), order_id),
       order_id,
       CAST(create_time AS DATE),
       CAST(pay_time AS DATE),
       CAST(confirm_time AS DATE),
       item_id,
       merchant_code,
       sku_id,
       quantity,
       pay_amount,
       COALESCE(goods_amount, pay_amount - COALESCE(shipping_fee, 0)),
       COALESCE(shipping_fee, 0),
       COALESCE(discount, 0),
       seller_income,
       (order_status IN ('交易关闭', '等待买家付款', '买家已付款待发货')
        AND pay_time IS NULL),
       (order_status = '交易成功'),
       refund_status,
       receiver_province
FROM stg_trade_orders;

-- S5.2b 记录商品金额回推的情况（口径透明度）
INSERT INTO dwd_etl_errors
SELECT 'E' || ROW_NUMBER() OVER (), 'v1-full-refresh', 'dwd_order_line', 'WARN',
       'DERIVED_GOODS_AMOUNT',
       '商品金额缺失，已用「实付-运费」回推: ' || COUNT(*) || ' 行',
       'stg_trade_orders', '', CURRENT_TIMESTAMP
FROM stg_trade_orders
WHERE goods_amount IS NULL AND pay_amount IS NOT NULL
HAVING COUNT(*) > 0;

-- S5.3 退款行
-- 退款阶段（售中/售后）判定：
--   优先用 stg_refunds.refund_stage（若导出含此列）
--   否则回退到「订单是否已确认收货」—— 需 JOIN stg_trade_orders 取 confirm_time
--   [UNCERTAIN-4] 部分导出无 refund_stage 列，此时按订单确认收货时间推断
DELETE FROM dwd_refund_line;
INSERT INTO dwd_refund_line
SELECT r.refund_id, r.order_id, r.sub_order_id,
       CAST(r.apply_time AS DATE), CAST(r.refund_success_time AS DATE),
       r.item_id, r.merchant_code, r.quantity, r.refund_amount,
       r.refund_status, r.refund_reason,
       CASE WHEN r.refund_stage IS NOT NULL AND r.refund_stage <> '' THEN r.refund_stage
            WHEN o.confirm_time IS NULL THEN 'BEFORE_SHIP'
            ELSE 'AFTER_SHIP' END
FROM stg_refunds r
LEFT JOIN (
    -- 按子订单号去重关联订单（一个子订单可能有多条退款记录）
    SELECT sub_order_id, MAX(confirm_time) AS confirm_time
    FROM stg_trade_orders
    GROUP BY sub_order_id
) o ON o.sub_order_id = r.sub_order_id;

-- S5.4 平台费用（账房）
-- 符号统一：ETL 后 amount 一律「支出为正」
DELETE FROM dwd_platform_fee;
INSERT INTO dwd_platform_fee
SELECT flow_id, posted_date, order_id,
       COALESCE((SELECT m.fee_category FROM dim_fee_mapping m
                 WHERE m.biz_type_raw = f.biz_type), 'OTHER'),
       f.biz_type,
       CASE WHEN f.income_expense = '支出' THEN ABS(f.amount) ELSE -ABS(f.amount) END,
       f.balance, f.remark
FROM stg_account_flow f;

-- S5.4b 未映射的费用类型（关键：不静默归 OTHER）
INSERT INTO dwd_etl_errors
SELECT 'E' || (100 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'dwd_platform_fee', 'WARN',
       'UNMAPPED_BIZ_TYPE',
       '账房业务类型未映射到 fee_category: ' || f.biz_type
         || ' (' || COUNT(*) || ' 笔, 合计 '
         || CAST(SUM(ABS(f.amount)) AS TEXT) || ')',
       'stg_account_flow', f.biz_type, CURRENT_TIMESTAMP
FROM stg_account_flow f
WHERE NOT EXISTS (SELECT 1 FROM dim_fee_mapping m WHERE m.biz_type_raw = f.biz_type)
GROUP BY f.biz_type;

-- S5.5 广告
DELETE FROM dwd_ad_daily;
INSERT INTO dwd_ad_daily
SELECT stat_date, channel, campaign_id, campaign_name, item_id,
       impressions, clicks, spend,
       attributed_orders, attributed_amount, cart_adds, favorites
FROM stg_ad_reports;

-- S5.6 成本批次
DELETE FROM dwd_cost_batch;
INSERT INTO dwd_cost_batch
SELECT batch_id, merchant_code, item_id, arrival_date, quantity,
       unit_cost,
       COALESCE(freight_total, 0) / NULLIF(quantity, 0),
       COALESCE(other_cost, 0) / NULLIF(quantity, 0),
       unit_cost
         + COALESCE(freight_total, 0) / NULLIF(quantity, 0)
         + COALESCE(other_cost, 0) / NULLIF(quantity, 0)
FROM stg_purchase_batches;

-- S5.6b 成本批次缺商家编码（无法与订单匹配）
INSERT INTO dwd_etl_errors
SELECT 'E' || (200 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'dwd_cost_batch', 'ERROR',
       'MISSING_MERCHANT_CODE',
       '成本批次缺少商家编码，无法与订单匹配: ' || batch_id,
       'stg_purchase_batches', batch_id, CURRENT_TIMESTAMP
FROM stg_purchase_batches WHERE merchant_code IS NULL OR merchant_code = '';

-- S5.7 库存
DELETE FROM dwd_inventory_snapshot;
INSERT INTO dwd_inventory_snapshot
SELECT stat_date, item_id, merchant_code, sku_id, sellable_qty, locked_qty, inbound_qty
FROM stg_inventory;

-- S5.8 dim_date 填充
DELETE FROM dim_date;
INSERT INTO dim_date
SELECT d::DATE, YEAR(d)::INTEGER, QUARTER(d)::INTEGER, MONTH(d)::INTEGER,
       WEEK(d)::INTEGER, ISODOW(d)::INTEGER, (ISODOW(d) >= 6), FALSE, NULL
FROM (
    SELECT UNNEST(GENERATE_SERIES(
        (SELECT COALESCE(MIN(stat_date), CURRENT_DATE) - INTERVAL 366 DAY
           FROM dwd_traffic_daily),
        (SELECT COALESCE(MAX(stat_date), CURRENT_DATE) + INTERVAL 366 DAY
           FROM dwd_traffic_daily),
        INTERVAL 1 DAY)) AS d
);


-- ===== S6 指标视图（Agent 的 SQL 真源） =====

-- 流量漏斗：日期 × 店铺/商品
-- 淘宝口径（已核实）：
--   访客数 = 去重人数
--   支付转化率 = 支付买家数 / 访客数
--   下单转化率 = 下单买家数 / 访客数
CREATE OR REPLACE VIEW v_traffic_daily AS
SELECT stat_date,
       stat_scope,
       item_id,
       source_channel,
       SUM(visitors)      AS visitors,
       SUM(page_views)    AS page_views,
       SUM(pay_buyers)    AS pay_buyers,
       SUM(pay_items)     AS pay_items,
       SUM(pay_amount)    AS pay_amount,
       SUM(order_buyers)  AS order_buyers,
       SUM(order_items)   AS order_items,
       SUM(cart_buyers)   AS cart_buyers,
       -- 支付转化率（淘宝官方口径）
       CASE WHEN SUM(visitors) > 0
            THEN CAST(SUM(pay_buyers) AS DOUBLE) / SUM(visitors) END AS pay_cvr,
       -- 下单转化率
       CASE WHEN SUM(visitors) > 0
            THEN CAST(SUM(order_buyers) AS DOUBLE) / SUM(visitors) END AS order_cvr,
       -- 客单价（淘宝口径 = 支付金额 / 支付买家数）
       CASE WHEN SUM(pay_buyers) > 0
            THEN SUM(pay_amount) / SUM(pay_buyers) END AS avg_order_value,
       -- 加购率 / 收藏率
       CASE WHEN SUM(visitors) > 0
            THEN CAST(SUM(cart_buyers) AS DOUBLE) / SUM(visitors) END AS cart_rate,
       CASE WHEN SUM(visitors) > 0
            THEN CAST(SUM(favorite_buyers) AS DOUBLE) / SUM(visitors) END AS favorite_rate
FROM dwd_traffic_daily
GROUP BY 1, 2, 3, 4;

-- 销售回款（含退款，按申请日归属）
-- [ASSUMPTION-2] 退款按「申请日」计入当期（淘宝口径常见）；
--                若卖家要求按「成功日」，改 apply_date → success_date 即可
CREATE OR REPLACE VIEW v_sales_daily AS
WITH sales AS (
    SELECT pay_date AS d, item_id, merchant_code,
           SUM(pay_amount)    AS pay_amount,      -- 买家实付（含运费）
           SUM(goods_amount)  AS goods_amount,    -- 商品金额
           SUM(shipping_fee)  AS shipping_fee,
           SUM(discount)      AS discount,
           SUM(quantity)      AS quantity
    FROM dwd_order_line
    WHERE NOT is_closed AND pay_date IS NOT NULL
    GROUP BY 1, 2, 3
), refunds AS (
    SELECT apply_date AS d, item_id, merchant_code,
           SUM(refund_amount) AS refund_amount,
           SUM(quantity)      AS refund_qty
    FROM dwd_refund_line
    WHERE refund_status = '退款成功'
    GROUP BY 1, 2, 3
)
SELECT COALESCE(s.d, r.d) AS stat_date,
       COALESCE(s.item_id, r.item_id) AS item_id,
       COALESCE(s.merchant_code, r.merchant_code) AS merchant_code,
       COALESCE(s.pay_amount, 0)    AS pay_amount,
       COALESCE(s.goods_amount, 0)  AS goods_amount,
       COALESCE(s.shipping_fee, 0)  AS shipping_fee,
       COALESCE(s.discount, 0)      AS discount,
       COALESCE(s.quantity, 0)      AS quantity,
       COALESCE(r.refund_amount, 0) AS refund_amount,
       COALESCE(r.refund_qty, 0)    AS refund_qty,
       -- 净销售额 = 实付 - 退款
       COALESCE(s.pay_amount, 0) - COALESCE(r.refund_amount, 0) AS net_sales
FROM sales s
FULL OUTER JOIN refunds r
  ON s.d = r.d AND s.item_id = r.item_id
 AND COALESCE(s.merchant_code, '') = COALESCE(r.merchant_code, '');

-- 利润链：日期 × 商品 × 商家编码
-- 费用分摊：账房费用按「当期销售额占比」分摊（同亚马逊版的 FIX-8 思路）
CREATE OR REPLACE VIEW v_profit_daily AS
WITH sales AS (
    SELECT stat_date, item_id, merchant_code,
           SUM(pay_amount) AS pay_amount,
           SUM(quantity)   AS quantity,
           SUM(net_sales)  AS net_sales
    FROM v_sales_daily GROUP BY 1, 2, 3
), sales_total AS (
    SELECT stat_date, SUM(net_sales) AS day_net_sales
    FROM v_sales_daily GROUP BY 1
), wac AS (
    -- 移动加权单位成本（按商家编码）
    SELECT merchant_code,
           SUM(total_unit_cost * quantity) / NULLIF(SUM(quantity), 0) AS wac
    FROM dwd_cost_batch GROUP BY merchant_code
), fees AS (
    -- 账房费用按日聚合（按类别）
    SELECT posted_date AS d,
           SUM(CASE WHEN fee_category = 'COMMISSION' THEN amount ELSE 0 END) AS commission,
           SUM(CASE WHEN fee_category = 'TECH_SERVICE' THEN amount ELSE 0 END) AS tech_service,
           SUM(CASE WHEN fee_category = 'SHIPPING_SUBSIDY' THEN amount ELSE 0 END) AS shipping_fee_plat,
           SUM(CASE WHEN fee_category = 'OTHER' THEN amount ELSE 0 END) AS other_fee
    FROM dwd_platform_fee GROUP BY 1
), ads AS (
    SELECT stat_date AS d, SUM(spend) AS ad_spend
    FROM dwd_ad_daily GROUP BY 1
)
SELECT s.stat_date, s.item_id, s.merchant_code,
       s.net_sales,
       s.quantity * COALESCE(w.wac, 0) AS cogs,
       -- 费用按该商品当日销售额占比分摊
       COALESCE(f.commission, 0)
         * s.net_sales / NULLIF(st.day_net_sales, 0)  AS commission,
       COALESCE(f.tech_service, 0)
         * s.net_sales / NULLIF(st.day_net_sales, 0)  AS tech_service,
       COALESCE(f.shipping_fee_plat, 0)
         * s.net_sales / NULLIF(st.day_net_sales, 0)  AS shipping_fee_plat,
       COALESCE(f.other_fee, 0)
         * s.net_sales / NULLIF(st.day_net_sales, 0)  AS other_fee,
       COALESCE(a.ad_spend, 0) AS ad_spend,
       -- 毛利（财务口径）= 净销售 - 商品成本 - 平台佣金 - 技术服务费
       s.net_sales - s.quantity * COALESCE(w.wac, 0)
         - COALESCE(f.commission, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
         - COALESCE(f.tech_service, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
         AS gross_margin,
       CASE WHEN s.net_sales > 0 THEN
         (s.net_sales - s.quantity * COALESCE(w.wac, 0)
          - COALESCE(f.commission, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
          - COALESCE(f.tech_service, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
         ) / s.net_sales END AS gross_margin_rate,
       -- 推广口径 = 财务口径再扣广告
       s.net_sales - s.quantity * COALESCE(w.wac, 0)
         - COALESCE(f.commission, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
         - COALESCE(f.tech_service, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
         - COALESCE(a.ad_spend, 0) AS margin_after_ads
FROM sales s
LEFT JOIN sales_total st ON st.stat_date = s.stat_date
LEFT JOIN wac w          ON w.merchant_code = s.merchant_code
LEFT JOIN fees f         ON f.d = s.stat_date
LEFT JOIN ads a          ON a.d = s.stat_date;

-- 广告效率：日期 × 渠道 × 计划
CREATE OR REPLACE VIEW v_ads_daily AS
SELECT stat_date, channel, campaign_id, campaign_name, item_id,
       SUM(impressions)       AS impressions,
       SUM(clicks)            AS clicks,
       SUM(spend)             AS spend,
       SUM(attributed_orders) AS attributed_orders,
       SUM(attributed_amount) AS attributed_amount,
       CASE WHEN SUM(impressions) > 0
            THEN CAST(SUM(clicks) AS DOUBLE) / SUM(impressions) END AS ctr,
       CASE WHEN SUM(clicks) > 0 THEN SUM(spend) / SUM(clicks) END AS cpc,
       CASE WHEN SUM(attributed_amount) > 0
            THEN SUM(spend) / SUM(attributed_amount) END AS acos,
       CASE WHEN SUM(attributed_orders) > 0
            THEN SUM(spend) / SUM(attributed_orders) END AS cost_per_order
FROM dwd_ad_daily
GROUP BY 1, 2, 3, 4, 5;

-- TACOS：日期（总广告花费 / 总净销售额）
CREATE OR REPLACE VIEW v_tacos_daily AS
WITH spend AS (
    SELECT stat_date AS d, SUM(spend) AS spend FROM dwd_ad_daily GROUP BY 1
), net AS (
    SELECT stat_date AS d, SUM(net_sales) AS net_sales FROM v_sales_daily GROUP BY 1
)
SELECT COALESCE(sp.d, n.d) AS stat_date,
       COALESCE(sp.spend, 0) AS ad_spend,
       COALESCE(n.net_sales, 0) AS net_sales,
       CASE WHEN COALESCE(n.net_sales, 0) > 0
            THEN COALESCE(sp.spend, 0) / n.net_sales END AS tacos
FROM spend sp FULL OUTER JOIN net n ON sp.d = n.d;

-- 退款分析：按原因归类
CREATE OR REPLACE VIEW v_refund_daily AS
SELECT apply_date AS stat_date,
       item_id,
       merchant_code,
       -- 最长前缀匹配：'质量问题-起球' 命中种子 '质量问题' → QUALITY
       COALESCE((SELECT r.reason_category FROM dim_refund_reason r
                 WHERE f.refund_reason LIKE r.reason_text || '%'
                 ORDER BY LENGTH(r.reason_text) DESC LIMIT 1), 'OTHER') AS reason_category,
       COUNT(*)                AS refund_count,
       SUM(quantity)           AS refund_qty,
       SUM(refund_amount)      AS refund_amount
FROM dwd_refund_line f
WHERE refund_status = '退款成功'
GROUP BY 1, 2, 3, 4;

-- 数字溯源（验证层门2 第4条）
CREATE OR REPLACE VIEW v_metric_trace AS
SELECT p.stat_date, p.item_id, p.merchant_code,
       'M13' AS metric_id, '毛利率（财务口径）' AS metric_name,
       p.net_sales       AS component_net_sales,
       p.cogs            AS component_cogs,
       p.commission      AS component_commission,
       p.tech_service    AS component_tech_service,
       p.gross_margin,
       p.gross_margin_rate,
       '净销售 - 商品成本 - 平台佣金 - 技术服务费' AS formula_expression,
       '退款按申请日；费用按账房实际扣款并依销售额占比分摊；成本为移动加权' AS caliber_note
FROM v_profit_daily p;


-- ===== S6.5 数据质量校验（验证层门1） =====

DELETE FROM sem_quality_result WHERE run_id = 'v1-full-refresh';

-- QC-01 整期净销售额不得为负
--   ⚠️ 修正说明：早期版本按「逐行」判定 net_sales >= 0，这是错的。
--   跨期退款是正常业务：10-01 卖出、10-02 退款成功，
--   退款按申请日归属 → 10-02 该商品没有新销售，净额自然为负。
--   真正的异常是「整期净额 < 0」（卖了一整期还倒亏），因此改为整期判定。
INSERT INTO sem_quality_result
SELECT 'Q' || ROW_NUMBER() OVER (), 'v1-full-refresh', 'M06', 'QC-01', 'ERROR',
       (COALESCE(SUM(net_sales), 0) >= 0),
       '整期净销售额 >= 0',
       CAST(COALESCE(SUM(net_sales), 0) AS TEXT),
       '整期净销售额为负（卖出总额小于退款总额），请检查数据',
       CURRENT_TIMESTAMP
FROM v_sales_daily;

-- QC-01b 单行净额大幅为负（可能是数据错误）
--   单行负值是正常的（跨期退款），但幅度不应超过该行退款额的 1.5 倍
INSERT INTO sem_quality_result
SELECT 'Q' || (50 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'M06', 'QC-01b', 'WARN',
       (COUNT(*) = 0), '|net_sales| <= refund * 1.5',
       CAST(COALESCE(MIN(net_sales), 0) AS TEXT),
       '单行净额为负且幅度异常: ' || CAST(COUNT(*) AS TEXT) || ' 行',
       CURRENT_TIMESTAMP
FROM v_sales_daily
WHERE net_sales < 0 AND ABS(net_sales) > refund_amount * 1.5;

-- QC-02 毛利率区间
INSERT INTO sem_quality_result
SELECT 'Q' || (100 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'M13', 'QC-02', 'ERROR',
       (COUNT(*) = 0), '[-1, 1]', CAST(COALESCE(MIN(gross_margin_rate), 0) AS TEXT),
       '毛利率越界: ' || CAST(COUNT(*) AS TEXT) || ' 行', CURRENT_TIMESTAMP
FROM v_profit_daily
WHERE gross_margin_rate IS NOT NULL
  AND (gross_margin_rate < -1 OR gross_margin_rate > 1);

-- QC-03 访客数为正
INSERT INTO sem_quality_result
SELECT 'Q' || (200 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'M01', 'QC-03', 'ERROR',
       (COUNT(*) = 0), '> 0', CAST(COALESCE(MIN(visitors), 0) AS TEXT),
       '访客数非正: ' || CAST(COUNT(*) AS TEXT) || ' 行', CURRENT_TIMESTAMP
FROM dwd_traffic_daily WHERE visitors IS NOT NULL AND visitors <= 0;

-- QC-04 转化率不得 > 1（淘宝官方提示：高峰时可能出现访客数 < 支付买家数）
INSERT INTO sem_quality_result
SELECT 'Q' || (300 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'M03', 'QC-04', 'WARN',
       (COUNT(*) = 0), '<= 1',
       CAST(COALESCE(MAX(pay_cvr), 0) AS TEXT),
       '支付转化率 > 100%，可能因流量高峰访客数低估（淘宝已知现象）: '
         || CAST(COUNT(*) AS TEXT) || ' 行', CURRENT_TIMESTAMP
FROM v_traffic_daily WHERE pay_cvr > 1;

-- QC-05 退款不得超过支付金额的 1.5 倍
INSERT INTO sem_quality_result
SELECT 'Q' || (400 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'M06', 'QC-05', 'ERROR',
       (COUNT(*) = 0), 'refund <= pay_amount * 1.5',
       CAST(COALESCE(MAX(refund_amount), 0) AS TEXT),
       '退款超过支付金额 1.5 倍: ' || CAST(COUNT(*) AS TEXT) || ' 行', CURRENT_TIMESTAMP
FROM v_sales_daily
WHERE pay_amount > 0 AND refund_amount > pay_amount * 1.5;

-- QC-06 商家编码缺失（成本无法匹配）
INSERT INTO sem_quality_result
SELECT 'Q' || (500 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'M11', 'QC-06', 'WARN',
       (COUNT(*) = 0), '全部有商家编码',
       CAST(COUNT(*) AS TEXT),
       '订单缺少商家编码，成本无法匹配: ' || CAST(COUNT(*) AS TEXT) || ' 行',
       CURRENT_TIMESTAMP
FROM dwd_order_line WHERE merchant_code IS NULL OR merchant_code = '';

-- QC-07 ETL 错误表 ERROR 级应为空
INSERT INTO sem_quality_result
SELECT 'Q' || (600 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'ALL', 'QC-07', 'ERROR',
       (COUNT(*) = 0), '0 条 ERROR', CAST(COUNT(*) AS TEXT),
       'ETL 存在未处理错误: ' || CAST(COUNT(*) AS TEXT) || ' 条', CURRENT_TIMESTAMP
FROM dwd_etl_errors WHERE run_id = 'v1-full-refresh' AND severity = 'ERROR';

-- QC-08 费用分摊不得重复或遗漏
INSERT INTO sem_quality_result
SELECT 'Q' || (700 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'M13', 'QC-08', 'ERROR',
       (COUNT(*) = 0), '分摊合计 = 账房总额',
       CAST(COALESCE(MAX(diff), 0) AS TEXT),
       '费用分摊出现重复或遗漏: ' || CAST(COUNT(*) AS TEXT) || ' 天', CURRENT_TIMESTAMP
FROM (
    SELECT p.stat_date,
           ABS(SUM(p.commission) - f.commission) AS diff
    FROM v_profit_daily p
    JOIN (SELECT posted_date d,
                 SUM(CASE WHEN fee_category = 'COMMISSION' THEN amount ELSE 0 END) commission
          FROM dwd_platform_fee GROUP BY 1) f ON f.d = p.stat_date
    GROUP BY 1, f.commission
) chk
WHERE diff > 0.01;

-- QC-09 账房未映射费用类型（口径透明度）
INSERT INTO sem_quality_result
SELECT 'Q' || (800 + ROW_NUMBER() OVER ()), 'v1-full-refresh', 'M13', 'QC-09', 'WARN',
       (COUNT(*) = 0), '全部已映射', CAST(COUNT(*) AS TEXT),
       '账房有 ' || CAST(COUNT(*) AS TEXT) || ' 种业务类型未映射，已归入 OTHER',
       CURRENT_TIMESTAMP
FROM dim_fee_mapping WHERE fee_category = 'OTHER';


-- ===== S7 指标注册（淘宝版 P0 指标） =====
-- 每条 caliber_rules 中如含 "uncertain": true，表示该口径未经卖家确认

DELETE FROM sem_metric_registry;
INSERT INTO sem_metric_registry
(metric_id, metric_name, category, priority, aliases, business_def,
 formula_view, formula_expression, metric_spec, time_grain, caliber_rules,
 footnote_template, quality_rules, uncertainty, status, source_tables,
 version, owner, created_at, updated_at)
VALUES

-- ---------- 流量转化 ----------
('M01','访客数','流量转化','P0',
 '["访客数","访客","UV","流量","进店人数"]',
 '访问店铺页面或宝贝详情页的去重人数，一个人在统计时间内访问多次只记一次',
 'v_traffic_daily','SUM(visitors)',
 '{"agg":"SUM","expr":"visitors","unit":"count"}',
 '["day","week","month"]',
 '{"dedup":"同一人在统计时间范围内多次访问只记一次（淘宝官方口径）","multi_terminal":"所有终端访客数 = PC端 + 无线端直接相加（非去重相加）","known_issue":"流量高峰时可能出现访客数 < 支付买家数（淘宝已知现象，交易数据快于浏览数据）"}',
 '按淘宝生意参谋口径：去重人数；多终端为分端相加',
 '["QC-03","QC-04"]',
 '{"level":"low","items":[],"note":"口径已核实，来源：生意参谋官方指标说明"}',
 'draft','["stg_sycm_traffic"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M02','浏览量','流量转化','P1',
 '["浏览量","PV","浏览次数"]',
 '店铺所有页面被访问的次数，一个人多次访问记为多次',
 'v_traffic_daily','SUM(page_views)',
 '{"agg":"SUM","expr":"page_views","unit":"count"}',
 '["day","week","month"]',
 '{"dedup":"不去重（与访客数相反）","multi_terminal":"所有终端浏览量 = PC端 + 无线端之和"}',
 '按淘宝口径：不去重的访问次数',
 '[]',
 '{"level":"low","items":[],"note":"口径已核实"}',
 'draft','["stg_sycm_traffic"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M03','支付转化率','流量转化','P0',
 '["支付转化率","转化率","CVR","成交转化率"]',
 '来访客户中最终完成支付的比例',
 'v_traffic_daily','pay_buyers / visitors',
 '{"agg":"RATIO","num_expr":"pay_buyers","num_agg":"SUM","den_expr":"visitors","den_agg":"SUM","unit":"percent"}',
 '["day","week","month"]',
 '{"numerator":"支付买家数（不是支付件数、不是订单数）","denominator":"访客数（去重人数）","vs_order_cvr":"与「下单转化率」不同：支付转化率只看最终付款","uncertain":true,"interview_pending":["是否要用下单转化率替代观察"]}',
 '支付转化率 = 支付买家数 / 访客数（淘宝官方口径）',
 '["QC-04"]',
 '{"level":"medium","items":["支付买家数的统计口径（是否含已退款买家）待确认"],"note":"公式已核实，分子口径待确认"}',
 'draft','["stg_sycm_traffic"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M04','下单转化率','流量转化','P1',
 '["下单转化率","下单率","加购转化"]',
 '来访客户中提交订单的比例（未必完成支付）',
 'v_traffic_daily','order_buyers / visitors',
 '{"agg":"RATIO","num_expr":"order_buyers","num_agg":"SUM","den_expr":"visitors","den_agg":"SUM","unit":"percent"}',
 '["day","week","month"]',
 '{"numerator":"下单买家数","denominator":"访客数","vs_pay_cvr":"与支付转化率并列观察，差值反映「下单未付款」流失"}',
 '下单转化率 = 下单买家数 / 访客数（淘宝官方口径）',
 '[]',
 '{"level":"low","items":[],"note":"口径已核实"}',
 'draft','["stg_sycm_traffic"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M05','客单价','流量转化','P1',
 '["客单价","平均订单金额","AOV","每客单价"]',
 '平均每个支付买家贡献的支付金额',
 'v_traffic_daily','pay_amount / pay_buyers',
 '{"agg":"RATIO","num_expr":"pay_amount","num_agg":"SUM","den_expr":"pay_buyers","den_agg":"SUM","unit":"currency"}',
 '["day","week","month"]',
 '{"numerator":"支付金额","denominator":"支付买家数（不是订单数）","note":"淘宝口径按「买家」而非「订单」计算"}',
 '客单价 = 支付金额 / 支付买家数（淘宝官方口径）',
 '[]',
 '{"level":"low","items":[],"note":"口径已核实"}',
 'draft','["stg_sycm_traffic"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

-- ---------- 销售回款 ----------
('M06','支付金额','销售回款','P0',
 '["支付金额","成交额","销售额","GMV","营业额"]',
 '买家拍下后通过支付宝支付给您的金额',
 'v_sales_daily','SUM(pay_amount)',
 '{"agg":"SUM","expr":"pay_amount","unit":"currency"}',
 '["day","week","month"]',
 '{"refund":"★未剔除事后退款金额（淘宝官方定义如此）","presale":"预售订单在付清尾款当天才计入","multi_terminal":"所有终端 = PC端 + 无线端之和","contains_shipping":"含运费（与「商品金额」区分）"}',
 '支付金额按淘宝口径：未剔除退款；预售付清尾款当天计入',
 '["QC-01","QC-05"]',
 '{"level":"medium","items":["「买家实际支付金额」是否含运费因导出报表而异，需核对实际文件"],"note":"定义已核实，含运费与否待核对"}',
 'draft','["stg_sycm_traffic","stg_trade_orders"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M07','净销售额','销售回款','P0',
 '["净销售额","净销售","实际收入","扣退款后"]',
 '支付金额扣除成功退款后的真实销售收入',
 'v_sales_daily','pay_amount - refund_amount',
 '{"agg":"SUM","expr":"pay_amount - refund_amount","unit":"currency"}',
 '["day","week","month"]',
 '{"refund_timing":"★退款按「申请日」计入当期（假设，访谈可改为「成功日」）","refund_scope":"仅统计 refund_status = 退款成功 的记录","uncertain":true}',
 '净销售额 = 支付金额 - 成功退款（按申请日归属）',
 '["QC-01","QC-05"]',
 '{"level":"high","items":["退款归属时间：申请日 vs 成功日 —— 未经卖家确认","是否扣除运费退款 —— 未经确认","售中退款与售后退款是否都计入 —— 未经确认"],"note":"★这是最需要访谈确认的口径"}',
 'draft','["stg_trade_orders","stg_refunds"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

-- ---------- 成本利润 ----------
('M10','平台佣金','成本利润','P0',
 '["佣金","平台佣金","扣点","天猫佣金"]',
 '淘宝/天猫按成交收取的佣金',
 'v_profit_daily','SUM(commission)',
 '{"agg":"SUM","expr":"commission","unit":"currency"}',
 '["day","week","month"]',
 '{"source":"取账房（收支明细）实际扣款，不用费率表估算","allocation":"按商品当日销售额占比分摊到商品","uncertain":true}',
 '佣金按账房实际扣款统计，依销售额占比分摊到商品',
 '["QC-08","QC-09"]',
 '{"level":"high","items":["账房「业务类型」的实际取值未知，费用归类映射表(dim_fee_mapping)需按真实导出文件补全","佣金是否含「天猫技术服务费」需确认"],"note":"★费用归类是最大不确定项"}',
 'draft','["stg_account_flow"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M11','商品成本 COGS','成本利润','P0',
 '["商品成本","成本","COGS","进货成本","货品成本"]',
 '卖出商品的采购成本加运费分摊',
 'v_profit_daily','quantity * wac',
 '{"agg":"SUM","expr":"cogs","unit":"currency"}',
 '["day","week","month"]',
 '{"cost_method":"★全批次移动加权平均（假设；严格 FIFO 待确认）","match_key":"按「商家编码」匹配成本批次","ship_qty":"用订单数量近似出库数量（跨期发货有偏差）","freight":"运费按批次数量摊到单位","uncertain":true}',
 '商品成本 = 移动加权单位成本 × 销售数量（成本按商家编码匹配）',
 '["QC-06"]',
 '{"level":"high","items":["成本计价方法（移动加权 vs FIFO vs 先进先出）未经卖家确认","商家编码是否在所有订单中都有填写 —— 待核对","出库数量用订单数量近似，跨期发货偏差未量化"],"note":"★成本口径直接决定毛利率，必须访谈确认"}',
 'draft','["stg_purchase_batches","stg_trade_orders"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M12','推广花费','成本利润','P0',
 '["推广花费","广告费","直通车花费","推广费","投放费用"]',
 '直通车/万相台等推广渠道的实际花费',
 'v_profit_daily','SUM(ad_spend)',
 '{"agg":"SUM","expr":"ad_spend","unit":"currency"}',
 '["day","week","month"]',
 '{"source":"取广告报表的「花费」列","channel":"直通车 + 万相台（多渠道路由）","attribution":"花费按发生日归属"}',
 '推广花费 = 各推广渠道当日花费之和',
 '[]',
 '{"level":"medium","items":["是否包含淘宝客佣金支出 —— 待确认","多渠道报表列名差异需核对"],"note":"花费列本身明确，渠道范围待确认"}',
 'draft','["stg_ad_reports"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M13','毛利率（财务口径）','成本利润','P0',
 '["毛利率","毛利","利润率","赚多少","利润"]',
 '净销售减去商品成本与平台费用的比例',
 'v_profit_daily','(net_sales - cogs - commission - tech_service) / net_sales',
 '{"agg":"RATIO","num_expr":"gross_margin","num_agg":"SUM","den_expr":"net_sales","den_agg":"SUM","unit":"percent"}',
 '["day","week","month"]',
 '{"refund":"退款按申请日计入当期","cogs":"移动加权（成本按商家编码匹配）","commission":"账房实际扣款，按销售额占比分摊","tech_service":"天猫技术服务费单列","excluded":"★不含广告费（见 M14）；不含运费补贴与其他费用","components":"每个扣减项单独可回溯，不做黑盒减法"}',
 '财务口径：净销售 - 商品成本 - 平台佣金 - 技术服务费；退款按申请日；不含广告费',
 '["QC-02","QC-08"]',
 '{"level":"high","items":["平台费用归类未经确认，可能遗漏或错配科目","商品成本计价方法未经确认","退款归属日期未经确认"],"note":"★三个上游口径都不确定，毛利率结论需标注"}',
 'draft','["stg_account_flow","stg_purchase_batches","stg_trade_orders","stg_refunds"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M14','毛利率（推广口径）','成本利润','P0',
 '["推广毛利率","投放后毛利","真实利润","净利率","扣广告后"]',
 '财务口径毛利率再扣除推广花费',
 'v_profit_daily','(net_sales - cogs - commission - tech_service - ad_spend) / net_sales',
 '{"agg":"RATIO","num_expr":"margin_after_ads","num_agg":"SUM","den_expr":"net_sales","den_agg":"SUM","unit":"percent"}',
 '["day","week","month"]',
 '{"ad_cost":"推广花费按发生日归属","purpose":"判断商品是真赚钱还是靠广告撑起来的虚假繁荣","note":"★这是运营决策最关键的口径"}',
 '推广口径 = 财务口径再扣当期推广花费',
 '["QC-02"]',
 '{"level":"high","items":["依赖 M13 与 M12 的全部不确定项","广告归因窗口与花费归属期可能不匹配"],"note":"上游不确定项会累积到这里"}',
 'draft','["stg_ad_reports","stg_account_flow","stg_purchase_batches"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

-- ---------- 广告效率 ----------
('M15','ROI','广告效率','P0',
 '["ROI","投产比","推广ROI","投入产出比"]',
 '推广花费带来成交金额的效率',
 'v_ads_daily','attributed_amount / spend',
 '{"agg":"RATIO","num_expr":"attributed_amount","num_agg":"SUM","den_expr":"spend","den_agg":"SUM","unit":"ratio"}',
 '["day","week","month"]',
 '{"numerator":"成交金额（广告报表口径）","denominator":"花费","attribution_window":"★归因窗口视渠道而定（直通车/万相台不同），未统一","uncertain":true}',
 'ROI = 推广成交金额 / 推广花费（归因窗口依渠道而定）',
 '[]',
 '{"level":"high","items":["各渠道归因窗口不同（可能当日/3天/15天），跨渠道汇总会失真","「成交金额」是否含间接成交（自然流量）待确认"],"note":"★跨渠道聚合前必须先统一归因窗口"}',
 'draft','["stg_ad_reports"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M16','TACOS','广告效率','P0',
 '["TACOS","广告占比","推广费占比","广告费率"]',
 '推广花费占净销售额的比例',
 'v_tacos_daily','ad_spend / net_sales',
 '{"agg":"RATIO","num_expr":"ad_spend","num_agg":"SUM","den_expr":"net_sales","den_agg":"SUM","unit":"percent"}',
 '["day","week","month"]',
 '{"numerator":"总推广花费","denominator":"净销售额（已扣退款）","note":"越低说明对广告依赖越小"}',
 'TACOS = 推广花费 / 净销售额',
 '[]',
 '{"level":"medium","items":["依赖 M07 净销售额的口径确认"],"note":"公式明确，分母口径待确认"}',
 'draft','["stg_ad_reports","stg_trade_orders"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

-- ---------- 退款 ----------
('M17','退款率','退款','P0',
 '["退款率","退货率","售后率","退款占比"]',
 '退款的金额或数量占支付的比例',
 'v_refund_daily','refund_amount / pay_amount',
 '{"agg":"CROSS_VIEW","num_view":"v_refund_daily","num_expr":"refund_amount","num_agg":"SUM","den_view":"v_sales_daily","den_expr":"pay_amount","den_agg":"SUM","unit":"percent"}',
 '["day","week","month"]',
 '{"numerator":"成功退款金额","denominator":"支付金额","scope":"仅 refund_status = 退款成功","timing":"按申请日归属","uncertain":true}',
 '退款率 = 成功退款金额 / 支付金额（按申请日归属）',
 '[]',
 '{"level":"medium","items":["是否区分售中退款（未发货）与售后退款（已收货）—— 影响归因结论","分母用支付金额还是净销售额 —— 待确认"],"note":"区分退款阶段对归因很关键"}',
 'draft','["stg_refunds","stg_trade_orders"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),

('M18','退款金额','退款','P0',
 '["退款金额","退款","退货金额","售后金额"]',
 '统计期内成功退款的金额',
 'v_refund_daily','SUM(refund_amount)',
 '{"agg":"SUM","expr":"refund_amount","unit":"currency"}',
 '["day","week","month"]',
 '{"timing":"★按退款申请时间计入当期（淘宝官方「退款金额」口径：退款申请时间在统计周期内）","scope":"仅统计退款成功的记录","contains_shipping":"退款金额可能含运费（淘宝：买家维权金额含商品货款、运费等）"}',
 '退款金额按申请时间归属当期（淘宝官方口径）',
 '["QC-05"]',
 '{"level":"medium","items":["「退款申请时间」口径已核实，但「仅统计成功退款」是本设计的取舍，淘宝报表可能含未成功退款"],"note":"归属时间已核实"}',
 'draft','["stg_refunds"]','v1.0','data-team',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP);


-- 维度白名单（单一真源）
DELETE FROM sem_metric_dimension;
INSERT INTO sem_metric_dimension (metric_id, dimension_name, dim_table, dim_key, enabled) VALUES
('M01','date','dim_date','date',1),('M01','item_id','dim_item','item_id',1),
('M01','source_channel',NULL,NULL,1),
('M02','date','dim_date','date',1),('M02','item_id','dim_item','item_id',1),
('M03','date','dim_date','date',1),('M03','item_id','dim_item','item_id',1),
('M04','date','dim_date','date',1),('M04','item_id','dim_item','item_id',1),
('M05','date','dim_date','date',1),
('M06','date','dim_date','date',1),('M06','item_id','dim_item','item_id',1),
('M06','merchant_code','dim_item','merchant_code',1),
('M07','date','dim_date','date',1),('M07','item_id','dim_item','item_id',1),
('M07','merchant_code','dim_item','merchant_code',1),
('M10','date','dim_date','date',1),('M10','item_id','dim_item','item_id',1),
('M11','date','dim_date','date',1),('M11','item_id','dim_item','item_id',1),
('M11','merchant_code','dim_item','merchant_code',1),
('M12','date','dim_date','date',1),('M12','channel',NULL,NULL,1),
('M13','date','dim_date','date',1),('M13','item_id','dim_item','item_id',1),
('M13','merchant_code','dim_item','merchant_code',1),
('M14','date','dim_date','date',1),('M14','item_id','dim_item','item_id',1),
('M15','date','dim_date','date',1),('M15','channel',NULL,NULL,1),
('M15','campaign_id',NULL,NULL,1),('M15','item_id','dim_item','item_id',1),
('M16','date','dim_date','date',1),
('M17','date','dim_date','date',1),('M17','item_id','dim_item','item_id',1),
('M17','reason_category',NULL,NULL,1),
('M18','date','dim_date','date',1),('M18','item_id','dim_item','item_id',1),
('M18','reason_category',NULL,NULL,1);

-- 一致性断言
CREATE OR REPLACE VIEW v_registry_consistency AS
SELECT metric_id, metric_name,
       CASE WHEN dimension_allowlist_legacy IS NOT NULL
            THEN 'FAIL: 旧列未废弃' ELSE 'OK' END AS check_result
FROM (SELECT metric_id, metric_name, NULL AS dimension_allowlist_legacy
      FROM sem_metric_registry)
UNION ALL
SELECT 'DIM-ORPHAN', '维度白名单指向不存在的表',
       CASE WHEN COUNT(*) = 0 THEN 'OK'
            ELSE 'FAIL: ' || CAST(COUNT(*) AS TEXT) || ' 条 dim_table 不存在' END
FROM sem_metric_dimension d
WHERE d.dim_table IS NOT NULL
  AND d.dim_table NOT IN (SELECT table_name FROM information_schema.tables);

-- Agent 一次性取指标定义
CREATE OR REPLACE VIEW v_metric_registry_json AS
SELECT r.metric_id, r.metric_name, r.category, r.priority,
       r.aliases, r.business_def, r.formula_view, r.formula_expression,
       r.time_grain, r.caliber_rules, r.footnote_template,
       r.quality_rules, r.uncertainty, r.status, r.version, r.owner,
       (SELECT STRING_AGG(d.dimension_name, ',') FROM sem_metric_dimension d
        WHERE d.metric_id = r.metric_id AND d.enabled = 1) AS allowed_dimensions
FROM sem_metric_registry r;

-- ★ 不确定项汇总视图（Agent 出报告前必查）
CREATE OR REPLACE VIEW v_uncertainty_report AS
SELECT metric_id, metric_name,
       json_extract_string(uncertainty, '$.level') AS level,
       uncertainty
FROM sem_metric_registry
WHERE json_extract_string(uncertainty, '$.level') IN ('medium', 'high')
ORDER BY CASE json_extract_string(uncertainty, '$.level')
              WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
         metric_id;


-- ============================================================================
-- 附录 A：不确定性清单（开发与访谈必读）
-- ============================================================================
--
-- 【已核实的口径】（来源：生意参谋官方指标说明、淘宝订单报表名词解释）
--   ✓ 访客数 = 去重人数；多终端为分端相加
--   ✓ 浏览量 = 不去重；多终端为分端相加
--   ✓ 支付转化率 = 支付买家数 / 访客数
--   ✓ 下单转化率 = 下单买家数 / 访客数
--   ✓ 客单价 = 支付金额 / 支付买家数
--   ✓ 支付金额 = 未剔除事后退款；预售付清尾款当天计入
--   ✓ 退款金额 = 按退款申请时间归属当期
--   ✓ 账房导出路径：千牛工作台 → 财务 → 账房 → 收支明细
--
-- 【[UNCERTAIN] 需访谈或核对真实文件确认】
--   UNCERTAIN-1  生意参谋不同版本导出列不一致（基础版/专业版）
--   UNCERTAIN-2  「已卖出的宝贝」与「订单报表」列名有差异
--   UNCERTAIN-3  商家实收金额（seller_income）部分导出无此列
--   UNCERTAIN-4  退款阶段（售中/售后）部分导出无此列
--   UNCERTAIN-5  ★账房「业务类型」的实际取值未知 —— 费用归类映射表
--                 (dim_fee_mapping) 目前只有常见几条，必须按真实文件补全
--   UNCERTAIN-6  直通车与万相台报表列不同；归因窗口可能不同
--
-- 【[ASSUMPTION] 为 v1.0 能跑通而做的假设】
--   ASSUMPTION-1  商品金额缺失时用「实付 - 运费」回推（已记 WARN）
--   ASSUMPTION-2  ★退款按「申请日」归属（可改 success_date）
--   ASSUMPTION-3  成本用「全批次移动加权平均」（非 FIFO）
--   ASSUMPTION-4  出库数量用订单数量近似（跨期发货有偏差）
--   ASSUMPTION-5  平台费用按「商品当日销售额占比」分摊到商品
--   ASSUMPTION-6  广告归因窗口跨渠道未统一，汇总时可能失真
--
-- 【访谈时必须问的 6 个问题】（按优先级）
--   1. 退款算在哪一天？申请日还是成功日？（→ M07/M18）
--   2. 商品成本怎么算？移动加权还是先进先出？（→ M11/M13）
--   3. 你的账房「业务类型」都有哪些？哪些算平台费用？（→ M10/M13）
--   4. 推广花费包含淘宝客佣金吗？（→ M12/M14）
--   5. 退款率的分母用支付金额还是净销售额？（→ M17）
--   6. 直通车/万相台的归因窗口各是多少天？（→ M15）
--
-- ============================================================================
-- 附录 B：口径变更操作（访谈后的标准动作）
-- ============================================================================
-- 场景 1：确认退款按成功日归属
--   UPDATE sem_metric_registry
--      SET caliber_rules = json_merge_patch(caliber_rules,
--            '{"refund_timing":"退款按成功日归属（访谈确认）"}'),
--          uncertainty   = '{"level":"low","items":[],"note":"访谈已确认"}'
--    WHERE metric_id IN ('M07','M18');
--   -- 同步改 v_sales_daily: apply_date → success_date
--
-- 场景 2：补全账房费用映射
--   INSERT INTO dim_fee_mapping VALUES
--     ('天猫佣金','COMMISSION','交易佣金'),
--     ('技术服务费','TECH_SERVICE','天猫技术服务费'),
--     ('...','...','...');
--
-- 场景 3：口径确认完成，状态转正
--   UPDATE sem_metric_registry
--      SET status='confirmed', version='v1.1', updated_at=CURRENT_TIMESTAMP
--    WHERE metric_id IN (...已确认的指标...);
-- ============================================================================
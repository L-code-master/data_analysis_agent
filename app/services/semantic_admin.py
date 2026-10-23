"""语义层库管理 —— 打开租户 DuckDB、版本化迁移、schema.sql 命名分段加载。

从 diagnosis.py 拆出（docs/16 P2-25）：DDL/迁移/分段加载是一类独立职责；
chat / agent_engine 直接依赖本模块，不再反向 import diagnosis。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.config import get_settings
from app.services import tenant as tn
from app.services.diag_errors import DiagnosisError

logger = logging.getLogger(__name__)


# ===========================================================================
# schema.sql 命名分段（docs/16 P2-26）
# ===========================================================================
# 诊断只重放 S5（ETL）与 S6.5（质量门）两段。原先在 run_diagnosis 里用
# 魔法字符串现场 index() 切片，锚点一改名就全诊断失败且报错难懂；
# 现在集中登记分段 + 加载时校验锚点存在（test_duckdb_e2e 有锚点守护测试）。
_SCHEMA_SEGMENTS: dict[str, tuple[str, str | None]] = {
    # name: (起始锚点, 结束锚点；None = 到文件末尾)
    'etl': ('-- ===== S5 ETL', '-- ===== S6 指标视图'),
    'quality': ('-- ===== S6.5 数据质量校验', '-- ===== S7 指标注册'),
}

# (路径, mtime, 文本) —— schema.sql 只在改动后才重读
_schema_text_cache: tuple[str, float, str] | None = None


def _schema_text() -> str:
    global _schema_text_cache
    f = get_settings().semantic_schema_file
    mtime = f.stat().st_mtime
    if _schema_text_cache and _schema_text_cache[0] == str(f) \
            and _schema_text_cache[1] == mtime:
        return _schema_text_cache[2]
    text = f.read_text(encoding='utf-8')
    _schema_text_cache = (str(f), mtime, text)
    return text


def load_schema_segment(name: str) -> str:
    """按名取 schema.sql 段落。锚点缺失立即抛错，绝不静默（P2-26）。"""
    try:
        start_anchor, end_anchor = _SCHEMA_SEGMENTS[name]
    except KeyError:
        raise ValueError(f'未登记的 schema 段: {name}') from None

    text = _schema_text()
    i = text.find(start_anchor)
    j = (text.find(end_anchor) if end_anchor else len(text))
    if i < 0 or (end_anchor and (j < 0 or j <= i)):
        raise DiagnosisError(
            f'schema.sql 段锚点缺失或顺序异常（段 {name}: '
            f'「{start_anchor}」→「{end_anchor}」）。'
            f'锚点被改动会导致诊断无法执行，请同步更新 '
            f'semantic_admin._SCHEMA_SEGMENTS', 'SCHEMA_ANCHOR_MISSING')
    return text[i:j]


def open_semantic_db(tenant_id: str):
    """打开该租户的 DuckDB 语义层库；首次建库并推进版本迁移。"""
    import duckdb

    p = tn.ensure_tenant_dirs(tenant_id)
    con = duckdb.connect(str(p.semantic_db))

    # 首次：应用 schema（schema.sql 会写入当前 SEMANTIC_SCHEMA_VERSION）
    has_table = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'sem_metric_registry'").fetchone()[0]
    if not has_table:
        schema_file = get_settings().semantic_schema_file
        con.execute(schema_file.read_text(encoding='utf-8'))

    # ★ 存量库按版本迁移（docs/16 P1-12）：视图/注册表只在建库时应用，
    #   schema 修订到不了旧库。只落后时执行，失败必须抛出 —— 不能再
    #   `except Exception: pass` 让视图停留在旧口径却照常出报告。
    migrate_semantic_db(con)
    return con


# ---------------------------------------------------------------------------
# 语义层版本化迁移（docs/16 P1-12）
# ---------------------------------------------------------------------------
# 版本号与 semantic/schema.sql 的 sem_schema_version 种子保持一致。
# 新增修订时：① 把改动写进 schema.sql（新库直接是最新口径）；
# ② 在 SEMANTIC_MIGRATIONS 追加一条（旧库按版本补丁）；
# ③ 提升 SEMANTIC_SCHEMA_VERSION 与 schema.sql 的种子版本。
SEMANTIC_SCHEMA_VERSION = 5

# 流量指标统一口径（与 schema.sql S5.1 的归一化、S7 的 metric_spec 对齐）
_TRAFFIC_BASE_FILTER = {'stat_scope': 'SHOP', 'source_channel': 'TOTAL'}
_TRAFFIC_METRIC_SPECS: dict[str, dict] = {
    'M01': {'agg': 'SUM', 'expr': 'visitors', 'unit': 'count',
            'base_filter': _TRAFFIC_BASE_FILTER},
    'M02': {'agg': 'SUM', 'expr': 'page_views', 'unit': 'count',
            'base_filter': _TRAFFIC_BASE_FILTER},
    'M03': {'agg': 'RATIO', 'num_expr': 'pay_buyers', 'num_agg': 'SUM',
            'den_expr': 'visitors', 'den_agg': 'SUM', 'unit': 'percent',
            'base_filter': _TRAFFIC_BASE_FILTER},
    'M04': {'agg': 'RATIO', 'num_expr': 'order_buyers', 'num_agg': 'SUM',
            'den_expr': 'visitors', 'den_agg': 'SUM', 'unit': 'percent',
            'base_filter': _TRAFFIC_BASE_FILTER},
    'M05': {'agg': 'RATIO', 'num_expr': 'pay_amount', 'num_agg': 'SUM',
            'den_expr': 'pay_buyers', 'den_agg': 'SUM', 'unit': 'currency',
            'base_filter': _TRAFFIC_BASE_FILTER},
}

# 利润链视图（与 schema.sql 的 v_profit_daily 保持一致）：
#   P0-4 广告费按净销售占比分摊；P1-8 缺批次时 COGS 保留 NULL。
_PROFIT_DAILY_VIEW_SQL = """CREATE OR REPLACE VIEW v_profit_daily AS
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
       -- ★ 分子分母同源（docs/16 P2-6）：SUM 跳过 NULL 行，不过滤的话
       --   缺金额批次的数量仍计入分母，WAC 被稀释偏低。
       SELECT merchant_code,
              SUM(total_unit_cost * quantity) / NULLIF(SUM(quantity), 0) AS wac
       FROM dwd_cost_batch
       WHERE total_unit_cost IS NOT NULL AND quantity > 0
       GROUP BY merchant_code
   ), fees AS (
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
          -- 缺批次保留 NULL（P1-8），绝不按 0 兜底
          s.quantity * w.wac AS cogs,
          COALESCE(f.commission, 0)
            * s.net_sales / NULLIF(st.day_net_sales, 0)  AS commission,
          COALESCE(f.tech_service, 0)
            * s.net_sales / NULLIF(st.day_net_sales, 0)  AS tech_service,
          COALESCE(f.shipping_fee_plat, 0)
            * s.net_sales / NULLIF(st.day_net_sales, 0)  AS shipping_fee_plat,
          COALESCE(f.other_fee, 0)
            * s.net_sales / NULLIF(st.day_net_sales, 0)  AS other_fee,
          COALESCE(COALESCE(a.ad_spend, 0)
                     * s.net_sales / NULLIF(st.day_net_sales, 0), 0) AS ad_spend,
          s.net_sales - s.quantity * w.wac
            - COALESCE(f.commission, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
            - COALESCE(f.tech_service, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
            AS gross_margin,
          CASE WHEN s.net_sales > 0 THEN
            (s.net_sales - s.quantity * w.wac
             - COALESCE(f.commission, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
             - COALESCE(f.tech_service, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
            ) / s.net_sales END AS gross_margin_rate,
          s.net_sales - s.quantity * w.wac
            - COALESCE(f.commission, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
            - COALESCE(f.tech_service, 0) * s.net_sales / NULLIF(st.day_net_sales, 0)
            - COALESCE(COALESCE(a.ad_spend, 0)
                         * s.net_sales / NULLIF(st.day_net_sales, 0), 0)
            AS margin_after_ads
   FROM sales s
   LEFT JOIN sales_total st ON st.stat_date = s.stat_date
   LEFT JOIN wac w          ON w.merchant_code = s.merchant_code
   LEFT JOIN fees f         ON f.d = s.stat_date
   LEFT JOIN ads a          ON a.d = s.stat_date"""


def _mig_v1(con) -> None:
    """v1：退款原因种子 + 最长前缀匹配（v1.2 归因）。"""
    con.execute(
        """INSERT INTO dim_refund_reason
           SELECT s.reason_text, s.reason_category FROM (
             VALUES ('质量问题','QUALITY'),('假冒','QUALITY'),
                    ('尺寸不符','SIZE'),('尺码','SIZE'),('大小不符','SIZE'),
                    ('物流','LOGISTICS'),('未收到货','LOGISTICS'),
                    ('描述不符','DESCRIPTION'),('与描述不符','DESCRIPTION'),
                    ('图片不符','DESCRIPTION')
           ) AS s(reason_text, reason_category)
           WHERE NOT EXISTS (
             SELECT 1 FROM dim_refund_reason r
             WHERE r.reason_text = s.reason_text)""")
    con.execute(
        """CREATE OR REPLACE VIEW v_refund_daily AS
           SELECT apply_date AS stat_date, item_id, merchant_code,
                  COALESCE((SELECT r.reason_category FROM dim_refund_reason r
                            WHERE f.refund_reason LIKE r.reason_text || '%'
                            ORDER BY LENGTH(r.reason_text) DESC LIMIT 1),
                           'OTHER') AS reason_category,
                  COUNT(*) AS refund_count, SUM(quantity) AS refund_qty,
                  SUM(refund_amount) AS refund_amount
           FROM dwd_refund_line f
           WHERE refund_status = '退款成功'
           GROUP BY 1, 2, 3, 4""")


def _mig_v2(con) -> None:
    """v2：广告费按当日净销售占比分摊（P0-4）。"""
    con.execute(_PROFIT_DAILY_VIEW_SQL)


def _mig_v3(con) -> None:
    """v3：移除 M17 维度白名单（P0-5，CROSS_VIEW 不能切片）。"""
    con.execute("DELETE FROM sem_metric_dimension WHERE metric_id = 'M17'")


def _mig_v4(con) -> None:
    """v4：COGS 缺批次保留 NULL（P1-8）+ 流量口径 base_filter（P1-9）。"""
    con.execute(_PROFIT_DAILY_VIEW_SQL)
    for mid, spec in _TRAFFIC_METRIC_SPECS.items():
        con.execute(
            'UPDATE sem_metric_registry SET metric_spec = ? '
            'WHERE metric_id = ?',
            [json.dumps(spec, ensure_ascii=False), mid])
    con.execute(
        "DELETE FROM sem_metric_dimension "
        "WHERE metric_id = 'M01' AND dimension_name = 'source_channel'")


def _mig_v5(con) -> None:
    """v5：WAC 分子分母同源过滤（P2-6）—— 缺金额批次不再稀释加权成本。"""
    con.execute(_PROFIT_DAILY_VIEW_SQL)


SEMANTIC_MIGRATIONS: list[tuple[int, str, Callable[[Any], None]]] = [
    (1, '退款原因最长前缀匹配', _mig_v1),
    (2, '广告费按净销售占比分摊（P0-4）', _mig_v2),
    (3, '移除 M17 维度白名单（P0-5）', _mig_v3),
    (4, 'COGS 缺批次保留 NULL + 流量口径 base_filter（P1-8/P1-9）', _mig_v4),
    (5, 'WAC 分子分母同源过滤（P2-6）', _mig_v5),
]


def migrate_semantic_db(con) -> int:
    """把存量租户库推进到 SEMANTIC_SCHEMA_VERSION，返回当前版本。

    ★ 失败必须抛出（docs/16 P1-12）：视图停在旧口径却照常出报告，
      比诊断失败更伤人 —— 用户看到的数字是错的却无从得知。
    """
    con.execute(
        """CREATE TABLE IF NOT EXISTS sem_schema_version (
               version      INTEGER PRIMARY KEY,
               description  TEXT,
               applied_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    row = con.execute(
        'SELECT COALESCE(MAX(version), 0) FROM sem_schema_version').fetchone()
    current = int(row[0] or 0) if row else 0
    if current >= SEMANTIC_SCHEMA_VERSION:
        return current

    for version, desc, fn in SEMANTIC_MIGRATIONS:
        if version <= current:
            continue
        try:
            fn(con)
            con.execute(
                'INSERT INTO sem_schema_version (version, description) '
                'VALUES (?, ?)', [version, desc])
        except Exception as e:              # noqa: BLE001
            raise DiagnosisError(
                f'语义层迁移 v{version}（{desc}）失败: {e}',
                'SEMANTIC_MIGRATION_FAILED') from e
        current = version
        logger.info('语义层迁移完成：v%d %s', version, desc)
    return current


def apply_semantic_upgrades(con) -> int:
    """兼容旧调用名的别名：语义层升级 = 按版本迁移。"""
    return migrate_semantic_db(con)

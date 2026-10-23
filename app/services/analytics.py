"""分析计算 —— 趋势、商品拆解、异常日、周对比、口径对比与快照。

从 diagnosis.py 拆出（docs/16 P2-25）：全部是对语义层视图的确定性聚合
（不调 LLM），输入是 DuckDB 连接与可选时间窗，输出是报告/Agent 共用的
dict 结构。qa_engine / agent_engine 直接依赖本模块，不再反向 import
diagnosis。
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# 时间窗辅助
# ---------------------------------------------------------------------------

def _tr_where(tr, col: str = 'stat_date') -> str:
    """时间范围过滤子句（参数化留白，由调用方传参）。"""
    if tr and tr.start:
        return f' WHERE {col} BETWEEN ? AND ? '
    return ' '


def _tr_params(tr) -> list:
    if tr and tr.start:
        return [str(tr.start), str(tr.end)]
    return []


def _pct(v: float) -> str:
    return f'{v * 100:.0f}%'


# ---------------------------------------------------------------------------
# 趋势
# ---------------------------------------------------------------------------

def build_trend(con, tr) -> list[dict]:
    """按日聚合净销售额与流量，供报告画趋势图。

    数据不进上下文 —— 这里只取聚合序列（每天 3 个数字），
    与"数据不进上下文"的原则一致。
    """
    try:
        sales = con.execute(
            'SELECT stat_date, SUM(net_sales) FROM v_sales_daily '
            'GROUP BY 1 ORDER BY 1').fetchall()
        traffic = con.execute(
            "SELECT stat_date, SUM(visitors), SUM(pay_buyers) "
            "FROM v_traffic_daily "
            "WHERE stat_scope = 'SHOP' AND source_channel = 'TOTAL' "
            "GROUP BY 1 ORDER BY 1").fetchall()
    except Exception:                       # noqa: BLE001
        return []

    tmap = {str(r[0]): (r[1] or 0, r[2] or 0) for r in traffic}
    smap = {str(d): float(net or 0) for d, net in sales}
    out = []
    # ★ docs/16 P2-10：以「销售 ∪ 流量」日期并集为骨架 —— 只用销售日
    #   会让零销售日整行缺失：访客被低估、异常检测对"掉到零"失明。
    #   零销售日净销售额补 0。
    for ds in sorted(set(smap) | set(tmap)):
        v, b = tmap.get(ds, (0, 0))
        out.append({
            'date': ds,
            'net_sales': round(smap.get(ds, 0.0), 2),
            'visitors': int(v),
            'pay_cvr': round(b / v, 4) if v else None,
        })
    if tr and tr.start:
        out = [t for t in out if str(tr.start) <= t['date'] <= str(tr.end)]
    return out


# ---------------------------------------------------------------------------
# 商品拆解
# ---------------------------------------------------------------------------

def build_product_breakdown(con, tr) -> list[dict]:
    """商品维度拆解 —— 回答"问题出在哪个商品"。

    全部来自既有视图的确定性聚合（不调 LLM）：
    v_sales_daily（销售/退款）+ v_profit_daily（毛利）+ v_refund_daily（原因）
    + stg_trade_orders（商品标题，DWD 未保留故回 STG 取）。
    """
    try:
        w, p = _tr_where(tr), _tr_params(tr)
        rows = con.execute(
            f'''SELECT s.item_id, s.merchant_code,
                       SUM(s.pay_amount), SUM(s.net_sales), SUM(s.refund_amount),
                       SUM(s.quantity)
                FROM v_sales_daily s {w}
                GROUP BY 1, 2 ORDER BY SUM(s.pay_amount) DESC NULLS LAST''',
            p).fetchall()

        gm = {}
        for r in con.execute(
                f'''SELECT item_id, SUM(gross_margin), SUM(net_sales)
                    FROM v_profit_daily {w} GROUP BY 1''', p).fetchall():
            gm[str(r[0])] = (float(r[1] or 0), float(r[2] or 0))

        reasons: dict[str, dict[str, float]] = {}
        for r in con.execute(
                f'''SELECT item_id, reason_category, SUM(refund_amount)
                    FROM v_refund_daily {w} GROUP BY 1, 2''', p).fetchall():
            reasons.setdefault(str(r[0]), {})[str(r[1])] = float(r[2] or 0)

        titles = {}
        try:
            for r in con.execute(
                    'SELECT item_id, ANY_VALUE(item_title) '
                    'FROM stg_trade_orders '
                    "WHERE COALESCE(item_title, '') != '' GROUP BY 1").fetchall():
                titles[str(r[0])] = str(r[1])
        except Exception:                   # noqa: BLE001  STG 可能未导入
            pass
    except Exception:                       # noqa: BLE001
        return []

    total_pay = sum(float(r[2] or 0) for r in rows) or 1.0
    total_refund = sum(float(r[4] or 0) for r in rows)
    total_net = sum(float(r[3] or 0) for r in rows) or 1.0
    total_gm = sum(gm.get(str(r[0]), (0, 0))[0] for r in rows)
    overall_refund_rate = (total_refund / total_pay) if total_pay else 0
    overall_margin = (total_gm / total_net) if total_net else 0

    out = []
    for item_id, mcode, pay, net, refund, qty in rows:
        item_id = str(item_id or '')
        pay, net, refund = float(pay or 0), float(net or 0), float(refund or 0)
        gmv, gnet = gm.get(item_id, (0.0, 0.0))
        refund_rate = refund / pay if pay else 0
        margin_rate = (gmv / gnet) if gnet else None

        flags = []
        # 阈值用"整体×1.25 且绝对值≥3%"：既防整体均值稀释（第 3 周 22% 摊平到
        # 4 周后只有 8%+，×1.5 会被整体拉高而漏报），也防小基数误报
        if pay >= total_pay * 0.03 and refund_rate > max(overall_refund_rate * 1.25, 0.03):
            flags.append('退款率偏高')
        if margin_rate is not None and gnet >= total_net * 0.03 \
                and overall_margin and margin_rate < overall_margin * 0.7:
            flags.append('毛利率偏低')
        top_reason = ''
        rs = reasons.get(item_id)
        if rs:
            top_reason = max(rs, key=rs.get)      # type: ignore[arg-type]

        out.append({
            'item_id': item_id,
            'merchant_code': str(mcode or ''),
            'title': titles.get(item_id, ''),
            'pay_amount': round(pay, 2),
            'share': round(pay / total_pay, 4),
            'net_sales': round(net, 2),
            'quantity': int(qty or 0),
            'refund_amount': round(refund, 2),
            'refund_rate': round(refund_rate, 4),
            'gross_margin': round(gmv, 2),
            'margin_rate': round(margin_rate, 4) if margin_rate is not None else None,
            'top_refund_reason': top_reason,
            'flags': flags,
        })
    return out


# ---------------------------------------------------------------------------
# 异常日检测
# ---------------------------------------------------------------------------

_ANOMALY_REL_THRESHOLD = 0.35      # 相对前 7 日基线的偏离比例


def detect_anomaly_days(trend: list[dict]) -> list[dict]:
    """异常日检测 —— 回答"哪天不对劲"。

    基线 = 前 7 日（不足则用已有的全部）均值；对净销售额 / 访客 / 转化率
    三条序列独立检测。只描述**观察到的事实特征**（确定性模板），
    不臆测原因 —— 原因提示统一用"建议核对当日投放/活动"话术。
    """
    out: list[dict] = []
    n = len(trend)
    for i in range(n):
        t = trend[i]
        base_window = trend[max(0, i - 7):i]
        if len(base_window) < 3:
            continue                      # 前几天基线不足，不判
        feats: list[str] = []

        def _rel(cur, key):
            vals = [x[key] for x in base_window if x.get(key) is not None]
            if not vals:
                return None
            avg = sum(vals) / len(vals)
            if avg <= 0:
                return None
            return (cur - avg) / avg if cur is not None else None

        for key, label, fmt in (
                ('net_sales', '净销售额', _pct),
                ('visitors', '访客数', _pct),
                ('pay_cvr', '转化率', _pct)):
            r = _rel(t.get(key), key)
            if r is not None and abs(r) >= _ANOMALY_REL_THRESHOLD:
                feats.append(f'{label}{"上升" if r > 0 else "下降"} {fmt(abs(r))}（对比前 7 日均值）')

        if not feats:
            continue

        # 组合特征 → 确定性"关注点"模板
        notes = []
        up_vis = any('访客数上升' in f for f in feats)
        down_cvr = any('转化率下降' in f for f in feats)
        down_net = any('净销售额下降' in f for f in feats)
        up_net = any('净销售额上升' in f for f in feats)
        if up_vis and (down_cvr or down_net):
            notes.append('流量放大但销售未同步 —— 建议核对当日投放计划与人群定向')
        if up_vis and up_net:
            notes.append('量额齐升 —— 若当日有大促/上新，可作为后续对比基准日')
        if down_net and not up_vis:
            notes.append('销售回落 —— 建议核对当日是否有缺货、下架或差评集中')
        if down_cvr:
            notes.append('转化率走低 —— 建议核对当日主图/价格/评价变化')

        out.append({
            'date': t['date'],
            'features': feats,
            'note': '；'.join(notes) if notes else '建议核对该日与前后日的经营动作差异',
        })
    return out


# ---------------------------------------------------------------------------
# 周度对比
# ---------------------------------------------------------------------------

def build_weekly_compare(con, tr) -> dict:
    """周度对比 —— 把"整体数字"变成"第几周、什么在变"。

    返回 {weeks: [...], biggest: {metric, last, base, delta}}。
    """
    try:
        w, p = _tr_where(tr), _tr_params(tr)
        sales = con.execute(
            f'''SELECT DATE_TRUNC('week', stat_date)::DATE wk,
                       SUM(pay_amount), SUM(net_sales), SUM(refund_amount),
                       COUNT(DISTINCT stat_date)
                FROM v_sales_daily {w} GROUP BY 1 ORDER BY 1''', p).fetchall()
        traffic_where = (f"stat_scope = 'SHOP' AND source_channel = 'TOTAL'"
                         + (' AND stat_date BETWEEN ? AND ?' if p else ''))
        traffic = con.execute(
            f'''SELECT DATE_TRUNC('week', stat_date)::DATE wk,
                       SUM(visitors), SUM(pay_buyers)
                FROM v_traffic_daily
                WHERE {traffic_where}
                GROUP BY 1 ORDER BY 1''', p).fetchall()
        ads = con.execute(
            f'''SELECT DATE_TRUNC('week', stat_date)::DATE wk, SUM(spend)
                FROM dwd_ad_daily {w} GROUP BY 1 ORDER BY 1''', p).fetchall()
    except Exception:                       # noqa: BLE001
        return {}

    tmap = {str(r[0]): (r[1] or 0, r[2] or 0) for r in traffic}
    amap = {str(r[0]): float(r[1] or 0) for r in ads}

    weeks = []
    for wk, pay, net, refund, days in sales:
        ks = str(wk)
        vis, buyers = tmap.get(ks, (0, 0))
        spend = amap.get(ks, 0.0)
        weeks.append({
            'label': ks,
            'days': int(days or 0),
            'pay_amount': round(float(pay or 0), 2),
            'net_sales': round(float(net or 0), 2),
            'visitors': int(vis or 0),
            'pay_cvr': round((buyers / vis), 4) if vis else None,
            # ★ docs/16 P2-9：pay=0 时退款率没有意义，置 None ——
            #   原先 `pay or 1` 会把退款算成 50000% 之类的天文数字。
            'refund_rate': (round(float(refund or 0) / float(pay), 4)
                            if pay else None),
            'ad_spend': round(spend, 2),
            'roi': round(float(net or 0) / spend, 2) if spend else None,
        })
    if len(weeks) < 2:
        return {'weeks': weeks, 'biggest': None}

    # 变化最大的维度：仅完整周（days ≥ 7）参与 —— 残周数据不完整，
    # 注释一直说"不参与"，原先回退分支却让它参与了（docs/16 P2-9）。
    full = [w for w in weeks if w['days'] >= 7]
    if len(full) < 2:
        return {'weeks': weeks, 'biggest': None}
    last, prev = full[-1], full[:-1]

    def _avg(key):
        vals = [w[key] for w in prev if w.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    cands = []
    for key, label, kind in (
            ('visitors', '访客数', 'abs'),
            ('pay_cvr', '转化率', 'pct'),
            ('net_sales', '净销售额', 'abs'),
            ('refund_rate', '退款率', 'pct'),
            ('roi', 'ROI', 'pct')):
        lv, bv = last.get(key), _avg(key)
        if lv is None or bv in (None, 0):
            continue
        delta = (lv - bv) / bv
        cands.append({'metric': label, 'last': lv, 'base': bv,
                      'delta': round(delta, 4), 'kind': kind})
    biggest = max(cands, key=lambda c: abs(c['delta'])) if cands else None
    return {'weeks': weeks, 'biggest': biggest}


# ---------------------------------------------------------------------------
# 口径对比与快照
# ---------------------------------------------------------------------------

def build_caliber_compare(metrics: list[dict]) -> dict:
    """L1 口径对比：财务口径 vs 推广口径毛利率并排呈现。

    两种口径本来就在指标集里（M13/M14），本函数只做"配对 + 差异解释"，
    不新增计算 —— 与"口径对比不动定义"的 L1 设计一致。
    """
    by_id = {m['metric_id']: m for m in metrics}
    m13, m14 = by_id.get('M13'), by_id.get('M14')
    if not m13 or not m14 or m13.get('value') is None or m14.get('value') is None:
        return {}
    return {
        'label': '毛利率',
        'items': [
            {'metric_id': 'M13', 'name': '财务口径',
             'desc': '扣商品成本 + 平台佣金 + 技术服务费',
             'value': m13['value'], 'unit': m13.get('unit', 'percent')},
            {'metric_id': 'M14', 'name': '推广口径',
             'desc': '财务口径基础上再扣广告花费',
             'value': m14['value'], 'unit': m14.get('unit', 'percent')},
        ],
        'diff': round(float(m13['value']) - float(m14['value']), 4),
        'note': (f'两口径相差 {_pct(max(float(m13["value"]), float(m14["value"])) - min(float(m13["value"]), float(m14["value"])))}，'
                 '即广告花费占销售的比重。看"店铺本质赚钱能力"用财务口径，'
                 '看"含投放的综合效益"用推广口径。'),
    }


def build_caliber_snapshot(con, metrics: list[dict], disclaimer: str) -> dict:
    """口径版本快照 —— 让半年后的"为什么和上期不一样"可回答。"""
    version = ''
    try:
        row = con.execute(
            'SELECT MAX(version) FROM sem_metric_registry').fetchone()
        version = str(row[0]) if row and row[0] else ''
    except Exception:                       # noqa: BLE001
        pass
    return {
        'registry_version': version,
        'defaults': '财务口径退款按申请日归属；成本为移动加权平均（详见口径说明）',
        'metric_uncertainty': {m['metric_id']: m.get('uncertainty', '')
                               for m in metrics},
        'disclaimer': disclaimer,
    }

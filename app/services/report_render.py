"""HTML 报告渲染 —— 把报告 JSON 渲染成自包含的可打印 HTML。

从 diagnosis.py 拆出（docs/16 P2-25）：纯字符串构建，只依赖标准库，
输入是 get_report_json 的 dict，输出整页 HTML。

安全纪律（docs/16 P1-17）：报告定位是"可转发/打印"，所有动态文本
（商品标题/文件名/警告/口径说明等来自 Excel 单元格的原始值）必须过 _e 转义。
"""

from __future__ import annotations

import html
from typing import Any


def _e(value: Any) -> str:
    """HTML 转义（docs/16 P1-17）：报告可转发/打印，所有动态文本必须转义。"""
    return html.escape(str(value if value is not None else ''))


def fmt_value(v: Any, unit: str) -> str:
    if v is None:
        return '—'
    if unit == 'percent' and isinstance(v, (int, float)):
        return f'{v * 100:.2f}%'
    if unit == 'currency' and isinstance(v, (int, float)):
        return f'¥{v:,.2f}'
    if isinstance(v, (int, float)):
        return f'{v:,.4g}' if isinstance(v, float) else f'{v:,}'
    return str(v)


def _svg_trend(trend: list[dict], anomaly_dates: set[str] | None = None) -> str:
    """纯字符串生成趋势 SVG（双轴：净销售额柱/线 + 访客数线 + 异常日标记）。

    不引外部 JS —— 报告是自包含的，可直接打印/转发。
    """
    if not trend or len(trend) < 2:
        return ''

    anomaly_dates = anomaly_dates or set()
    W, H = 860, 200
    PL, PR, PT, PB = 50, 56, 18, 30          # 边距
    iw, ih = W - PL - PR, H - PT - PB

    nets = [t['net_sales'] for t in trend]
    viss = [t['visitors'] for t in trend]
    nmax = max(nets) * 1.1 or 1
    vmax = max(viss) * 1.1 or 1
    n = len(trend)

    def xy(i, v, vmax_):
        x = PL + i * (iw / (n - 1))
        y = PT + ih - (v / vmax_) * ih
        return f'{x:.1f},{y:.1f}'

    # 净销售额（面积 + 折线，绿色）
    pts = ' '.join(xy(i, v, nmax) for i, v in enumerate(nets))
    area = (f'{PL},{PT + ih} ' + pts + f' {PL + iw},{PT + ih}')
    # 访客数（虚线，蓝色）
    vpts = ' '.join(xy(i, v, vmax) for i, v in enumerate(viss))

    # 异常日标记（竖虚线 + 顶部圆点）
    marks = []
    for i, t in enumerate(trend):
        if t['date'] not in anomaly_dates:
            continue
        x = PL + i * (iw / (n - 1))
        marks.append(
            f'<line x1="{x:.1f}" y1="{PT}" x2="{x:.1f}" y2="{PT + ih}" '
            f'stroke="#d46b08" stroke-width="1" stroke-dasharray="3,3" opacity="0.7"/>'
            f'<circle cx="{x:.1f}" cy="{PT - 4}" r="3" fill="#d46b08"/>')

    # x 轴标签（最多 8 个）
    step = max(1, n // 8)
    labels = []
    for i in range(0, n, step):
        x = PL + i * (iw / (n - 1))
        labels.append(
            f'<text x="{x:.0f}" y="{H - 8}" text-anchor="middle" '
            f'font-size="10" fill="#8c959f">'
            f'{_e(trend[i]["date"][5:])}</text>')

    # y 轴刻度
    yl = []
    for k in (0, 0.5, 1.0):
        y = PT + ih - k * ih
        val = nmax * k
        yl.append(f'<text x="{PL - 6}" y="{y + 4:.0f}" text-anchor="end" '
                  f'font-size="10" fill="#2c6e49">¥{val:,.0f}</text>')
        yl.append(f'<line x1="{PL}" y1="{y:.0f}" x2="{PL + iw}" y2="{y:.0f}" '
                  f'stroke="#eef1f4"/>')
    vy = PT + ih * 0.5
    yl.append(f'<text x="{PL + iw + 8}" y="{vy + 4:.0f}" font-size="10" '
              f'fill="#3b82f6">{int(vmax * 0.5):,}</text>')
    yl.append(f'<text x="{PL + iw + 8}" y="{PT + 12}" font-size="10" '
              f'fill="#3b82f6">{int(vmax):,}</text>')

    return f'''<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">
      <polygon points="{area}" fill="rgba(44,110,73,0.10)"/>
      <polyline points="{pts}" fill="none" stroke="#2c6e49" stroke-width="2.5"/>
      <polyline points="{vpts}" fill="none" stroke="#3b82f6" stroke-width="1.8"
                stroke-dasharray="5,4" opacity="0.85"/>
      {''.join(marks)}
      {''.join(yl)}
      {''.join(labels)}
    </svg>'''


def _metric_cards(metrics: list[dict]) -> str:
    """顶部四张统计卡片（挑最有故事性的指标）。"""
    by_id = {m['metric_id']: m for m in metrics}
    order = [('M06', '支付金额'), ('M07', '净销售额'),
             ('M13', '毛利率（财务）'), ('M03', '支付转化率')]
    cards = []
    for mid, label in order:
        m = by_id.get(mid)
        if not m or m.get('value') is None:
            continue
        warn = m.get('uncertainty') == 'high'
        cls = 'card warn' if warn else 'card'
        cards.append(
            f'<div class="{cls}"><div class="label">{label}</div>'
            f'<div class="value">{_e(fmt_value(m["value"], m.get("unit", "")))}</div>'
            f'<div class="formula">{_e(m.get("formula", "")) or "&nbsp;"}</div></div>')
    return f'<div class="cards">{"".join(cards)}</div>' if cards else ''


def _caliber_compare_html(cc: dict) -> str:
    """L1 口径对比区块：双口径并排 + 差异解释。"""
    if not cc:
        return ''
    items = ''.join(
        f'<div class="cc-item"><div class="cc-name">{_e(it["name"])}</div>'
        f'<div class="cc-value">{_e(fmt_value(it["value"], it.get("unit", "")))}</div>'
        f'<div class="cc-desc">{_e(it.get("desc", ""))}</div></div>'
        for it in cc['items'])
    return f'''
<h2>口径对比 · {_e(cc['label'])}</h2>
<div class="cc">{items}</div>
<p class="cc-note">{_e(cc.get('note', ''))}</p>'''


def _anomaly_html(anomalies: list[dict]) -> str:
    """异常日区块：日期 + 观察到的特征 + 建议核对方向。"""
    if not anomalies:
        return ''
    rows = []
    for a in anomalies[:8]:                 # 最多展示 8 天
        feats = '；'.join(a['features'])
        rows.append(
            f'<tr><td class="mono">{_e(a["date"][5:])}</td>'
            f'<td>{_e(feats)}</td>'
            f'<td class="note">{_e(a.get("note", ""))}</td></tr>')
    return f'''
<h2>异常日定位</h2>
<p class="sec-desc">基线 = 前 7 日均值；偏离 ≥35% 的日子。特征是观察到的事实，
核对方向是建议 —— 系统不猜测原因，请结合当日实际经营动作确认。</p>
<table><thead><tr><th style="width:70px">日期</th><th>观察到的特征</th>
<th>建议核对方向</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>'''


def _products_html(products: list[dict]) -> str:
    """商品维度区块：拆解到商品，异常项标"重点关注"。"""
    if not products:
        return ''
    rows = []
    for p in products[:12]:
        flags = ('<span class="flag">重点关注</span>' if p['flags'] else '')
        reasons = (f'<span class="reason">{p["top_refund_reason"]}</span>'
                   if p['top_refund_reason'] and p['refund_amount'] > 0 else '—')
        title = _e(p['title'] or p['merchant_code'] or p['item_id'])
        rows.append(
            f'<tr><td>{title}{flags}</td>'
            f'<td class="num">{_e(fmt_value(p["share"], "percent"))}</td>'
            f'<td class="num">¥{p["pay_amount"]:,.0f}</td>'
            f'<td class="num">¥{p["net_sales"]:,.0f}</td>'
            f'<td class="num">{_e(fmt_value(p["refund_rate"], "percent"))}</td>'
            f'<td>{_e(reasons)}</td>'
            f'<td class="num">{_e(fmt_value(p["margin_rate"], "percent"))}</td></tr>')
    flagged = [p for p in products if p['flags']]
    hint = ''
    if flagged:
        names = _e('、'.join((p['title'] or p['merchant_code'] or p['item_id'])
                              for p in flagged))
        reasons_txt = _e('；'.join(sorted({f for p in flagged for f in p['flags']})))
        hint = (f'<p class="sec-desc">⚠ 重点关注：{names} —— {reasons_txt}。'
                f'退款原因与商品明细见「退款售后明细」数据源。</p>')
    return f'''
<h2>商品维度拆解</h2>{hint}
<table><thead><tr><th>商品</th><th style="text-align:right">销售占比</th>
<th style="text-align:right">支付金额</th><th style="text-align:right">净销售额</th>
<th style="text-align:right">退款率</th><th>主要退款原因</th>
<th style="text-align:right">毛利率</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>'''


def _weekly_html(weekly: dict) -> str:
    """周度对比区块：末周 vs 之前均值，突出变化最大的维度。"""
    weeks = (weekly or {}).get('weeks') or []
    if len(weeks) < 2:
        return ''
    biggest = (weekly or {}).get('biggest')

    def wk_label(iso: str, days: int) -> str:
        suffix = f'（{days} 天，不完整）' if days < 7 else ''
        return f'{iso[5:]}起{suffix}'

    rows = []
    for w in weeks:
        cvr = (f'{w["pay_cvr"] * 100:.2f}%' if w.get('pay_cvr') is not None else '—')
        roi = f'{w["roi"]:.1f}' if w.get('roi') is not None else '—'
        rows.append(
            f'<tr><td class="mono">{_e(wk_label(w["label"], w.get("days", 7)))}</td>'
            f'<td class="num">{w["visitors"]:,}</td>'
            f'<td class="num">{cvr}</td>'
            f'<td class="num">¥{w["net_sales"]:,.0f}</td>'
            f'<td class="num">{w["refund_rate"] * 100:.2f}%</td>'
            f'<td class="num">¥{w["ad_spend"]:,.0f}</td>'
            f'<td class="num">{roi}</td></tr>')

    summary = ''
    if biggest:
        k = biggest['kind']
        if k == 'pct':
            last_s = f'{biggest["last"] * 100:.2f}%'
            base_s = f'{biggest["base"] * 100:.2f}%'
        else:
            last_s = (f'{biggest["last"]:,}')
            base_s = f'{biggest["base"]:,}'
        arrow = '↑' if biggest['delta'] > 0 else '↓'
        summary = (f'<p class="sec-desc">变化最大的维度：<b>{biggest["metric"]}</b> '
                   f'（末周 {last_s} vs 之前均值 {base_s}，'
                   f'{arrow} {abs(biggest["delta"]) * 100:.0f}%）</p>')
    return f'''
<h2>周度对比</h2>{summary}
<table><thead><tr><th>周（周一起）</th><th style="text-align:right">访客</th>
<th style="text-align:right">转化率</th><th style="text-align:right">净销售额</th>
<th style="text-align:right">退款率</th><th style="text-align:right">广告花费</th>
<th style="text-align:right">ROI</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>'''


def build_html(data: dict) -> str:
    """生成 HTML 报告（v1.2：归因区块 + 口径对比 + 异常日 + 商品/周拆解）。"""
    metrics = data.get('metrics', [])
    trend = data.get('trend') or []
    anomalies = data.get('anomaly_days') or []
    products = data.get('products') or []
    weekly = data.get('weekly') or {}
    cc = data.get('caliber_compare') or {}
    caliber = data.get('caliber') or {}

    # 指标表
    rows = []
    for m in metrics:
        lvl = m.get('uncertainty', 'low')
        badge = ('<span class="badge warn">待确认</span>' if lvl == 'high' else
                 ('<span class="badge mid">部分待确认</span>'
                  if lvl == 'medium' else
                  '<span class="badge ok">已核实</span>'))
        rows.append(
            f'<tr><td>{_e(m.get("metric_name",""))}</td>'
            f'<td class="num">{_e(fmt_value(m.get("value"), m.get("unit","")))}</td>'
            f'<td class="formula">{_e(m.get("formula",""))}</td>'
            f'<td>{badge}</td></tr>')

    # ★ 警告文本可能含 Excel 原始内容（商家编码、清洗失败样例），必须转义
    #   —— 完整报告 XSS 加固见 docs/16 P1-17（批次 3）
    warns = ''.join(f'<li>{html.escape(str(w))}</li>'
                    for w in data.get('warnings', []))
    footnote = _e(data.get('footnote', '') or
                  '所有指标口径均已核实。').replace('\n', '<br>')
    src = data.get('source', {})
    tr = data.get('time_range') or {}
    period = _e(f"{tr.get('start', '')} ~ {tr.get('end', '')}"
                if tr.get('start') else '全部数据')
    anomaly_dates = {a['date'] for a in anomalies}
    trend_svg = _svg_trend(trend, anomaly_dates)
    cards = _metric_cards(metrics)
    files = '、'.join(
        f'{_e(f.get("name", ""))}({int(f.get("rows", 0) or 0)}行)'
        for f in src.get('files', [])[:6])

    caliber_line = ''
    if caliber.get('registry_version'):
        caliber_line = (f'<span>口径版本 <b>{_e(caliber["registry_version"])}</b>'
                        f'（本次计算快照已随报告存档）</span>')

    legend_extra = ('<span><span class="k a">▍</span>异常日</span>'
                    if anomaly_dates else '')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>经营诊断报告</title>
<style>
 *{{box-sizing:border-box}}
 body{{font-family:-apple-system,"Microsoft YaHei","PingFang SC",sans-serif;
      max-width:960px;margin:0 auto;padding:32px 20px;color:#1f2328;line-height:1.65;
      background:#fafbfc}}
 .sheet{{background:#fff;border-radius:14px;padding:32px 36px;
        box-shadow:0 1px 4px rgba(0,0,0,.06)}}
 h1{{font-size:22px;margin:0 0 4px;color:#1a3d2b}}
 .sub{{color:#8c959f;font-size:13px;margin-bottom:22px}}
 .meta{{display:flex;flex-wrap:wrap;gap:12px 28px;font-size:13px;color:#57606a;
       background:#f6f8fa;padding:14px 18px;border-radius:8px;margin-bottom:24px}}
 .meta b{{color:#1f2328}}
 .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
        gap:14px;margin:0 0 28px}}
 .card{{border:1px solid #e5e7eb;border-radius:10px;padding:16px 18px}}
 .card.warn{{border-color:#f3c8c2;background:#fffaf9}}
 .card .label{{font-size:12px;color:#8c959f;margin-bottom:4px}}
 .card .value{{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums;
              color:#1a3d2b}}
 .card.warn .value{{color:#b3412f}}
 .card .formula{{font-family:ui-monospace,Consolas,monospace;font-size:11px;
                color:#8c959f;margin-top:6px;min-height:14px}}
 .cc{{display:flex;gap:14px;margin:6px 0 4px;flex-wrap:wrap}}
 .cc-item{{flex:1;min-width:220px;border:1px solid #d4e8da;background:#f0f9f1;
          border-radius:10px;padding:14px 18px}}
 .cc-item .cc-name{{font-size:13px;color:#2c6e49;font-weight:600}}
 .cc-item .cc-value{{font-size:30px;font-weight:650;color:#1a3d2b;
                    font-variant-numeric:tabular-nums;margin:2px 0}}
 .cc-item .cc-desc{{font-size:12px;color:#8c959f}}
 .cc-note{{font-size:13px;color:#57606a;margin:8px 0 0}}
 h2{{margin:34px 0 12px;color:#1a3d2b;font-size:17px;
    display:flex;align-items:center;gap:8px}}
 h2::before{{content:"";display:inline-block;width:4px;height:18px;
            background:#2c6e49;border-radius:2px}}
 .sec-desc{{font-size:13px;color:#57606a;margin:0 0 10px}}
 .legend{{font-size:12px;color:#8c959f;margin:-6px 0 10px;display:flex;gap:20px;
         flex-wrap:wrap}}
 .legend .k{{display:inline-block;width:22px;height:0;border-top:3px solid #2c6e49;
            vertical-align:middle;margin-right:6px}}
 .legend .k.b{{border-top:2px dashed #3b82f6}}
 .legend .k.a{{border:none;width:auto;height:auto;color:#d46b08;font-weight:700;
               vertical-align:baseline}}
 table{{width:100%;border-collapse:collapse;margin:14px 0}}
 th,td{{padding:10px 12px;border-bottom:1px solid #eceef1;text-align:left;
       font-size:14px}}
 th{{background:#f6f8fa;font-weight:600;font-size:13px;color:#57606a}}
 td.num{{font-variant-numeric:tabular-nums;font-weight:650;text-align:right;
        font-size:15px}}
 td.mono{{font-family:ui-monospace,Consolas,monospace;font-size:13px}}
 td.formula{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;
             color:#8c959f}}
 td.note{{font-size:13px;color:#57606a}}
 .badge{{padding:2px 9px;border-radius:10px;font-size:11px;white-space:nowrap}}
 .badge.warn{{background:#fff1f0;color:#cf1322}}
 .badge.mid{{background:#fff7e6;color:#d46b08}}
 .badge.ok{{background:#f0f9f1;color:#2c6e49}}
 .flag{{background:#fff1f0;color:#cf1322;font-size:11px;border-radius:9px;
       padding:1px 8px;margin-left:8px;white-space:nowrap}}
 .reason{{font-size:12px;color:#8c959f}}
 .disclaimer{{background:#fffbe6;border-left:4px solid #faad14;padding:16px 20px;
             border-radius:0 8px 8px 0;font-size:13.5px}}
 footer{{margin-top:36px;padding-top:16px;border-top:1px solid #eceef1;
         font-size:12px;color:#8c959f;display:flex;justify-content:space-between;
         flex-wrap:wrap;gap:8px}}
 @media print{{body{{background:#fff;padding:0}} .sheet{{box-shadow:none;
   border-radius:0;padding:0}}}}
</style></head><body>
<div class="sheet">
<h1>经营诊断报告</h1>
<div class="sub">淘宝店铺 · 自动生成</div>

<div class="meta">
  <span>统计周期 <b>{period}</b></span>
  <span>数据源 <b>{int(src.get('file_count', 0) or 0)}</b> 个 / {int(src.get('row_count', 0) or 0):,} 行</span>
  <span>报告编号 <b>{_e(str(data.get('diagnosis_id', ''))[:12])}</b></span>
  <span>生成时间 {_e(str(data.get('generated_at', ''))[:19].replace('T', ' '))}</span>
</div>

{cards}

{_caliber_compare_html(cc)}

{f'<h2>净销售额与访客数趋势</h2><div class="legend">'
 f'<span><span class="k"></span>净销售额（左轴）</span>'
 f'<span><span class="k b"></span>访客数（右轴）</span>{legend_extra}</div>{trend_svg}'
 if trend_svg else ''}

{_anomaly_html(anomalies)}

{_products_html(products)}

{_weekly_html(weekly)}

<h2>核心指标</h2>
<table>
<thead><tr><th>指标</th><th style="text-align:right">数值</th>
<th>计算方式</th><th>口径状态</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="4">无可用指标</td></tr>'}</tbody>
</table>

{f'<h2>计算提示</h2><ul style="font-size:13px;color:#57606a">{warns}</ul>'
 if warns else ''}

<div class="disclaimer">
<strong>口径说明与待确认项</strong><br>{footnote}
</div>

<footer>
  <span>本报告由数据分析 Agent 自动生成；标注「待确认」的口径建议核对后再做经营决策。</span>
  <span>数据源：{files}</span>
  {caliber_line}
</footer>
</div>
</body></html>"""

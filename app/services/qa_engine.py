"""形态 A：对话式查询引擎（docs/14）—— 有对话，没有 LLM 循环。

问题 → 意图识别（规则）→ 确定性计算 → 模板化自然语言回答。
答案全部来自 SQL 计算，零 LLM、零幻觉，口径脚注随答附上。

能力边界：查数类问题（"X 是多少"）。"为什么"类问题返回 NotImplementedRoute，
由上层（chat 服务）转给形态 B（Agent 引擎）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.semantic.models import CaliberConflict
from app.semantic.registry import SemanticRegistry
from app.semantic.renderer import SQLRenderer, RenderError
from app.semantic.timerange import DateRange, TimeRangeError, parse_range

__all__ = ['QaAnswer', 'QaEngine', 'NeedsAgentError', 'ClarifyUserError']


class NeedsAgentError(Exception):
    """问题超出形态 A 的能力（需多步推理/归因）—— 上层转形态 B。"""


class ClarifyUserError(Exception):
    """需要用户澄清/引导的问题（docs/16 P2-3）—— 直接返回澄清文案，
    **不**转形态 B：口径歧义交给 LLM 就是让它自选口径作答，违反
    「歧义必报错不猜」的产品纪律；寒暄类走 Agent 则白烧配额。"""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


@dataclass
class QaAnswer:
    text: str                       # 自然语言回答
    route: str = 'qa'
    meta: dict = None               # metric_id / time_range / related_calibers...

    def __post_init__(self):
        if self.meta is None:
            self.meta = {}


# 触发"归因类问题"的关键词 → 直接进形态 B
_WHY_KEYWORDS = ('为什么', '怎么回事', '怎么办', '原因', '该怎么', '建议',
                 '该不该', '是不是', '分析一下', '帮我看看', '怎么优化')

# 归因区块类问法（形态 A 也能答 —— 这些是确定性聚合）
_PRODUCTS_HINTS = ('哪个商品', '哪款', '各商品', '商品拆解', '分商品', '每个商品')
_WEEKLY_HINTS = ('每周', '周对比', '各周', '按周')
_ANOMALY_HINTS = ('哪天', '异常', '波动', '突然', '峰值')

# 寒暄/无关意图（docs/16 P2-3 轻量兜底）：短且命中寒暄词的提问
# 不进 KeyError→Agent 路径白烧配额，直接给引导文案。
_CHITCHAT_HINTS = ('你好', '您好', '在吗', '你是谁', '你会什么', '能做什么',
                   '谢谢', '多谢', '辛苦', '再见', '拜拜', 'hi', 'hello',
                   '帮助', '怎么用', '使用说明')


class QaEngine:
    """查数式问答。所有计算走既有确定性组件，绝不调 LLM。"""

    def __init__(self, con, registry: SemanticRegistry):
        self.con = con
        self.reg = registry
        self.renderer = SQLRenderer(registry)

    # ------------------------------------------------------------------

    def answer(self, question: str) -> QaAnswer:
        """回答一个查数类问题。超出能力抛 NeedsAgentError。

        口径歧义 / 寒暄类抛 ClarifyUserError（直接澄清，不进 Agent）。
        """
        q = (question or '').strip()
        if not q:
            raise NeedsAgentError('空问题')

        # 寒暄/使用引导（docs/16 P2-3）：短问句且命中寒暄词 → 直接引导，
        # 不烧 Agent 配额。真正的数据问题不会命中（含指标词/数字/时间）。
        ql = q.lower()
        if len(q) <= 20 and any(h in ql for h in _CHITCHAT_HINTS):
            raise ClarifyUserError(
                '你好！我是店铺经营分析助手，基于你上传的千牛报表回答问题：\n'
                '· 查数：「这个月毛利率多少」「上周退款率」「哪个商品退款率高」\n'
                '· 归因：「为什么这周退款变多」「帮我看看利润下滑的原因」\n'
                '先在「数据上传」页导入报表并完成一次诊断，就可以开始提问。')

        # 归因/建议类 → 形态 B
        if any(k in q for k in _WHY_KEYWORDS):
            raise NeedsAgentError('归因/建议类问题需要多步推理')

        tr = self._parse_time(q)

        # ---- 归因区块类（确定性聚合，A 可答）----
        if any(k in q for k in _PRODUCTS_HINTS):
            return self._answer_products(tr)
        if any(k in q for k in _WEEKLY_HINTS):
            return self._answer_weekly(tr)
        if any(k in q for k in _ANOMALY_HINTS):
            return self._answer_anomaly(tr)
        if '口径' in q and ('对比' in q or '差别' in q or '差别' in q or '哪个' in q):
            return self._answer_caliber()

        # ---- 指标查询（主路径）----
        return self._answer_metric(q, tr)

    # ------------------------------------------------------------------

    def _parse_time(self, q: str) -> DateRange | None:
        """从问题里抠时间窗；没有则 None（全部数据）。"""
        # 显式日期段（诊断同款格式）
        import re
        m = re.search(r'(\d{4}-\d{2}-\d{2})\s*[~到至]\s*(\d{4}-\d{2}-\d{2})', q)
        if m:
            try:
                return parse_range(f'{m.group(1)}~{m.group(2)}')
            except TimeRangeError:
                pass
        for phrase in ('这个月', '本月', '上月', '上个月', '这周', '本周',
                       '上周', '今天', '昨天', '最近7天', '最近30天', '最近半年'):
            if phrase in q:
                try:
                    return parse_range(phrase)
                except TimeRangeError:
                    continue
        return None

    def _answer_metric(self, q: str, tr: DateRange | None) -> QaAnswer:
        # 先抠掉时间词，剩下的做指标识别（避免时间词干扰别名匹配）
        probe = q
        for phrase in ('这个月', '本月', '上月', '上个月', '这周', '本周', '上周',
                       '今天', '昨天', '最近7天', '最近30天', '最近半年'):
            probe = probe.replace(phrase, '')
        import re
        probe = re.sub(r'\d{4}-\d{2}-\d{2}\s*[~到至]\s*\d{4}-\d{2}-\d{2}', '', probe)
        probe = probe.replace('多少', '').replace('是几', '').replace('呢', '') \
                     .replace('？', '').replace('?', '').strip()

        try:
            metric = self.reg.resolve(probe or q)
        except CaliberConflict as e:
            # ★ docs/16 P2-3：口径歧义必须让用户明确 —— 绝不转形态 B
            #   让 LLM 自选口径作答（违反「歧义必报错不猜」）。
            cands = '、'.join(e.candidates)
            raise ClarifyUserError(
                f'「{probe}」命中多个口径：{cands}。\n'
                f'请指明其一再问（例如带上「财务口径」「推广口径」等字样），'
                f'我不猜口径 —— 两种口径的数字可能差很多。')
        except KeyError:
            raise NeedsAgentError(f'没有从问题里识别出指标：{probe!r}')

        try:
            rq = self.renderer.render(metric_id=metric.metric_id, time_range=tr)
        except (RenderError, CaliberConflict) as e:
            raise NeedsAgentError(f'指标渲染失败: {e}')

        row = self.con.execute(rq.sql, rq.params).fetchone()
        value = row[0] if row else None

        unit = rq.unit or ''
        if value is None:
            text = f'{metric.metric_name}：该时间范围内没有数据。'
        elif unit == 'percent':
            text = f'{metric.metric_name} = {value * 100:.2f}%'
        elif unit == 'currency':
            text = f'{metric.metric_name} = ¥{value:,.2f}'
        else:
            text = f'{metric.metric_name} = {value:,}'

        period = (f'{tr.start} ~ {tr.end}' if tr else '全部数据')
        text += f'（统计周期：{period}）'
        if metric.formula_expression:
            text += f'\n计算方式：{metric.formula_expression}'
        note = self.reg.ambiguity_note(probe or q)
        if note:
            text += f'\n提示：{note}'
        if rq.footnote:
            text += f'\n口径说明：{rq.footnote}'

        return QaAnswer(text=text, meta={
            'metric_id': metric.metric_id,
            'value': value, 'unit': unit,
            'time_range': period,
        })

    # ---- 归因区块复用（v1.2 的确定性聚合，同一套数字）----

    def _answer_products(self, tr) -> QaAnswer:
        from app.services.analytics import build_product_breakdown
        rows = build_product_breakdown(self.con, tr)
        if not rows:
            return QaAnswer(text='当前没有商品维度的数据（需要已上传并诊断过订单数据）。')
        lines = []
        for p in rows[:8]:
            name = p['title'] or p['merchant_code'] or p['item_id']
            flag = ' ⚠重点关注' if p['flags'] else ''
            lines.append(
                f"· {name}：占比 {p['share'] * 100:.0f}%，净销售额 ¥{p['net_sales']:,.0f}，"
                f"退款率 {p['refund_rate'] * 100:.1f}%"
                + (f"（主因 {p['top_refund_reason']}）" if p['top_refund_reason']
                   and p['refund_amount'] > 0 else '')
                + flag)
        return QaAnswer(text='商品维度拆解：\n' + '\n'.join(lines),
                        meta={'kind': 'products'})

    def _answer_weekly(self, tr) -> QaAnswer:
        from app.services.analytics import build_weekly_compare
        w = build_weekly_compare(self.con, tr)
        weeks = w.get('weeks') or []
        if len(weeks) < 2:
            return QaAnswer(text='周对比数据不足（至少需要跨两周的数据）。')
        lines = []
        for x in weeks:
            cvr = f"{x['pay_cvr'] * 100:.2f}%" if x.get('pay_cvr') is not None else '—'
            rr = (f"{x['refund_rate'] * 100:.1f}%"
                  if x.get('refund_rate') is not None else '—')
            days = f"（{x['days']}天，不完整）" if (x.get('days') or 7) < 7 else ''
            lines.append(f"· {x['label'][5:]} 起{days}：访客 {x['visitors']:,}，"
                         f"转化率 {cvr}，净销售额 ¥{x['net_sales']:,.0f}，"
                         f"退款率 {rr}")
        big = w.get('biggest')
        if big:
            k = big['kind']
            fmt = (lambda v: f'{v * 100:.1f}%') if k == 'pct' else \
                  (lambda v: f'{v:,.0f}')
            lines.append(f"\n变化最大的维度：{big['metric']}"
                         f"（末完整周 {fmt(big['last'])} vs 之前均值 {fmt(big['base'])}，"
                         f"{'↑' if big['delta'] > 0 else '↓'}{abs(big['delta']) * 100:.0f}%）")
        return QaAnswer(text='周度对比：\n' + '\n'.join(lines), meta={'kind': 'weekly'})

    def _answer_anomaly(self, tr) -> QaAnswer:
        from app.services.analytics import build_trend, detect_anomaly_days
        trend = build_trend(self.con, tr)
        anoms = detect_anomaly_days(trend)
        if not anoms:
            return QaAnswer(text='没有检测到异常日（各指标相对前 7 日均值'
                                 '偏离均在 35% 以内）。', meta={'kind': 'anomaly'})
        lines = []
        for a in anoms[:6]:
            lines.append(f"· {a['date']}：" + '；'.join(a['features'][:2])
                         + f" → {a['note']}")
        return QaAnswer(text=f'检测到 {len(anoms)} 个异常日：\n' + '\n'.join(lines),
                        meta={'kind': 'anomaly'})

    def _answer_caliber(self) -> QaAnswer:
        from app.services.analytics import build_caliber_compare
        # 用注册表自造 metrics 形参不划算 —— 直接查 M13/M14
        try:
            items = []
            for mid in ('M13', 'M14'):
                rq = self.renderer.render(metric_id=mid, time_range=None)
                row = self.con.execute(rq.sql, rq.params).fetchone()
                if row and row[0] is not None:
                    items.append({'metric_id': mid, 'value': row[0],
                                  'unit': rq.unit})
            cc = build_caliber_compare(items)
        except Exception:                       # noqa: BLE001
            cc = {}
        if not cc:
            return QaAnswer(text='口径对比需要毛利率相关数据（当前数据不足）。')
        a, b = cc['items']
        return QaAnswer(
            text=(f"{cc['label']} 口径对比：\n"
                  f"· {a['name']}（{a['metric_id']}）：{a['value'] * 100:.2f}% —— {a['desc']}\n"
                  f"· {b['name']}（{b['metric_id']}）：{b['value'] * 100:.2f}% —— {b['desc']}\n"
                  f"{cc['note']}"),
            meta={'kind': 'caliber_compare'})

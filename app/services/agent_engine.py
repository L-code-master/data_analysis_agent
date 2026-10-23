"""形态 B：Agent 引擎 —— LLM 循环（docs/14）。

这是本项目第一个真正的 Agent 循环（v1.4）：
    模型决定调用什么工具 → 执行 → 看结果 → 决定下一步 → 直到给出回答。

四条硬纪律（每条都有对应机制）：
  1. **数字只来自工具**：system prompt 明令禁止编造数字；工具返回什么就是什么
  2. **LoopGuard**：相同调用第 3 次阻止、连败 8 次终止
  3. **上下文超限必压缩**：接入 v1.3 的 Compressor（L1/L2；summarizer 未注入）
  4. **成本必记账**：每次 LLM 调用的 usage（含 reasoning tokens）进 usage_records
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from app.config import get_settings
from app.memory import Compressor, TokenEstimator
from app.services import billing
from app.services.llm_client import LlmClient, LlmError

__all__ = ['AgentEngine', 'AgentTurnResult', 'LoopGuard', 'build_tools']

MAX_TURNS = 8                       # 循环步数上限（防失控）

logger = logging.getLogger(__name__)


class LoopGuard:
    """循环守卫（阈值 3 / 3 / 8）。

    - IDENTICAL_CALL_BLOCK：相同 (工具+参数) 第 3 次出现 → 阻止该次调用
    - FAILURE_WARN：同一工具连续失败 3 次 → 警告注入
    - FAILURE_HALT：累计连续失败 8 次 → 终止循环
    """

    IDENTICAL_CALL_BLOCK = 3
    FAILURE_WARN = 3
    FAILURE_HALT = 8

    def __init__(self):
        self._call_counts: dict[str, int] = {}
        self._fails_by_tool: dict[str, int] = {}
        self._consecutive_fails = 0
        self.warnings: list[str] = []
        self.blocked: list[str] = []

    def check_call(self, name: str, args: str) -> tuple[bool, str]:
        key = f'{name}:{args}'
        n = self._call_counts.get(key, 0) + 1
        self._call_counts[key] = n
        if n >= self.IDENTICAL_CALL_BLOCK:
            self.blocked.append(key)
            return False, (f'该调用已重复 {n} 次 —— 已被循环守卫阻止。'
                           f'请换一种参数或基于已有结果作答。')
        return True, ''

    def record_result(self, name: str, ok: bool) -> bool:
        """记录工具成败。返回 False = 连续失败超限，应终止循环。"""
        if ok:
            self._fails_by_tool[name] = 0
            self._consecutive_fails = 0
            return True
        self._fails_by_tool[name] = self._fails_by_tool.get(name, 0) + 1
        self._consecutive_fails += 1
        if self._fails_by_tool[name] == self.FAILURE_WARN:
            self.warnings.append(f'工具 {name} 已连续失败 '
                                 f'{self._fails_by_tool[name]} 次')
        return self._consecutive_fails < self.FAILURE_HALT


@dataclass
class AgentTurnResult:
    answer: str = ''
    tool_trace: list[dict] = field(default_factory=list)   # 工具调用轨迹（溯源）
    turns: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    guard_warnings: list[str] = field(default_factory=list)
    error: str = ''


# ---------------------------------------------------------------------------
# 工具集（全部是既有确定性函数的封装 —— 模型调用它们，天然带溯源）
# ---------------------------------------------------------------------------

def build_tools(reg, con_getter) -> tuple[list[dict], dict]:
    """返回 (OpenAI tools 规格, 工具实现表)。

    con_getter: () -> DuckDB 连接（惰性获取，工具内使用后归还）。
    """
    spec = [
        {'type': 'function', 'function': {
            'name': 'list_metrics',
            'description': '列出可计算的指标目录（ID、名称、口径、公式）。',
            'parameters': {'type': 'object', 'properties': {},
                           'required': []}}},
        {'type': 'function', 'function': {
            'name': 'compute_metric',
            'description': '计算一个指标的值。metric_id 见 list_metrics；'
                           'time_range 形如 "2026-09-01~2026-09-28"，留空=全部数据。',
            'parameters': {'type': 'object', 'properties': {
                'metric_id': {'type': 'string'},
                'time_range': {'type': 'string', 'description': '可留空'}},
                'required': ['metric_id']}}},
        {'type': 'function', 'function': {
            'name': 'product_breakdown',
            'description': '按商品拆解销售/退款/毛利率，并标记异常商品（重点关注）。'
                           '适合回答"哪个商品有问题"。',
            'parameters': {'type': 'object', 'properties': {
                'time_range': {'type': 'string', 'description': '可留空'}},
                'required': []}}},
        {'type': 'function', 'function': {
            'name': 'weekly_compare',
            'description': '按周对比访客/转化率/净销售额/退款率/ROI，'
                           '并指出变化最大的维度。适合"第几周发生了什么"。',
            'parameters': {'type': 'object', 'properties': {
                'time_range': {'type': 'string', 'description': '可留空'}},
                'required': []}}},
        {'type': 'function', 'function': {
            'name': 'anomaly_days',
            'description': '检测异常日（偏离前 7 日均值 ≥35%），返回特征描述'
                           '与建议核对方向。适合"哪天不对劲/有没有波动"。',
            'parameters': {'type': 'object', 'properties': {
                'time_range': {'type': 'string', 'description': '可留空'}},
                'required': []}}},
    ]

    from app.services.analytics import (
        build_product_breakdown, build_trend, build_weekly_compare,
        detect_anomaly_days,
    )
    from app.semantic.timerange import parse_range

    def _tr(text):
        if not text:
            return None
        try:
            return parse_range(text)
        except Exception:                       # noqa: BLE001
            return None

    def _list_metrics(_args: dict) -> str:
        rows = []
        for m in reg.all():
            note = {'high': '（口径待确认）', 'medium': '（部分待确认）'}.get(
                m.uncertainty.level.value if hasattr(m, 'uncertainty') and
                m.uncertainty else '', '')
            rows.append(f'{m.metric_id} {m.metric_name}{note}：'
                        f'{m.formula_expression}')
        return '\n'.join(rows) or '（无指标）'

    def _compute_metric(args: dict) -> str:
        from app.semantic.registry import SemanticRegistry
        from app.semantic.renderer import SQLRenderer
        con = con_getter()            # open_semantic_db 已按版本迁移
        # ★ 复用 ask 作用域加载好的 registry（docs/16 P1-16）：
        #   指标定义与 metric_spec 都在内存里，不再每次工具调用重建。
        #   仅在未传入 registry 时（单测/独立工具）回退到现建。
        r2 = reg
        if r2 is None:
            r2 = SemanticRegistry(con)
            r2.load()
        # ★ P3-1：异常向上抛给 _dispatch 统一包装 —— 失败与否由结构化
        #   通道告知 LoopGuard，不再靠返回文本前缀嗅探。
        rq = SQLRenderer(r2).render(metric_id=args['metric_id'],
                                    time_range=_tr(args.get('time_range')))
        row = con.execute(rq.sql, rq.params).fetchone()
        value = row[0] if row else None
        if value is None:
            return f'{args["metric_id"]}：该范围内无数据'
        unit = rq.unit or ''
        if unit == 'percent':
            vs = f'{value * 100:.2f}%'
        elif unit == 'currency':
            vs = f'¥{value:,.2f}'
        else:
            vs = f'{value:,}'
        foot = f'；口径：{rq.footnote}' if rq.footnote else ''
        return f'{args["metric_id"]} = {vs}{foot}'

    def _products(args: dict) -> str:
        rows = build_product_breakdown(con_getter(), _tr(args.get('time_range')))
        if not rows:
            return '无商品维度数据（需先上传订单数据）'
        out = []
        for p in rows[:10]:
            name = p['title'] or p['merchant_code'] or p['item_id']
            extra = (f'；主因 {p["top_refund_reason"]}'
                     if p['top_refund_reason'] and p['refund_amount'] > 0 else '')
            flag = '；⚠' + '/'.join(p['flags']) if p['flags'] else ''
            out.append(f'{name}：占比{p["share"] * 100:.0f}% '
                       f'支付¥{p["pay_amount"]:,.0f} 净销¥{p["net_sales"]:,.0f} '
                       f'退款率{p["refund_rate"] * 100:.1f}%'
                       f'{" 毛利率" + format((p["margin_rate"] or 0) * 100, ".1f") + "%" if p["margin_rate"] is not None else ""}'
                       f'{extra}{flag}')
        return '\n'.join(out)

    def _weekly(args: dict) -> str:
        w = build_weekly_compare(con_getter(), _tr(args.get('time_range')))
        weeks = w.get('weeks') or []
        if not weeks:
            return '无周度数据'

        def _row(x: dict) -> str:
            rr = (f"{x['refund_rate'] * 100:.1f}%"
                  if x.get('refund_rate') is not None else '—')   # P2-9: pay=0 → None
            roi = x.get('roi') if x.get('roi') is not None else '—'
            return (f"{x['label'][5:]}起({x.get('days', 7)}天): "
                    f"访客{x['visitors']:,} 净销¥{x['net_sales']:,.0f} "
                    f"退款率{rr} ROI{roi}")

        out = [_row(x) for x in weeks]
        big = w.get('biggest')
        if big:
            out.append(f"变化最大: {big['metric']} 末周{big['last']} vs 均{big['base']}"
                       f" ({'↑' if big['delta'] > 0 else '↓'}{abs(big['delta']) * 100:.0f}%)")
        return '\n'.join(out)

    def _anomalies(args: dict) -> str:
        trend = build_trend(con_getter(), _tr(args.get('time_range')))
        anoms = detect_anomaly_days(trend)
        if not anoms:
            return '未检测到异常日'
        return '\n'.join(f"{a['date']}: {'；'.join(a['features'])} → {a['note']}"
                         for a in anoms[:8])

    impl = {
        'list_metrics': _list_metrics,
        'compute_metric': _compute_metric,
        'product_breakdown': _products,
        'weekly_compare': _weekly,
        'anomaly_days': _anomalies,
    }
    return spec, impl


SYSTEM_PROMPT = '''你是淘宝店铺的经营分析助手。数据已由用户上传并完成清洗，存于语义层。

工作纪律（必须遵守）：
1. 你回答中的**每一个数字**必须来自工具返回结果，禁止编造或凭记忆估算。
2. 先用工具查证，再下结论；比较类问题先拿到两边的数再比较。
3. 工具返回"无数据"时如实告知，不要猜。
4. 回答末尾注明关键口径（工具结果里带"口径："的都要转达）。
5. 回答用中文，简洁、直接给结论，再给依据。适合淘宝卖家阅读。'''


# L4 紧急压缩触发词（docs/16 P2-2）：不同网关对上下文超限的报错
# 措辞不一，按关键词识别；命中后 compress_reactive 压缩并重试一次。
_CONTEXT_OVERFLOW_MARKERS = (
    'context_length', 'context length', 'context window',
    'maximum context', 'too many tokens', 'input too long',
    'token limit', 'exceeds the context', 'prompt is too long',
)


def _is_context_overflow(err: Exception) -> bool:
    text = str(err).lower()
    return any(m in text for m in _CONTEXT_OVERFLOW_MARKERS)


class AgentEngine:
    """形态 B 的循环本体。一次 run() = 处理一个用户问题的完整循环。"""

    def __init__(self, client: LlmClient, tenant_id: str,
                 tools_spec: list[dict], tools_impl: dict,
                 max_turns: int = MAX_TURNS,
                 max_cost_usd: float | None = None,
                 deadline_seconds: float | None = None):
        self.client = client
        self.tenant_id = tenant_id
        self.tools_spec = tools_spec
        self.tools_impl = tools_impl
        self.max_turns = max_turns
        s = get_settings()
        self.max_cost_usd = (s.agent_max_cost_usd
                             if max_cost_usd is None else max_cost_usd)
        self.deadline_seconds = (s.agent_max_duration_seconds
                                 if deadline_seconds is None
                                 else deadline_seconds)
        self.compressor = Compressor(
            estimator=TokenEstimator(),
            summarizer=None,                 # L3 摘要需注入 LLM，二期接
            # ★ P3-13：预算不再硬编码 60k —— 与 LLM_CONTEXT_WINDOW 脱钩
            #   会让换小窗口模型时压缩永不触发、超限直接失败。
            #   按窗口 1/4 估（扣除输出预留与系统提示），下限 20k。
            budget_tokens=max(20_000, s.llm_context_window // 4),
            keep_recent=10,
        )
        self.model = s.llm_model

    # ------------------------------------------------------------------

    def run(self, question: str, history: list[dict] | None = None,
            data_context: str = '') -> AgentTurnResult:
        """执行一轮 Agent 循环。

        history: 之前的对话 [{'role','content'}]（多轮上下文）。
        data_context: 数据范围说明（docs/15）—— 注入 system prompt，
            模型回答时可主动说明"本回答基于哪些文件、哪个周期"。
        """
        res = AgentTurnResult()
        guard = LoopGuard()
        injected_warnings: set[str] = set()   # 已注入对话的 guard 警告（P3-1）
        system = SYSTEM_PROMPT
        if data_context:
            system += f'\n\n{data_context}'
        messages: list[dict] = [{'role': 'system', 'content': system}]
        messages += (history or [])[-20:]          # 最近 20 条历史
        messages.append({'role': 'user', 'content': question})

        # ★ 整体 deadline（docs/16 P1-13）：单轮超时不够，
        #   最坏 8 轮 × 重试仍可能拖很久。
        deadline = (time.monotonic() + self.deadline_seconds
                    if self.deadline_seconds and self.deadline_seconds > 0
                    else None)

        def budget_error() -> str:
            if deadline is not None and time.monotonic() > deadline:
                return f'分析超时（超过 {self.deadline_seconds:.0f} 秒）'
            if self.max_cost_usd and self.max_cost_usd > 0 \
                    and res.cost_usd >= self.max_cost_usd:
                return (f'分析成本超过上限'
                        f'（{res.cost_usd:.4f}/{self.max_cost_usd:.4f} USD）')
            return ''

        try:
            reactive_tried = False          # L4 紧急压缩只重试一次（P2-2）
            for turn in range(self.max_turns):
                res.turns = turn + 1
                exceeded = budget_error()
                if exceeded:
                    res.error = exceeded
                    res.guard_warnings = guard.warnings
                    return res

                # 上下文预算检查 → 压缩（v1.3 库的第二个用户）
                if self.compressor.should_compress(messages):
                    cr = self.compressor.compress(messages)
                    messages = cr.messages
                    if cr.events:
                        res.tool_trace.append({'tool': '_compress',
                                               'level': cr.level,
                                               'events': cr.events[:3]})

                try:
                    resp = self.client.chat(messages, tools=self.tools_spec,
                                            temperature=0.2)
                except LlmError as e:
                    # ★ docs/16 P2-2：L4 紧急压缩此前零调用方 —— 上下文
                    #   超限直接整轮失败。现在命中超限报错就激进压缩后
                    #   重试一次（仍失败才放弃）。
                    if not reactive_tried and _is_context_overflow(e):
                        reactive_tried = True
                        cr = self.compressor.compress_reactive(messages,
                                                               error=e)
                        messages = cr.messages
                        res.tool_trace.append(
                            {'tool': '_compress', 'level': cr.level,
                             'events': cr.events[:3]})
                        continue
                    raise
                self._record_usage(res, resp)

                if not resp.wants_tool:
                    res.answer = resp.content or '（模型未返回内容）'
                    res.guard_warnings = guard.warnings
                    return res

                # 还要继续调工具 → 先确认预算，避免再烧一轮
                exceeded = budget_error()
                if exceeded:
                    res.error = exceeded
                    res.guard_warnings = guard.warnings
                    return res

                messages.append({'role': 'assistant', 'content': resp.content,
                                 'tool_calls': [
                                     {'id': c['id'], 'type': 'function',
                                      'function': {'name': c['name'],
                                                   'arguments': c['arguments']}}
                                     for c in resp.tool_calls]})

                for call in resp.tool_calls:
                    ok, block_msg = guard.check_call(call['name'],
                                                     call['arguments'])
                    if not ok:
                        result_text, tool_ok = block_msg, False
                    else:
                        # ★ P3-1：成败由 _dispatch 结构化返回（异常即失败），
                        #   不再用「计算失败」中文前缀嗅探 —— 商品名等
                        #   合法返回值以该前缀开头会被误判成工具失败。
                        result_text, tool_ok = self._dispatch(call['name'],
                                                              call['arguments'])
                    if not guard.record_result(call['name'], tool_ok):
                        res.error = (f'循环终止：连续失败 {LoopGuard.FAILURE_HALT} 次'
                                     f'（工具 {call["name"]}）')
                        res.guard_warnings = guard.warnings
                        return res
                    res.tool_trace.append({'tool': call['name'],
                                           'ok': tool_ok,
                                           'args': (call['arguments'] or '')[:200],
                                           'result': result_text[:300]})
                    messages.append({'role': 'tool',
                                     'tool_call_id': call['id'],
                                     'content': result_text[:4000]})
                    # ★ P3-1：FAILURE_WARN 不再只躺在结果里 —— 注入对话，
                    #   让模型立刻换策略（否则同一坑连踩到 HALT）。
                    for w in guard.warnings:
                        if w in injected_warnings:
                            continue
                        injected_warnings.add(w)
                        messages.append({'role': 'user', 'content':
                                         f'（系统提示：{w}。请改用其他工具或'
                                         f'参数，或基于已有结果直接回答）'})

            # 步数耗尽 → 强制收口（先确认预算，超限不再发起新调用）
            exceeded = budget_error()
            if exceeded:
                res.error = exceeded
                res.guard_warnings = guard.warnings
                return res
            messages.append({'role': 'user', 'content':
                             '（步数已达上限，请基于已获得的信息直接回答，'
                             '不要再调用工具）'})
            resp = self.client.chat(messages, temperature=0.2)
            self._record_usage(res, resp)
            res.answer = resp.content or '（超步数且未获得回答）'
            res.guard_warnings = guard.warnings
            return res

        except LlmError as e:
            res.error = f'LLM 调用失败: {e}'
            return res
        except Exception as e:                      # noqa: BLE001
            res.error = f'{type(e).__name__}: {e}'
            return res

    # ------------------------------------------------------------------

    def _dispatch(self, name: str, args_json: str) -> tuple[str, bool]:
        """执行工具。返回 (给模型看的文本, 是否成功) —— P3-1 结构化失败通道。"""
        fn = self.tools_impl.get(name)
        if fn is None:
            return f'计算失败：未知工具 {name}', False
        try:
            args = json.loads(args_json) if args_json and \
                args_json.strip().startswith('{') else {}
        except json.JSONDecodeError:
            args = {}
        try:
            return fn(args), True
        except Exception as e:                      # noqa: BLE001
            return f'计算失败：{type(e).__name__}: {e}', False

    def _record_usage(self, res: AgentTurnResult, resp) -> None:
        res.input_tokens += resp.input_tokens
        res.output_tokens += resp.output_tokens
        res.reasoning_tokens += resp.reasoning_tokens

        # ★ 单次调用成本（docs/16 P1-1）：usage_records 每行必须存**本次**
        #   用量，不能把累计值当单次值入账 —— 否则 3 次各 $0.01 会记成
        #   $0.01 + $0.02 + $0.03 = $0.06，轮数越多放大越严重，成本熔断
        #   被虚增值提前触发，付费用户被错误 402。
        call_cost = round(_estimate_cost(resp.input_tokens, resp.output_tokens), 6)
        res.cost_usd = round(res.cost_usd + call_cost, 6)
        try:
            billing.record_cost(self.tenant_id, call_cost, model=self.model,
                                input_tokens=resp.input_tokens,
                                output_tokens=resp.output_tokens,
                                reasoning_tokens=resp.reasoning_tokens)
        except Exception:                           # noqa: BLE001
            # 记账失败不阻断对话，但绝不能静默：成本漏记会让熔断统计失真
            logger.exception('LLM 成本记账失败 tenant=%s model=%s cost=%.6f',
                             self.tenant_id, self.model, call_cost)


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """粗估成本（USD）—— qwen3.8-flash 量级，reasoning 计入输出侧。"""
    return (input_tokens / 1_000_000 * 0.11
            + output_tokens / 1_000_000 * 0.55)

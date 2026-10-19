"""四级上下文压缩器 —— 通用可复用库（v1.3，docs/10）。

设计目标：
  1. **零业务依赖**：输入输出都是 OpenAI 格式消息，不 import 本项目业务模块；
     任何多轮智能体（二期"追问式分析"或其他 Agent 项目）直接复用。
  2. **三处针对性缺陷防护**：
       G1  L3 摘要不丢中段历史 → computed_registry 键值对原样保留，不进 LLM
       G2  摘要必须过有效性校验 → 空串/短串（<30 字符）放弃压缩，返回原消息
       G3  snip 不一次删光中段 → 分级删除，每轮最多 MAX_SNIP 条
  3. **绝不返回未修复孤儿的消息列表**：tool_calls / tool 配对完整性是
     API 不 400 的前提，每级结束后必跑 _fix_orphaned_tool_pairs。
  4. **压缩失败回退**：任何一级失败都返回原消息 —— 宁可不压，不清空。

四级（详见 compress() 的顺序说明）：
  L1 microcompact   截断旧的超长工具结果（保护最近 keep_recent 条）—— 最便宜，先试
  L3 compact        summarizer 增量摘要 + computed_registry（需注入摘要器）
                    —— 信息保真度高于删除，故在 L2 之前（实施修正）
  L2 snip           分级删除中段消息（保首条 + 最近 keep_recent 条）—— 最后手段
  L4 reactive       API 已报 context/token 类错误时的紧急路径（直冲 L2 强制版）
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable

from app.memory.estimator import TokenEstimator

__all__ = ['Compressor', 'CompressResult', 'TokenEstimator',
           'fix_orphaned_tool_pairs', 'REACTIVE_API_ERRORS']

# 触发 L4 的 API 错误关键字（大小写不敏感匹配异常文本）
REACTIVE_API_ERRORS = (
    'context_length', 'context window', 'maximum context',
    'token limit', 'too many tokens', 'max_tokens',
)

TOOL_RESULT_KEEP_CHARS = 400       # L1：旧工具结果超过此长度即截断
SUMMARY_MIN_CHARS = 30             # G2：摘要低于此长度视为无效
MAX_SNIP = 3                       # G3：每轮删除的上限


def _is_cjk_agnostic_text(v) -> str:
    return v if isinstance(v, str) else ''


@dataclass
class CompressResult:
    """压缩结果。events 供观测层记录（与 observe 的 compress 事件对齐）。"""
    messages: list[dict]
    level: str = 'none'                      # none|microcompact|snip|compact|reactive
    tokens_before: int = 0
    tokens_after: int = 0
    events: list[dict] = field(default_factory=list)


def fix_orphaned_tool_pairs(messages: list[dict]) -> list[dict]:
    """修复被压缩切断的 tool_calls / tool 配对。

    规则：
      - assistant.tool_calls 中无响应的 call → 从 tool_calls 里移除；
        若全部无响应 → 剥离 tool_calls，content 为空时补占位文本
      - tool 消息的 tool_call_id 无来源（没有任何 assistant 发起过）→ 删除
    """
    called = set()                       # assistant 发起过的 call id
    for m in messages:
        for c in m.get('tool_calls') or []:
            called.add(c.get('id'))
    responded = set()                    # 实际有响应消息的 call id
    for m in messages:
        if m.get('role') == 'tool':
            responded.add(m.get('tool_call_id'))

    out: list[dict] = []
    for m in messages:
        role = m.get('role')
        calls = m.get('tool_calls')
        if role == 'assistant' and calls:
            kept = [c for c in calls if c.get('id') in responded]
            if len(kept) == len(calls):
                out.append(m)
                continue
            m2 = dict(m)
            if kept:
                m2['tool_calls'] = kept
            else:
                m2.pop('tool_calls', None)
                if not _is_cjk_agnostic_text(m2.get('content')).strip():
                    m2['content'] = '[工具调用已被压缩移除]'
            out.append(m2)
        elif role == 'tool':
            if m.get('tool_call_id') in called:      # 有发起来源，保留
                out.append(m)
        else:
            out.append(m)
    return out


class Compressor:
    """四级压缩器。

    Args:
        estimator: token 估算器（可替换为真实 tokenizer 的适配器）。
        summarizer: L3 用，callable(text) -> str。None 表示不启用 L3。
        budget_tokens: 消息区预算（超过即触发压缩）。
        keep_recent: 最近 N 条永不压缩。
        computed_registry: 旁路账本 {key: value} —— 已确认的事实/指标值，
            L3 时以键值对原样保留在摘要里（G1：不进 LLM 摘要，永不重算）。
        keep_recent_reactive: L4 紧急模式下的保护条数（更小 = 删得更狠）。
    """

    def __init__(self,
                 estimator: TokenEstimator | None = None,
                 summarizer: Callable[[str], str] | None = None,
                 budget_tokens: int = 89_600,
                 keep_recent: int = 12,
                 computed_registry: dict[str, str] | None = None,
                 keep_recent_reactive: int = 4):
        self.est = estimator or TokenEstimator()
        self.summarizer = summarizer
        self.budget_tokens = budget_tokens
        self.keep_recent = keep_recent
        self.computed_registry = computed_registry or {}
        self.keep_recent_reactive = keep_recent_reactive

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def should_compress(self, messages: list[dict]) -> bool:
        return self.est.count_messages(messages) > self.budget_tokens

    def compress(self, messages: list[dict]) -> CompressResult:
        """按 L1 → L3 → L2 逐级压缩，直到回到预算内或无力再压。

        顺序说明（实施修正，与 docs/10 初稿不同，已回写）：
        L3（摘要）在 L2（删除）**之前** —— 摘要比删除的信息保真度高；
        若 L2 先行会把中段删光，L3 将无原料可摘要。
        成本上 L1（纯截断）最便宜故最先；L3 有 LLM 成本但仅在注入
        summarizer 且 L1 不够时才发生。
        """
        before = self.est.count_messages(messages)
        if before <= self.budget_tokens:
            return CompressResult(messages=messages, level='none',
                                  tokens_before=before, tokens_after=before)

        msgs = copy.deepcopy(messages)
        events: list[dict] = []
        level = 'none'

        # ---- L1：截断旧的超长工具结果 ----
        msgs, n_cut = self._microcompact(msgs, events)
        if n_cut:
            level = 'microcompact'
        if self.est.count_messages(msgs) <= self.budget_tokens:
            return self._done(msgs, level, before, events)

        # ---- L3：LLM 增量摘要（未注入摘要器则跳过）----
        if self.summarizer is not None and self._compact(msgs, events):
            level = 'compact'
            if self.est.count_messages(msgs) <= self.budget_tokens:
                return self._done(msgs, level, before, events)

        # ---- L2：分级删除中段（最后手段，信息损失最大）----
        msgs, n_removed = self._snip(msgs, events)
        if n_removed:
            level = 'snip'
        return self._done(msgs, level, before, events)

    def compress_reactive(self, messages: list[dict],
                          error: Exception | str = '') -> CompressResult:
        """L4：API 已报 context/token 类错误时的紧急路径。

        直接 L2 激进版（保护条数减到 keep_recent_reactive，无视预算判断
        —— API 报错本身已证明超限），不走 L1/L3。删到保护边界为止。
        """
        before = self.est.count_messages(messages)
        msgs = copy.deepcopy(messages)
        events: list[dict] = [{'action': 'reactive',
                               'error': str(error)[:200]}]
        msgs, _ = self._snip(msgs, events, keep_recent=self.keep_recent_reactive,
                             force=True)
        return self._done(msgs, 'reactive', before, events)

    # ------------------------------------------------------------------
    # L1 Microcompact
    # ------------------------------------------------------------------

    def _microcompact(self, msgs: list[dict], events: list[dict]
                      ) -> tuple[list[dict], int]:
        """截断旧的（keep_recent 之外的）超长工具结果。返回 (消息, 截断数)。"""
        protected = len(msgs) - self.keep_recent
        n_cut = 0
        for i in range(max(0, protected)):
            m = msgs[i]
            if m.get('role') != 'tool':
                continue
            text = _is_cjk_agnostic_text(m.get('content'))
            if len(text) <= TOOL_RESULT_KEEP_CHARS:
                continue
            kept = text[:TOOL_RESULT_KEEP_CHARS]
            trunc = (f'{kept}\n…[compression: 已截断 '
                     f'{len(text) - TOOL_RESULT_KEEP_CHARS} 字符]')
            msgs[i] = {**m, 'content': trunc}
            n_cut += 1
            events.append({'action': 'microcompact', 'index': i,
                           'before_chars': len(text),
                           'after_chars': len(trunc)})
        if n_cut:
            msgs = fix_orphaned_tool_pairs(msgs)
        return msgs, n_cut

    # ------------------------------------------------------------------
    # L2 Snip
    # ------------------------------------------------------------------

    def _snip(self, msgs: list[dict], events: list[dict],
              keep_recent: int | None = None,
              force: bool = False) -> tuple[list[dict], int]:
        """分级删除中段：每轮最多 MAX_SNIP 条，优先最旧；保首条 + 最近 N 条。

        force=True（L4 紧急模式）：跳过预算判断，删到保护边界为止。
        """
        keep_recent = self.keep_recent if keep_recent is None else keep_recent
        total_removed = 0

        while True:
            if not force:
                now = self.est.count_messages(msgs)
                if now <= self.budget_tokens:
                    break
            # 可删区间：跳过首条与最近 keep_recent 条
            lo, hi = 1, max(1, len(msgs) - keep_recent)
            if lo >= hi:
                break
            victims = list(range(lo, hi))[:MAX_SNIP]      # G3：分级，最旧优先
            if not victims:
                break
            removed_roles = [msgs[i].get('role') for i in victims]
            msgs = [m for i, m in enumerate(msgs) if i not in set(victims)]
            msgs = fix_orphaned_tool_pairs(msgs)
            total_removed += len(victims)
            events.append({'action': 'snip', 'removed_indexes': victims,
                           'removed_roles': removed_roles,
                           'tokens_after': self.est.count_messages(msgs)})
        return msgs, total_removed

    # ------------------------------------------------------------------
    # L3 Compact
    # ------------------------------------------------------------------

    def _compact(self, msgs: list[dict], events: list[dict]) -> bool:
        """中段整体替换为一条摘要消息（G1/G2 修补版）。成功返回 True。"""
        lo, hi = 1, max(1, len(msgs) - self.keep_recent)
        if lo >= hi:
            return False
        middle = msgs[lo:hi]
        if not middle:
            return False

        # 组装给摘要器的文本：角色 + 内容概要（工具结果只给头部）
        parts = []
        for m in middle:
            role = m.get('role', '?')
            text = _is_cjk_agnostic_text(m.get('content'))[:600]
            parts.append(f'[{role}] {text}')
        prompt = ('\n以下是多轮对话的中段历史，请压缩为一段保留关键事实与结论的摘要：\n'
                  + '\n'.join(parts))

        try:
            summary = self.summarizer(prompt) if self.summarizer else ''   # type: ignore[misc]
        except Exception as e:                        # noqa: BLE001
            # 摘要器异常 → 放弃压缩（纪律：宁可不压）
            events.append({'action': 'compact_failed',
                           'reason': f'summarizer error: {e}'})
            return False

        summary = _is_cjk_agnostic_text(summary).strip()
        if len(summary) < SUMMARY_MIN_CHARS:          # G2：无效摘要 → 放弃
            events.append({'action': 'compact_failed',
                           'reason': f'summary too short ({len(summary)} chars)'})
            return False

        # G1：已确认的事实键值对不进 LLM 摘要，原样拼接
        registry_block = ''
        if self.computed_registry:
            lines = '\n'.join(f'- {k}: {v}'
                              for k, v in self.computed_registry.items())
            registry_block = ('\n\n## 已确认的值（不得重算）\n' + lines)

        compact_msg = {
            'role': 'user',
            'content': (f'<context-compaction>\n{summary}{registry_block}\n'
                        f'</context-compaction>'),
        }
        msgs[:] = msgs[:lo] + [compact_msg] + msgs[hi:]
        events.append({'action': 'compact',
                       'summarized_messages': len(middle),
                       'summary_chars': len(summary)})
        return True

    # ------------------------------------------------------------------

    def _done(self, msgs: list[dict], level: str, before: int,
              events: list[dict]) -> CompressResult:
        after = self.est.count_messages(msgs)
        return CompressResult(messages=msgs, level=level,
                              tokens_before=before, tokens_after=after,
                              events=events)

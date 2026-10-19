"""模块 9：记忆层四级压缩库（app/memory/）。

不需要 LLM —— L3 用注入的 fake summarizer。
断言计划见 docs/10 §6。
"""
import os
import sys
import pathlib

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.memory import (                    # noqa: E402
    CompressResult, Compressor, TokenEstimator, fix_orphaned_tool_pairs,
)

fails: list[str] = []


def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)


def assistant(text, calls=None):
    m = {'role': 'assistant', 'content': text}
    if calls:
        m['tool_calls'] = calls
    return m


def tool(call_id, text):
    return {'role': 'tool', 'tool_call_id': call_id, 'content': text}


def call(cid, name='compute', args='{}'):
    return {'id': cid, 'type': 'function',
            'function': {'name': name, 'arguments': args}}


def _verify_pairs(msgs) -> bool:
    """tool_calls 与 tool 消息完全配对。"""
    called = set()
    for m in msgs:
        for c in m.get('tool_calls') or []:
            called.add(c.get('id'))
    responded = {m.get('tool_call_id') for m in msgs if m.get('role') == 'tool'}
    return called == responded and all(
        m.get('role') != 'assistant' or (m.get('tool_calls') or m.get('content'))
        for m in msgs)


print('=' * 78)
print('模块 9：四级压缩库')
print('=' * 78)

# ---------------------------------------------------------------------------
print('\n[1] TokenEstimator')
est = TokenEstimator()
check('中文 1 字 ≈ 1 token', est.count_text('经营诊断') == 4, est.count_text('经营诊断'))
check('英文 4 字符 ≈ 1 token', est.count_text('abcd') == 1, est.count_text('abcd'))
check('空文本 0', est.count_text('') == 0)
m = {'role': 'user', 'content': '你好世界'}
n1 = est.count_message(m)
check('消息含结构开销', n1 == 4 + 4, n1)
m2 = {'role': 'assistant', 'content': '', 'tool_calls': [call('t1', 'f', '{"a":1}')]}
n2 = est.count_message(m2)
check('tool_calls 计入估算', n2 > 4, n2)
check('多条消息求和', est.count_messages([m, m2]) == n1 + n2)

# ---------------------------------------------------------------------------
print('\n[2] 未超预算 → 原样返回')
msgs = [{'role': 'system', 'content': '你是分析师'},
        {'role': 'user', 'content': '算毛利率'}]
comp = Compressor(estimator=est, budget_tokens=1000)
r = comp.compress(msgs)
check('level=none', r.level == 'none', r.level)
check('消息原样（同一对象）', r.messages is msgs)
check('前后 token 相等', r.tokens_before == r.tokens_after)

# ---------------------------------------------------------------------------
print('\n[3] L1 microcompact：截断旧工具结果')
long_tool = 'x' * 2000
msgs = ([{'role': 'system', 'content': 'sys'}]
        + [assistant('step', [call(f't{i}')]) for i in range(5)]
        + [tool(f't{i}', long_tool) for i in range(5)]
        + [{'role': 'user', 'content': '最近的问题'}])
budget = est.count_messages(msgs) - 500      # 制造轻度超限（L1 可解）
comp = Compressor(estimator=est, budget_tokens=budget, keep_recent=2)
r = comp.compress(msgs)
check('触发 microcompact', r.level == 'microcompact', r.level)
truncated = [m for m in r.messages if m.get('role') == 'tool'
             and '已截断' in (m.get('content') or '')]
check('超长旧工具结果被截断', len(truncated) >= 3, len(truncated))
tail_tools = [m for m in r.messages if m.get('role') == 'tool'
              and '已截断' not in (m.get('content') or '')]
check('最近 keep_recent 条不受影响', len(tail_tools) >= 1, len(tail_tools))
check('截断后回到预算内', r.tokens_after <= budget,
      f'{r.tokens_after} <= {budget}')
check('无孤儿（配对完整）', _verify_pairs(r.messages))
check('事件记录截断', any(e['action'] == 'microcompact' for e in r.events))

# ---------------------------------------------------------------------------
print('\n[4] L2 snip：分级删除中段')


msgs = ([{'role': 'system', 'content': 'sys 首条任务指令'}]
        + [{'role': 'user', 'content': f'问题{i} ' + 'y' * 120} for i in range(30)]
        + [{'role': 'user', 'content': '最近的问题'}])
budget = est.count_messages(msgs) - 800      # L1 无工具可截 → 直接到 L2
comp = Compressor(estimator=est, budget_tokens=budget, keep_recent=12)
r = comp.compress(msgs)
check('触发 snip', r.level == 'snip', r.level)
check('首条（任务指令）保留', r.messages[0]['content'].startswith('sys'))
check('最近 12 条保留', r.messages[-1]['content'] == '最近的问题'
      and len(r.messages) <= 30 + 1 - 3)
snip_events = [e for e in r.events if e['action'] == 'snip']
check('★ G3：每轮最多删 3 条', all(len(e['removed_indexes']) <= 3
                                    for e in snip_events),
      [len(e['removed_indexes']) for e in snip_events])
check('分级推进（多轮事件）', len(snip_events) >= 2, len(snip_events))
check('token 显著下降且保护区完整（首条+最近12条）',
      r.tokens_after < r.tokens_before and len(r.messages) >= 13,
      f'{r.tokens_before}->{r.tokens_after}, {len(r.messages)} 条')

# 孤儿场景：中段含 assistant(tool_calls)+tool 对，删除后必须修复
msgs = ([{'role': 'system', 'content': 'sys'}]
        + [{'role': 'user', 'content': f'q{i} ' + 'z' * 150} for i in range(10)]
        + [assistant('calling', [call('c1')]), tool('c1', 'r' * 200)]
        + [{'role': 'user', 'content': f'q{i} ' + 'z' * 150} for i in range(10, 20)]
        + [{'role': 'user', 'content': '最新'}])
budget = est.count_messages(msgs) - 600
comp = Compressor(estimator=est, budget_tokens=budget, keep_recent=12)
r = comp.compress(msgs)
check('★ 删除后无孤儿 tool_calls（配对完整）', _verify_pairs(r.messages))

# ---------------------------------------------------------------------------
print('\n[5] 孤儿修复函数（直接单测）')
fixed = fix_orphaned_tool_pairs([
    {'role': 'system', 'content': 's'},
    assistant('partial', [call('a'), call('b')]),
    tool('a', 'resp-a'),                      # b 无响应
    tool('ghost', 'resp-ghost'),              # 无来源
])
calls_left = fixed[1].get('tool_calls') or []
check('无响应的 call 被剥离', [c['id'] for c in calls_left] == ['a'])
check('孤儿 tool 消息被删除',
      all(m.get('tool_call_id') != 'ghost' for m in fixed))
fixed2 = fix_orphaned_tool_pairs([
    {'role': 'system', 'content': 's'},
    assistant('', [call('only')]),            # 全部无响应
    {'role': 'user', 'content': 'u'},
])
check('全无响应时剥离 tool_calls 且补占位文本',
      'tool_calls' not in fixed2[1] and fixed2[1]['content'],
      fixed2[1].get('content'))

# ---------------------------------------------------------------------------
print('\n[6] L3 compact：摘要 + computed_registry（G1）')
registry = {'M13|finance|2026-09': '51.0%', 'M17|refund': '6.6%'}
fake_summaries = []


def fake_summarizer(text: str) -> str:
    fake_summaries.append(text)
    return '用户询问了多个经营指标，已计算毛利率与退款率，讨论了第3周的波动原因。'


msgs = ([{'role': 'system', 'content': 'sys'}]
        + [{'role': 'user', 'content': f'q{i} ' + 'w' * 200} for i in range(25)]
        + [{'role': 'user', 'content': '最新'}])
budget = est.count_messages(msgs) - 1000    # L1 不够 → L3 摘要后应能回到预算内
comp = Compressor(estimator=est, summarizer=fake_summarizer,
                  budget_tokens=budget, keep_recent=4,
                  computed_registry=registry)
r = comp.compress(msgs)
check('触发 compact', r.level == 'compact', r.level)
compact_msgs = [m for m in r.messages if 'context-compaction' in (m.get('content') or '')]
check('产出摘要消息', len(compact_msgs) == 1)
if compact_msgs:
    blob = compact_msgs[0]['content']
    check('★ G1：registry 键值原样保留', 'M13|finance|2026-09: 51.0%' in blob)
    check('★ G1：标注不得重算', '不得重算' in blob)
check('summarizer 收到中段内容', any('q5' in s for s in fake_summaries))
check('摘要后回到预算内或显著降低', r.tokens_after < r.tokens_before)

# G2：无效摘要 → 放弃压缩
comp_bad = Compressor(estimator=est, summarizer=lambda t: '太短',
                      budget_tokens=budget, keep_recent=12)
r2 = comp_bad.compress(msgs)
check('★ G2：短摘要放弃 compact', r2.level != 'compact', r2.level)
check('G2：事件记录失败原因',
      any(e['action'] == 'compact_failed' and 'too short' in e['reason']
          for e in r2.events))


def boom(_t):
    raise RuntimeError('LLM 挂了')


comp_err = Compressor(estimator=est, summarizer=boom,
                      budget_tokens=budget, keep_recent=12)
r3 = comp_err.compress(msgs)
check('★ 摘要器异常 → 放弃且不抛出', r3.level != 'compact', r3.level)
check('异常路径记录事件',
      any(e['action'] == 'compact_failed' and 'summarizer error' in e['reason']
          for e in r3.events))

# ---------------------------------------------------------------------------
print('\n[7] L4 reactive：紧急路径')
msgs = [{'role': 'system', 'content': 's'}] \
    + [{'role': 'user', 'content': f'q{i} ' + 'v' * 200} for i in range(30)]
comp = Compressor(estimator=est, budget_tokens=10_000, keep_recent=12,
                  keep_recent_reactive=4)
r = comp.compress_reactive(msgs, error='Error: maximum context length is 8192 tokens')
check('level=reactive', r.level == 'reactive', r.level)
check('紧急模式删到保护边界（首条 + keep_recent_reactive=4）',
      len(r.messages) == 1 + 4, len(r.messages))
check('事件记录错误信息', any(e['action'] == 'reactive' for e in r.events))
check('首条保留', r.messages[0]['role'] == 'system')

# ---------------------------------------------------------------------------
print('\n[8] 幂等与通用性')
r_a = comp.compress(msgs := ([{'role': 'system', 'content': 's'}]
                             + [{'role': 'user', 'content': f'q{i}'} for i in range(20)]))
r_b = comp.compress(r_a.messages)
check('已压缩消息再压不再变化（预算内）',
      r_b.level == 'none' or r_b.tokens_after == r_a.tokens_after)
weird = [{'role': 'user', 'content': '任意业务内容', 'custom_field': {'x': 1}}]
r_w = Compressor(estimator=est, budget_tokens=1).compress(weird)
check('未知字段不影响行为（不抛错）', isinstance(r_w, CompressResult))

print()
print('=' * 78)
print('模块 9: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)
sys.exit(1 if fails else 0)

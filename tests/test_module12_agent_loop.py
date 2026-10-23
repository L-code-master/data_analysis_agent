"""模块 12：对话式分析 · 形态 B（Agent 循环，mock LLM，零真实调用）。

验证 docs/14 形态 B 的引擎机制：循环、工具分发、LoopGuard（3/3/8）、
成本记账、压缩接入、失败终止。LLM 用脚本化 Fake（见 llm_client.FakeLlmClient）。
"""
import os
import sys
import pathlib
import tempfile

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = pathlib.Path(__file__).resolve().parents[1]
# ★ 用工作区内临时目录：DSH 沙箱下 %TEMP% 无法创建 SQLite 库
TMP = ROOT / '.testtmp' / f'agent_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "agent.db"}'
os.environ['TENANTS_ROOT'] = str(TMP / 'tenants')
os.environ['APP_SECRET_KEY'] = 'test-secret-key-for-jwt-signing-32chars-minimum'
os.environ['LLM_API_KEY'] = 'fake-key-for-engine-test'

sys.path.insert(0, str(ROOT))

from app.config import reset_settings_cache       # noqa: E402
reset_settings_cache()
from app.storage import db                        # noqa: E402
db.apply_schema()

from app.services.agent_engine import (           # noqa: E402
    AgentEngine, LoopGuard, build_tools,
)
from app.services.llm_client import FakeLlmClient, LlmResponse  # noqa: E402
from app.storage import db as _db                 # noqa: E402
from app.services import billing                  # noqa: E402

fails: list[str] = []


def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)


def call(cid, name, args='{}'):
    return {'id': cid, 'name': name, 'arguments': args}


def resp_tools(*calls):
    return LlmResponse(content='', tool_calls=list(calls),
                       finish_reason='tool_calls', input_tokens=100,
                       output_tokens=50, reasoning_tokens=10)


TENANT = 'tenant-agent-test'


def make_tenant():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tenants (id,name,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", (TENANT, '引擎测试租户', 'active', now, now))
        conn.execute(
            "INSERT OR IGNORE INTO credit_accounts (id,tenant_id,balance,"
            "total_purchased,total_used,trial_used,cost_used_usd,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ('acc1', TENANT, 0, 0, 0, 0, 0.0, now, now))


make_tenant()

# fake 工具实现（引擎逻辑测试不需要真实数据）
FAKE_IMPL = {
    'list_metrics': lambda a: 'M13 毛利率(财务)：净销售-成本-佣金\nM14 毛利率(推广)：财务口径再扣广告',
    'compute_metric': lambda a: f"{a.get('metric_id')} = 51.00%；口径：退款按申请日",
    'always_fail': lambda a: '计算失败：测试故意失败',
}


def new_engine(script, impl=None, max_turns=8, **kw):
    return AgentEngine(
        client=FakeLlmClient(script),
        tenant_id=TENANT,
        tools_spec=[{'type': 'function', 'function': {'name': 'x',
                                                      'parameters': {}}}],
        tools_impl=impl or FAKE_IMPL,
        max_turns=max_turns,
        **kw,
    )


print('=' * 78)
print('模块 12：形态 B —— Agent 循环（mock LLM）')
print('=' * 78)

# ---------------------------------------------------------------------------
print('\n[1] 正常循环：查目录 → 算指标 → 回答')
script = [
    resp_tools(call('c1', 'list_metrics')),
    resp_tools(call('c2', 'compute_metric', '{"metric_id": "M13"}')),
    LlmResponse(content='毛利率（财务口径）为 51.00%，口径为退款按申请日。',
                finish_reason='stop', input_tokens=80, output_tokens=40,
                reasoning_tokens=5),
]
eng = new_engine(script)
res = eng.run('这个月毛利率多少？')
check('★ 循环 3 轮完成', res.turns == 3, res.turns)
check('★ 返回最终回答', '51.00%' in res.answer, res.answer[:50])
check('★ 工具轨迹 2 条', len(res.tool_trace) == 2,
      [t['tool'] for t in res.tool_trace])
check('轨迹含工具结果', '51.00%' in res.tool_trace[1]['result'])
check('usage 累计（3 次调用）',
      res.input_tokens == 280 and res.output_tokens == 140,
      f'{res.input_tokens}/{res.output_tokens}')
check('reasoning tokens 单列', res.reasoning_tokens == 25, res.reasoning_tokens)
check('★ 成本记账（usage_records）',
      billing.usage_summary(TENANT)['calls'] >= 1,
      billing.usage_summary(TENANT))
check('成本估算非零', res.cost_usd > 0, res.cost_usd)

# ★ P1-1：每行必须存单次成本，而不是累计值
with _db.get_conn() as c:
    cost_rows = c.execute(
        "SELECT input_tokens,output_tokens,cost_usd FROM usage_records "
        "WHERE tenant_id=? ORDER BY created_at", (TENANT,)).fetchall()
per_call = round(100 / 1_000_000 * 0.11 + 50 / 1_000_000 * 0.55, 6)
third_call = round(80 / 1_000_000 * 0.11 + 40 / 1_000_000 * 0.55, 6)
expected = [per_call, per_call, third_call]
sum_cost = sum(r[2] for r in cost_rows)
check('★ 3 次调用记 3 行 usage', len(cost_rows) == 3, len(cost_rows))
check('★ 每行存单次成本（不是累计值）',
      all(abs(r[2] - e) < 1e-9 for r, e in zip(cost_rows, expected)),
      [round(r[2], 8) for r in cost_rows])
check('★ usage 合计 = 引擎返回成本',
      abs(sum_cost - res.cost_usd) < 1e-9,
      f'{sum_cost:.8f} vs {res.cost_usd:.8f}')

# 消息结构校验（发给 LLM 的第 2 轮应含 tool 消息）
fake: FakeLlmClient = eng.client
round2 = fake.calls[1]['messages']
check('★ tool 结果注入上下文',
      any(m.get('role') == 'tool' and 'M13' in m.get('content', '')
          for m in round2))
check('system prompt 含纪律（数字来自工具）',
      '禁止编造' in fake.calls[0]['messages'][0]['content'])

# ---------------------------------------------------------------------------
print('\n[2] 多轮历史注入')
res = new_engine([LlmResponse(content='好的', input_tokens=10, output_tokens=5)]).run(
    '接着上面说', history=[
        {'role': 'user', 'content': '前面问了毛利率'},
        {'role': 'assistant', 'content': '毛利率 51%'},
    ])
check('★ 历史进入上下文', '前面问了毛利率' in
      str(new_engine.__dict__ if False else ''), '') if False else None
eng2 = new_engine([LlmResponse(content='收到', input_tokens=10, output_tokens=5)])
eng2.run('接着说', history=[{'role': 'user', 'content': '前面问了毛利率'}])
check('★ 历史进入上下文',
      any('前面问了毛利率' in str(m.get('content', ''))
          for m in eng2.client.calls[0]['messages']))

# ---------------------------------------------------------------------------
print('\n[3] LoopGuard（阈值 3/3/8）')
g = LoopGuard()
ok1, _ = g.check_call('t', '{"a":1}')
ok2, _ = g.check_call('t', '{"a":1}')
ok3, msg3 = g.check_call('t', '{"a":1}')
check('相同调用前 2 次放行', ok1 and ok2)
check('★ 第 3 次被阻止', not ok3 and '阻止' in msg3, msg3[:40])
ok_diff, _ = g.check_call('t', '{"a":2}')
check('不同参数不受影响', ok_diff)

g2 = LoopGuard()
for i in range(7):
    g2.record_result('tool_x', False)
still = g2.record_result('tool_x', False)
check('★ 连续失败 8 次终止', not still)
g2.record_result('tool_x', True)
check('成功重置连败计数', g2.record_result('tool_x', False))

# 引擎级：连续同调用 → guard 拦截后模型仍要最终回答
script = [
    resp_tools(call('d1', 'compute_metric', '{"metric_id": "M13"}')),
    resp_tools(call('d2', 'compute_metric', '{"metric_id": "M13"}')),
    resp_tools(call('d3', 'compute_metric', '{"metric_id": "M13"}')),   # 第3次被阻
    LlmResponse(content='基于已获得的数据回答：51.00%。', input_tokens=50,
                output_tokens=30),
]
res = new_engine(script).run('毛利率')
check('★ 引擎内 guard 生效（被阻调用换成了提示文本）',
      any('循环守卫阻止' in t['result'] for t in res.tool_trace),
      [t['result'][:30] for t in res.tool_trace])
check('被阻后仍能收口回答', '51.00%' in res.answer)

# ---------------------------------------------------------------------------
print('\n[4] 连续失败终止（8 次）')
script = [resp_tools(call(f'f{i}', 'always_fail')) for i in range(10)]
res = new_engine(script, max_turns=12).run('测失败')
check('★ 连续失败 8 次循环终止', '连续失败' in res.error, res.error[:60])
check('失败终止不带答案', res.answer == '')

# ---------------------------------------------------------------------------
print('\n[5] 步数上限兜底收口')
script = [resp_tools(call(f's{i}', 'list_metrics')) for i in range(3)]
script.append(LlmResponse(content='基于目录信息作答。', input_tokens=10,
                          output_tokens=10))
res = new_engine(script, max_turns=3).run('列出所有指标')
check('★ 步数耗尽后强制收口', '基于目录' in res.answer, res.answer[:40])
check('实际轮次 ≤ 上限+1', res.turns <= 4, res.turns)

# ---------------------------------------------------------------------------
print('\n[6] 上下文压缩接入（超预算触发 v1.3 压缩库）')
# run() 只保留最近 20 条历史 → 单条造长（3000 token × 20 条 > 60000 预算）
history = [{'role': 'user', 'content': f'历史问题{i} ' + '长' * 3000}
           for i in range(25)]
script = [LlmResponse(content='收到', input_tokens=10, output_tokens=5)]
res = new_engine(script).run('现在回答', history=history)
comp_events = [t for t in res.tool_trace if t['tool'] == '_compress']
check('★ 超预算触发压缩', len(comp_events) >= 1,
      comp_events[0]['level'] if comp_events else '未触发')
if comp_events:
    check('压缩级别为 snip（未注入 summarizer）',
          comp_events[0]['level'] in ('snip', 'microcompact'), comp_events[0]['level'])

# ---------------------------------------------------------------------------
print('\n[7] LLM 异常路径')
script = [RuntimeError('boom'), LlmResponse(content='重试后成功', input_tokens=5,
                                            output_tokens=5)]
res = new_engine(script).run('测试')
check('客户端异常被捕获（错误文本含原始异常）',
      res.error and 'boom' in res.error, res.error[:50])

script2 = [LlmResponse(content='直接回答', input_tokens=5, output_tokens=5)]
res2 = new_engine(script2).run('无需工具的问题')
check('零工具直接回答（turns=1）', res2.turns == 1 and res2.answer == '直接回答')

# ---------------------------------------------------------------------------
print('\n[8] 工具集规格（build_tools）')
import duckdb                                         # noqa: E402
con = duckdb.connect()
spec, impl = build_tools(reg=None, con_getter=lambda: con)
names = [t['function']['name'] for t in spec]
check('★ 5 个工具注册', names == ['list_metrics', 'compute_metric',
                                   'product_breakdown', 'weekly_compare',
                                   'anomaly_days'], names)
check('每个工具都有 description', all(t['function'].get('description')
                                       for t in spec))
check('实现表与规格一致', set(impl) == set(names))
con.close()

# ---------------------------------------------------------------------------
print('\n[9] 数据上下文注入（docs/15）')
eng3 = new_engine([LlmResponse(content='好的', input_tokens=10, output_tokens=5)])
eng3.run('毛利率多少', data_context=
         '当前数据上下文：6 个数据源（样例数据.xlsx），周期 2026-09-01 ~ 2026-09-28，'
         '诊断于 2026-10-19。回答涉及数据范围时请主动说明。')
sys_msg = eng3.client.calls[0]['messages'][0]['content']
check('★ data_context 注入 system prompt',
      '当前数据上下文' in sys_msg and '2026-09-01' in sys_msg)
check('注入在纪律 prompt 之后（追加式）', '禁止编造' in sys_msg)
eng4 = new_engine([LlmResponse(content='ok', input_tokens=5, output_tokens=5)])
eng4.run('无上下文')
check('不传 data_context 时 prompt 不变',
      '当前数据上下文' not in eng4.client.calls[0]['messages'][0]['content'])

# ---------------------------------------------------------------------------
print('\n[10] 预算护栏（docs/16 P1-13）')
from unittest.mock import patch                          # noqa: E402
eng_deadline = new_engine(
    [LlmResponse(content='不该被调用', input_tokens=1, output_tokens=1)],
    deadline_seconds=1)
# 第一次取 now 用于计算 deadline，第二次检查时已过 1000 秒
with patch('app.services.agent_engine.time.monotonic',
           side_effect=[1000.0, 2000.0]):
    res_deadline = eng_deadline.run('超时测试')
check('★ deadline 立即终止', bool(res_deadline.error)
      and '超时' in res_deadline.error, res_deadline.error)
check('超时后不再调用 LLM', len(eng_deadline.client.calls) == 0,
      len(eng_deadline.client.calls))

eng_cost = new_engine(
    [resp_tools(call('b1', 'list_metrics')),
     resp_tools(call('b2', 'list_metrics')),
     LlmResponse(content='不该继续', input_tokens=1, output_tokens=1)],
    max_cost_usd=0.000001)
res_cost = eng_cost.run('成本测试')
check('★ 成本超过上限终止',
      '成本超过上限' in (res_cost.error or ''), res_cost.error)
check('成本终止前只调用 1 次 LLM',
      len(eng_cost.client.calls) == 1, len(eng_cost.client.calls))

print()
print('=' * 78)
print('模块 12: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)
sys.exit(1 if fails else 0)

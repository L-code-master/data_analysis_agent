"""模块 11：对话式分析 · 形态 A（查数问答，零 LLM）。

验证 docs/14 形态 A：问题 → 意图识别 → 确定性计算 → 模板回答。
全部答案来自 SQL —— 断言数字正确、口径随答、路由正确。
"""
import os
import sys
import pathlib
import tempfile
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = pathlib.Path(__file__).resolve().parents[1]
# ★ 用工作区内临时目录：DSH 沙箱下 %TEMP% 无法创建 SQLite 库
TMP = ROOT / '.testtmp' / f'chatqa_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "qa.db"}'
os.environ['TENANTS_ROOT'] = str(TMP / 'tenants')
os.environ['APP_SECRET_KEY'] = 'test-secret-key-for-jwt-signing-32chars-minimum'
os.environ['SIGNUP_FREE_CREDITS'] = '1'
os.environ['LLM_API_KEY'] = ''            # ★ 形态 A 不需要；同时验证无 key 时形态 B 优雅降级

sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient         # noqa: E402

from app.config import reset_settings_cache       # noqa: E402
reset_settings_cache()
from app.main import app                          # noqa: E402
from app.storage import db                        # noqa: E402
from app.services import sample_data              # noqa: E402

db.apply_schema()
client = TestClient(app)
fails: list[str] = []


def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)


print('=' * 78)
print('模块 11：形态 A —— 对话式查数（零 LLM）')
print('=' * 78)

# ---------------------------------------------------------------------------
print('\n[0] 准备数据：注册 + 样例上传 + 诊断')
r = client.post('/api/v1/auth/register', json={
    'email': 'qa@test.com', 'password': 'QaTest!2026x', 'tenant_name': '问答店'})
check('注册', r.status_code == 201, r.status_code)
TENANT_ID = r.json()['user']['tenant_id']
T = r.json()['token']['access_token']
H = {'Authorization': f'Bearer {T}'}

xlsx = sample_data.build_xlsx()
r = client.post('/api/v1/sources', headers=H,
                files={'file': ('样例.xlsx', xlsx,
                                'application/vnd.openxmlformats-'
                                'officedocument.spreadsheetml.sheet')})
srcs = r.json()
check('上传拆分 6 源', r.status_code == 201 and len(srcs) == 6, len(srcs))
for s in srcs:
    p = client.get(f'/api/v1/sources/{s["source_id"]}/profile', headers=H).json()
    m = {x['target_field']: x['source_column'] for x in p['mappings']}
    client.post(f'/api/v1/sources/{s["source_id"]}/mapping', headers=H,
                json={'mapping': m})
ids = [s['source_id'] for s in srcs]
r = client.post('/api/v1/diagnoses', headers=H,
                json={'source_ids': ids, 'time_range': '2026-09-01~2026-09-28'})
DID = r.json()['diagnosis_id']
for _ in range(120):
    r = client.get(f'/api/v1/diagnoses/{DID}', headers=H)
    if r.json().get('status') in ('succeeded', 'failed'):
        break
    time.sleep(0.4)
check('诊断成功（数据就绪）', r.json().get('status') == 'succeeded',
      r.json().get('status'))

# ---------------------------------------------------------------------------
print('\n[1] 会话管理')
r = client.post('/api/v1/chat/sessions', headers=H, params={'title': '经营问答'})
check('建会话 201', r.status_code == 201, r.status_code)
SID = r.json()['session_id']
r = client.get('/api/v1/chat/sessions', headers=H)
check('会话列表', r.status_code == 200 and len(r.json()) >= 1)
r = client.get(f'/api/v1/chat/sessions/{SID}/messages', headers=H)
check('消息历史空', r.status_code == 200 and r.json() == [])


def ask(q: str):
    r = client.post(f'/api/v1/chat/sessions/{SID}/ask', headers=H,
                    json={'question': q})
    return r.status_code, (r.json() if r.status_code == 200 else {})

# ---------------------------------------------------------------------------
print('\n[2] 指标查询（主路径）')
st, a = ask('毛利率是多少')
check('★ 毛利率走 qa 路由', st == 200 and a.get('route') == 'qa', a.get('route'))
check('★ 数字正确（≈51%）',
      '51' in a.get('answer', ''), a.get('answer', '')[:60])
check('答案含口径/计算方式', '计算方式' in a.get('answer', ''))
check('答案含统计周期', '统计周期' in a.get('answer', ''))

st, a = ask('支付转化率多少')
check('转化率走 qa', a.get('route') == 'qa')
check('★ 转化率正确（≈2.5%）', '2.5' in a.get('answer', ''),
      a.get('answer', '')[:60])

st, a = ask('访客数多少')
check('访客数 = 51,283', '51,283' in a.get('answer', ''),
      a.get('answer', '')[:60])

# ---------------------------------------------------------------------------
print('\n[3] 时间窗解析')
st, a = ask('2026-09-15~2026-09-21 的毛利率是多少')
check('显式日期段生效', '2026-09-15 ~ 2026-09-21' in a.get('answer', ''),
      a.get('answer', '')[:80])
st, a2 = ask('最近30天 毛利率多少')
check('短语时间窗生效（不报错）', st == 200 and a2.get('route') == 'qa',
      a2.get('route'))

# ---------------------------------------------------------------------------
print('\n[4] 归因区块问答（v1.2 确定性聚合的复用）')
st, a = ask('哪个商品退款率高')
check('★ 商品拆解走 qa', a.get('route') == 'qa', a.get('route'))
check('答案含连衣裙', '连衣裙' in a.get('answer', ''))
check('答案含重点关注标记', '重点关注' in a.get('answer', ''))

st, a = ask('每周对比一下销售')
check('周对比走 qa', a.get('route') == 'qa')
check('答案含周标签', '起' in a.get('answer', '') and '变化最大' in a.get('answer', ''))

st, a = ask('哪天有异常波动')
check('异常日走 qa', a.get('route') == 'qa')
check('答案含异常日', '异常日' in a.get('answer', '') or '没有检测到' in a.get('answer', ''))

st, a = ask('毛利率两种口径对比')
check('口径对比走 qa', a.get('route') == 'qa')
check('答案含财务/推广口径', '财务口径' in a.get('answer', '')
      and '推广口径' in a.get('answer', ''))

# ---------------------------------------------------------------------------
print('\n[5] 路由到形态 B（无 key 时降级提示）')
st, a = ask('为什么转化率跌了？帮我分析一下')
check('为什么类问题不进 qa', a.get('route') != 'qa', a.get('route'))
check('★ 无 LLM 时降级为 no_llm 提示', a.get('route') == 'no_llm', a.get('route'))
check('降级提示引导查数', '查数' in a.get('answer', ''))

# 口径歧义（"毛利率"精确命中 M13 不歧义；构造真歧义较难 → 验证 resolve 保护不误伤）
st, a = ask('毛利是多少')
check('模糊词不崩溃（命中或降级均可）', st == 200, st)

# ---------------------------------------------------------------------------
print('\n[6] 消息落库（route 标记）')
r = client.get(f'/api/v1/chat/sessions/{SID}/messages', headers=H)
msgs = r.json()
user_msgs = [m for m in msgs if m['role'] == 'user']
qa_msgs = [m for m in msgs if m['route'] == 'qa']
check('用户消息已存', len(user_msgs) >= 10, len(user_msgs))
check('★ qa 回答带 route 标记', len(qa_msgs) >= 8, len(qa_msgs))
check('meta 含指标信息',
      any(m['meta'].get('metric_id') for m in qa_msgs))

# 跨租户隔离
r2 = client.post('/api/v1/auth/register', json={
    'email': 'other@test.com', 'password': 'OtherTest!2026x', 'tenant_name': '别家'})
T2 = r2.json()['token']['access_token']
r = client.get(f'/api/v1/chat/sessions/{SID}/messages',
               headers={'Authorization': f'Bearer {T2}'})
check('跨租户读不到会话消息', r.status_code in (404, 200) and
      (r.status_code == 404 or r.json() == []), r.status_code)

# ---------------------------------------------------------------------------
print('\n[7] 数据上下文（docs/15：对话基于哪份数据）')
r = client.get('/api/v1/chat/context', headers=H)
check('context 200', r.status_code == 200)
c = r.json()
check('★ 数据可用（基于最近诊断）', c['data']['available'] is True)
check('★ 文件数 = 6（诊断时的全部数据源）', c['data']['file_count'] == 6,
      c['data']['file_count'])
check('周期正确', '2026-09-01' in (c['data'].get('time_range') or ''),
      c['data'].get('time_range'))
check('配额 = 5（1 次赠送 × 5）', c['agent_quota']['quota'] == 5,
      c['agent_quota'])
check('已用 0', c['agent_quota']['used'] == 0)

# stale：上传新文件但不诊断 → 上下文标记过期
_new_csv = ('订单编号,子订单编号,订单创建时间,买家付款时间,发货时间,'
            '确认收货时间,订单状态,退款状态,买家会员名,商品ID,商品标题,'
            '商家编码,SKU ID,sku属性,单价,购买数量,买家实际支付金额,'
            '商品金额,运费,优惠金额,商家实收金额,收货省份\n'
            'TBX,TBX-1,2026-09-20 10:00:00,2026-09-20 10:05:00,'
            '2026-09-21 18:00:00,2026-09-24 12:00:00,交易成功,无退款,'
            'b9,ITEM-X,测试裙,MC-X,SKU-1,黑;M,100.00,1,100.00,100.00,'
            '0.00,0.00,100.00,浙江\n').encode('utf-8-sig')
r = client.post('/api/v1/sources', headers=H,
                files={'file': ('新订单.csv', _new_csv, 'text/csv')})
check('新文件上传 201', r.status_code == 201, r.status_code)
r = client.get('/api/v1/chat/context', headers=H)
c2 = r.json()
check('★ stale = true（新上传未诊断）', c2['data']['stale'] is True)
check('newer_uploads ≥ 1', c2['data']['newer_uploads'] >= 1)
check('★ 上下文仍指向旧诊断（file_count 仍 6）',
      c2['data']['file_count'] == 6)

# ---------------------------------------------------------------------------
print('\n[8] Agent 对话配额（每诊断额度 ×5；A 不受限；被拒不落库）')
# 直接造 5 条 agent 回答记录（模拟已用完配额）
import datetime as _dt
import uuid as _uuid
from app.storage import db as _db
with _db.transaction() as _conn:
    for _ in range(5):
        _conn.execute(
            'INSERT INTO chat_messages (id,session_id,tenant_id,role,content,'
            "route,meta,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (_uuid.uuid4().hex, SID, TENANT_ID, 'assistant',
             '历史 agent 回答', 'agent', '{}',
             _dt.datetime.now(_dt.timezone.utc).isoformat()))

r = client.get('/api/v1/chat/context', headers=H)
check('已用 = 5', r.json()['agent_quota']['used'] == 5,
      r.json()['agent_quota'])

msgs_before = len(client.get(f'/api/v1/chat/sessions/{SID}/messages',
                             headers=H).json())
r = client.post(f'/api/v1/chat/sessions/{SID}/ask', headers=H,
                json={'question': '为什么转化率跌了？帮我分析'})
check('★ 配额用尽 → 402', r.status_code == 402, r.status_code)
check('错误码 AGENT_QUOTA_EXCEEDED',
      r.json().get('code') == 'AGENT_QUOTA_EXCEEDED')
check('★ 被拒问题不落库',
      len(client.get(f'/api/v1/chat/sessions/{SID}/messages',
                     headers=H).json()) == msgs_before)

# 形态 A 仍不受限
st, a = ask('支付金额多少')
check('★ 配额用尽后查数（A）仍正常', st == 200 and a.get('route') == 'qa')

# 购买增加配额（pack5 = +5 次诊断 → 配额 30）
# ★ 核销走服务层 mark_paid：API 的 confirm 现在仅平台管理员可调
#   （docs/16 P0-1），本模块测的是配额记账而非核销权限
from app.services import billing as _billing_svc          # noqa: E402
o = client.post('/api/v1/billing/orders', headers=H,
                json={'sku': 'pack5'}).json()
_billing_svc.mark_paid(o['order_no'], reviewed_by='test')
r = client.get('/api/v1/chat/context', headers=H)
check('★ 购买后配额 = 30（(5购买+1赠送)×5）',
      r.json()['agent_quota']['quota'] == 30, r.json()['agent_quota'])

# admin 不受限
with _db.transaction() as _conn:
    _conn.execute("UPDATE users SET role='admin' WHERE email='qa@test.com'")
r = client.post(f'/api/v1/chat/sessions/{SID}/ask', headers=H,
                json={'question': '为什么转化率跌了？帮我分析'})
check('★ admin 不受配额限制（200，走 no_llm 因无 key）',
      r.status_code == 200 and r.json().get('route') == 'no_llm',
      f'{r.status_code}/{r.json().get("route") if r.status_code == 200 else r.text[:60]}')

# ---------------------------------------------------------------------------
print('\n[9] 成本熔断错误映射（docs/16 P1-7）')
from unittest.mock import patch                              # noqa: E402
from app.services import chat as _chat_svc                   # noqa: E402
with patch.object(_chat_svc, 'ask',
                  side_effect=_chat_svc.CostQuotaExceededError(
                      '本月算力额度已用完（6.00/5.00 USD）')):
    r = client.post(f'/api/v1/chat/sessions/{SID}/ask', headers=H,
                    json={'question': '为什么转化率跌了？帮我分析'})
check('★ 成本熔断 → 402', r.status_code == 402, r.status_code)
check('错误码 COST_QUOTA_EXCEEDED',
      r.json().get('code') == 'COST_QUOTA_EXCEEDED', r.text[:80])

# ---------------------------------------------------------------------------
print('\n[10] 提问走 JSON body 且限长（docs/16 P2-18）')
r = client.post(f'/api/v1/chat/sessions/{SID}/ask', headers=H,
                json={'question': 'x' * 2001})
check('★ 超长问题 422', r.status_code == 422, r.status_code)
r = client.post(f'/api/v1/chat/sessions/{SID}/ask', headers=H,
                params={'question': '毛利率多少'})
check('★ 旧 query 方式不再接受（422）', r.status_code == 422, r.status_code)

print()
print('=' * 78)
print('模块 11: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)
sys.exit(1 if fails else 0)

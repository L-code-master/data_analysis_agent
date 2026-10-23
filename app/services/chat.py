"""对话式分析服务 —— 形态 A/B 的统一入口与路由（docs/14）。

路由规则：先试形态 A（查数，确定性、零成本）；A 判定需要多步推理 →
转形态 B（Agent 循环，LLM + 工具）。两类回答都落库（chat_messages），
带 route 标记 —— 月底看"多少问题被 A 秒答、多少进了 B"，就是路由健康度。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

from app.config import get_settings
from app.storage import db
from app.services.agent_engine import AgentEngine, build_tools
from app.services.llm_client import LlmClient
from app.services import billing, locks
from app.services.qa_engine import NeedsAgentError, QaAnswer, QaEngine
from app.semantic.registry import SemanticRegistry
from app.services.diagnosis import _open_semantic_db


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _rg(row, name, idx):
    return row[name] if hasattr(row, 'keys') else row[idx]


# ---------------------------------------------------------------------------
# 会话与消息存储
# ---------------------------------------------------------------------------

def create_session(tenant_id: str, user_id: str = '', title: str = '') -> dict:
    sid = uuid.uuid4().hex
    now = _now()
    with db.transaction() as conn:
        conn.execute(
            'INSERT INTO chat_sessions (id,tenant_id,created_by,title,'
            'created_at,updated_at) VALUES (?,?,?,?,?,?)',
            (sid, tenant_id, user_id or None, title[:80], now, now))
    return {'session_id': sid, 'title': title[:80], 'created_at': now}


def list_sessions(tenant_id: str, limit: int = 30) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            'SELECT id,title,created_at,updated_at FROM chat_sessions '
            'WHERE tenant_id = ? ORDER BY updated_at DESC LIMIT ?',
            (tenant_id, int(limit))).fetchall()
    return [{'session_id': _rg(r, 'id', 0),
             'title': _rg(r, 'title', 1) or '',
             'created_at': _rg(r, 'created_at', 2),
             'updated_at': _rg(r, 'updated_at', 3)} for r in rows]


def get_session(tenant_id: str, session_id: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT id,title,created_at FROM chat_sessions '
            'WHERE id = ? AND tenant_id = ?', (session_id, tenant_id)).fetchone()
    if row is None:
        return None
    return {'session_id': _rg(row, 'id', 0), 'title': _rg(row, 'title', 1) or '',
            'created_at': _rg(row, 'created_at', 2)}


def list_messages(tenant_id: str, session_id: str, limit: int = 100) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            'SELECT id,role,content,route,meta,created_at FROM chat_messages '
            'WHERE session_id = ? AND tenant_id = ? '
            'ORDER BY created_at LIMIT ?',
            (session_id, tenant_id, int(limit))).fetchall()
    out = []
    for r in rows:
        meta = {}
        try:
            meta = json.loads(_rg(r, 'meta', 4) or '{}')
        except (json.JSONDecodeError, TypeError):
            pass
        out.append({'message_id': _rg(r, 'id', 0),
                    'role': _rg(r, 'role', 1),
                    'content': _rg(r, 'content', 2),
                    'route': _rg(r, 'route', 3) or '',
                    'meta': meta,
                    'created_at': _rg(r, 'created_at', 5)})
    return out


def _save_message(session_id: str, tenant_id: str, role: str, content: str,
                  route: str = '', meta: dict | None = None) -> str:
    mid = uuid.uuid4().hex
    with db.transaction() as conn:
        conn.execute(
            'INSERT INTO chat_messages (id,session_id,tenant_id,role,content,'
            'route,meta,created_at) VALUES (?,?,?,?,?,?,?,?)',
            (mid, session_id, tenant_id, role, content, route,
             json.dumps(meta or {}, ensure_ascii=False), _now()))
        conn.execute('UPDATE chat_sessions SET updated_at = ? WHERE id = ?',
                     (_now(), session_id))
    return mid


# ---------------------------------------------------------------------------
# 数据上下文（docs/15 第一部分）：对话基于哪份数据
# ---------------------------------------------------------------------------

def get_data_context(tenant_id: str) -> dict:
    """返回对话当前基于的数据范围（最近一次成功诊断的快照）。

    stale = sources 表存在比该诊断更晚的上传（新数据未诊断，回答基于旧数据）。
    """
    from app.services import diagnosis as dg

    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT id, finished_at FROM diagnoses '
            'WHERE tenant_id = ? AND status = ? '
            'ORDER BY created_at DESC LIMIT 1',
            (tenant_id, 'succeeded')).fetchone()
    if row is None:
        return {'available': False, 'stale': False,
                'message': '尚无诊断数据 —— 请先上传数据并完成一次诊断'}

    did = _rg(row, 'id', 0)
    finished = _rg(row, 'finished_at', 1) or ''
    try:
        rpt = dg.get_report_json(tenant_id, did)
    except Exception:                               # noqa: BLE001
        return {'available': False, 'stale': False,
                'message': '最近诊断的报告不可读，请重新诊断'}

    src = rpt.get('source', {})
    tr = rpt.get('time_range') or {}
    with db.get_conn() as conn:
        newer = conn.execute(
            'SELECT COUNT(*) FROM sources WHERE tenant_id = ? AND created_at > ?',
            (tenant_id, finished)).fetchone()[0]

    return {
        'available': True,
        'diagnosis_id': did,
        'files': [f['name'] for f in src.get('files', [])][:8],
        'file_count': src.get('file_count', 0),
        'row_count': src.get('row_count', 0),
        'time_range': (f"{tr.get('start', '')} ~ {tr.get('end', '')}"
                       if tr.get('start') else '全部数据'),
        'diagnosed_at': finished,
        'stale': bool(newer),
        'newer_uploads': int(newer or 0),
    }


def _data_context_prompt(ctx: dict) -> str:
    """数据范围的 prompt 注入文本（Agent 回答时可主动引用）。"""
    if not ctx.get('available'):
        return '当前数据上下文：语义库暂无数据（工具查询会返回无数据）。'
    stale = ('；⚠注意：用户已上传更新的数据但尚未重新诊断，当前回答基于旧数据'
             if ctx.get('stale') else '')
    return (f"当前数据上下文：{ctx.get('file_count', 0)} 个数据源"
            f"（{('、'.join(ctx.get('files', [])[:4]))}），"
            f"周期 {ctx.get('time_range', '')}，诊断于 "
            f"{str(ctx.get('diagnosed_at', ''))[:10]}{stale}。"
            f"回答涉及数据范围时请主动说明。")


# ---------------------------------------------------------------------------
# Agent 对话配额（docs/15 第二部分）
# ---------------------------------------------------------------------------

class AgentQuotaExceededError(Exception):
    """Agent 对话配额用尽（形态 A 查数不受限）。"""

    def __init__(self, quota: int, used: int):
        self.quota = quota
        self.used = used
        super().__init__(
            f'Agent 追问次数已用完（{used}/{quota}）。'
            f'每获得 1 次诊断额度附送 5 次追问；查数类问题仍然免费。')


class CostQuotaExceededError(Exception):
    """本月 LLM 成本熔断（docs/16 P1-7：chat 路径此前完全不检查）。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def agent_chat_quota(tenant_id: str) -> tuple[int, int]:
    """返回 (配额, 已用)。配额 -1 = 不限。

    配额 =（购买 + 赠送获得的诊断次数）× AGENT_CHATS_PER_CREDIT；
    已用 = 该租户 route='agent' 的回答条数（agent_error 不计 —— 失败不收费）。
    """
    s = get_settings()
    per = s.agent_chats_per_credit
    with db.get_conn() as conn:
        used = conn.execute(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE tenant_id = ? AND route = 'agent'",
            (tenant_id,)).fetchone()[0]
    if per <= 0:
        return -1, int(used)

    with db.get_conn() as conn:
        grants = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM credit_ledger "
            "WHERE tenant_id = ? AND reason = 'grant'",
            (tenant_id,)).fetchone()[0]
    from app.services import billing as _billing
    acc = _billing.get_account(tenant_id)
    acquired = (acc.total_purchased or 0) + int(grants or 0)
    return acquired * per, int(used)


def _is_admin_user(user_id: str) -> bool:
    from app.services import auth as _auth
    try:
        u = _auth.get_user(user_id)
        return bool(u and u.is_admin)
    except Exception:                               # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 问答主入口（A → B 路由）
# ---------------------------------------------------------------------------

def ask(tenant_id: str, user_id: str, session_id: str,
        question: str) -> dict:
    """回答一个问题。返回 {answer, route, meta}。

    路由：形态 A（确定性查数）优先；NeedsAgentError → 形态 B（LLM 循环）。
    配额：仅形态 B 受限（每获得 1 次诊断额度附送 AGENT_CHATS_PER_CREDIT 次）；
    被配额拒绝的问题不落库。

    ★ 并发纪律（docs/16 P0-7）：DuckDB 只在持租户锁期间使用。
      第一段（持锁）：开库 + 升级补丁 + 形态 A 查数，结束即关连接放锁；
      第二段（放锁）：Agent 循环的 LLM 调用不占锁 —— 每次工具调用
      单独短持锁（开连接 → 查询 → 关连接），避免数十秒的 LLM 往返
      饿死同租户的诊断请求。
    """
    question = (question or '').strip()
    if not question:
        return {'answer': '请输入问题。', 'route': 'none', 'meta': {}}

    if get_session(tenant_id, session_id) is None:
        return {'answer': '会话不存在', 'route': 'error', 'meta': {}}

    history: list[dict] = []

    # ---- 第一段（持租户锁）：语义层 + 形态 A ----
    with locks.tenant_lock(tenant_id):
        # _open_semantic_db 已按版本迁移（只落后时执行一次，docs/16 P1-12/16）
        con = _open_semantic_db(tenant_id)
        try:
            reg = SemanticRegistry(con)
            reg.load()

            # ---- 形态 A：查数（零 LLM，永不限流）----
            qa = QaEngine(con, reg)
            try:
                a: QaAnswer = qa.answer(question)
                _save_message(session_id, tenant_id, 'user', question)
                _save_message(session_id, tenant_id, 'assistant', a.text,
                              route='qa', meta=a.meta)
                return {'answer': a.text, 'route': 'qa', 'meta': a.meta}
            except NeedsAgentError as reason:
                # ---- 配额检查（admin 不受限；被拒不落库）----
                if not _is_admin_user(user_id):
                    quota, used = agent_chat_quota(tenant_id)
                    if quota >= 0 and used >= quota:
                        raise AgentQuotaExceededError(quota, used)

                if not get_settings().llm_api_key:
                    text = (f'该问题需要多步分析（{reason}），但当前未配置 LLM。'
                            f'试试查数类问题，如"这个月毛利率多少""哪个商品退款率高"。')
                    _save_message(session_id, tenant_id, 'user', question)
                    _save_message(session_id, tenant_id, 'assistant', text,
                                  route='no_llm')
                    return {'answer': text, 'route': 'no_llm', 'meta': {}}

                # ★ 成本熔断（docs/16 P1-7）：诊断入口有 check_cost_quota，
                #   chat 的 Agent 循环此前完全不检查 —— 可绕过月上限烧 LLM。
                ok, cost_reason = billing.check_cost_quota(tenant_id)
                if not ok:
                    raise CostQuotaExceededError(cost_reason)

                _save_message(session_id, tenant_id, 'user', question)
                history = [{'role': m['role'], 'content': m['content']}
                           for m in list_messages(tenant_id, session_id)[:-1]
                           if m['role'] in ('user', 'assistant')]
        finally:
            try:
                con.close()
            except Exception:                       # noqa: BLE001
                pass

    # ---- 第二段（已放锁）：Agent 循环（LLM 调用不占锁）----
    ctx = get_data_context(tenant_id)

    # 工具用完即关的连接池：con_getter 开的连接由 _locked 包装层回收
    opened: list = []

    def con_getter():
        c = _open_semantic_db(tenant_id)
        opened.append(c)
        return c

    def _locked(fn):
        def call(args: dict) -> str:
            with locks.tenant_lock(tenant_id):
                try:
                    return fn(args)
                finally:
                    while opened:
                        try:
                            opened.pop().close()
                        except Exception:           # noqa: BLE001
                            pass
        return call

    spec, impl = build_tools(reg, con_getter)
    impl = {name: _locked(fn) for name, fn in impl.items()}
    engine = AgentEngine(LlmClient(), tenant_id, spec, impl)
    res = engine.run(question, history=history,
                     data_context=_data_context_prompt(ctx))

    if res.error and not res.answer:
        text = f'分析失败：{res.error}'
        route = 'agent_error'
    else:
        text = res.answer
        route = 'agent'
    meta = {'tool_trace': res.tool_trace, 'turns': res.turns,
            'cost_usd': res.cost_usd,
            'tokens': {'in': res.input_tokens, 'out': res.output_tokens,
                       'reasoning': res.reasoning_tokens},
            'guard_warnings': res.guard_warnings,
            'data_context': {
                'files': ctx.get('files', []),
                'time_range': ctx.get('time_range', ''),
            } if ctx.get('available') else {}}
    _save_message(session_id, tenant_id, 'assistant', text,
                  route=route, meta=meta)
    return {'answer': text, 'route': route, 'meta': meta}

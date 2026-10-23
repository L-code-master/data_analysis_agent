"""对话式分析路由（v1.4：形态 A 查数问答 + 形态 B Agent 归因）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import Ctx, enforce_rate_limit
from app.api.schemas import (
    AskRequest, ChatAnswerResponse, ChatContextResponse, ChatMessageResponse,
    ChatSessionResponse,
)
from app.config import get_settings
from app.services import chat as chat_svc
from app.services.tenant import TenantSuspendedError

router = APIRouter(prefix='/chat', tags=['对话分析'])


@router.get('/context', response_model=ChatContextResponse,
            summary='数据上下文（对话基于哪份数据 + 配额余量）')
async def get_context(ctx: Ctx):
    """对话当前基于最近一次成功诊断的数据快照；stale=有新上传未诊断。"""
    data = chat_svc.get_data_context(ctx.tenant_id)
    quota, used = chat_svc.agent_chat_quota(ctx.tenant_id)
    return ChatContextResponse(data=data, agent_quota={
        'quota': quota, 'used': used,
        'unlimited': quota < 0 or ctx.user.is_admin,
    })


@router.post('/sessions', response_model=ChatSessionResponse,
             status_code=201, summary='新建对话')
async def create_session(ctx: Ctx, title: str = ''):
    s = chat_svc.create_session(ctx.tenant_id, ctx.user.user_id, title)
    return ChatSessionResponse(**s)


@router.get('/sessions', response_model=list[ChatSessionResponse],
            summary='对话列表')
async def list_sessions(ctx: Ctx, limit: int = 30):
    return [ChatSessionResponse(**s)
            for s in chat_svc.list_sessions(ctx.tenant_id, limit)]


@router.get('/sessions/{session_id}/messages',
            response_model=list[ChatMessageResponse],
            summary='对话消息历史')
async def get_messages(ctx: Ctx, session_id: str, limit: int = 100):
    return [ChatMessageResponse(**m)
            for m in chat_svc.list_messages(ctx.tenant_id, session_id, limit)]


@router.post('/sessions/{session_id}/ask',
             response_model=ChatAnswerResponse,
             summary='提问（自动路由：查数走 A，归因走 B 的 Agent 循环）')
def ask(ctx: Ctx, session_id: str, req: AskRequest):
    """同步返回完整回答。

    形态 A（查数）毫秒级；形态 B（Agent 归因）涉及真实 LLM 循环，
    数秒到数十秒 —— 前端展示 loading。

    ★ 问题走 JSON body（docs/16 P2-18）：旧实现放 URL query，
      敏感问题会进反向代理/浏览器历史日志；同时限制长度（2000）。

    ★ 同步 def（非 async）：FastAPI 自动放入线程池执行，
      避免数十秒的 DuckDB/LLM 同步调用冻结事件循环（docs/16 P0-7）。
    """
    if chat_svc.get_session(ctx.tenant_id, session_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={'code': 'SESSION_NOT_FOUND',
                                    'message': '对话不存在'})

    # ★ 每租户 ask 限速（docs/16 P1-7）：Agent 循环会真实烧 LLM，
    #   配额之外再加一道速率闸门。
    s = get_settings()
    enforce_rate_limit('ask', ctx.tenant_id, s.rl_ask_per_minute, 60)

    try:
        r = chat_svc.ask(ctx.tenant_id, ctx.user.user_id, session_id,
                     req.question)
    except chat_svc.AgentQuotaExceededError as e:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={'code': 'AGENT_QUOTA_EXCEEDED', 'message': str(e),
                    'quota': e.quota, 'used': e.used}) from e
    except chat_svc.CostQuotaExceededError as e:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={'code': 'COST_QUOTA_EXCEEDED',
                    'message': e.message}) from e
    except TenantSuspendedError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={'code': e.code, 'message': str(e)}) from e
    return ChatAnswerResponse(answer=r['answer'], route=r['route'],
                              meta=r.get('meta') or {})

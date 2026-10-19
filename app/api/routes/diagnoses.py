"""诊断路由：发起诊断、SSE 进度、结果、报告。

设计要点（见 docs/01-DESIGN.md §3 B）：
  **长任务不能塞进请求周期** —— 一次诊断可能跑数十秒到数分钟。
    POST /diagnoses              → 202 + diagnosis_id（立即返回）
    GET  /diagnoses/{id}/events  → SSE 流式进度
    GET  /diagnoses/{id}         → 轮询兜底
    GET  /diagnoses/{id}/report  → HTML 报告
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse

from app.api.deps import Ctx
from app.api.schemas import (
    DiagnosisResponse, DiagnosisResultResponse, MetricValue,
    StartDiagnosisRequest,
)
from app.services import billing, diagnosis as dg

router = APIRouter(prefix='/diagnoses', tags=['诊断'])


def _handle(e: Exception) -> HTTPException:
    if isinstance(e, billing.InsufficientCreditsError):
        return HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={'code': e.code, 'message': e.message,
                    'balance': e.balance})
    if isinstance(e, dg.DiagnosisError):
        code = status.HTTP_404_NOT_FOUND if e.code == 'DIAGNOSIS_NOT_FOUND' \
            else status.HTTP_400_BAD_REQUEST
        return HTTPException(status_code=code,
                             detail={'code': e.code, 'message': e.message})
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                         detail={'code': 'INTERNAL', 'message': str(e)})


# ---------------------------------------------------------------------------
# 任务注册表（进程内）
# ---------------------------------------------------------------------------
# 说明：MVP 用进程内字典 + 线程。uvicorn 必须 --workers 1，
# 多 worker 会导致任务状态不一致（见 04-云服务器部署.md）。
#
# ⚠️ 关键设计：进度事件必须**缓冲**，不能只在订阅后推。
#    诊断可能在客户端订阅 SSE 之前就跑完了（小文件只需几百毫秒），
#    若无缓冲，前端会收到空的进度流 —— 用户什么都看不到。
#    因此队列绑定到 diagnosis_id，先到的事件留在队列里，订阅时一并回放。
_progress: dict[str, "queue.Queue[dict]"] = {}
_history: dict[str, list[dict]] = {}      # 已发事件回放缓冲
_lock = threading.Lock()
_MAX_HISTORY = 200


def _get_queue(did: str) -> queue.Queue:
    with _lock:
        q = _progress.get(did)
        if q is None:
            q = queue.Queue(maxsize=1000)
            _progress[did] = q
        return q


def _emit(did: str, event: dict) -> None:
    """发事件：入队 + 记入历史（供晚订阅的客户端回放）。"""
    if not did:
        return
    q = _get_queue(did)
    try:
        q.put_nowait(event)
    except queue.Full:
        pass
    with _lock:
        h = _history.setdefault(did, [])
        h.append(event)
        if len(h) > _MAX_HISTORY:
            del h[0]


def _terminal_event(did: str) -> dict | None:
    """取历史中的终态事件（done / error）。

    用途：若客户端在诊断完成之后才订阅 SSE，历史里已经没有
    可回放的 done 事件（或事件被裁剪），需要合成一个终态事件，
    否则前端会一直等不到“完成”。
    """
    with _lock:
        for ev in reversed(_history.get(did, [])):
            if ev.get('type') in ('done', 'error'):
                return ev
    return None


def _history_of(did: str) -> list[dict]:
    with _lock:
        return list(_history.get(did, []))


def _synthetic_terminal(ctx: Any, diagnosis_id: str, info: dict) -> str:
    """合成一个终态 SSE 事件（done / error）。

    用途：任务已结束但 done 事件因时序原因未入队时，保证前端
    仍能收到终态信号，不至于一直转圈。
    """
    if info.get('status') == 'failed':
        ev = {'type': 'error', 'message': info.get('error', '诊断失败')}
    else:
        metrics: list = []
        try:
            rpt = dg.get_report_json(ctx.tenant_id, diagnosis_id)
            metrics = rpt.get('metrics', [])
        except Exception:                       # noqa: BLE001
            pass
        ev = {
            'type': 'done',
            'result': {
                'diagnosis_id': diagnosis_id,
                'status': info.get('status', 'succeeded'),
                'metrics': metrics,
                'quality_passed': info.get('quality_passed', False),
                'cost_usd': info.get('cost_usd', 0.0),
            },
        }
    return f'data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n'


def _drop_queue(did: str) -> None:
    with _lock:
        _progress.pop(did, None)
        _history.pop(did, None)


# ---------------------------------------------------------------------------
# 发起诊断
# ---------------------------------------------------------------------------

@router.post('', response_model=DiagnosisResponse,
             status_code=status.HTTP_202_ACCEPTED, summary='发起诊断')
async def start_diagnosis(ctx: Ctx, req: StartDiagnosisRequest):
    """发起一次诊断。

    立即返回 202 + diagnosis_id，实际执行在后台线程。
    用 `GET /diagnoses/{id}/events` 订阅进度。
    """
    # 前置校验（同步、快）——额度不足直接 402，不浪费线程
    # 超级管理员放行（docs/12：只免额度这一道闸）
    if not ctx.user.is_admin:
        acc = billing.get_account(ctx.tenant_id)
        if not acc.can_diagnose:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={'code': 'INSUFFICIENT_CREDITS',
                        'message': f'诊断次数不足（当前 {acc.balance} 次），请先购买',
                        'balance': acc.balance})

    ok, reason = billing.check_cost_quota(ctx.tenant_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={'code': 'COST_QUOTA_EXCEEDED', 'message': reason})

    # 校验数据源属于该租户
    try:
        for sid in req.source_ids:
            dg.get_source(ctx.tenant_id, sid)
    except dg.SourceNotFoundError as e:
        raise _handle(e) from e

    tenant_id = ctx.tenant_id
    user_id = ctx.user.user_id
    source_ids = list(req.source_ids)
    time_range = req.time_range
    metric_ids = req.metric_ids

    # ★ 预生成 diagnosis_id：进度事件需要绑定到 ID，
    #   而诊断内部生成 ID 时可能已有若干事件发生（会丢事件）。
    pre_did = uuid.uuid4().hex
    holder: dict[str, str] = {'did': pre_did}

    def run() -> None:
        def on_progress(phase: str, msg: str) -> None:
            _emit(pre_did, {'type': 'progress', 'phase': phase, 'message': msg})

        try:
            res = dg.run_diagnosis(
                tenant_id, user_id, source_ids,
                time_range_text=time_range, metric_ids=metric_ids,
                progress=on_progress,
                diagnosis_id=pre_did)
            _emit(pre_did, {'type': 'done', 'result': res.to_dict()})
        except Exception as e:                       # noqa: BLE001
            _emit(pre_did, {
                'type': 'error',
                'code': getattr(e, 'code', type(e).__name__),
                'message': str(e),
            })

    thread = threading.Thread(target=run, daemon=True,
                              name=f'diag-{tenant_id[:8]}')
    thread.start()

    # 等任务把诊断行落库（最多 3 秒）
    info: dict | None = None
    for _ in range(300):
        try:
            info = dg.get_diagnosis(tenant_id, pre_did)
            break
        except dg.DiagnosisError:
            await asyncio.sleep(0.01)

    if info is None:
        # 任务还没落库（极少见）—— 返回排队态，前端靠 SSE 拿结果
        return DiagnosisResponse(
            diagnosis_id=pre_did, status='queued', phase='checking',
            report_url='', error='', cost_usd=0.0,
            created_at='', finished_at='')

    return DiagnosisResponse(
        diagnosis_id=info['diagnosis_id'], status=info['status'],
        phase=info['phase'], quality_passed=info['quality_passed'],
        report_url=(f'/api/v1/diagnoses/{pre_did}/report'
                    if info['report_ref'] else ''),
        error=info['error'], cost_usd=info['cost_usd'],
        created_at=info['created_at'], finished_at=info['finished_at'])


# ---------------------------------------------------------------------------
# SSE 进度流
# ---------------------------------------------------------------------------

@router.get('/{diagnosis_id}/events', summary='诊断进度（SSE）')
async def diagnosis_events(ctx: Ctx, diagnosis_id: str):
    """订阅诊断进度。

    ⚠️ Nginx 必须关闭缓冲，否则进度不会实时推送 —— 见 04-云服务器部署.md
    """
    # 校验归属（防跨租户订阅）
    try:
        dg.get_diagnosis(ctx.tenant_id, diagnosis_id)
    except dg.DiagnosisError as e:
        raise _handle(e) from e

    async def event_stream():
        # ★ 回放历史事件：诊断可能在订阅前就产生了若干进度
        #   （小文件几百毫秒就跑完），不回放会导致前端看不到任何进度。
        #
        # ⚠️ 关键：回放时**不要**从实时队列里丢弃事件 ——
        #    历史与队列是两份记录，回放历史不等于消费队列。
        #    早期版本按 sent 条数丢弃队列事件，会把「回放后新到的事件」
        #    一起丢掉，导致 done 永远收不到。
        for ev in _history_of(diagnosis_id):
            yield f'data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n'
            if ev.get('type') in ('done', 'error'):
                yield f'data: {json.dumps({"type": "end"}, ensure_ascii=False)}\n\n'
                return

        # 历史里没有终态事件 → 一边发 ready，一边检查任务是否已结束
        yield f'data: {json.dumps({"type": "ready"}, ensure_ascii=False)}\n\n'

        q = _get_queue(diagnosis_id)

        # 历史回放结束后仍未见终态 → 任务可能已结束
        try:
            info0 = dg.get_diagnosis(ctx.tenant_id, diagnosis_id)
            if info0['status'] in ('succeeded', 'failed', 'canceled'):
                yield _synthetic_terminal(ctx, diagnosis_id, info0)
                yield f'data: {json.dumps({"type": "end"}, ensure_ascii=False)}\n\n'
                return
        except Exception:                   # noqa: BLE001
            pass

        # 任务仍在跑 → 进入实时消费循环
        q = _get_queue(diagnosis_id)

        idle = 0
        terminal_grace = 0
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                idle += 1
                if idle > 300:                  # 约 30s 无事件
                    yield ': keepalive\n\n'
                    idle = 0

                await asyncio.sleep(0.1)

                # ⚠️ 终止判定必须留宽限期：
                #    诊断内部是「先写 DB status=succeeded，再 _emit(done)」，
                #    若在这里一看到 succeeded 就 break，会抢在 done 之前结束流，
                #    前端就永远拿不到 done 事件（实测踩过这个坑）。
                #    因此：状态已是终态时，再等 2 秒让 done 入队；
                #    若始终没等到，则合成一个终态事件。
                try:
                    info = dg.get_diagnosis(ctx.tenant_id, diagnosis_id)
                    if info['status'] in ('succeeded', 'failed', 'canceled') and q.empty():
                        terminal_grace += 1
                        if terminal_grace > 20:     # 20 × 0.1s = 2s
                            if not any(e.get('type') in ('done', 'error')
                                       for e in _history_of(diagnosis_id)):
                                yield _synthetic_terminal(ctx, diagnosis_id, info)
                            yield f'data: {json.dumps({"type": "end", "status": info["status"]}, ensure_ascii=False)}\n\n'
                            break
                    else:
                        terminal_grace = 0
                except Exception:               # noqa: BLE001
                    break
                continue

            idle = 0
            terminal_grace = 0
            yield f'data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n'
            if item.get('type') in ('done', 'error'):
                yield f'data: {json.dumps({"type": "end"}, ensure_ascii=False)}\n\n'
                break

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',       # ★ 让 Nginx 不缓冲
        })


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

@router.get('', response_model=list[DiagnosisResponse], summary='诊断列表')
async def list_diagnoses(ctx: Ctx, limit: int = 50):
    rows = dg.list_diagnoses(ctx.tenant_id, limit=limit)
    return [DiagnosisResponse(
        diagnosis_id=r['diagnosis_id'], status=r['status'], phase=r['phase'],
        quality_passed=r['quality_passed'],
        report_url=(f'/api/v1/diagnoses/{r["diagnosis_id"]}/report'
                    if r['status'] == 'succeeded' else ''),
        created_at=r['created_at'], finished_at=r['finished_at'],
    ) for r in rows]


@router.get('/{diagnosis_id}', response_model=DiagnosisResultResponse,
            summary='诊断状态与结果')
async def get_diagnosis(ctx: Ctx, diagnosis_id: str):
    try:
        info = dg.get_diagnosis(ctx.tenant_id, diagnosis_id)
    except dg.DiagnosisError as e:
        raise _handle(e) from e

    metrics: list[MetricValue] = []
    footnote = ''
    warnings: list[str] = []
    duration = 0
    trend: list[dict] = []
    extras: dict[str, dict | list] = {}

    if info['status'] == 'succeeded' and info.get('report_ref'):
        try:
            rpt = dg.get_report_json(ctx.tenant_id, diagnosis_id)
            metrics = [MetricValue(**{k: m.get(k) for k in
                                      ('metric_id', 'metric_name', 'value',
                                       'unit', 'formula', 'uncertainty')})
                       for m in rpt.get('metrics', [])]
            footnote = rpt.get('footnote', '')
            warnings = rpt.get('warnings', [])
            trend = rpt.get('trend') or []
            # v1.2 归因区块 + L1 口径对比（报告 blob 是唯一真源）
            for key in ('products', 'anomaly_days', 'weekly',
                        'caliber_compare', 'caliber'):
                default: dict | list = {} if key in ('weekly', 'caliber_compare',
                                                     'caliber') else []
                extras[key] = rpt.get(key) or default
        except Exception:                       # noqa: BLE001
            pass

    return DiagnosisResultResponse(
        diagnosis_id=info['diagnosis_id'], status=info['status'],
        metrics=metrics, footnote=footnote, warnings=warnings,
        quality_passed=info['quality_passed'],
        report_url=(f'/api/v1/diagnoses/{diagnosis_id}/report'
                    if info.get('report_ref') else ''),
        error=info['error'], duration_ms=duration, cost_usd=info['cost_usd'],
        trend=trend,
        products=extras.get('products', []),
        anomaly_days=extras.get('anomaly_days', []),
        weekly=extras.get('weekly', {}),
        caliber_compare=extras.get('caliber_compare', {}),
        caliber=extras.get('caliber', {}))


@router.get('/{diagnosis_id}/report', response_class=HTMLResponse,
            summary='HTML 报告')
async def get_report(ctx: Ctx, diagnosis_id: str):
    try:
        html = dg.render_report_html(ctx.tenant_id, diagnosis_id)
    except dg.DiagnosisError as e:
        raise _handle(e) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={'code': 'REPORT_NOT_READY',
                                    'message': '报告尚未生成'}) from e
    return HTMLResponse(content=html)


@router.get('/{diagnosis_id}/report.json', summary='报告原始数据')
async def get_report_json(ctx: Ctx, diagnosis_id: str):
    try:
        return dg.get_report_json(ctx.tenant_id, diagnosis_id)
    except dg.DiagnosisError as e:
        raise _handle(e) from e
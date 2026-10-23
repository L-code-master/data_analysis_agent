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
import logging
import queue
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import Ctx, enforce_rate_limit
from app.api.schemas import (
    DiagnosisResponse, DiagnosisResultResponse, MetricValue,
    StartDiagnosisRequest,
)
from app.config import get_settings
from app.services import billing, diagnosis as dg
from app.storage import db

router = APIRouter(prefix='/diagnoses', tags=['诊断'])
logger = logging.getLogger(__name__)


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
    from app.services.tenant import TenantSuspendedError
    if isinstance(e, TenantSuspendedError):        # P2-20：停用租户
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                             detail={'code': e.code, 'message': str(e)})
    logger.exception('诊断接口未处理异常: %s', e)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                         detail={'code': 'INTERNAL',
                                 'message': '服务内部错误'})


# ---------------------------------------------------------------------------
# 任务注册表（进程内）
# ---------------------------------------------------------------------------
# 说明：MVP 用进程内字典 + 线程。uvicorn 必须 --workers 1，
# 多 worker 会导致任务状态不一致（见 04-云服务器部署.md）。
#
# ⚠️ 关键设计：进度事件必须**缓冲**，不能只在订阅后推。
#    诊断可能在客户端订阅 SSE 之前就跑完了（小文件只需几百毫秒），
#    若无缓冲，前端会收到空的进度流 —— 用户什么都看不到。
#    因此历史绑定到 diagnosis_id，先到的事件留在历史里，订阅时一并回放。
#
# ★ 每个订阅者独立队列（docs/16 P2-24）：旧实现所有订阅共享一个 Queue，
#   两个标签页/刷新重叠时会互相"抢"事件，另一个连接可能收不到 done。
#   每个事件带单调 seq，历史回放与实时队列重叠时按 seq 去重。
_PROGRESS_TTL_SECONDS = 2 * 3600          # 历史/订阅的兜底保留时长
_progress: dict[str, list["queue.Queue[dict]"]] = {}   # did → 订阅者队列
_history: dict[str, list[dict]] = {}      # 已发事件回放缓冲
_seq: dict[str, int] = {}
_last_activity: dict[str, float] = {}
_lock = threading.Lock()
_MAX_HISTORY = 200


def _subscribe(did: str) -> "queue.Queue[dict]":
    """注册一个独立订阅队列。先注册再回放历史，避免中间丢事件。"""
    q: queue.Queue[dict] = queue.Queue(maxsize=1000)
    with _lock:
        _progress.setdefault(did, []).append(q)
        _last_activity[did] = time.monotonic()
    return q


def _unsubscribe(did: str, q: "queue.Queue[dict]") -> None:
    with _lock:
        subs = _progress.get(did)
        if subs is not None:
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                _progress.pop(did, None)


def _emit(did: str, event: dict) -> None:
    """发事件：写历史 + 分发给所有订阅者（每个订阅者一份）。"""
    if not did:
        return
    with _lock:
        _last_activity[did] = time.monotonic()
        seq = _seq.get(did, 0) + 1
        _seq[did] = seq
        ev = dict(event)
        ev['seq'] = seq
        h = _history.setdefault(did, [])
        h.append(ev)
        if len(h) > _MAX_HISTORY:
            del h[0]
        subs = list(_progress.get(did, []))
    for q in subs:
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass                     # 慢订阅者不拖垮诊断线程


def _sweep_progress() -> int:
    """清理长期无活动的进度历史/订阅（docs/16 P1-15）。"""
    cutoff = time.monotonic() - _PROGRESS_TTL_SECONDS
    with _lock:
        stale = [d for d, ts in _last_activity.items() if ts < cutoff]
    for d in stale:
        _drop_queue(d)
    return len(stale)


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
        _seq.pop(did, None)
        _last_activity.pop(did, None)


# ---------------------------------------------------------------------------
# 发起诊断
# ---------------------------------------------------------------------------

# ★ 并发上限（docs/16 P0-7）：每个诊断线程都要开 DuckDB 连接、抢 CPU。
#   无上限时恶意/误触用户可瞬间堆出大量线程耗尽单进程资源。
_MAX_RUNNING_DIAGNOSES = 4
_diag_slots = threading.BoundedSemaphore(_MAX_RUNNING_DIAGNOSES)


def _precheck(tenant_id: str, is_admin: bool,
              source_ids: list[str]) -> None:
    """同步前置校验（在线程池执行，避免阻塞事件循环）。"""
    # ★ 停用租户同步 403（P2-20）—— service 层 run_diagnosis 还有
    #   同样的检查兜底，但那里在后台线程里，客户端只能看到 202 后失败。
    from app.services import tenant as tn
    try:
        tn.ensure_tenant_active(tenant_id)
    except tn.TenantSuspendedError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={'code': e.code, 'message': str(e)}) from e

    # 额度不足直接 402，不浪费线程；超管放行（docs/12：只免额度这一道闸）
    if not is_admin:
        acc = billing.get_account(tenant_id)
        if not acc.can_diagnose:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={'code': 'INSUFFICIENT_CREDITS',
                        'message': f'诊断次数不足（当前 {acc.balance} 次），请先购买',
                        'balance': acc.balance})

    ok, reason = billing.check_cost_quota(tenant_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={'code': 'COST_QUOTA_EXCEEDED', 'message': reason})

    # ★ 同租户互斥（docs/16 P0-7）：已有进行中的诊断则拒绝。
    #   两份诊断并发跑会交错 DELETE/INSERT 同一批 STG 表 ——
    #   产出数字错误的报告或撞写事务双双退单。串行化由两层保证：
    #   这里尽早拒绝（好 UX），run_diagnosis 内的租户锁兜底（正确性）。
    with db.get_conn() as conn:
        running = conn.execute(
            "SELECT id FROM diagnoses WHERE tenant_id = ? "
            "AND status IN ('queued','running') LIMIT 1",
            (tenant_id,)).fetchone()
    if running is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={'code': 'DIAGNOSIS_RUNNING',
                    'message': '已有诊断正在进行中，请等待完成后再发起'})

    # 校验数据源属于该租户
    try:
        srcs = [dg.get_source(tenant_id, sid) for sid in source_ids]
    except dg.SourceNotFoundError as e:
        raise _handle(e) from e

    # ★ 同类型多数据源立即拒绝（docs/16 P0-6）：服务层 run_diagnosis 里
    #   还有同样的检查兜底，但这里能立刻给客户端明确的 400，
    #   而不是 202 后错误发生在落库之前、客户端永远等不到具体原因。
    from app.adapters.taobao_fields import get_spec
    seen: dict[str, str] = {}
    for x in srcs:
        spec = get_spec(x.source_type)
        name = x.original_name or x.filename
        if spec.target_table in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={'code': 'DUPLICATE_SOURCE_TYPE',
                        'message': (f'包含多个「{spec.display_name}」类型的数据源'
                                    f'（{seen[spec.target_table]} 与 {name}）。'
                                    '请合并为一个文件后重新上传')})
        seen[spec.target_table] = name


@router.post('', response_model=DiagnosisResponse,
             status_code=status.HTTP_202_ACCEPTED, summary='发起诊断')
async def start_diagnosis(ctx: Ctx, req: StartDiagnosisRequest):
    """发起一次诊断。

    立即返回 202 + diagnosis_id，实际执行在后台线程。
    用 `GET /diagnoses/{id}/events` 订阅进度。
    """
    s = get_settings()
    enforce_rate_limit('diagnosis', ctx.tenant_id,
                       s.rl_diagnosis_per_minute, 60)
    _sweep_progress()          # 顺手清理过期进度历史（P1-15）

    # ★ 前置校验在线程池执行（含 SQLite 读写，docs/16 P0-7）
    await run_in_threadpool(_precheck, ctx.tenant_id, ctx.user.is_admin,
                            list(req.source_ids))

    # 全局并发上限：非阻塞获取，拿不到立即 503（不在事件循环上等）
    if not _diag_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={'code': 'SYSTEM_BUSY',
                    'message': f'当前并发诊断已达上限（{_MAX_RUNNING_DIAGNOSES}），请稍后再试'})

    tenant_id = ctx.tenant_id
    user_id = ctx.user.user_id
    source_ids = list(req.source_ids)
    time_range = req.time_range
    metric_ids = req.metric_ids

    # ★ 预生成 diagnosis_id：进度事件需要绑定到 ID，
    #   而诊断内部生成 ID 时可能已有若干事件发生（会丢事件）。
    pre_did = uuid.uuid4().hex

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
        finally:
            _diag_slots.release()
            # 诊断线程的 SQLite thread-local 连接显式关闭，不靠 GC
            db.close_conn()

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
        _sweep_progress()
        # ★ 先注册独立队列，再回放历史 —— 注册与快照之间新到的事件
        #   会同时出现在历史和队列里，靠 seq 去重（docs/16 P2-24）。
        q = _subscribe(diagnosis_id)
        replayed_max = 0
        try:
            for ev in _history_of(diagnosis_id):
                replayed_max = max(replayed_max,
                                   int(ev.get('seq', 0) or 0))
                yield f'data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n'
                if ev.get('type') in ('done', 'error'):
                    yield f'data: {json.dumps({"type": "end"}, ensure_ascii=False)}\n\n'
                    return

            # 历史里没有终态事件 → 一边发 ready，一边检查任务是否已结束
            yield f'data: {json.dumps({"type": "ready"}, ensure_ascii=False)}\n\n'

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
                        if info['status'] in ('succeeded', 'failed', 'canceled') \
                                and q.empty():
                            terminal_grace += 1
                            if terminal_grace > 20:     # 20 × 0.1s = 2s
                                if not any(e.get('type') in ('done', 'error')
                                           for e in _history_of(diagnosis_id)):
                                    yield _synthetic_terminal(
                                        ctx, diagnosis_id, info)
                                yield f'data: {json.dumps({"type": "end", "status": info["status"]}, ensure_ascii=False)}\n\n'
                                break
                        else:
                            terminal_grace = 0
                    except Exception:               # noqa: BLE001
                        break
                    continue

                idle = 0
                terminal_grace = 0
                # 已回放过的事件（注册与快照之间的重叠部分）跳过
                if int(item.get('seq', 0) or 0) <= replayed_max:
                    continue
                yield f'data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n'
                if item.get('type') in ('done', 'error'):
                    yield f'data: {json.dumps({"type": "end"}, ensure_ascii=False)}\n\n'
                    break
        finally:
            _unsubscribe(diagnosis_id, q)

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
def list_diagnoses(ctx: Ctx, limit: int = 50):
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
def get_diagnosis(ctx: Ctx, diagnosis_id: str):
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
def get_report(ctx: Ctx, diagnosis_id: str):
    try:
        html = dg.render_report_html(ctx.tenant_id, diagnosis_id)
    except dg.DiagnosisError as e:
        raise _handle(e) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={'code': 'REPORT_NOT_READY',
                                    'message': '报告尚未生成'}) from e
    # ★ 报告是自包含 HTML（内联样式 + SVG，无脚本/外链），
    #   用严格 CSP 再兜一层 XSS/点击劫持（docs/16 P1-17）。
    return HTMLResponse(content=html, headers={
        'Content-Security-Policy': (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"),
        'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
    })


@router.get('/{diagnosis_id}/report.json', summary='报告原始数据')
def get_report_json(ctx: Ctx, diagnosis_id: str):
    try:
        return dg.get_report_json(ctx.tenant_id, diagnosis_id)
    except dg.DiagnosisError as e:
        raise _handle(e) from e
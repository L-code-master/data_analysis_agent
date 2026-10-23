"""FastAPI 应用入口。

启动：
    uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

★ --workers 1 是刻意的：SQLite 单写者 + 进程内任务调度，
  多 worker 会导致任务状态不一致（见 docs/04-云服务器部署.md）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.routes import admin as admin_routes
from app.api.routes import auth as auth_routes
from app.api.routes import billing as billing_routes
from app.api.routes import chat as chat_routes
from app.api.routes import diagnoses as diag_routes
from app.api.routes import samples as sample_routes
from app.api.routes import sources as source_routes
from app.api.schemas import HealthResponse
from app.config import get_settings
from app.storage import db

VERSION = '0.1.0'

logger = logging.getLogger('app')


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动自检 + 建表。

    自检不过直接拒绝启动 —— 宁可起不来，也不要带病运行。
    """
    s = get_settings()

    problems = s.validate_for_startup()
    if problems:
        for p in problems:
            logger.error('启动自检失败: %s', p)
        raise RuntimeError(
            '启动自检未通过：\n  - ' + '\n  - '.join(problems))

    # 非阻塞提醒：功能降级但不影响启动（如未配 LLM key → 形态 B 降级）
    for w in s.warnings_for_startup():
        logger.warning('启动提醒: %s', w)

    db.apply_schema()
    logger.info('数据库就绪: %s', s.database_path)

    # 未启用邮箱验证的部署：历史用户视为已核验（避免前端误提示）
    if not s.email_verification_required:
        from app.services import auth as auth_svc
        try:
            await run_in_threadpool(auth_svc.backfill_legacy_verified)
        except Exception:                   # noqa: BLE001
            logger.exception('历史用户邮箱核验回填失败')

    # ★ 启动对账（docs/16 P1-3）：daemon 线程随进程消失后，遗留的
    #   running 诊断会永久卡死、已扣额度无退款路径。启动时标记失败并
    #   幂等补退款，同时重试补偿表里的待退款。同步 DB 操作放线程池执行。
    from app.services import diagnosis as diagnosis_svc
    try:
        stats = await run_in_threadpool(diagnosis_svc.reconcile_on_startup)
        logger.info('启动对账完成: %s', stats)
    except Exception:                       # noqa: BLE001
        # 对账失败不阻断启动：后续每次诊断失败仍会退款，补偿表保留待重试
        logger.exception('启动对账失败')

    # ★ WAL checkpoint（docs/16 P2-23）：单进程重启时无并发写，
    #   趁机把 -wal 落回主库（实测曾 462KB 主库挂 4.1MB wal ——
    #   直接拷贝 app.db 备份会丢最新计费流水）。
    try:
        ck = await run_in_threadpool(db.wal_checkpoint, 'TRUNCATE')
        logger.info('WAL checkpoint: %s', ck)
    except Exception:                       # noqa: BLE001
        logger.exception('WAL checkpoint 失败（不影响启动）')

    # 确保租户根目录存在
    s.tenants_path.mkdir(parents=True, exist_ok=True)
    s.log_path.mkdir(parents=True, exist_ok=True)

    logger.info('启动完成 env=%s model=%s sandbox_isolated=%s',
                s.app_env, s.llm_model, s.sandbox_network_isolated)
    yield
    db.close_conn()


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    s = get_settings()

    app = FastAPI(
        title='经营诊断 Agent API',
        version=VERSION,
        description=(
            '淘宝店铺经营诊断服务。\n\n'
            '**认证**：除 `/health` 与 `/billing/plans` 外均需 '
            '`Authorization: Bearer <access_token>`。\n\n'
            '**长任务**：`POST /diagnoses` 立即返回 202，'
            '用 `GET /diagnoses/{id}/events` 订阅 SSE 进度。'
        ),
        lifespan=lifespan,
        docs_url='/docs' if not s.is_production else None,
        redoc_url=None,
        # ★ 生产环境连 openapi.json 也关闭（docs/16 P2-15）
        openapi_url=None if s.is_production else '/openapi.json',
    )

    # ---- CORS（生产必须明确域名，禁止 *）----
    origins = s.cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=['*'],
            allow_headers=['*'],
        )

    # ---- 统一错误格式 ----
    @app.exception_handler(HTTPException)
    async def http_exc_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            body = detail
        else:
            body = {'code': f'HTTP_{exc.status_code}', 'message': str(detail)}
        return JSONResponse(status_code=exc.status_code, content=body,
                            headers=exc.headers)

    # ★ P3-16：请求参数校验失败也统一 {code, message} 契约 ——
    #   Pydantic 默认返回 {"detail": [...]}，前端 errMsg() 解析不到 message
    @app.exception_handler(RequestValidationError)
    async def validation_exc_handler(request: Request,
                                     exc: RequestValidationError):
        errs = []
        for e in exc.errors()[:5]:
            loc = '.'.join(str(x) for x in e.get('loc', [])[1:]) or '?'
            errs.append(f'{loc}: {e.get("msg", "")}')
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={'code': 'VALIDATION_ERROR',
                     'message': '请求参数不合法：' + '；'.join(errs)})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        logger.exception('未处理的异常: %s', exc)
        msg = str(exc) if not s.is_production else '服务内部错误'
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'code': 'INTERNAL', 'message': msg})

    # ---- 路由 ----
    api = '/api/v1'
    app.include_router(auth_routes.router, prefix=api)
    app.include_router(admin_routes.router, prefix=api)
    app.include_router(sample_routes.router, prefix=api)
    app.include_router(source_routes.router, prefix=api)
    app.include_router(diag_routes.router, prefix=api)
    app.include_router(billing_routes.router, prefix=api)
    app.include_router(chat_routes.router, prefix=api)

    # ---- 健康检查（无需登录）----
    @app.get('/api/v1/health', response_model=HealthResponse,
             tags=['系统'], summary='健康检查（公开，仅状态）')
    async def health():
        # ★ 公开端点不泄露 DB 路径/表名/沙箱细节（docs/16 P2-13）；
        #   详情在管理员接口 /admin/health。
        try:
            ok = bool(db.health_check().get('ok'))
        except Exception:                           # noqa: BLE001
            ok = False
        return HealthResponse(status='ok' if ok else 'degraded',
                              version=VERSION)

    return app


app = create_app()
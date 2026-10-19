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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    app.include_router(sample_routes.router, prefix=api)
    app.include_router(source_routes.router, prefix=api)
    app.include_router(diag_routes.router, prefix=api)
    app.include_router(billing_routes.router, prefix=api)
    app.include_router(chat_routes.router, prefix=api)

    # ---- 健康检查（无需登录）----
    @app.get('/api/v1/health', response_model=HealthResponse,
             tags=['系统'], summary='健康检查')
    async def health():
        cfg = get_settings()
        try:
            dbh = db.health_check()
        except Exception as e:                      # noqa: BLE001
            dbh = {'ok': False, 'error': str(e)}

        return HealthResponse(
            status='ok' if dbh.get('ok') else 'degraded',
            version=VERSION,
            database=dbh,
            sandbox={
                'network_isolated': cfg.sandbox_network_isolated,
                'mode': cfg.sandbox_mode,
            })

    return app


app = create_app()
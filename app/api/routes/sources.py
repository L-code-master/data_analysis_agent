"""数据源路由：上传、画像、映射确认。"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.deps import Ctx
from app.api.schemas import (
    ConfirmMappingRequest, MappingItem, ProfileResponse, SourceResponse,
)
from app.services import diagnosis as dg
from app.services import tenant as tn

router = APIRouter(prefix='/sources', tags=['数据源'])


def _handle(e: Exception) -> HTTPException:
    if isinstance(e, dg.SourceNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                             detail={'code': e.code, 'message': e.message})
    if isinstance(e, dg.SourceTypeUnknownError):
        # 422 = 请求格式正确但语义无法处理（文件能读，但认不出是哪种报表）
        return HTTPException(status_code=422,
                             detail={'code': e.code, 'message': e.message})
    if isinstance(e, dg.DiagnosisError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail={'code': e.code, 'message': e.message})
    if isinstance(e, tn.TenantPathError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail={'code': 'INVALID_PATH', 'message': str(e)})
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                         detail={'code': 'INTERNAL', 'message': str(e)})


@router.post('', response_model=list[SourceResponse],
             status_code=status.HTTP_201_CREATED, summary='上传数据源文件')
async def upload_source(ctx: Ctx, file: UploadFile = File(...)):
    """上传千牛导出文件。

    支持 .xlsx / .xls / .csv。文件大小与类型在服务端二次校验。
    **文件名由服务端生成**，不使用客户端文件名（防路径穿越）。

    ★ **多工作表自动拆分**：一个含多张报表的 Excel（如样例数据）
    会被拆分为多个数据源；无法识别的工作表（如"说明"）自动跳过。
    返回列表，CSV 恒为 1 项。
    """
    content = await file.read()
    try:
        records = dg.register_source(
            ctx.tenant_id, ctx.user.user_id,
            file.filename or 'upload.csv', content)
    except Exception as e:
        raise _handle(e) from e

    return [SourceResponse(
        source_id=r.id,
        name=(r.original_name or r.filename)
              + (f' / {r.sheet_name}' if r.sheet_name else ''),
        source_type=r.source_type, sheet_name=r.sheet_name,
        size_bytes=r.size_bytes, row_count=r.row_count,
        status=r.status, created_at=r.created_at) for r in records]


@router.get('', response_model=list[SourceResponse], summary='数据源列表')
async def list_sources(ctx: Ctx, limit: int = 50):
    rows = dg.list_sources(ctx.tenant_id, limit=limit)
    return [SourceResponse(
        source_id=r['source_id'], name=r['name'],
        source_type=r.get('source_type', ''),
        sheet_name=r.get('sheet_name', ''),
        size_bytes=r['size_bytes'], row_count=r['row_count'],
        status=r['status'], created_at=r['created_at']) for r in rows]


@router.get('/{source_id}/profile', response_model=ProfileResponse,
            summary='数据画像（不含数据行）')
async def get_profile(ctx: Ctx, source_id: str):
    try:
        prof = dg.profile_source(ctx.tenant_id, source_id)
    except Exception as e:
        raise _handle(e) from e

    return ProfileResponse(
        source_id=prof.source_id,
        source_type=prof.source_type,
        display_name=prof.display_name,
        row_count=prof.row_count,
        column_count=len(prof.columns),
        columns=prof.columns,
        mappings=[MappingItem(**m) for m in prof.mappings],
        missing_required=prof.missing_required,
        needs_confirmation=prof.needs_confirmation,
        schema_fingerprint=prof.schema_fingerprint,
        warnings=prof.warnings,
    )


@router.post('/{source_id}/mapping', summary='确认字段映射')
async def confirm_mapping_route(ctx: Ctx, source_id: str,
                                req: ConfirmMappingRequest):
    """确认「源列名 → 语义字段」的映射。

    确认后按 schema 指纹缓存，**下次同结构文件自动应用，无需二次确认**。
    """
    try:
        res = dg.confirm_mapping(
            ctx.tenant_id, ctx.user.user_id, source_id, req.mapping)
    except Exception as e:
        raise _handle(e) from e
    return res
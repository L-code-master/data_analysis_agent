"""样例数据路由 —— 让新用户 5 分钟内看到一份完整诊断报告。

公开接口（无需登录）：潜在客户可以先下载看效果，再决定是否注册。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from app.services import sample_data

router = APIRouter(prefix='/samples', tags=['样例数据'])

XLSX_MEDIA = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


@router.get('', summary='样例数据简介')
async def sample_info():
    """样例数据的故事与结构（用于前端展示）。"""
    info = sample_data.story_summary()
    return {
        **info,
        'download_url': '/api/v1/samples/download',
        'usage': [
            '下载 Excel（含 6 张报表 + 使用说明）',
            '注册账号（赠送 1 次体验诊断）',
            '把 Excel 拖入"上传数据"，系统自动拆分为 6 个数据源',
            '选时间范围 2026-09-01~2026-09-28，点"开始诊断"',
        ],
    }


@router.get('/download', summary='下载样例数据 Excel')
async def download_sample():
    """下载样例数据（一个 Excel，7 个工作表：说明 + 6 张报表）。

    上传此文件时会被自动拆分为 6 个数据源（说明页自动跳过）。
    """
    content = sample_data.build_xlsx()
    return Response(
        content=content,
        media_type=XLSX_MEDIA,
        headers={
            'Content-Disposition':
                'attachment; filename="sample-data.xlsx"; '
                'filename*=UTF-8\'\'%E6%A0%B7%E4%BE%8B%E6%95%B0%E6%8D%AE.xlsx',
            'Content-Length': str(len(content)),
        },
    )
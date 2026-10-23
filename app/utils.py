"""跨服务共用的小工具（docs/16 P3-7）。

原先 `_now` / `_rg` / `_row_get` 在 diagnosis / chat / billing 各抄一份，
口径一旦分叉（时区、行访问兼容）就是对账事故 —— 统一收口到这里。
"""

from __future__ import annotations

import datetime as dt
from typing import Any


def now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（服务端统一时间口径）。"""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def utc_now() -> dt.datetime:
    """当前 UTC 时间（带时区）。"""
    return dt.datetime.now(dt.timezone.utc)


def row_get(row: Any, name: str, idx: int) -> Any:
    """兼容 sqlite3.Row（有 keys）与裸 tuple/duckdb 行的取值。"""
    if hasattr(row, 'keys'):
        return row[name]
    return row[idx]


def pct(v: float) -> str:
    """比例值 → 百分比文案（0.35 → '35%'）。"""
    return f'{v * 100:.0f}%'

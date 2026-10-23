"""SQLite 连接管理。

并发纪律（FastAPI 多线程下必须遵守）：
  - WAL 模式：读写可并发
  - busy_timeout：避免瞬时 database is locked
  - foreign_keys：外键约束生效
  - 每线程独立连接（threading.local），避免跨线程复用

用法：
    with get_conn() as conn:
        conn.execute(...)
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import get_settings

_local = threading.local()
_init_lock = threading.Lock()
_initialized_paths: set[str] = set()

# 每个连接都要执行的 PRAGMA
_PRAGMAS = (
    'PRAGMA journal_mode = WAL',
    'PRAGMA busy_timeout = 5000',
    'PRAGMA foreign_keys = ON',
    'PRAGMA synchronous = NORMAL',
)


def _configure(conn: sqlite3.Connection) -> None:
    """配置单个连接的 PRAGMA 与行工厂。"""
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    conn.row_factory = sqlite3.Row


def _db_path() -> Path:
    path = get_settings().database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_conn() -> sqlite3.Connection:
    """获取当前线程的连接（惰性创建）。"""
    path = _db_path()
    key = str(path)

    conn = getattr(_local, 'conn', None)
    if conn is not None and getattr(_local, 'key', None) == key:
        return conn

    # 换库或首次创建：关旧开新
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    _configure(conn)
    _local.conn = conn
    _local.key = key

    # 首次见到这个库时，确保 schema 已应用
    with _init_lock:
        if key not in _initialized_paths:
            _initialized_paths.add(key)

    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """事务上下文。异常自动回滚。

    注意：连接使用 isolation_level=None（自动提交），
    因此这里显式 BEGIN / COMMIT。

    ★ 必须用 BEGIN IMMEDIATE（docs/16 P1-5）：
      默认的 BEGIN（deferred）在「先 SELECT 后 UPDATE」的事务里
      会等到写语句才升级锁；WAL 下若期间已有别的连接提交写事务，
      升级会直接抛 SQLITE_BUSY_SNAPSHOT（busy_timeout 对快照冲突无效）。
      mark_paid / cancel_order / rotate_refresh_token 都属于这种模式。
      IMMEDIATE 在事务开始时就取得写锁，把冲突挡在 BEGIN 处并由
      busy_timeout 正常等待。
    """
    conn = get_conn()
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
    except Exception:
        conn.execute('ROLLBACK')
        raise
    else:
        conn.execute('COMMIT')


def close_conn() -> None:
    """关闭当前线程连接（测试/优雅退出用）。"""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None
        _local.key = None


def apply_schema(schema_file: Path | None = None) -> None:
    """应用 schema.sql（幂等）。

    幂等性两层保障：
      1. 全部 CREATE TABLE IF NOT EXISTS
      2. 对已存在的旧库，逐个补列（ALTER 失败 = 列已存在，忽略）
    """
    if schema_file is None:
        # 固定指向项目内的 app/storage/schema.sql（不依赖 DB 路径推导）
        schema_file = Path(__file__).resolve().parent / 'schema.sql'
    if not schema_file.exists():
        raise FileNotFoundError(f'schema 文件不存在: {schema_file}')

    conn = get_conn()
    conn.executescript(schema_file.read_text(encoding='utf-8'))

    # 旧库迁移：schema.sql 只对新库生效，这里给已有表补列
    migrations = (
        "ALTER TABLE sources ADD COLUMN sheet_name TEXT DEFAULT ''",
        # docs/16 P1-6：额度批次化后，流水要能回指批次（退款回原批次）
        "ALTER TABLE credit_ledger ADD COLUMN lot_id TEXT",
        # docs/16 P1-18：access token 撤销水位 / refresh token 轮换族
        "ALTER TABLE users ADD COLUMN token_valid_after TEXT",
        "ALTER TABLE refresh_tokens ADD COLUMN family_id TEXT",
    )
    for stmt in migrations:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass          # 列已存在

    # ★ 重建 sources 去重索引（含 sheet_name）。
    #   顺序必须在 ALTER 之后 —— 一个 Excel 拆出的 6 个数据源
    #   共享同一 sha256，必须按 (tenant, sha256, sheet) 去重。
    conn.execute('DROP INDEX IF EXISTS idx_sources_dedup')
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_dedup "
        "ON sources(tenant_id, sha256, COALESCE(sheet_name, ''))")

    with _init_lock:
        _initialized_paths.add(str(_db_path()))


def health_check() -> dict:
    """数据库健康检查。"""
    conn = get_conn()
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    ]
    journal = conn.execute('PRAGMA journal_mode').fetchone()[0]
    fk = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    return {
        'ok': True,
        'path': str(_db_path()),
        'tables': len(tables),
        'table_names': tables,
        'journal_mode': journal,
        'foreign_keys': bool(fk),
    }
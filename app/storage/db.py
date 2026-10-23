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
    """应用 schema.sql（幂等）+ 推进顺序迁移。

    幂等性两层保障：
      1. 全部 CREATE TABLE IF NOT EXISTS
      2. 旧库补丁走 app_migrations 版本表按序执行（见 apply_migrations）
    """
    if schema_file is None:
        # 固定指向项目内的 app/storage/schema.sql（不依赖 DB 路径推导）
        schema_file = Path(__file__).resolve().parent / 'schema.sql'
    if not schema_file.exists():
        raise FileNotFoundError(f'schema 文件不存在: {schema_file}')

    conn = get_conn()
    conn.executescript(schema_file.read_text(encoding='utf-8'))

    apply_migrations()

    # ★ 重建 sources 去重索引（含 sheet_name）。
    #   顺序必须在迁移之后 —— 一个 Excel 拆出的 6 个数据源
    #   共享同一 sha256，必须按 (tenant, sha256, sheet) 去重。
    conn.execute('DROP INDEX IF EXISTS idx_sources_dedup')
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_dedup "
        "ON sources(tenant_id, sha256, COALESCE(sheet_name, ''))")


# ---------------------------------------------------------------------------
# 顺序迁移（docs/16 P2-27）
# ---------------------------------------------------------------------------
# 原先靠硬编码 ALTER 元组 + 无差别吞 OperationalError —— 没有版本记录，
# 旧库停在哪个补丁无从得知，非「列已存在」的真实错误也被吞掉。
# 现在每次迁移落 app_migrations 版本表；只有明确的「duplicate column」
# 视为已应用（老版 apply_schema 补过列但没有版本记录），其余失败必须抛出。
_APP_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, 'sources 多工作表列',
     ["ALTER TABLE sources ADD COLUMN sheet_name TEXT DEFAULT ''"]),
    (2, '额度批次化：流水回指批次（P1-6）',
     ['ALTER TABLE credit_ledger ADD COLUMN lot_id TEXT']),
    (3, 'token 撤销水位 + refresh 轮换族（P1-18）',
     ['ALTER TABLE users ADD COLUMN token_valid_after TEXT',
      'ALTER TABLE refresh_tokens ADD COLUMN family_id TEXT']),
    (4, '诊断-数据源血缘表 + chat_messages 租户索引（P2-29）',
     ['''CREATE TABLE IF NOT EXISTS diagnosis_sources (
            diagnosis_id  TEXT NOT NULL,
            source_id     TEXT NOT NULL,
            source_type   TEXT NOT NULL DEFAULT '',
            imported_rows INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (diagnosis_id, source_id))''',
      'CREATE INDEX IF NOT EXISTS idx_dsrc_source '
      'ON diagnosis_sources(source_id)',
      'CREATE INDEX IF NOT EXISTS idx_chat_tenant '
      'ON chat_messages(tenant_id, created_at DESC)']),
]


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _exec_idempotent(conn: sqlite3.Connection, stmts: list[str]) -> None:
    """执行迁移语句；仅容忍「列已存在」（老库被旧逻辑补过列）。"""
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if 'duplicate column name' not in str(e).lower():
                raise


def apply_migrations() -> list[int]:
    """把库推进到最新版本，返回本次应用的版本列表。"""
    import logging
    log = logging.getLogger(__name__)
    conn = get_conn()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS app_migrations ('
        ' version INTEGER PRIMARY KEY, description TEXT,'
        ' applied_at TEXT NOT NULL)')
    row = conn.execute(
        'SELECT COALESCE(MAX(version), 0) FROM app_migrations').fetchone()
    current = int(row[0] or 0)

    applied: list[int] = []
    for version, desc, stmts in _APP_MIGRATIONS:
        if version <= current:
            continue
        with transaction():
            _exec_idempotent(conn, stmts)
            conn.execute(
                'INSERT INTO app_migrations (version, description, applied_at) '
                'VALUES (?, ?, ?)', (version, desc, _now_iso()))
        applied.append(version)
        log.info('应用库迁移 v%d：%s', version, desc)
    return applied


# ---------------------------------------------------------------------------
# WAL checkpoint（docs/16 P2-23）
# ---------------------------------------------------------------------------

def _wal_sizes() -> dict:
    dbp = _db_path()
    wal = dbp.parent / (dbp.name + '-wal')
    return {
        'db_bytes': dbp.stat().st_size if dbp.exists() else 0,
        'wal_bytes': wal.stat().st_size if wal.exists() else 0,
    }


def wal_checkpoint(mode: str = 'TRUNCATE') -> dict:
    """手工 checkpoint。

    WAL 只增不 checkpoint 时（实测 app.db 462KB 而 -wal 4.1MB），
    直接拷贝 app.db 备份会丢最新计费流水。启动对账与备份脚本调用。
    """
    conn = get_conn()
    before = _wal_sizes()
    row = conn.execute(f'PRAGMA wal_checkpoint({mode})').fetchone()
    return {'mode': mode, 'result': list(row or ()),
            'before': before, 'after': _wal_sizes()}


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
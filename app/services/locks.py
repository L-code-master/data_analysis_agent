"""进程内每租户互斥锁 —— 保护 DuckDB 语义层的并发访问。

为什么需要（docs/16 P0-7）：租户的 ``tenants/{id}/semantic.duckdb`` 是
单文件库，诊断（STG 整表 DELETE/INSERT + ETL 的 CREATE OR REPLACE）
与对话（语义层升级补丁 DDL + 指标查询）并发执行时，语句会交错执行 ——
轻则撞写事务随机 500 / 诊断被打断退单，重则 ETL 读到对方半截导入的
数据、**产出数字错误却标记成功的报告**。

部署约束是 ``--workers 1``（见 docs/04），因此进程内锁即可覆盖全部
并发面；跨进程部署前必须换成文件锁或消息队列串行化。

用法：
    from app.services import locks

    lk = locks.tenant_lock(tenant_id)
    with lk:                      # 持锁期间独占该租户的 DuckDB
        con = semantic_admin.open_semantic_db(tenant_id)
        try:
            ...
        finally:
            con.close()

设计取舍：锁只覆盖 DuckDB 会话（open → 用 → close），**绝不跨 LLM
调用持有** —— 归因对话的 Agent 循环可能跑数十秒，持锁会饿死该租户的
诊断与查数请求。Agent 工具的每次调用单独短持锁（见 chat.ask 的包装）。
"""

from __future__ import annotations

import threading

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


def tenant_lock(tenant_id: str) -> threading.RLock:
    """取该租户的可重入锁（不存在则创建）。"""
    with _guard:
        lk = _locks.get(tenant_id)
        if lk is None:
            lk = threading.RLock()
            _locks[tenant_id] = lk
        return lk

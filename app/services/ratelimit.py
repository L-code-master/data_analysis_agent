"""进程内滑动窗口限速（docs/16 P1-7）。

为什么不用 slowapi：本项目是单 worker 架构（见 docs/04），
进程内计数器已经能挡住"注册小号刷免费额度 / 高频 ask 烧 LLM"，
不引入额外依赖与中间件。**多实例部署时必须换成 Redis**，
否则每个实例各算各的。

用法::

    ok, retry_after = hit('register:1.2.3.4', limit=10, window_seconds=3600)
    if not ok: ...          # retry_after 秒后可再试

    enforce('ask', tenant_id, limit=30, window_seconds=60)   # 超限抛异常
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque

# 保护容器本身的上限：攻击者用随机 key（如伪造 X-Forwarded-For）灌爆内存
_MAX_KEYS = 20_000


class RateLimitExceeded(Exception):
    """超出速率限制。"""

    def __init__(self, scope: str, retry_after: int):
        self.scope = scope
        self.retry_after = max(1, int(retry_after))
        super().__init__(
            f'请求过于频繁（{scope}），请 {self.retry_after} 秒后再试')


_windows: dict[str, deque[float]] = {}
_lock = threading.Lock()


def hit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """记一次命中。返回 (是否放行, 建议等待秒数)。"""
    if limit <= 0 or window_seconds <= 0:        # <=0 视为关闭
        return True, 0

    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        if len(_windows) >= _MAX_KEYS:
            _prune(cutoff)

        dq = _windows.get(key)
        if dq is None:
            dq = deque()
            _windows[key] = dq

        while dq and dq[0] <= cutoff:
            dq.popleft()

        if len(dq) >= limit:
            retry_after = math.ceil(dq[0] + window_seconds - now)
            return False, max(1, retry_after)

        dq.append(now)
    return True, 0


def enforce(scope: str, key: str, limit: int, window_seconds: int) -> None:
    """超限时抛 RateLimitExceeded。"""
    ok, retry_after = hit(f'{scope}:{key}', limit, window_seconds)
    if not ok:
        raise RateLimitExceeded(scope, retry_after)


def _prune(cutoff: float) -> None:
    """清理空窗口与过期窗口（调用方持锁）。"""
    stale = [k for k, dq in _windows.items()
             if not dq or dq[-1] <= cutoff]
    for k in stale:
        _windows.pop(k, None)
    # 仍然过多（大量长窗口 key）→ 丢弃最早插入的一批，保证内存有界
    if len(_windows) >= _MAX_KEYS:
        for k in list(_windows)[: _MAX_KEYS // 4]:
            _windows.pop(k, None)


def reset() -> None:
    """测试用：清空全部计数。"""
    with _lock:
        _windows.clear()


def active_keys() -> int:
    """测试/观测用：当前窗口 key 数。"""
    with _lock:
        return len(_windows)
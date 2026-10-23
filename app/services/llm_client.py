"""LLM 客户端 —— OpenAI 兼容协议（阿里云百炼等）。

v1.4 形态 B 的基础设施。两条纪律：
  1. 去相关抖动重试：429/5xx 按指数退避 + 随机抖动重试
     —— 该端点实测会临时限流。
  2. usage 全量返回：input/output/reasoning_tokens 分别记录，
     否则推理模型的成本不可解释（docs/01 §7.5 实测 reasoning 可达 13 倍）。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from app.config import get_settings


@dataclass
class LlmResponse:
    content: str = ''
    tool_calls: list[dict] = field(default_factory=list)
    # [{"id": ..., "name": ..., "arguments": str}]
    finish_reason: str = ''
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


class LlmError(Exception):
    pass


class LlmClient:
    """OpenAI 兼容 chat/completions 客户端（同步 —— 引擎跑在后台线程）。"""

    RETRY_STATUS = {429, 500, 502, 503, 504}
    MAX_RETRIES = 3

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, session: requests.Session | None = None):
        s = get_settings()
        self.api_key = api_key or s.llm_api_key
        self.base_url = (base_url or s.llm_base_url).rstrip('/')
        self.model = model or s.llm_model
        # ★ 单次调用超时（docs/16 P1-13）：旧值 120s × 重试 × 8 轮
        #   最坏可拖到一小时，把整个 Agent 请求挂死。
        self.timeout = s.llm_timeout_seconds
        self.http = session or requests.Session()

    # ------------------------------------------------------------------

    def chat(self, messages: list[dict],
             tools: list[dict] | None = None,
             max_tokens: int | None = None,
             temperature: float = 0.2) -> LlmResponse:
        """一次对话补全（带抖动重试）。"""
        if not self.api_key:
            raise LlmError('LLM_API_KEY 未配置 —— 形态 B 需要真实 LLM')

        payload: dict[str, Any] = {
            'model': self.model,
            'messages': messages,
            'temperature': temperature,
        }
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
        if max_tokens:
            payload['max_tokens'] = max_tokens

        last_err: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            if attempt:
                # 去相关抖动：基础退避 × 随机抖动（0.5~1.5）
                base = min(2 ** attempt, 20)
                time.sleep(base * random.uniform(0.5, 1.5))
            try:
                resp = self.http.post(
                    f'{self.base_url}/chat/completions',
                    headers={'Authorization': f'Bearer {self.api_key}'},
                    json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                continue                     # 网络错误 → 重试
            if resp.status_code in self.RETRY_STATUS:
                last_err = LlmError(f'HTTP {resp.status_code}: {resp.text[:200]}')
                continue
            if resp.status_code != 200:
                raise LlmError(f'HTTP {resp.status_code}: {resp.text[:300]}')
            # ★ P3-3：200 但非 JSON（网关错误页等）必须转成 LlmError，
            #   原先 JSONDecodeError 冒泡出去既不被重试也不被引擎分类。
            try:
                data = resp.json()
            except ValueError:
                last_err = LlmError(f'响应不是 JSON: {resp.text[:200]}')
                continue
            return self._parse(data)

        raise LlmError(f'重试 {self.MAX_RETRIES} 次后仍失败: {last_err}')

    # ------------------------------------------------------------------

    @staticmethod
    def _parse(data: dict) -> LlmResponse:
        try:
            choice = data['choices'][0]
            msg = choice.get('message', {}) or {}
            calls = []
            for c in msg.get('tool_calls') or []:
                fn = c.get('function', {}) or {}
                calls.append({
                    'id': c.get('id') or f'call_{len(calls)}',
                    'name': fn.get('name', ''),
                    'arguments': fn.get('arguments', '{}'),
                })
            usage = data.get('usage', {}) or {}
            # 兼容 reasoning 字段的多种命名。
            # ★ P3-3：completion_tokens_details 可能为 null ——
            #   原先在 None 上 .get 会 AttributeError 且成本不记账。
            details = usage.get('completion_tokens_details') or {}
            rt = (usage.get('reasoning_tokens')
                  or details.get('reasoning_tokens')
                  or 0)
            return LlmResponse(
                content=msg.get('content') or '',
                tool_calls=calls,
                finish_reason=choice.get('finish_reason', ''),
                input_tokens=usage.get('prompt_tokens', 0) or 0,
                output_tokens=usage.get('completion_tokens', 0) or 0,
                reasoning_tokens=int(rt or 0),
            )
        except (KeyError, IndexError, TypeError) as e:
            raise LlmError(f'响应解析失败: {e}; body={str(data)[:200]}') from e


# ---------------------------------------------------------------------------
# 共享单例（docs/16 P3-6）
# ---------------------------------------------------------------------------
# 原先每次 ask 都新建 LlmClient（连带 requests.Session）且从不关闭 ——
# 长对话会积累连接。客户端无状态（配置来自 settings），全局复用一个。
_shared_client: LlmClient | None = None


def get_shared_client() -> LlmClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = LlmClient()
    return _shared_client


class FakeLlmClient:
    """脚本化假客户端 —— 引擎测试用（零真实调用）。

    脚本按轮次返回；每项是 LlmResponse 或 Exception。
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []            # 记录每次收到的 messages
        self.model = 'fake-model'

    def chat(self, messages, tools=None, **_kw) -> LlmResponse:
        self.calls.append({'messages': [dict(m) for m in messages],
                           'tools': tools})
        if not self.script:
            return LlmResponse(content='（脚本耗尽）', finish_reason='stop')
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

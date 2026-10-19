"""Token 估算器 —— 可插拔的上下文测量。

独立于业务：只认 OpenAI 格式消息，不知道"指标""租户"为何物。
默认实现是启发式（无 tokenizer 依赖）；接真实 tokenizer 时替换实例即可，
Compressor 不感知。

估算纪律（docs/01 R10）：启发式会低估推理型模型的实际消耗，
配额应按估算值 ×1.15 保守取。
"""

from __future__ import annotations

import math


class TokenEstimator:
    """中英混合启发式估算：1 个 CJK 字符 ≈ 1 token，4 个非 CJK 字符 ≈ 1 token。

    Args:
        char_ratio: 简单模式的平均每 token 字符数（纯文本快捷估算用）。
        per_message_overhead: 每条消息的结构开销（role/包装等），默认 4。
    """

    def __init__(self, char_ratio: float = 3.0,
                 per_message_overhead: int = 4):
        self.char_ratio = max(1.0, char_ratio)
        self.per_message_overhead = per_message_overhead

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
        other = len(text) - cjk
        return cjk + math.ceil(other / 4)

    def count_message(self, msg: dict) -> int:
        """估算单条消息的 token 数（含 tool_calls 结构）。"""
        n = self.per_message_overhead
        content = msg.get('content')
        if isinstance(content, str):
            n += self.count_text(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    n += self.count_text(str(part.get('text', '')))
                elif isinstance(part, str):
                    n += self.count_text(part)

        for call in msg.get('tool_calls') or []:
            fn = call.get('function', {})
            n += self.count_text(str(fn.get('name', '')))
            n += self.count_text(str(fn.get('arguments', '')))
            n += 3                      # call 结构开销
        return n

    def count_messages(self, msgs: list[dict]) -> int:
        return sum(self.count_message(m) for m in msgs)

"""诊断域错误类型（从 diagnosis.py 拆出，docs/16 P2-25）。

独立成模块的原因：semantic_admin / analytics 是被 diagnosis 依赖的叶模块，
但迁移失败仍需抛 DiagnosisError 家族（路由按 code 统一映射 HTTP 状态）。
错误类型放在三方共用的这里，避免叶模块反向 import diagnosis
（原先 agent_engine/qa_engine 反向 import diagnosis 私有函数的事实循环依赖）。
"""

from __future__ import annotations


class DiagnosisError(Exception):
    def __init__(self, message: str, code: str = 'DIAGNOSIS_ERROR'):
        super().__init__(message)
        self.code = code
        self.message = message


class SourceNotFoundError(DiagnosisError):
    def __init__(self, sid: str):
        super().__init__(f'数据源不存在: {sid}', 'SOURCE_NOT_FOUND')


class SourceTypeUnknownError(DiagnosisError):
    def __init__(self, detail: str):
        super().__init__(
            f'无法识别文件类型。{detail}', 'SOURCE_TYPE_UNKNOWN')


class MappingNotConfirmedError(DiagnosisError):
    def __init__(self):
        super().__init__(
            '字段映射尚未确认 —— 请先核对映射再执行诊断',
            'MAPPING_NOT_CONFIRMED')


class QualityGateError(DiagnosisError):
    def __init__(self, detail: str):
        super().__init__(f'数据质量校验未通过：{detail}', 'QUALITY_GATE_FAILED')

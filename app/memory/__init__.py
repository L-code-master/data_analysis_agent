"""记忆层 —— 通用可复用的多轮上下文管理组件。

对外契约（其他智能体直接 import 这三个名字即可）：
    Compressor        四级压缩器（L1 截断 / L2 分级删除 / L3 摘要 / L4 紧急）
    CompressResult    压缩结果（含 events，供观测层记录）
    TokenEstimator    可插拔 token 估算器

零业务依赖：不 import app 的任何业务模块。设计见 docs/10。
"""

from app.memory.compressor import (
    CompressResult,
    Compressor,
    REACTIVE_API_ERRORS,
    fix_orphaned_tool_pairs,
)
from app.memory.estimator import TokenEstimator

__all__ = ['Compressor', 'CompressResult', 'TokenEstimator',
           'fix_orphaned_tool_pairs', 'REACTIVE_API_ERRORS']

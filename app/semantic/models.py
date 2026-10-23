"""语义层数据模型。

与 semantic/schema.sql 的表结构一一对应。
所有 dataclass 均为不可变（frozen），避免运行期被意外修改口径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar


class UncertaintyLevel(str, Enum):
    """口径不确定度。"""
    LOW = 'low'        # 已核实
    MEDIUM = 'medium'  # 部分待确认
    HIGH = 'high'      # 关键口径未确认，结论需标注

    @property
    def label(self) -> str:
        return {'low': '已核实', 'medium': '部分待确认', 'high': '关键口径待确认'}[self.value]

    @property
    def blocks_report(self) -> bool:
        """high 级别是否阻止出报告？当前策略：不阻止，但强制加脚注。"""
        return False


class MetricStatus(str, Enum):
    DRAFT = 'draft'
    CONFIRMED = 'confirmed'


@dataclass(frozen=True)
class Uncertainty:
    """不确定项。"""
    level: UncertaintyLevel = UncertaintyLevel.LOW
    items: tuple[str, ...] = ()
    note: str = ''

    @classmethod
    def from_json(cls, raw: str | None) -> Uncertainty:
        if not raw:
            return cls()
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return cls()
        lvl = d.get('level', 'low')
        try:
            level = UncertaintyLevel(lvl)
        except ValueError:
            level = UncertaintyLevel.MEDIUM
        return cls(
            level=level,
            items=tuple(d.get('items', []) or []),
            note=d.get('note', ''),
        )

    def to_json(self) -> str:
        return json.dumps({
            'level': self.level.value,
            'items': list(self.items),
            'note': self.note,
        }, ensure_ascii=False)


@dataclass(frozen=True)
class QualityRule:
    """质量规则引用（真正的规则实现在 schema.sql 的 QC-xx 中）。"""
    rule_id: str
    severity: str = 'ERROR'   # ERROR | WARN

    @property
    def blocks(self) -> bool:
        return self.severity == 'ERROR'


@dataclass(frozen=True)
class MetricDefinition:
    """一个指标定义。对应 sem_metric_registry 一行。"""

    metric_id: str
    metric_name: str
    category: str
    priority: str

    aliases: tuple[str, ...] = ()
    business_def: str = ''
    formula_view: str = ''
    formula_expression: str = ''
    # ★ 机器可执行规格随加载一起缓存（docs/16 P1-16）：
    #   否则渲染器每次都要回查 sem_metric_registry；连接关闭后也无法复用
    #   已加载的 registry（chat 的 Agent 工具调用场景）。
    metric_spec_raw: str = ''

    time_grain: tuple[str, ...] = ('day', 'week', 'month')
    caliber_rules: dict[str, Any] = field(default_factory=dict)
    footnote_template: str = ''
    quality_rules: tuple[QualityRule, ...] = ()
    uncertainty: Uncertainty = field(default_factory=Uncertainty)

    status: MetricStatus = MetricStatus.DRAFT
    source_tables: tuple[str, ...] = ()
    version: str = 'v1.0'
    owner: str = ''

    # 允许的切片维度（由 sem_metric_dimension 单独查询填充）
    allowed_dimensions: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    # sem_metric_registry 的列顺序（须与 semantic/schema.sql 一致）。
    # DuckDB 返回裸 tuple（无 .keys()），必须按位置映射 ——
    # 这是本模块原先只支持 sqlite3.Row、漏掉 DuckDB 的根因。
    _COLUMNS: ClassVar[tuple[str, ...]] = (
        'metric_id', 'metric_name', 'category', 'priority',
        'aliases', 'business_def', 'formula_view', 'formula_expression',
        'metric_spec', 'time_grain', 'caliber_rules', 'footnote_template',
        'quality_rules', 'uncertainty', 'status', 'source_tables',
        'version', 'owner', 'created_at', 'updated_at',
    )

    @classmethod
    def from_row(cls, row: Any, allowed_dimensions: tuple[str, ...] = ()) -> MetricDefinition:
        """从数据库行构造。

        同时支持：
          - sqlite3.Row（有 .keys()）
          - DuckDB 裸 tuple（按 _COLUMNS 位置映射）
          - dict
        """
        def _json_tuple(raw: Any) -> tuple[str, ...]:
            if not raw:
                return ()
            try:
                v = json.loads(raw)
                return tuple(v) if isinstance(v, list) else ()
            except (json.JSONDecodeError, TypeError):
                return ()

        def _json_dict(raw: Any) -> dict[str, Any]:
            if not raw:
                return {}
            try:
                v = json.loads(raw)
                return v if isinstance(v, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}

        # ---- 统一取值：dict / sqlite3.Row / tuple ----
        if isinstance(row, dict):
            def _get(name: str, default: Any = None) -> Any:
                return row.get(name, default)
        elif hasattr(row, 'keys'):
            _keys = set(row.keys())

            def _get(name: str, default: Any = None) -> Any:
                return row[name] if name in _keys else default
        else:
            def _get(name: str, default: Any = None) -> Any:
                try:
                    idx = cls._COLUMNS.index(name)
                except ValueError:
                    return default
                return row[idx] if idx < len(row) else default

        rules_raw = _json_tuple(_get('quality_rules'))
        rules = tuple(
            QualityRule(rule_id=r, severity='ERROR') for r in rules_raw
        )

        try:
            status = MetricStatus(str(_get('status', 'draft') or 'draft'))
        except ValueError:
            status = MetricStatus.DRAFT

        return cls(
            metric_id=str(_get('metric_id', '') or ''),
            metric_name=str(_get('metric_name', '') or ''),
            category=str(_get('category', '') or ''),
            priority=str(_get('priority', 'P1') or 'P1'),
            aliases=_json_tuple(_get('aliases')),
            business_def=str(_get('business_def', '') or ''),
            formula_view=str(_get('formula_view', '') or ''),
            formula_expression=str(_get('formula_expression', '') or ''),
            metric_spec_raw=str(_get('metric_spec', '') or ''),
            time_grain=_json_tuple(_get('time_grain')) or ('day', 'week', 'month'),
            caliber_rules=_json_dict(_get('caliber_rules')),
            footnote_template=str(_get('footnote_template', '') or ''),
            quality_rules=rules,
            uncertainty=Uncertainty.from_json(_get('uncertainty')),
            status=status,
            source_tables=_json_tuple(_get('source_tables')),
            version=str(_get('version', 'v1.0') or 'v1.0'),
            owner=str(_get('owner', '') or ''),
            allowed_dimensions=allowed_dimensions,
        )

    # ------------------------------------------------------------------
    # 派生
    # ------------------------------------------------------------------
    @property
    def blocking_rules(self) -> tuple[QualityRule, ...]:
        return tuple(r for r in self.quality_rules if r.blocks)

    @property
    def needs_disclaimer(self) -> bool:
        """是否需要在报告中加口径声明。"""
        return self.uncertainty.level != UncertaintyLevel.LOW

    def matches_alias(self, text: str) -> bool:
        """自然语言别名匹配（大小写不敏感）。"""
        t = text.strip().lower()
        if not t:
            return False
        if self.metric_name.lower() == t or self.metric_id.lower() == t:
            return True
        return any(a.lower() == t for a in self.aliases)

    def contains_alias(self, text: str) -> bool:
        """别名出现在输入中（输入更长）。

        例：输入"帮我看下毛利率怎么样"包含别名"毛利率" → True
        """
        t = text.strip().lower()
        if not t:
            return False
        if self.metric_name.lower() in t or self.metric_id.lower() in t:
            return True
        return any(a.lower() in t for a in self.aliases)

    def alias_contains(self, text: str) -> bool:
        """输入出现在别名中（别名更长）—— 用于发现"部分匹配"。

        例：输入"毛利率"是别名"推广毛利率"的子串 → True
        这类指标不会被精确匹配选中，但属于同族口径，
        应作为"相关指标"提示给用户。
        """
        t = text.strip().lower()
        if not t or len(t) < 2:
            return False
        if t in self.metric_name.lower():
            return True
        return any(t in a.lower() for a in self.aliases)

    def supports_dimension(self, dimension: str) -> bool:
        return dimension in self.allowed_dimensions

    def to_dict(self) -> dict[str, Any]:
        return {
            'metric_id': self.metric_id,
            'metric_name': self.metric_name,
            'category': self.category,
            'priority': self.priority,
            'aliases': list(self.aliases),
            'business_def': self.business_def,
            'formula_view': self.formula_view,
            'formula_expression': self.formula_expression,
            'time_grain': list(self.time_grain),
            'caliber_rules': self.caliber_rules,
            'footnote_template': self.footnote_template,
            'quality_rules': [r.rule_id for r in self.quality_rules],
            'uncertainty': {
                'level': self.uncertainty.level.value,
                'label': self.uncertainty.level.label,
                'items': list(self.uncertainty.items),
                'note': self.uncertainty.note,
            },
            'status': self.status.value,
            'version': self.version,
            'allowed_dimensions': list(self.allowed_dimensions),
        }


@dataclass(frozen=True)
class QualityCheckResult:
    """一条质量检查结果。"""
    rule_id: str
    metric_id: str
    severity: str
    passed: bool
    expected: str = ''
    actual: str = ''
    message: str = ''

    @property
    def blocks(self) -> bool:
        return self.severity == 'ERROR' and not self.passed

    # sem_quality_result 查询的列顺序（见 registry.quality_results）
    _COLUMNS: ClassVar[tuple[str, ...]] = (
        'rule_id', 'metric_id', 'severity', 'passed', 'expected', 'actual', 'message',
    )

    @classmethod
    def from_row(cls, row: Any) -> QualityCheckResult:
        """兼容 sqlite3.Row / DuckDB tuple / dict。"""
        if isinstance(row, dict):
            g = lambda n, d=None: row.get(n, d)          # noqa: E731
        elif hasattr(row, 'keys'):
            _k = set(row.keys())
            g = lambda n, d=None: row[n] if n in _k else d  # noqa: E731
        else:
            def g(n, d=None):
                try:
                    i = cls._COLUMNS.index(n)
                except ValueError:
                    return d
                return row[i] if i < len(row) else d

        return cls(
            rule_id=str(g('rule_id', '') or ''),
            metric_id=str(g('metric_id', '') or ''),
            severity=str(g('severity', 'ERROR') or 'ERROR'),
            passed=bool(g('passed', False)),
            expected=str(g('expected', '') or ''),
            actual=str(g('actual', '') or ''),
            message=str(g('message', '') or ''),
        )


@dataclass(frozen=True)
class CaliberConflict(Exception):
    """口径歧义 —— 必须显式指定，禁止猜测。

    这是本产品最重要的设计：同一个"毛利率"在多个口径下是不同指标，
    系统必须报错而不是随便选一个。
    """
    query: str
    candidates: tuple[str, ...]

    def __str__(self) -> str:
        names = '、'.join(self.candidates)
        return (f'「{self.query}」对应 {len(self.candidates)} 个口径：{names}。'
                f'请明确指出你要看哪一个。')

    def __post_init__(self) -> None:
        # dataclass(frozen) + Exception 需要显式初始化 args
        Exception.__init__(self, str(self))


@dataclass(frozen=True)
class FieldMapping:
    """源列名 → 语义字段 的映射建议。"""
    source_column: str
    target_field: str
    confidence: float          # 0.0 ~ 1.0
    reason: str = ''
    confirmed: bool = False

    @property
    def needs_review(self) -> bool:
        """置信度低 → 必须人工确认。"""
        return self.confidence < 0.8

    @property
    def confidence_label(self) -> str:
        if self.confidence >= 0.9:
            return '高'
        if self.confidence >= 0.8:
            return '中'
        if self.confidence >= 0.5:
            return '低（需确认）'
        return '极低（需确认）'

    def to_dict(self) -> dict[str, Any]:
        return {
            'source_column': self.source_column,
            'target_field': self.target_field,
            'confidence': round(self.confidence, 3),
            'confidence_label': self.confidence_label,
            'needs_review': self.needs_review,
            'reason': self.reason,
            'confirmed': self.confirmed,
        }
"""SQL 渲染器 —— 把「指标 + 维度 + 时间窗」组装成可执行 SQL。

这是语义层的出口：Agent 只输出结构化的查询意图，
渲染器负责生成 SQL。**Agent 全程不写 SQL，也不碰业务逻辑。**

设计要点：
  1. 维度白名单校验前置 —— 白名单外的维度直接拒绝（防模型乱切片）
  2. 全部参数化 —— 维度值、日期一律走占位符，杜绝注入
  3. 比率型指标分子分母同维度集聚合 —— 避免"先算日均再相除"的错误
  4. 口径脚注随结果返回 —— 让每个数字都能自我解释
  5. 不拼接用户输入到 SQL 结构位（表名/列名只来自白名单）

用法::

    r = SQLRenderer(registry)
    q = r.render(metric_id='M13', dimensions=('date','item_id'),
                 time_range=parse_range('本周'))
    # q.sql / q.params / q.footnote / q.columns
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from app.semantic.models import MetricDefinition, UncertaintyLevel
from app.semantic.timerange import CompareMode, DateRange, comparison_range


class RenderError(ValueError):
    """SQL 渲染失败（维度越权、规格缺失等）。"""


# 允许作为分组列的维度名（白名单的第二道防线）
_SAFE_IDENT = __import__('re').compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# 逻辑维度名 → 视图中的实际列名。
# 语义层对外用 `date`（业务语言），但所有指标视图统一用 `stat_date`。
# 这个映射是渲染器的职责 —— 让上层不必知道物理列名。
DIMENSION_COLUMN_MAP: dict[str, str] = {
    'date': 'stat_date',
    'item_id': 'item_id',
    'merchant_code': 'merchant_code',
    'channel': 'channel',
    'campaign_id': 'campaign_id',
    'source_channel': 'source_channel',
    'reason_category': 'reason_category',
    'stat_scope': 'stat_scope',
}

# 允许做等值过滤但不在白名单里的附加列（这些是视图自带、风险可控的列）
FILTERABLE_EXTRA: frozenset[str] = frozenset({
    'item_id', 'merchant_code', 'channel', 'campaign_id',
    'reason_category', 'stat_scope', 'source_channel',
})


def resolve_dimension(dim: str) -> str:
    """把逻辑维度名解析成视图中的物理列名。

    Raises:
        RenderError: 维度不在已知映射中
    """
    col = DIMENSION_COLUMN_MAP.get(dim)
    if col is None:
        raise RenderError(
            f'未知维度 {dim!r}。已知维度: {", ".join(sorted(DIMENSION_COLUMN_MAP))}')
    return col


@dataclass
class RenderedQuery:
    """渲染结果。"""
    sql: str
    params: list[Any]
    metric: MetricDefinition
    time_range: DateRange | None
    dimensions: tuple[str, ...]
    columns: tuple[str, ...]
    unit: str = 'count'
    footnote: str = ''
    warnings: list[str] = field(default_factory=list)
    compare_sql: str = ''
    compare_params: list[Any] = field(default_factory=list)
    compare_range: DateRange | None = None

    def to_dict(self) -> dict:
        return {
            'metric_id': self.metric.metric_id,
            'metric_name': self.metric.metric_name,
            'sql': self.sql,
            'params': self.params,
            'columns': list(self.columns),
            'dimensions': list(self.dimensions),
            'unit': self.unit,
            'time_range': (self.time_range.to_dict()
                           if self.time_range is not None else None),
            'footnote': self.footnote,
            'warnings': self.warnings,
            'has_compare': bool(self.compare_sql),
            'compare_range': self.compare_range.to_dict() if self.compare_range else None,
        }


def _require_ident(name: str) -> str:
    """校验标识符安全（白名单之外的第二道防线）。"""
    if not _SAFE_IDENT.match(name or ''):
        raise RenderError(f'非法标识符: {name!r}')
    return name


class SQLRenderer:
    """SQL 渲染器。"""

    def __init__(self, registry):
        """registry: SemanticRegistry 实例（已 load）。"""
        self._reg = registry

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def render(
        self,
        metric_id: str,
        dimensions: Iterable[str] = (),
        time_range: DateRange | None = None,
        *,
        compare: str = CompareMode.NONE,
        limit: int = 1000,
        filters: dict[str, Any] | None = None,
        metric_ids: Iterable[str] | None = None,
    ) -> RenderedQuery:
        """渲染查询。

        Args:
            metric_id: 主指标 ID（如 'M13'）
            dimensions: 分组维度（必须在白名单内）
            time_range: 时间窗（None 表示不限制时间）
            compare: 对比模式（none/prev/yoy/lw/lm）
            limit: 结果行数上限
            filters: 等值过滤，如 {'item_id': 'ITEM-A'}
            metric_ids: 多指标时传入（会把多个指标渲染成并列列）

        Raises:
            RenderError: 维度越权 / 规格缺失 / 非法值
        """
        metric = self._reg.get(metric_id)
        if metric is None:
            raise RenderError(f'未知指标: {metric_id}')

        # ---- 1. 维度白名单校验（防模型乱切片）----
        dims = tuple(dimensions or ())
        ok, errors = self._reg.validate_dimensions(metric_id, dims)
        if not ok:
            raise RenderError('维度校验失败: ' + '；'.join(errors))
        for d in dims:
            _require_ident(d)

        # ---- 2. 解析 metric_spec ----
        spec = self._parse_spec(metric)

        # ---- 3. 待渲染的指标列表（支持一次查多个指标）----
        targets: list[tuple[MetricDefinition, dict]] = [(metric, spec)]
        if metric_ids:
            for mid in metric_ids:
                if mid == metric_id:
                    continue
                m2 = self._reg.get(mid)
                if m2 is None:
                    raise RenderError(f'未知指标: {mid}')
                # 同视图才能并列（否则要 UNION，语义不清）
                if m2.formula_view != metric.formula_view:
                    raise RenderError(
                        f'指标 {mid} 属于视图 {m2.formula_view}，'
                        f'与 {metric_id} 的 {metric.formula_view} 不同，无法并列查询')
                targets.append((m2, self._parse_spec(m2)))

        # ---- 4. 组装 SELECT ----
        view = _require_ident(metric.formula_view)
        selects: list[str] = []
        columns: list[str] = []
        cross_view_metrics: list[tuple[str, str]] = []

        # 逻辑维度 → 物理列；输出时用 `AS 逻辑名` 保持对上层友好的列名
        phys_dims: list[tuple[str, str]] = []      # (logical, physical)
        for d in dims:
            phys = resolve_dimension(d)
            phys_dims.append((d, phys))
            if phys == d:
                selects.append(phys)
            else:
                selects.append(f'{phys} AS {d}')
            columns.append(d)

        group_cols = [p for _, p in phys_dims]

        for m, sp in targets:
            alias = m.metric_id.lower()
            if sp.get('agg', '').upper() == 'CROSS_VIEW':
                # ★ 跨视图指标禁止带维度切片（docs/16 P0-5）：
                #   其分子/分母是「整个时间窗的全局标量子查询」，与分组
                #   维度没有任何关联 —— 渲染成
                #   SELECT item_id, <全局值> ... GROUP BY item_id 后
                #   每组都会拿到同一个全店值，是彻底错误的数字。
                #   在渲染层硬拒绝（双保险：白名单也已同步移除 M17 维度）。
                if group_cols:
                    raise RenderError(
                        f'指标 {m.metric_id}（{m.metric_name}）为跨视图口径，'
                        f'不支持按维度 {list(dims)} 切片 —— 每组会得到相同的'
                        f'全店值。请去掉维度查询总量，或分别查询分子/分母指标')
                expr, _ = self._expression_cross_view(sp, limit)
                # 跨视图表达式自带 4 个日期占位符，标记以便 build 补参数
                cross_view_metrics.append((alias, expr))
                selects.append(f'{expr} AS {alias}')
            else:
                selects.append(f'{self._expression(sp)} AS {alias}')
            columns.append(alias)

        # ---- 5. WHERE（把时间条件与过滤条件分开记录，便于对比查询复用）----
        #   time_cond: ('stat_date BETWEEN ? AND ?', [start, end]) 或 None
        #   filter_conds: [(sql_fragment, params), ...]
        time_cond: tuple[str, list[Any]] | None = None
        if time_range is not None:
            time_cond = ('stat_date BETWEEN ? AND ?',
                         [time_range.start, time_range.end])

        filter_conds: list[tuple[str, list[Any]]] = []
        if filters:
            for k, v in filters.items():
                _require_ident(k)
                if k not in dims and k not in FILTERABLE_EXTRA:
                    raise RenderError(
                        f'不支持按 {k!r} 过滤。'
                        f'允许: 已选维度 {sorted(dims)} 或 {sorted(FILTERABLE_EXTRA)}')
                phys_k = DIMENSION_COLUMN_MAP.get(k, k)
                filter_conds.append((f'{phys_k} = ?', [v]))

        # ---- 5b. 指标自带的固定口径过滤（docs/16 P1-9）----
        #   例：流量指标只取 stat_scope='SHOP' AND source_channel='TOTAL'，
        #   否则「合计行 + 分渠道行 / 店铺级 + 商品级」会被 SUM 重复计数。
        #   过滤字段只允许已知维度列，值一律参数化（不拼接用户输入）。
        base_conds = self._base_filter_conds(targets)

        def build(rng: DateRange | None) -> tuple[str, list[Any]]:
            """按给定日期区间生成 SQL 与参数。

            参数顺序必须与 SQL 中 `?` 的出现顺序一致：
              1) SELECT 中跨视图子查询的日期（每个跨视图指标 4 个）
              2) WHERE 中的过滤条件
              3) WHERE 中的时间条件
            """
            ps: list[Any] = []
            # 1) 跨视图子查询的日期参数
            #    跨视图表达式带 4 个日期占位符，无论如何都必须绑定：
            #    - 有时间窗 → 用该区间
            #    - 无时间窗 → 用全历史区间（1900-01-01 ~ 2999-12-31）
            if cross_view_metrics:
                rng_s = rng.start if rng is not None else date(1900, 1, 1)
                rng_e = rng.end if rng is not None else date(2999, 12, 31)
                for _alias, _expr in cross_view_metrics:
                    ps.extend([rng_s, rng_e, rng_s, rng_e])

            frags: list[str] = []
            # 2a) 指标口径过滤（固定条件，先于用户过滤）
            for frag, fparams in base_conds:
                frags.append(frag)
                ps.extend(fparams)
            # 2b) 用户过滤条件
            for frag, fparams in filter_conds:
                frags.append(frag)
                ps.extend(fparams)
            # 3) 时间条件
            if time_cond is not None and rng is not None:
                frags.append(time_cond[0])
                ps.extend([rng.start, rng.end])

            s = f'SELECT {", ".join(selects)}\nFROM {view}'
            if frags:
                s += '\nWHERE ' + '\n  AND '.join(frags)
            if group_cols:
                s += '\nGROUP BY ' + ', '.join(group_cols)
                s += '\nORDER BY ' + ', '.join(group_cols)
            s += f'\nLIMIT {int(limit)}'
            return s, ps

        sql, params = build(time_range)

        # ---- 6. 对比区间（复用 build，保证参数顺序一致）----
        cmp_sql, cmp_params, cmp_range = '', [], None
        if compare != CompareMode.NONE and time_range is not None:
            cmp_range = comparison_range(time_range, compare)
            if cmp_range is not None:
                cmp_sql, cmp_params = build(cmp_range)

        # ---- 7. 脚注与告警 ----
        footnote = self._footnote(metric, time_range, targets)
        warnings = self._warnings(metric, targets)

        return RenderedQuery(
            sql=sql,
            params=params,
            metric=metric,
            time_range=time_range,       # None = 不限制时间（P2-7）
            dimensions=dims,
            columns=tuple(columns),
            unit=spec.get('unit', 'count'),
            footnote=footnote,
            warnings=warnings,
            compare_sql=cmp_sql,
            compare_params=cmp_params,
            compare_range=cmp_range,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _base_filter_conds(self, targets: list[tuple[MetricDefinition, dict]]
                           ) -> list[tuple[str, list[Any]]]:
        """把 metric_spec.base_filter（等值过滤）转成 SQL 片段。

        同一次查询的多个指标必须口径一致，否则拒绝 —— 否则一个 WHERE
        无法同时满足两种基础过滤，静默取其一就是错数字。
        """
        conds: list[tuple[str, list[Any]]] = []
        signature: str | None = None
        for m, sp in targets:
            bf = sp.get('base_filter')
            if not bf:
                continue
            if not isinstance(bf, dict):
                raise RenderError(
                    f'指标 {m.metric_id} 的 base_filter 必须是对象')
            parts: list[tuple[str, list[Any]]] = []
            for k, v in sorted(bf.items()):
                _require_ident(k)
                if k not in DIMENSION_COLUMN_MAP and k not in FILTERABLE_EXTRA:
                    raise RenderError(
                        f'指标 {m.metric_id} 的 base_filter 含未知列 {k!r}')
                if not isinstance(v, (str, int, float, bool)):
                    raise RenderError(
                        f'指标 {m.metric_id} 的 base_filter 值必须是标量')
                parts.append((f'{DIMENSION_COLUMN_MAP.get(k, k)} = ?', [v]))
            sig = json.dumps(sorted(bf.items()), ensure_ascii=False, default=str)
            if signature is None:
                signature = sig
                conds = parts
            elif sig != signature:
                raise RenderError(
                    f'同一次查询的指标口径过滤不一致（{m.metric_id}）—— '
                    f'请分开查询这些指标')
        return conds

    def _parse_spec(self, metric: MetricDefinition) -> dict:
        """解析 metric_spec（机器可执行规格）。"""
        # ★ 优先用加载时缓存的规格（docs/16 P1-16）：registry 可能在
        #   原连接关闭后仍被复用（chat Agent 工具调用），此时不能再回查 DB。
        raw = getattr(metric, 'metric_spec_raw', '') or None
        if raw is None:
            raw = getattr(metric, '_metric_spec_raw', None)
        if raw is None:
            raw = self._reg.metric_spec(metric.metric_id)
        if not raw:
            raise RenderError(
                f'指标 {metric.metric_id}（{metric.metric_name}）'
                f'缺少 metric_spec，无法渲染 SQL。'
                f'请在 sem_metric_registry 中补全该列。')
        try:
            spec = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise RenderError(
                f'指标 {metric.metric_id} 的 metric_spec 不是合法 JSON: {e}') from e
        if 'agg' not in spec:
            raise RenderError(f'指标 {metric.metric_id} 的 metric_spec 缺少 agg 字段')
        return spec

    def _expression(self, spec: dict) -> str:
        """根据 spec 生成聚合表达式（不含参数绑定）。

        需要参数的跨视图表达式由 _expression_cross_view 处理。
        """
        agg = spec['agg'].upper()

        if agg == 'SUM':
            expr = _require_ident_expr(spec.get('expr', ''))
            return f'SUM({expr})'

        if agg == 'AVG':
            expr = _require_ident_expr(spec.get('expr', ''))
            return f'AVG({expr})'

        if agg == 'COUNT':
            expr = _require_ident_expr(spec.get('expr', ''))
            return f'COUNT({expr})'

        if agg == 'RATIO':
            n = f"{spec.get('num_agg','SUM')}({_require_ident_expr(spec['num_expr'])})"
            d = f"{spec.get('den_agg','SUM')}({_require_ident_expr(spec['den_expr'])})"
            return f'{n} / NULLIF({d}, 0)'

        # 跨视图指标需要额外的日期参数，由渲染器统一处理
        if agg == 'CROSS_VIEW':
            raise RenderError(
                'CROSS_VIEW 指标需由渲染器特殊处理，不应走到这里')

        raise RenderError(f'不支持的聚合类型: {agg}')

    def _expression_cross_view(self, spec: dict, limit: int
                               ) -> tuple[str, list[Any]]:
        """生成跨视图表达式与它自带的参数。

        跨视图指标（如退款率 = 退款金额 / 支付金额）的分子分母在不同视图，
        必须各自带一份日期条件 —— 因此单独返回参数列表，由调用方拼接。
        """
        nv = _require_ident(spec['num_view'])
        dv = _require_ident(spec['den_view'])
        n = f"{spec.get('num_agg', 'SUM')}({_require_ident_expr(spec['num_expr'])})"
        d = f"{spec.get('den_agg', 'SUM')}({_require_ident_expr(spec['den_expr'])})"
        expr = (
            f'(SELECT {n} FROM {nv} WHERE stat_date BETWEEN ? AND ?)'
            f' / NULLIF('
            f'(SELECT {d} FROM {dv} WHERE stat_date BETWEEN ? AND ?), 0)'
        )
        return expr, []

    def _footnote(self, metric: MetricDefinition, time_range, targets) -> str:
        """生成口径脚注。"""
        parts: list[str] = []
        if metric.footnote_template:
            parts.append(metric.footnote_template)
        if time_range is not None:
            parts.append(f'统计口径：{time_range.start} 至 {time_range.end}'
                         f'（{time_range.days} 天）')

        # 不确定项提示
        uncertain = [m for m, _ in targets
                     if m.uncertainty.level != UncertaintyLevel.LOW]
        if uncertain:
            names = '、'.join(f'{m.metric_name}({m.metric_id})' for m in uncertain)
            parts.append(f'⚠ {names} 的口径尚未与您确认，建议核对后再做决策')

        return '；'.join(parts)

    def _warnings(self, metric: MetricDefinition, targets) -> list[str]:
        """收集需要提示的口径注意事项。"""
        w: list[str] = []
        for m, _ in targets:
            if m.uncertainty.level == UncertaintyLevel.HIGH:
                for item in m.uncertainty.items:
                    w.append(f'{m.metric_name}：{item}')
        return w


def _require_ident_expr(expr: str) -> str:
    """校验表达式安全。

    允许：列名、加减乘除、括号、数字、NULLIF 等已知函数。
    拒绝：分号、注释、子查询关键字、引号。
    """
    if not expr or not expr.strip():
        raise RenderError('表达式不能为空')
    dangerous = (';', '--', '/*', '*/', "'", '"', 'drop ', 'delete ',
                 'insert ', 'update ', 'union ', 'exec ', 'xp_')
    low = expr.lower()
    for d in dangerous:
        if d in low:
            raise RenderError(f'表达式含非法片段 {d!r}: {expr!r}')
    return expr


# ---------------------------------------------------------------------------
# 便捷：指标清单（给前端选指标用）
# ---------------------------------------------------------------------------

def metric_catalog(registry) -> list[dict]:
    """给前端/Agent 用的指标目录（不含完整口径，省 token）。"""
    return [
        {
            'metric_id': m.metric_id,
            'metric_name': m.metric_name,
            'category': m.category,
            'priority': m.priority,
            'aliases': list(m.aliases),
            'business_def': m.business_def,
            'dimensions': list(m.allowed_dimensions),
            'time_grain': list(m.time_grain),
            'uncertainty_level': m.uncertainty.level.value,
        }
        for m in sorted(registry.all(), key=lambda x: x.metric_id)
    ]
"""指标注册表 —— 语义层的唯一入口。

职责：
  1. 加载/查询指标定义（含维度白名单）
  2. 自然语言别名解析（口径歧义必须报错，不猜）
  3. 质量校验结果查询（验证层门1）
  4. 不确定项汇总（报告必须带的口径声明）

设计原则：
  - 口径 = 数据：所有定义从 DuckDB 的 sem_* 表读取，代码里不硬编码
  - 歧义必抛错：一个词对应多个指标时，抛 CaliberConflict
  - 不确定即披露：high 级别的不确定项必须出现在报告脚注

DuckDB 依赖说明：本模块通过一个薄适配层访问 DuckDB。
当前环境若未安装 duckdb，可用 SQLite 兼容模式（供测试）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from app.semantic.models import (
    CaliberConflict,
    MetricDefinition,
    MetricStatus,
    QualityCheckResult,
    UncertaintyLevel,
)


class SemanticRegistry:
    """指标注册表。

    用法::

        reg = SemanticRegistry(db_path)
        reg.load()

        m = reg.get('M13')                      # 按 ID
        m = reg.resolve('毛利率')                # 按别名（可能抛 CaliberConflict）
        m = reg.resolve('毛利率', 'M13')          # 消歧
        reg.check_dimension('M13', 'item_id')   # 维度白名单校验
    """

    def __init__(self, conn: Any):
        """
        conn: 已连接的 DuckDB/SQLite 连接（由调用方管理生命周期）。
        """
        self._conn = conn
        self._metrics: dict[str, MetricDefinition] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def load(self) -> int:
        """从 sem_metric_registry + sem_metric_dimension 加载全部指标。"""
        # ★ docs/16 P2-28：显式列名（与 MetricDefinition._COLUMNS 一致），
        #   不用 SELECT * —— schema.sql 在中间加列会让位置映射整体错位
        #   （_COLUMNS 与表列的一致性由 test_duckdb_e2e 守护测试拦截）。
        cols = ', '.join(MetricDefinition._COLUMNS)
        rows = self._conn.execute(
            f'SELECT {cols} FROM sem_metric_registry ORDER BY metric_id'
        ).fetchall()

        # 维度白名单（只取 enabled=1）
        dim_rows = self._conn.execute(
            'SELECT metric_id, dimension_name FROM sem_metric_dimension '
            'WHERE enabled = 1 ORDER BY metric_id, dimension_name'
        ).fetchall()

        dims: dict[str, list[str]] = {}
        for r in dim_rows:
            # 兼容 sqlite3.Row（有 keys）与 DuckDB 裸 tuple（按位置）
            if hasattr(r, 'keys'):
                mid = r['metric_id']
                dname = r['dimension_name']
            else:
                mid, dname = r[0], r[1]
            dims.setdefault(mid, []).append(dname)

        metrics: dict[str, MetricDefinition] = {}
        failures: list[str] = []
        for row in rows:
            if hasattr(row, 'keys'):
                raw_mid = row['metric_id']
            else:
                raw_mid = row[0]

            m = MetricDefinition.from_row(row, tuple(dims.get(raw_mid, [])))

            # 防御：解析失败（metric_id 为空）不静默跳过，显式报错
            if not m.metric_id:
                failures.append(str(raw_mid))
                continue
            metrics[m.metric_id] = m

        if failures:
            raise ValueError(
                f'以下指标行解析失败（metric_id 为空）: {failures}。'
                f'通常是 from_row 与表结构不匹配 —— 请检查 _COLUMNS 顺序。')

        self._metrics = metrics
        self._loaded = True
        return len(metrics)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, metric_id: str) -> MetricDefinition | None:
        """按 ID 精确取。"""
        self._ensure_loaded()
        return self._metrics.get(metric_id.strip().upper())

    def metric_spec(self, metric_id: str) -> str | None:
        """取指标的机器可执行规格（metric_spec 原始 JSON 字符串）。

        单独查询而不塞进 MetricDefinition，原因：
        metric_spec 只在渲染 SQL 时需要；缓存进 dataclass 会污染
        to_dict() 的输出（那是给 Agent / 前端看的）。
        """
        rows = self._conn.execute(
            'SELECT metric_spec FROM sem_metric_registry WHERE metric_id = ?',
            [metric_id.strip().upper()],
        ).fetchall()
        if not rows:
            return None
        return rows[0][0]

    def all(self) -> list[MetricDefinition]:
        self._ensure_loaded()
        return list(self._metrics.values())

    def by_category(self, category: str) -> list[MetricDefinition]:
        self._ensure_loaded()
        return [m for m in self._metrics.values() if m.category == category]

    def by_priority(self, priority: str) -> list[MetricDefinition]:
        self._ensure_loaded()
        return [m for m in self._metrics.values() if m.priority == priority.upper()]

    def categories(self) -> list[str]:
        self._ensure_loaded()
        return sorted({m.category for m in self._metrics.values()})

    def find(self, text: str) -> list[MetricDefinition]:
        """模糊查找：返回所有命中的指标（精确命中优先于子串命中）。

        策略（两级）：
          1. 精确命中（别名与输入完全一致）—— 意图明确
          2. 精确无命中才退化为子串命中 —— 意图模糊

        注意：精确优先会**掩盖部分匹配**。例如输入"毛利率"精确命中 M13，
        但"毛利率"也是 M14 别名"推广毛利率"的子串。
        此时用 find_with_related() 可同时拿到相关指标用于提示。
        """
        self._ensure_loaded()
        text = (text or '').strip()
        if not text:
            return []
        exact = [m for m in self._metrics.values() if m.matches_alias(text)]
        if exact:
            return exact
        return [m for m in self._metrics.values() if m.contains_alias(text)]

    def find_with_related(self, text: str) -> tuple[list[MetricDefinition],
                                                    list[MetricDefinition]]:
        """返回 (直接命中, 相关但未命中)。

        相关指标 = 别名包含该输入的指标（即输入是更长别名的子串）。
        用途：用户说"毛利率"时，直接给 M13，同时提示"另有推广口径 M14"。
        """
        self._ensure_loaded()
        text = (text or '').strip()
        if not text:
            return [], []

        primary = self.find(text)
        primary_ids = {m.metric_id for m in primary}

        related = [
            m for m in self._metrics.values()
            if m.metric_id not in primary_ids and m.alias_contains(text)
        ]
        return primary, sorted(related, key=lambda m: m.metric_id)

    def ambiguity_note(self, text: str) -> str:
        """生成歧义提示文案（无相关指标时返回空串）。

        这是"口径透明"的轻量落地：不打断用户的明确查询，
        但主动告知同族口径的存在。
        """
        primary, related = self.find_with_related(text)
        if not related or len(primary) != 1:
            return ''
        chosen = primary[0]
        others = '、'.join(f'{m.metric_name}（{m.metric_id}）' for m in related)
        return (f'「{text}」已按 {chosen.metric_name}（{chosen.metric_id}）计算。'
                f'另有相近口径：{others} —— 如需比较请明确指定。')

    def resolve(self, text: str, metric_id: str | None = None) -> MetricDefinition:
        """解析自然语言 → 指标定义。

        Args:
            text: 用户输入，如 "毛利率"
            metric_id: 显式指定时跳过歧义检测

        Raises:
            CaliberConflict: 命中多个指标且未显式指定 —— 这是刻意的设计，
                不允许猜测口径。
            KeyError: 完全无命中。
        """
        self._ensure_loaded()

        if metric_id:
            m = self.get(metric_id)
            if m is None:
                raise KeyError(f'未知指标 ID: {metric_id!r}')
            return m

        hits = self.find(text)
        if not hits:
            raise KeyError(f'无法从 {text!r} 识别出指标。'
                           f'可用指标: {", ".join(sorted(self._metrics))}')
        if len(hits) > 1:
            # ★ 口径歧义：必须报错，这是产品的核心纪律
            raise CaliberConflict(
                query=text,
                candidates=tuple(f'{h.metric_id} {h.metric_name}' for h in hits),
            )
        return hits[0]

    # ------------------------------------------------------------------
    # 维度白名单
    # ------------------------------------------------------------------
    def check_dimension(self, metric_id: str, dimension: str) -> tuple[bool, str]:
        """校验某指标是否允许按该维度切片。

        这是防模型乱切片的关键 —— 白名单外一律拒绝。
        """
        m = self.get(metric_id)
        if m is None:
            return False, f'未知指标: {metric_id}'
        if dimension in m.allowed_dimensions:
            return True, ''
        return False, (
            f'指标 {metric_id}（{m.metric_name}）不支持按 {dimension!r} 切片。'
            f'允许的维度: {", ".join(m.allowed_dimensions) or "（无）"}'
        )

    def validate_dimensions(self, metric_id: str,
                            dimensions: Iterable[str]) -> tuple[bool, list[str]]:
        """批量校验。返回 (全部通过, 失败原因列表)。"""
        errors: list[str] = []
        for d in dimensions:
            ok, reason = self.check_dimension(metric_id, d)
            if not ok:
                errors.append(reason)
        return (not errors), errors

    # ------------------------------------------------------------------
    # 质量校验（验证层门1）
    # ------------------------------------------------------------------
    def quality_results(self, run_id: str) -> list[QualityCheckResult]:
        """取某次 ETL/校验运行的完整结果。"""
        rows = self._conn.execute(
            'SELECT rule_id, metric_id, severity, passed, expected, actual, message '
            'FROM sem_quality_result WHERE run_id = ? ORDER BY rule_id',
            [run_id],
        ).fetchall()
        return [QualityCheckResult.from_row(r) for r in rows]

    def blocking_failures(self, run_id: str) -> list[QualityCheckResult]:
        """取阻塞级失败（ERROR 且未通过）—— 非空则禁止出报告。"""
        return [r for r in self.quality_results(run_id) if r.blocks]

    def warnings(self, run_id: str) -> list[QualityCheckResult]:
        """取警告级失败（WARN 且未通过）—— 需在报告中提示。"""
        return [r for r in self.quality_results(run_id)
                if r.severity == 'WARN' and not r.passed]

    def quality_passed(self, run_id: str) -> tuple[bool, str]:
        """门1 总判定。"""
        blockers = self.blocking_failures(run_id)
        if blockers:
            msgs = '；'.join(f'[{r.rule_id}] {r.message}' for r in blockers[:5])
            return False, f'数据质量校验未通过（{len(blockers)} 项）: {msgs}'
        return True, ''

    # ------------------------------------------------------------------
    # 不确定项
    # ------------------------------------------------------------------
    def uncertain_metrics(self,
                          min_level: UncertaintyLevel = UncertaintyLevel.MEDIUM
                          ) -> list[MetricDefinition]:
        """列出达到指定不确定度及以上的指标。"""
        self._ensure_loaded()
        order = {UncertaintyLevel.LOW: 0, UncertaintyLevel.MEDIUM: 1,
                 UncertaintyLevel.HIGH: 2}
        threshold = order[min_level]
        result = [m for m in self._metrics.values()
                  if order[m.uncertainty.level] >= threshold]
        return sorted(result, key=lambda m: (-order[m.uncertainty.level], m.metric_id))

    def disclaimer_for(self, metric_ids: Iterable[str]) -> str:
        """为给定指标集合生成口径声明（附在报告末尾）。

        这是"不确定即披露"原则的落地：报告不能说"这是毛利率"，
        而要说"这是按 X 口径算的毛利率，其中 Y 项待你确认"。
        """
        self._ensure_loaded()
        lines: list[str] = []
        seen: set[str] = set()

        for mid in metric_ids:
            m = self.get(mid)
            if m is None or m.metric_id in seen:
                continue
            seen.add(m.metric_id)

            if m.uncertainty.level == UncertaintyLevel.LOW:
                continue

            marker = '⚠' if m.uncertainty.level == UncertaintyLevel.HIGH else '·'
            lines.append(f'{marker} **{m.metric_name}**（{m.metric_id}）'
                         f'— {m.uncertainty.level.label}')
            if m.footnote_template:
                lines.append(f'   口径：{m.footnote_template}')
            for item in m.uncertainty.items:
                lines.append(f'   - 待确认：{item}')

        if not lines:
            return ''
        header = ('\n---\n\n### 口径说明与待确认项\n\n'
                  '本报告的数字按以下口径计算。'
                  '标注 ⚠ 的口径尚未与您确认，建议核对后再做决策：\n')
        return header + '\n'.join(lines)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        self._ensure_loaded()
        by_level: dict[str, int] = {}
        for m in self._metrics.values():
            by_level[m.uncertainty.level.value] = \
                by_level.get(m.uncertainty.level.value, 0) + 1
        return {
            'total': len(self._metrics),
            'by_category': {c: len(self.by_category(c)) for c in self.categories()},
            'by_priority': {p: len(self.by_priority(p)) for p in ('P0', 'P1', 'P2')},
            'by_status': {
                'draft': sum(1 for m in self._metrics.values()
                             if m.status == MetricStatus.DRAFT),
                'confirmed': sum(1 for m in self._metrics.values()
                                 if m.status == MetricStatus.CONFIRMED),
            },
            'by_uncertainty': by_level,
        }
"""时间窗解析 —— 把自然语言时间表达转成 SQL 日期区间。

支持：
    今天 / 昨天 / 前天
    本周 / 上周 / 最近7天 / 最近30天
    本月 / 上月 / 本季度 / 上季度 / 今年 / 去年
    2026-10 / 2026-10-01 / 2026-10-01~2026-10-15

时区约定：**北京时间（Asia/Shanghai）**
淘宝商家后台所有日期均为北京时间，本项目统一按此处理。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta


class TimeRangeError(ValueError):
    """时间表达无法解析。"""


@dataclass(frozen=True)
class DateRange:
    """日期区间（闭区间，含首尾两天）。"""
    start: date
    end: date
    label: str = ''          # 人类可读标签
    grain: str = 'day'       # day | week | month

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def previous(self) -> DateRange:
        """上一个等长区间（用于同环比）。"""
        span = timedelta(days=self.days)
        return DateRange(
            start=self.start - span,
            end=self.end - span,
            label=f'{self.label}(上一期)' if self.label else '',
            grain=self.grain,
        )

    def to_dict(self) -> dict:
        return {
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'days': self.days,
            'label': self.label,
            'grain': self.grain,
        }


# ---------------------------------------------------------------------------
# 基准"今天"
# ---------------------------------------------------------------------------

def _today() -> date:
    """返回北京时间的今天。

    不依赖系统时区设置 —— 显式用 UTC+8 计算。
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('Asia/Shanghai')).date()
    except Exception:
        # 回退：按 UTC+8 偏移
        return (datetime.utcnow() + timedelta(hours=8)).date()


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

_RELATIVE_DAYS = re.compile(r'^(?:最近|过去|近)\s*(\d+)\s*(?:天|日)$')
_ABSOLUTE_RANGE = re.compile(
    r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*[~～至到\-—]+\s*'
    r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$')
_ABS_MONTH = re.compile(r'^(\d{4})[-/.](\d{1,2})$')
_ABS_DAY = re.compile(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$')
_ABS_YEAR = re.compile(r'^(\d{4})\s*年?$')


def parse_range(text: str, today: date | None = None) -> DateRange:
    """把时间表达解析为 DateRange。

    Raises:
        TimeRangeError: 无法识别
    """
    t = (text or '').strip()
    if not t:
        raise TimeRangeError('时间表达不能为空')

    base = today or _today()
    norm = t.replace(' ', '')

    # ---- 相对天数 ----
    m = _RELATIVE_DAYS.match(norm)
    if m:
        n = int(m.group(1))
        if n <= 0:
            raise TimeRangeError(f'天数必须为正: {n}')
        return DateRange(base - timedelta(days=n - 1), base, f'最近{n}天', 'day')

    # ---- 绝对区间 ----
    m = _ABSOLUTE_RANGE.match(norm)
    if m:
        g = [int(x) for x in m.groups()]
        s = date(g[0], g[1], g[2])
        e = date(g[3], g[4], g[5])
        if s > e:
            s, e = e, s
        return DateRange(s, e, f'{s}~{e}', 'day')

    # ---- 绝对日 ----
    m = _ABS_DAY.match(norm)
    if m:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return DateRange(d, d, d.isoformat(), 'day')

    # ---- 绝对月 ----
    m = _ABS_MONTH.match(norm)
    if m:
        return _month_range(int(m.group(1)), int(m.group(2)))

    # ---- 绝对年 ----
    m = _ABS_YEAR.match(norm)
    if m:
        y = int(m.group(1))
        return DateRange(date(y, 1, 1), date(y, 12, 31), f'{y}年', 'month')

    # ---- 命名区间 ----
    return _named_range(norm, base)


def _named_range(norm: str, base: date) -> DateRange:
    """处理"本周/上月"这类命名区间。"""
    if norm in ('今天', '今日', '本日'):
        return DateRange(base, base, '今天', 'day')
    if norm in ('昨天', '昨日'):
        d = base - timedelta(days=1)
        return DateRange(d, d, '昨天', 'day')
    if norm in ('前天',):
        d = base - timedelta(days=2)
        return DateRange(d, d, '前天', 'day')

    if norm in ('本周', '这周', '本星期'):
        return _week_range(base)
    if norm in ('上周', '上星期', '前一周'):
        return _week_range(base - timedelta(days=7), label='上周')

    if norm in ('本月', '这个月', '本月份'):
        return _month_range(base.year, base.month)
    if norm in ('上月', '上个月', '前一个月'):
        y, m = (base.year, base.month - 1) if base.month > 1 else (base.year - 1, 12)
        return _month_range(y, m, label='上月')

    if norm in ('本季度', '这季度', '本季'):
        return _quarter_range(base.year, (base.month - 1) // 3 + 1)
    if norm in ('上季度', '上个季度', '上季'):
        q = (base.month - 1) // 3 + 1
        if q == 1:
            return _quarter_range(base.year - 1, 4, label='上季度')
        return _quarter_range(base.year, q - 1, label='上季度')

    if norm in ('今年', '本年', '本年度'):
        return DateRange(date(base.year, 1, 1), date(base.year, 12, 31),
                         f'{base.year}年', 'month')
    if norm in ('去年', '上年', '上一年'):
        y = base.year - 1
        return DateRange(date(y, 1, 1), date(y, 12, 31), f'{y}年', 'month')

    if norm in ('近7天', '最近七天', '一周'):
        return DateRange(base - timedelta(days=6), base, '最近7天', 'day')
    if norm in ('近30天', '最近三十天', '一个月'):
        return DateRange(base - timedelta(days=29), base, '最近30天', 'day')
    if norm in ('近90天', '最近九十天', '三个月'):
        return DateRange(base - timedelta(days=89), base, '最近90天', 'day')

    raise TimeRangeError(
        f'无法识别的时间表达: {norm!r}。'
        f'支持：今天/昨天/本周/上周/本月/上月/本季度/今年/去年/'
        f'最近7天/最近30天/2026-10/2026-10-01/2026-10-01~2026-10-15')


def _week_range(d: date, label: str = '本周') -> DateRange:
    """自然周（周一~周日）。

    淘宝商家习惯按自然周看数（周一为起点）。
    """
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return DateRange(monday, sunday, label, 'week')


def _month_range(y: int, m: int, label: str = '') -> DateRange:
    if not 1 <= m <= 12:
        raise TimeRangeError(f'月份非法: {m}')
    last = calendar.monthrange(y, m)[1]
    return DateRange(date(y, m, 1), date(y, m, last),
                     label or f'{y}-{m:02d}', 'month')


def _quarter_range(y: int, q: int, label: str = '') -> DateRange:
    if not 1 <= q <= 4:
        raise TimeRangeError(f'季度非法: {q}')
    sm = (q - 1) * 3 + 1
    em = sm + 2
    last = calendar.monthrange(y, em)[1]
    return DateRange(date(y, sm, 1), date(y, em, last),
                     label or f'{y}Q{q}', 'month')


# ---------------------------------------------------------------------------
# 对比区间
# ---------------------------------------------------------------------------

class CompareMode:
    """同环比模式。"""
    NONE = 'none'
    PREVIOUS_PERIOD = 'prev'      # 环比（上一等长区间）
    YEAR_OVER_YEAR = 'yoy'        # 同比（去年同期）
    LAST_WEEK = 'lw'              # 上周同期
    LAST_MONTH = 'lm'             # 上月同期


def comparison_range(rng: DateRange, mode: str) -> DateRange | None:
    """生成对比区间。"""
    if mode == CompareMode.NONE:
        return None
    if mode == CompareMode.PREVIOUS_PERIOD:
        return rng.previous()
    if mode == CompareMode.YEAR_OVER_YEAR:
        # 去年同期：年份 -1，日期裁剪到合法范围
        s = _shift_year(rng.start, -1)
        e = _shift_year(rng.end, -1)
        return DateRange(s, e, f'{rng.label}(去年同期)', rng.grain)
    if mode == CompareMode.LAST_WEEK:
        return DateRange(rng.start - timedelta(days=7),
                         rng.end - timedelta(days=7),
                         f'{rng.label}(上周同期)', rng.grain)
    if mode == CompareMode.LAST_MONTH:
        s = _shift_month(rng.start, -1)
        e = _shift_month(rng.end, -1)
        return DateRange(s, e, f'{rng.label}(上月同期)', rng.grain)
    raise TimeRangeError(f'未知对比模式: {mode!r}')


def _shift_year(d: date, delta: int) -> date:
    """按年平移，2/29 裁剪到 2/28。"""
    y = d.year + delta
    try:
        return d.replace(year=y)
    except ValueError:
        return date(y, 2, 28)


def _shift_month(d: date, delta: int) -> date:
    """按月平移，日期裁剪到目标月最大日。"""
    m = d.month - 1 + delta
    y = d.year + m // 12
    m = m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))
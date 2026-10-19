"""淘宝适配器 —— 把千牛导出文件转成标准 STG 表。

流程：
    sniff()      识别文件属于哪种数据源（按列名特征打分）
    read()       读取文件（自动找表头行、自动探测编码）
    suggest()    生成字段映射建议（含置信度）
    apply()      按确认后的映射写入标准 STG 表
    validate()   校验必填字段是否齐全

不依赖 pandas 之外的重型库；不使用 duckdb（由上层负责写入）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.adapters.taobao_fields import (
    SOURCE_SPECS, SourceSpec, get_spec,
)
from app.semantic.models import FieldMapping


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SniffResult:
    """文件类型识别结果。"""
    spec: SourceSpec | None
    score: float                      # 0.0 ~ 1.0
    matched_columns: list[str] = field(default_factory=list)
    candidates: list[tuple[str, float]] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        return self.spec is not None and self.score >= 0.5

    @property
    def needs_user_choice(self) -> bool:
        """多个候选分数接近 → 需要用户确认。"""
        if len(self.candidates) < 2:
            return False
        top, second = self.candidates[0][1], self.candidates[1][1]
        return (top - second) < 0.15


@dataclass
class ReadResult:
    """文件读取结果。"""
    headers: list[str]
    rows: list[list[Any]]
    sheet_name: str = ''
    header_row_index: int = 0
    encoding: str = 'utf-8'
    warnings: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass
class AdapterError:
    """适配过程的一个问题。"""
    severity: str        # ERROR | WARN
    code: str
    message: str
    column: str = ''
    row_index: int = -1


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r'[\s\u3000]+')
_PUNCT_RE = re.compile(r'[（）()【】\[\]：:，,。.、/／\-_*]')


def normalize_column(name: Any) -> str:
    """列名归一化：去空白、去标点、转小写、全角转半角。

    这样 "支付金额（元）" 与 "支付金额(元)" 与 " 支付金额 " 都能匹配上。
    """
    if name is None:
        return ''
    s = str(name).strip()
    # 全角字母数字 → 半角
    s = ''.join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
        for c in s
    )
    s = _WS_RE.sub('', s)
    s = _PUNCT_RE.sub('', s)
    return s.lower()


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def detect_encoding(path: Path, sample_size: int = 65536) -> str:
    """探测 CSV 编码。

    国内导出常见：UTF-8-BOM / GBK / GB18030。
    优先用 charset-normalizer（若可用），否则按 BOM + 试解码。
    """
    raw = path.read_bytes()[:sample_size]

    # BOM 优先
    for bom, enc in ((b'\xef\xbb\xbf', 'utf-8-sig'),
                     (b'\xff\xfe', 'utf-16-le'),
                     (b'\xfe\xff', 'utf-16-be')):
        if raw.startswith(bom):
            return enc

    try:
        import charset_normalizer
        best = charset_normalizer.from_bytes(raw).best()
        if best and best.encoding:
            return best.encoding
    except ImportError:
        pass

    # 回退：逐个试
    for enc in ('utf-8', 'gb18030', 'gbk'):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return 'utf-8'


def read_file(path: Path, spec: SourceSpec | None = None,
              sheet_name: str | None = None) -> ReadResult:
    """读取 Excel 或 CSV，自动定位表头行。

    sheet_name: 指定读 Excel 的哪个工作表（None = 自动选择）。
    表头行定位：在 spec.header_row_candidates 指定的几个候选行里，
    选「非空单元格最多且能匹配到已知列名」的那一行。
    """
    suffix = path.suffix.lower()
    warnings: list[str] = []

    if suffix in ('.xlsx', '.xlsm'):
        headers, rows, sheet, hdr_idx = _read_excel(path, spec, sheet_name)
        encoding = ''
    elif suffix == '.xls':
        headers, rows, sheet, hdr_idx = _read_excel(path, spec, sheet_name)
        encoding = ''
        warnings.append('.xls 为旧格式，建议另存为 .xlsx 以获得更好的兼容性')
    elif suffix == '.csv':
        if sheet_name:
            raise ValueError('CSV 文件不支持指定工作表')
        encoding = detect_encoding(path)
        headers, rows, hdr_idx = _read_csv(path, encoding, spec)
        sheet = ''
        if encoding not in ('utf-8', 'utf-8-sig'):
            warnings.append(f'检测到编码 {encoding}，已按此解码')
    else:
        raise ValueError(
            f'不支持的文件类型: {suffix}。'
            f'支持 .xlsx / .xls / .csv')

    # 清理列名空白
    headers = [str(h).strip() if h is not None else '' for h in headers]

    return ReadResult(
        headers=headers,
        rows=rows,
        sheet_name=sheet,
        header_row_index=hdr_idx,
        encoding=encoding,
        warnings=warnings,
    )


def _pick_header_row(
    raw_rows: list[list[Any]], candidates: tuple[int, ...],
    spec: SourceSpec | None,
) -> int:
    """在候选行中选出表头行。

    打分策略（不依赖 spec 也要能工作 —— 因为识别发生在读取之后）：
      1. 列数（非空单元格数）—— 表头行的列数通常接近文件的最大列数
      2. 该行是否"像表头"：单元格多为文本、无数字为主的单元格
      3. 若给了 spec，叠加"命中已知列名"的加分

    这样即使调用方未传 spec，也能正确跳过"导出时间：xxx"这类前置行。
    """
    if not raw_rows:
        return 0

    n_rows = len(raw_rows)
    # 候选行：给定的候选 + 兜底扫描前 8 行（真实导出前置行很少超过 5 行）
    cand = set(i for i in (candidates or (0,)) if i < n_rows)
    cand |= set(range(min(n_rows, 8)))
    idxs = sorted(cand)

    # 文件的最大列数（取前 50 行的众数上限）
    max_cols = max((len(r) for r in raw_rows[:50]), default=1)

    known: set[str] = set()
    if spec is not None:
        known = {normalize_column(a) for r in spec.rules for a in r.aliases}

    def score_row(i: int) -> tuple[float, int]:
        cells = raw_rows[i]
        non_empty = [c for c in cells if c is not None and str(c).strip()]
        n = len(non_empty)
        if n == 0:
            return (-1.0, 0)

        s = 0.0
        # ① 列数占比（权重最高）
        s += 3.0 * (n / max_cols)

        # ② "像表头"：文本占比高、纯数字单元格少
        text_like = 0
        for c in non_empty:
            t = str(c).strip()
            if re.fullmatch(r'[-+]?[\d,.\s%¥￥$]*', t):
                continue          # 纯数字/金额 → 像数据行
            text_like += 1
        s += 2.0 * (text_like / n)

        # ③ 命中已知列名（有 spec 时）
        if known:
            normd = {normalize_column(c) for c in non_empty}
            s += 2.0 * (len(normd & known) / max(len(known), 1))

        return (s, n)

    best_i = idxs[0]
    best_key = (-2.0, -1)
    for i in idxs:
        key = score_row(i)
        if key > best_key:
            best_i, best_key = i, key
    return best_i


def _read_excel(path: Path, spec: SourceSpec | None,
                sheet_name: str | None = None):
    """读 Excel。返回 (headers, rows, sheet_name, header_row_index)。

    sheet_name: 指定工作表；None 时按 required_sheets / 第一张表选择。
    """
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise ImportError(
            'openpyxl 未安装。请执行: pip install openpyxl') from e

    wb = load_workbook(str(path), read_only=True, data_only=True)

    if sheet_name:
        if sheet_name not in wb.sheetnames:
            wb.close()
            raise ValueError(f'工作表不存在: {sheet_name!r}（可用: {wb.sheetnames}）')
    else:
        sheet_name = wb.sheetnames[0]
        if spec and spec.required_sheets:
            for name in spec.required_sheets:
                if name in wb.sheetnames:
                    sheet_name = name
                    break

    ws = wb[sheet_name]
    raw: list[list[Any]] = []
    for row in ws.iter_rows(values_only=True):
        raw.append(list(row))
        if len(raw) > 200_000:      # 安全上限
            break
    wb.close()

    if not raw:
        return [], [], sheet_name, 0

    hdr = _pick_header_row(raw, spec.header_row_candidates if spec else (0,), spec)
    headers = raw[hdr]
    body = [r for r in raw[hdr + 1:] if any(c is not None and str(c).strip() for c in r)]
    return headers, body, sheet_name, hdr


def _read_csv(path: Path, encoding: str, spec: SourceSpec | None):
    """读 CSV。返回 (headers, rows, header_row_index)。"""
    text = path.read_text(encoding=encoding, errors='replace')
    # 自动判断分隔符
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=',\t;|')
        delim = dialect.delimiter
    except csv.Error:
        delim = ','

    reader = csv.reader(io.StringIO(text), delimiter=delim)
    raw = []
    for row in reader:
        raw.append(row)
        if len(raw) > 200_000:
            break

    if not raw:
        return [], [], 0

    hdr = _pick_header_row(raw, spec.header_row_candidates if spec else (0,), spec)
    headers = raw[hdr]
    body = [r for r in raw[hdr + 1:]
            if any(c is not None and str(c).strip() for c in r)]
    return headers, body, hdr


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------

def sniff(headers: Iterable[str]) -> SniffResult:
    """按列名识别文件属于哪种数据源。

    打分 = 命中字段数 / 该规格字段数，并对必填字段加权。
    """
    norm = {normalize_column(h) for h in headers if h}
    if not norm:
        return SniffResult(spec=None, score=0.0)

    scored: list[tuple[str, float, list[str]]] = []
    for key, spec in SOURCE_SPECS.items():
        matched: list[str] = []
        score = 0.0
        for rule in spec.rules:
            for alias in rule.aliases:
                if normalize_column(alias) in norm:
                    matched.append(rule.target)
                    # 必填字段权重更高
                    score += 2.0 if rule.required else 1.0
                    break
        max_score = sum(2.0 if r.required else 1.0 for r in spec.rules)
        ratio = score / max_score if max_score else 0.0
        scored.append((key, ratio, matched))

    scored.sort(key=lambda x: -x[1])
    top_key, top_ratio, top_matched = scored[0]

    return SniffResult(
        spec=SOURCE_SPECS[top_key] if top_ratio > 0 else None,
        score=top_ratio,
        matched_columns=top_matched,
        candidates=[(k, round(r, 3)) for k, r, _ in scored if r > 0],
    )


# ---------------------------------------------------------------------------
# 字段映射建议
# ---------------------------------------------------------------------------

def suggest(headers: list[str], spec: SourceSpec) -> list[FieldMapping]:
    """为每个标准字段生成映射建议。

    匹配优先级：
      1. 归一化后完全相等 → 置信度 1.0
      2. 归一化后一个是另一个的子串 → 置信度 0.75
      3. 无匹配 → 不输出该映射（由 validate 报缺失）
    """
    norm_to_original: dict[str, str] = {}
    for h in headers:
        n = normalize_column(h)
        if n and n not in norm_to_original:
            norm_to_original[n] = h

    result: list[FieldMapping] = []
    used: set[str] = set()

    for rule in spec.rules:
        hit: str | None = None
        conf = 0.0
        reason = ''

        # 1. 精确
        for alias in rule.aliases:
            na = normalize_column(alias)
            if na in norm_to_original and norm_to_original[na] not in used:
                hit = norm_to_original[na]
                conf = rule.confidence
                reason = '列名完全匹配'
                break

        # 2. 子串
        if hit is None:
            for alias in rule.aliases:
                na = normalize_column(alias)
                for n, orig in norm_to_original.items():
                    if orig in used or len(na) < 3:
                        continue
                    if na in n or n in na:
                        hit = orig
                        conf = min(rule.confidence, 0.75) * 0.85
                        reason = f'列名部分匹配（与「{alias}」相近）'
                        break
                if hit:
                    break

        if hit is None:
            continue

        used.add(hit)
        result.append(FieldMapping(
            source_column=hit,
            target_field=rule.target,
            confidence=round(conf, 3),
            reason=reason + (f'；{rule.note}' if rule.note else ''),
            confirmed=False,
        ))

    return result


def missing_required(mappings: list[FieldMapping], spec: SourceSpec) -> list[str]:
    """返回缺失的必填字段。"""
    mapped = {m.target_field for m in mappings}
    return [r.target for r in spec.rules if r.required and r.target not in mapped]


def low_confidence(mappings: list[FieldMapping],
                   threshold: float = 0.8) -> list[FieldMapping]:
    """返回需要人工确认的映射。"""
    return [m for m in mappings if m.confidence < threshold]


# ---------------------------------------------------------------------------
# 应用映射（生成标准行）
# ---------------------------------------------------------------------------

def apply_mapping(
    read_result: ReadResult,
    mappings: list[FieldMapping],
    spec: SourceSpec,
) -> tuple[list[dict[str, Any]], list[AdapterError]]:
    """按映射把原始行转成标准 STG 行。

    返回 (标准行列表, 错误列表)。不静默丢弃任何行 ——
    解析失败的行会被记录为 AdapterError。
    """
    header_index = {h: i for i, h in enumerate(read_result.headers)}
    col_of: dict[str, int] = {}
    for m in mappings:
        idx = header_index.get(m.source_column)
        if idx is not None:
            col_of[m.target_field] = idx

    rows: list[dict[str, Any]] = []
    errors: list[AdapterError] = []

    for ri, raw in enumerate(read_result.rows):
        record: dict[str, Any] = {}
        for target, idx in col_of.items():
            value = raw[idx] if idx < len(raw) else None
            record[target] = _coerce(target, value)
        # 空行跳过（不算错误）
        if all(v in (None, '') for v in record.values()):
            continue
        rows.append(record)

    # 必填字段缺失检查（整体，不是逐行）
    for field_name in missing_required(mappings, spec):
        errors.append(AdapterError(
            severity='ERROR',
            code='MISSING_REQUIRED_FIELD',
            message=f'必填字段「{field_name}」未能从源文件中匹配到',
            column=field_name,
        ))

    return rows, errors


# ---------------------------------------------------------------------------
# 字段类型表（由 semantic/schema.sql 定义）
# ---------------------------------------------------------------------------
# 用「类型驱动」而非「字段名猜测」做转换。
# 名称匹配太脆弱：例如 seller_income 不含任何数值提示词，
# 会被漏掉，导致 '6,000.00' 原样进库 → DuckDB 转 DOUBLE 失败。
FIELD_TYPES: dict[str, dict[str, str]] = {
    'stg_sycm_traffic': {
        'stat_date': 'date', 'stat_scope': 'text', 'item_id': 'text',
        'item_title': 'text', 'visitors': 'int', 'page_views': 'int',
        'pay_buyers': 'int', 'pay_items': 'int', 'pay_amount': 'float',
        'order_buyers': 'int', 'order_items': 'int', 'order_amount': 'float',
        'bounce_rate': 'float', 'stay_time': 'float',
        'source_channel': 'text', 'cart_buyers': 'int',
        'favorite_buyers': 'int',
    },
    'stg_trade_orders': {
        'order_id': 'text', 'sub_order_id': 'text',
        'create_time': 'datetime', 'pay_time': 'datetime',
        'ship_time': 'datetime', 'confirm_time': 'datetime',
        'order_status': 'text', 'refund_status': 'text',
        'buyer_nick': 'text', 'item_id': 'text', 'item_title': 'text',
        'merchant_code': 'text', 'sku_id': 'text', 'sku_props': 'text',
        'price': 'float', 'quantity': 'int', 'pay_amount': 'float',
        'goods_amount': 'float', 'shipping_fee': 'float',
        'discount': 'float', 'seller_income': 'float',
        'buyer_message': 'text', 'seller_memo': 'text',
        'receiver_province': 'text',
    },
    'stg_refunds': {
        'refund_id': 'text', 'order_id': 'text', 'sub_order_id': 'text',
        'apply_time': 'datetime', 'refund_success_time': 'datetime',
        'refund_status': 'text', 'refund_type': 'text', 'item_id': 'text',
        'merchant_code': 'text', 'quantity': 'int', 'refund_amount': 'float',
        'refund_reason': 'text', 'refund_stage': 'text',
    },
    'stg_account_flow': {
        'flow_id': 'text', 'posted_date': 'date', 'trade_time': 'datetime',
        'order_id': 'text', 'biz_type': 'text', 'income_expense': 'text',
        'amount': 'float', 'balance': 'float', 'remark': 'text',
        'counterparty': 'text',
    },
    'stg_ad_reports': {
        'stat_date': 'date', 'channel': 'text', 'campaign_id': 'text',
        'campaign_name': 'text', 'item_id': 'text', 'impressions': 'int',
        'clicks': 'int', 'ctr': 'float', 'spend': 'float', 'cpc': 'float',
        'attributed_orders': 'int', 'attributed_items': 'int',
        'attributed_amount': 'float', 'attributed_gmv': 'float',
        'cart_adds': 'int', 'favorites': 'int',
    },
    'stg_purchase_batches': {
        'batch_id': 'text', 'merchant_code': 'text', 'item_id': 'text',
        'purchase_date': 'date', 'arrival_date': 'date', 'quantity': 'int',
        'unit_cost': 'float', 'freight_total': 'float',
        'other_cost': 'float', 'supplier': 'text', 'currency': 'text',
    },
    'stg_inventory': {
        'stat_date': 'date', 'item_id': 'text', 'merchant_code': 'text',
        'sku_id': 'text', 'sellable_qty': 'int', 'locked_qty': 'int',
        'inbound_qty': 'int',
    },
}


def field_type_of(table: str, field: str) -> str:
    """查字段类型；表或字段未知时返回 'text'（保守，不猜数值）。"""
    return FIELD_TYPES.get(table, {}).get(field, 'text')


def coerce_for_table(table: str, field: str, value: Any) -> Any:
    """按目标表的字段类型做转换（类型驱动，不靠名字猜）。"""
    return _coerce_typed(field_type_of(table, field), value)


def _coerce_typed(kind: str, value: Any) -> Any:
    """按声明的类型做归一化。"""
    if value is None:
        return None
    s = str(value).strip()
    if s == '' or s in ('-', '--', '—', '/', 'NULL', 'null', 'None'):
        return None

    if kind in ('date', 'datetime'):
        return _normalize_datetime(s)
    if kind in ('float', 'int'):
        v = _normalize_number(s)
        if isinstance(v, str):          # 解析失败 → 保留原文（可追溯）
            return None if kind == 'float' else None
        if kind == 'int' and isinstance(v, float):
            return int(round(v))
        return v
    return s


# 兼容保留：名称提示词（仅供未在 FIELD_TYPES 中登记的字段兜底）
_NUMERIC_HINTS = (
    'amount', 'price', 'fee', 'cost', 'discount', 'spend', 'ctr', 'cpc',
    'rate', 'qty', 'quantity', 'visitors', 'views', 'buyers', 'items',
    'impressions', 'clicks', 'orders', 'balance', 'income',
)


def _coerce(field_name: str, value: Any) -> Any:
    """按目标字段名做类型归一化（名称启发式，兜底用）。

    优先使用 coerce_for_table()（类型驱动）。
    此函数保留以兼容旧调用点与测试。
    """
    if value is None:
        return None
    s = str(value).strip()
    if s == '' or s in ('-', '--', '—', '/', 'NULL', 'null'):
        return None

    lower = field_name.lower()

    # 日期/时间
    if any(k in lower for k in ('date', 'time')):
        return _normalize_datetime(s)

    # 数值
    if any(k in lower for k in _NUMERIC_HINTS):
        return _normalize_number(s)

    return s


def _normalize_number(s: str) -> float | int | str:
    """去千分位、货币符号、百分号。"""
    original = s
    s = s.replace(',', '').replace('，', '')
    s = s.replace('¥', '').replace('￥', '').replace('$', '').strip()

    is_pct = s.endswith('%')
    if is_pct:
        s = s[:-1].strip()
    # 括号表示负数：(123.45)
    neg = s.startswith('(') and s.endswith(')')
    if neg:
        s = s[1:-1].strip()
    # 前导 +/-
    if s.startswith('+'):
        s = s[1:]

    # 处理 "1.5万" 这类
    mult = 1.0
    for unit, m in (('万', 10_000), ('亿', 100_000_000), ('千', 1_000)):
        if s.endswith(unit):
            s = s[:-1].strip()
            mult = m
            break

    try:
        v = float(s) * mult
    except ValueError:
        return original   # 解析不了就原样返回，保留可追溯性

    if neg:
        v = -v
    if is_pct:
        v = v / 100.0
    # 整数保持 int
    return int(v) if v.is_integer() and not is_pct else v


_DT_PATTERNS = (
    re.compile(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?'),
    re.compile(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$'),
    re.compile(r'^(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?'),
)


def _normalize_datetime(s: str) -> str | None:
    """归一化日期/时间字符串为 ISO 格式。

    支持：2026-10-01 / 2026/10/01 / 2026.10.01 / 2026年10月1日
         2026-10-01 10:30:00
    """
    for pat in _DT_PATTERNS:
        m = pat.match(s.strip())
        if not m:
            continue
        g = m.groups()
        y, mo, d = int(g[0]), int(g[1]), int(g[2])
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None
        hh = int(g[3]) if len(g) > 3 and g[3] else 0
        mi = int(g[4]) if len(g) > 4 and g[4] else 0
        ss = int(g[5]) if len(g) > 5 and g[5] else 0
        if hh or mi or ss:
            return f'{y:04d}-{mo:02d}-{d:02d} {hh:02d}:{mi:02d}:{ss:02d}'
        return f'{y:04d}-{mo:02d}-{d:02d}'
    return None


# ---------------------------------------------------------------------------
# 文件指纹
# ---------------------------------------------------------------------------

def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    """内容哈希（用于去重）。"""
    h = hashlib.sha256()
    with path.open('rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def schema_fingerprint(headers: Iterable[str]) -> str:
    """列集合指纹（用于字段映射复用 —— 同结构跳过二次确认）。"""
    norm = sorted({normalize_column(h) for h in headers if h})
    return hashlib.sha256('|'.join(norm).encode('utf-8')).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 推荐入口：两步式（先识别，再按规格重读）
# ---------------------------------------------------------------------------

@dataclass
class PreparedSource:
    """一次完整的「读取 + 识别」结果。"""
    path: Path
    read: ReadResult
    sniff: SniffResult
    spec: SourceSpec | None
    mappings: list[FieldMapping] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    low_confidence: list[FieldMapping] = field(default_factory=list)
    schema_fingerprint: str = ''
    file_sha256: str = ''

    @property
    def ok(self) -> bool:
        return self.spec is not None and not self.missing_required

    def needs_confirmation(self) -> bool:
        """是否需要人工确认映射。"""
        if self.spec is None:
            return True
        if self.sniff.needs_user_choice:
            return True
        return bool(self.missing_required or self.low_confidence)


def prepare(path: Path, sheet_name: str | None = None) -> PreparedSource:
    """一步完成：读文件 → 识别类型 → 生成映射建议。

    sheet_name: 指定 Excel 工作表（None = 自动）。
    内部做两步读取：第一次用通用表头探测读，识别出类型后，
    第二次用该类型的 header_row_candidates 重读以获得最准的表头。
    """
    # 第一步：通用读取（不传 spec，靠内容特征找表头）
    first = read_file(path, sheet_name=sheet_name)
    sn = sniff(first.headers)

    read = first
    if sn.spec is not None:
        # 第二步：按识别出的规格重读，表头定位更准
        reread = read_file(path, sn.spec, sheet_name=sheet_name)
        if reread.headers and len(reread.headers) >= len(first.headers):
            read = reread

    mappings: list[FieldMapping] = []
    missing: list[str] = []
    low: list[FieldMapping] = []
    if sn.spec is not None:
        mappings = suggest(read.headers, sn.spec)
        missing = missing_required(mappings, sn.spec)
        low = low_confidence(mappings)

    return PreparedSource(
        path=path,
        read=read,
        sniff=sn,
        spec=sn.spec,
        mappings=mappings,
        missing_required=missing,
        low_confidence=low,
        schema_fingerprint=schema_fingerprint(read.headers),
        file_sha256=file_sha256(path),
    )


def _prepared_from_read(path: Path, read: ReadResult,
                        sn: SniffResult, sha: str) -> PreparedSource:
    """从已读好的 ReadResult 构造 PreparedSource（prepare_all 内部用）。"""
    mappings: list[FieldMapping] = []
    missing: list[str] = []
    low: list[FieldMapping] = []
    if sn.spec is not None:
        mappings = suggest(read.headers, sn.spec)
        missing = missing_required(mappings, sn.spec)
        low = low_confidence(mappings)

    return PreparedSource(
        path=path, read=read, sniff=sn, spec=sn.spec,
        mappings=mappings, missing_required=missing, low_confidence=low,
        schema_fingerprint=schema_fingerprint(read.headers),
        file_sha256=sha,
    )


def prepare_all(path: Path) -> list[PreparedSource]:
    """识别 Excel 的**所有工作表**，每个能识别的表产出一个 PreparedSource。

    用途：卖家常把多张导出报表放在一个 Excel 里（或用我们的样例数据），
    上传一个文件即可拆分为多个数据源 —— 而不是逼用户导出 6 个文件。

    规则：
      - CSV → 退化为 [prepare(path)]（识别不出则返回空列表）
      - Excel → 逐表嗅探；置信度 >= 0.5 的才收录；"说明"类表自动跳过
    """
    suffix = path.suffix.lower()
    sha = file_sha256(path)

    if suffix not in ('.xlsx', '.xlsm', '.xls'):
        p = prepare(path)
        return [p] if p.spec is not None else []

    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise ImportError(
            'openpyxl 未安装。请执行: pip install openpyxl') from e

    results: list[PreparedSource] = []
    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        for name in wb.sheetnames:
            raw: list[list[Any]] = []
            for row in wb[name].iter_rows(values_only=True):
                raw.append(list(row))
                if len(raw) > 200_000:
                    break
            if not raw:
                continue

            # 嗅探该表（用通用表头定位）
            hdr = _pick_header_row(raw, (0,), None)
            headers = [str(h).strip() if h is not None else ''
                       for h in raw[hdr]]
            sn = sniff(headers)
            if not sn.confident:
                continue          # 说明页 / 无法识别 → 跳过

            # 用规格重定位表头，更准
            hdr2 = _pick_header_row(
                raw, sn.spec.header_row_candidates, sn.spec)
            headers2 = [str(h).strip() if h is not None else ''
                        for h in raw[hdr2]]
            body = [r for r in raw[hdr2 + 1:]
                    if any(c is not None and str(c).strip() for c in r)]

            read = ReadResult(
                headers=headers2, rows=body, sheet_name=name,
                header_row_index=hdr2, encoding='',
                warnings=[],
            )
            results.append(_prepared_from_read(path, read, sn, sha))
    finally:
        wb.close()

    return results
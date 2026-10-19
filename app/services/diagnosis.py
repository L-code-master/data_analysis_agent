"""诊断服务 —— 把上传的数据变成一份可交付的诊断报告。

流程（对应设计文档的运行时数据流）：
    1. 上传        save_upload → sources 表
    2. 画像        adapter.prepare → 列结构 + 映射建议（不载入数据到内存）
    3. 确认映射    人工确认 → field_mappings 表（驱动"第二次免确认"）
    4. 执行诊断    导入 STG → ETL → 质量门 → 渲染 SQL → 计算结果
    5. 产出报告    HTML 报告 + 口径声明

关键设计：
  - 数据不进上下文：画像只返回列结构，不返回数据行
  - 口径不猜：歧义抛 CaliberConflict
  - 质量门不过不出报告
  - 每个数字可溯源（computed_registry）
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.adapters import taobao
from app.adapters.taobao_fields import get_spec
from app.config import get_settings
from app.services import billing, tenant as tn
from app.semantic.models import CaliberConflict
from app.semantic.registry import SemanticRegistry
from app.semantic.renderer import SQLRenderer, RenderError
from app.semantic.timerange import DateRange, parse_range
from app.storage import db


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


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _rg(row, name: str, idx: int):
    return row[name] if hasattr(row, 'keys') else row[idx]


# ===========================================================================
# 数据结构
# ===========================================================================

@dataclass
class SourceRecord:
    id: str
    tenant_id: str
    filename: str
    original_name: str
    size_bytes: int
    sha256: str
    platform: str
    source_type: str          # sycm_traffic / trade_orders / ...
    row_count: int
    profile_ref: str
    status: str
    created_at: str
    sheet_name: str = ''      # Excel 工作表名（多表拆分用；CSV 为 ''）

    def to_dict(self) -> dict:
        return {
            'source_id': self.id,
            'filename': self.original_name or self.filename,
            'sheet_name': self.sheet_name,
            'size_bytes': self.size_bytes,
            'platform': self.platform,
            'source_type': self.source_type,
            'row_count': self.row_count,
            'status': self.status,
            'created_at': self.created_at,
        }


@dataclass
class ProfileResult:
    """数据画像 —— 只含结构，不含数据行。"""
    source_id: str
    source_type: str
    display_name: str
    row_count: int
    columns: list[str]
    mappings: list[dict]
    missing_required: list[str]
    needs_confirmation: bool
    schema_fingerprint: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'source_id': self.source_id,
            'source_type': self.source_type,
            'display_name': self.display_name,
            'row_count': self.row_count,
            'column_count': len(self.columns),
            'columns': self.columns,
            'mappings': self.mappings,
            'missing_required': self.missing_required,
            'needs_confirmation': self.needs_confirmation,
            'schema_fingerprint': self.schema_fingerprint,
            'warnings': self.warnings,
        }


@dataclass
class DiagnosisResult:
    """诊断结果。"""
    diagnosis_id: str
    tenant_id: str
    status: str
    metrics: list[dict] = field(default_factory=list)
    footnote: str = ''
    warnings: list[str] = field(default_factory=list)
    quality_passed: bool = False
    report_ref: str = ''
    error: str = ''
    duration_ms: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            'diagnosis_id': self.diagnosis_id,
            'status': self.status,
            'metrics': self.metrics,
            'footnote': self.footnote,
            'warnings': self.warnings,
            'quality_passed': self.quality_passed,
            'report_url': (f'/api/v1/diagnoses/{self.diagnosis_id}/report'
                           if self.report_ref else ''),
            'error': self.error,
            'duration_ms': self.duration_ms,
            'cost_usd': round(self.cost_usd, 4),
        }


# ===========================================================================
# 1. 上传
# ===========================================================================

def register_source(tenant_id: str, user_id: str, original_name: str,
                    content: bytes) -> list[SourceRecord]:
    """保存上传文件并登记为数据源。

    ★ 支持多工作表 Excel：一个含 6 张报表的 xlsx 会被自动拆分为
      6 个数据源（卖家常把多张导出放在一个工作簿里；样例数据也是这种形态）。
      "说明"之类无法识别的表会被自动跳过。

    返回创建/更新的数据源列表（CSV 恒为 1 个；多表 Excel 为 N 个）。
    """
    ok, reason = tn.validate_upload(tenant_id, original_name, len(content))
    if not ok:
        raise DiagnosisError(reason, 'UPLOAD_REJECTED')

    path, digest = tn.save_upload(tenant_id, original_name, content)

    # 识别（多表逐个嗅探）
    try:
        prepared_list = taobao.prepare_all(path)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise DiagnosisError(f'文件解析失败: {e}', 'PARSE_FAILED') from e

    if not prepared_list:
        path.unlink(missing_ok=True)
        raise SourceTypeUnknownError(
            '请确认这是千牛导出的报表（生意参谋/订单/退款/账房/广告/成本）。'
            '提示：也可以先下载一份样例数据试试。')

    records: list[SourceRecord] = []
    now = _now()
    safe_name = tn.safe_filename(original_name)

    for prepared in prepared_list:
        sheet = prepared.read.sheet_name or ''
        spec = prepared.spec

        # 画像存为产物
        profile_blob = json.dumps({
            'source_type': spec.key,
            'display_name': spec.display_name,
            'sheet_name': sheet,
            'columns': prepared.read.headers,
            'row_count': prepared.read.row_count,
            'header_row_index': prepared.read.header_row_index,
            'encoding': prepared.read.encoding,
            'mappings': [m.to_dict() for m in prepared.mappings],
            'missing_required': prepared.missing_required,
            'schema_fingerprint': prepared.schema_fingerprint,
        }, ensure_ascii=False).encode('utf-8')
        _aid, _apath = tn.put_artifact(tenant_id, 'profile', profile_blob)

        sid = uuid.uuid4().hex
        with db.transaction() as conn:
            # 同租户内按 (内容, 工作表) 去重
            dup = conn.execute(
                'SELECT id FROM sources WHERE tenant_id = ? AND sha256 = ? '
                "AND sheet_name = COALESCE(?, '')",
                (tenant_id, digest, sheet)).fetchone()
            if dup:
                sid = _rg(dup, 'id', 0)
                conn.execute(
                    'UPDATE sources SET row_count = ?, profile_ref = ?, '
                    'status = ? WHERE id = ?',
                    (prepared.read.row_count, _aid, 'profiled', sid))
            else:
                conn.execute(
                    'INSERT INTO sources (id,tenant_id,uploaded_by,filename,'
                    'original_name,sheet_name,size_bytes,sha256,platform,'
                    'row_count,profile_ref,status,created_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (sid, tenant_id, user_id, path.name, safe_name, sheet,
                     len(content), digest, 'taobao',
                     prepared.read.row_count, _aid, 'profiled', now))

        records.append(SourceRecord(
            id=sid, tenant_id=tenant_id, filename=path.name,
            original_name=safe_name, sheet_name=sheet,
            size_bytes=len(content), sha256=digest, platform='taobao',
            source_type=spec.key, row_count=prepared.read.row_count,
            profile_ref=_aid, status='profiled', created_at=now))

    return records


def get_source(tenant_id: str, source_id: str) -> SourceRecord:
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT id,tenant_id,filename,original_name,sheet_name,size_bytes,'
            'sha256,platform,row_count,profile_ref,status,created_at '
            'FROM sources WHERE id = ? AND tenant_id = ?',
            (source_id, tenant_id)).fetchone()
    if row is None:
        raise SourceNotFoundError(source_id)

    # source_type 从画像产物读
    stype = ''
    try:
        ap = tn.get_artifact_path(tenant_id, 'profile', _rg(row, 'profile_ref', 10))
        stype = json.loads(ap.read_text(encoding='utf-8')).get('source_type', '')
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return SourceRecord(
        id=_rg(row, 'id', 0), tenant_id=_rg(row, 'tenant_id', 1),
        filename=_rg(row, 'filename', 2), original_name=_rg(row, 'original_name', 3) or '',
        sheet_name=_rg(row, 'sheet_name', 4) or '',
        size_bytes=_rg(row, 'size_bytes', 5), sha256=_rg(row, 'sha256', 6),
        platform=_rg(row, 'platform', 7) or 'taobao', source_type=stype,
        row_count=_rg(row, 'row_count', 8) or 0,
        profile_ref=_rg(row, 'profile_ref', 9) or '',
        status=_rg(row, 'status', 10), created_at=_rg(row, 'created_at', 11))


# ===========================================================================
# 2. 画像
# ===========================================================================

def profile_source(tenant_id: str, source_id: str) -> ProfileResult:
    """生成数据画像（只含结构，不含数据行）。"""
    src = get_source(tenant_id, source_id)
    spec = get_spec(src.source_type)

    p = tn.tenant_paths(tenant_id)
    fpath = p.inbox / src.filename
    tn.assert_inside_tenant(tenant_id, fpath)

    if not fpath.exists():
        raise SourceNotFoundError(source_id)

    prepared = taobao.prepare(fpath, sheet_name=src.sheet_name or None)

    # 查是否已有确认过的映射（同结构直接复用）
    prior = _load_confirmed_mapping(tenant_id, prepared.schema_fingerprint)
    mappings = [m.to_dict() for m in prepared.mappings]
    if prior:
        for m in mappings:
            if m['target_field'] in prior:
                m['confirmed'] = True
                m['needs_review'] = False
                m['source_column'] = prior[m['target_field']]

    return ProfileResult(
        source_id=source_id,
        source_type=spec.key,
        display_name=spec.display_name,
        row_count=prepared.read.row_count,
        columns=[str(c) for c in prepared.read.headers],
        mappings=mappings,
        missing_required=prepared.missing_required,
        needs_confirmation=bool(prepared.missing_required or
                                any(m['needs_review'] for m in mappings)),
        schema_fingerprint=prepared.schema_fingerprint,
        warnings=list(prepared.read.warnings) + list(spec.notes),
    )


def _load_confirmed_mapping(tenant_id: str, fingerprint: str) -> dict[str, str]:
    """取该租户对同结构文件的已确认映射。"""
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT mapping FROM field_mappings WHERE tenant_id = ? '
            'AND source_schema = ?', (tenant_id, fingerprint)).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(_rg(row, 'mapping', 0))
    except (json.JSONDecodeError, TypeError):
        return {}


# ===========================================================================
# 3. 确认映射
# ===========================================================================

def confirm_mapping(tenant_id: str, user_id: str, source_id: str,
                    mapping: dict[str, str],
                    confidence: dict[str, float] | None = None) -> dict:
    """确认字段映射。

    mapping: {目标字段: 源列名}

    确认后写入 field_mappings（按 schema hash 复用），
    下次同结构文件自动应用，无需二次确认。
    """
    src = get_source(tenant_id, source_id)
    spec = get_spec(src.source_type)

    target_fields = {r.target for r in spec.rules}
    unknown = set(mapping) - target_fields
    if unknown:
        raise DiagnosisError(f'未知的目标字段: {sorted(unknown)}', 'BAD_MAPPING')

    required = {r.target for r in spec.rules if r.required}
    missing = required - set(mapping)
    if missing:
        raise DiagnosisError(
            f'缺少必填字段映射: {sorted(missing)}。'
            f'请确认这些字段对应源文件中的哪一列。', 'MISSING_REQUIRED_MAPPING')

    prof = profile_source(tenant_id, source_id)
    now = _now()

    with db.transaction() as conn:
        conn.execute(
            'INSERT INTO field_mappings (id,tenant_id,source_schema,platform,'
            'mapping,confidence,confirmed_by,created_at,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(tenant_id, source_schema) DO UPDATE SET '
            'mapping = excluded.mapping, confidence = excluded.confidence, '
            'confirmed_by = excluded.confirmed_by, updated_at = excluded.updated_at',
            (uuid.uuid4().hex, tenant_id, prof.schema_fingerprint, 'taobao',
             json.dumps(mapping, ensure_ascii=False),
             json.dumps(confidence or {}, ensure_ascii=False),
             user_id, now, now))

    return {
        'source_id': source_id,
        'confirmed_fields': len(mapping),
        'schema_fingerprint': prof.schema_fingerprint,
        'message': f'已确认 {len(mapping)} 个字段映射，下次同结构文件将自动应用',
    }


# ===========================================================================
# 4. 执行诊断
# ===========================================================================

def _open_semantic_db(tenant_id: str):
    """打开（并在首次时初始化）该租户的 DuckDB 语义层库。"""
    import duckdb

    p = tn.ensure_tenant_dirs(tenant_id)
    con = duckdb.connect(str(p.semantic_db))

    # 首次：应用 schema
    has_table = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'sem_metric_registry'").fetchone()[0]
    if not has_table:
        schema_file = get_settings().semantic_schema_file
        con.execute(schema_file.read_text(encoding='utf-8'))
    return con


# ---------------------------------------------------------------------------
# 语义层升级（存量租户补丁，幂等）
# ---------------------------------------------------------------------------
# 背景：视图段（S6）与维表种子只在**建库时**应用；schema 后续修订时，
# 存量租户的 DuckDB 不会自动更新。每条修订在这里以幂等方式重放：
#   U1  dim_refund_reason 种子 + v_refund_daily 最长前缀匹配（v1.2 归因）
_SEMANTIC_UPGRADES = [
    # U1a：退款原因种子（幂等）
    """INSERT INTO dim_refund_reason
       SELECT s.reason_text, s.reason_category FROM (
         VALUES ('质量问题','QUALITY'),('假冒','QUALITY'),
                ('尺寸不符','SIZE'),('尺码','SIZE'),('大小不符','SIZE'),
                ('物流','LOGISTICS'),('未收到货','LOGISTICS'),
                ('描述不符','DESCRIPTION'),('与描述不符','DESCRIPTION'),
                ('图片不符','DESCRIPTION')
       ) AS s(reason_text, reason_category)
       WHERE NOT EXISTS (
         SELECT 1 FROM dim_refund_reason r WHERE r.reason_text = s.reason_text)""",
    # U1b：退款原因匹配改为最长前缀（'质量问题-起球' → QUALITY）
    """CREATE OR REPLACE VIEW v_refund_daily AS
       SELECT apply_date AS stat_date, item_id, merchant_code,
              COALESCE((SELECT r.reason_category FROM dim_refund_reason r
                        WHERE f.refund_reason LIKE r.reason_text || '%'
                        ORDER BY LENGTH(r.reason_text) DESC LIMIT 1),
                       'OTHER') AS reason_category,
              COUNT(*) AS refund_count, SUM(quantity) AS refund_qty,
              SUM(refund_amount) AS refund_amount
       FROM dwd_refund_line f
       WHERE refund_status = '退款成功'
       GROUP BY 1, 2, 3, 4""",
]


def _apply_semantic_upgrades(con) -> None:
    """对存量租户库重放语义层修订（幂等，失败不阻断诊断）。"""
    for sql in _SEMANTIC_UPGRADES:
        try:
            con.execute(sql)
        except Exception:                   # noqa: BLE001
            pass


def _import_stg(con, prepared, mapping: dict[str, str], table: str) -> int:
    """把源文件数据导入标准 STG 表。"""
    spec = get_spec(prepared.spec.key)
    read = prepared.read

    # 用「源列名 → 目标字段」的反向映射做投影
    col_of: dict[str, int] = {}
    header_idx = {h: i for i, h in enumerate(read.headers)}
    for target, src_col in mapping.items():
        idx = header_idx.get(src_col)
        if idx is not None:
            col_of[target] = idx

    if not col_of:
        return 0

    con.execute(f'DELETE FROM {table}')

    targets = sorted(col_of)
    placeholders = ','.join(['?'] * len(targets))
    cols = ','.join(targets)
    sql = f'INSERT INTO {table} ({cols}) VALUES ({placeholders})'

    rows = []
    for raw in read.rows:
        vals = []
        for t in targets:
            v = raw[col_of[t]] if col_of[t] < len(raw) else None
            # 类型驱动转换（按目标表的列类型，不靠字段名猜）
            vals.append(taobao.coerce_for_table(table, t, v))
        if all(x is None for x in vals):
            continue
        rows.append(vals)

    if rows:
        con.executemany(sql, rows)
    return len(rows)


def run_diagnosis(tenant_id: str, user_id: str,
                  source_ids: str | list[str],
                  time_range_text: str = '',
                  metric_ids: list[str] | None = None,
                  progress: Callable[[str, str], None] | None = None,
                  diagnosis_id: str = ''
                  ) -> DiagnosisResult:
    """执行一次完整诊断。

    Args:
        source_ids: **一组**数据源 ID。一次完整诊断通常需要多个文件：
                    订单（必需）+ 流量 + 退款 + 账房 + 广告。
                    传单个字符串也支持（向后兼容）。
        time_range_text: 时间窗，如 '本月' / '2026-10-01~2026-10-03'
        metric_ids: 要计算的指标；默认算 P0 全集
        diagnosis_id: 预生成的诊断 ID。调用方（如 SSE 路由）需要提前知道
                      ID 才能把进度事件绑定到正确的流上，因此允许外部传入。

    步骤：扣额度 → 逐个导入 → ETL → 质量门 → 计算指标 → 生成报告
    任一步失败都会退还额度。
    """
    s = get_settings()
    t0 = dt.datetime.now()
    did = diagnosis_id or uuid.uuid4().hex

    # 统一成列表
    if isinstance(source_ids, str):
        ids = [source_ids]
    else:
        ids = [x for x in (source_ids or []) if x]
    if not ids:
        raise DiagnosisError('至少需要一个数据源', 'NO_SOURCE')

    def emit(phase: str, msg: str = '') -> None:
        if progress:
            progress(phase, msg)

    # ---- 0. 前置检查 ----
    emit('checking', '检查额度与映射')
    from app.services import auth as _auth_svc          # 局部导入防循环
    is_admin = False
    try:
        actor = _auth_svc.get_user(user_id)
        is_admin = bool(actor and actor.is_admin)
    except Exception:                                   # noqa: BLE001
        pass

    if not is_admin:
        acc = billing.get_account(tenant_id)
        if not acc.can_diagnose:
            raise billing.InsufficientCreditsError(acc.balance)

    ok, reason = billing.check_cost_quota(tenant_id)
    if not ok:
        raise DiagnosisError(reason, 'COST_QUOTA_EXCEEDED')

    # 校验全部数据源存在且属于该租户
    srcs = [get_source(tenant_id, sid) for sid in ids]
    primary = srcs[0]

    # ---- 1. 建诊断记录（先落库，便于前端轮询）----
    with db.transaction() as conn:
        conn.execute(
            'INSERT INTO diagnoses (id,tenant_id,created_by,source_id,status,'
            'progress_phase,created_at,started_at) VALUES (?,?,?,?,?,?,?,?)',
            (did, tenant_id, user_id, primary.id, 'running', 'checking',
             _now(), _now()))

    # ---- 2. 扣额度（admin 免扣但留审计痕；失败则退还）----
    try:
        if is_admin:
            billing.record_admin_usage(tenant_id, did)
            _audit_admin_free(tenant_id, user_id, did)
        else:
            billing.consume_credit(tenant_id, did, note='诊断消耗')
    except billing.InsufficientCreditsError as e:
        _fail_diagnosis(did, str(e))
        raise

    con = None
    try:
        # ---- 3. 打开语义层库 ----
        emit('loading', '加载语义层')
        con = _open_semantic_db(tenant_id)
        _apply_semantic_upgrades(con)

        # ---- 4. 逐个导入（★ 多文件）----
        p = tn.tenant_paths(tenant_id)
        imported: list[tuple[str, int]] = []

        for src in srcs:
            spec = get_spec(src.source_type)
            emit('importing',
                 f'导入 {src.original_name or src.filename}'
                 + (f' / {src.sheet_name}' if src.sheet_name else ''))
            fpath = p.inbox / src.filename
            tn.assert_inside_tenant(tenant_id, fpath)
            if not fpath.exists():
                raise SourceNotFoundError(src.id)

            prepared = taobao.prepare(fpath, sheet_name=src.sheet_name or None)
            confirmed = _load_confirmed_mapping(
                tenant_id, prepared.schema_fingerprint)
            mapping = confirmed or {m.target_field: m.source_column
                                    for m in prepared.mappings}

            for target in spec.required_fields:
                if target not in mapping:
                    raise MappingNotConfirmedError()

            n = _import_stg(con, prepared, mapping, spec.target_table)
            imported.append((spec.key, n))
            emit('importing', f'{spec.display_name}: {n} 行')

        emit('importing', '导入完成: ' + ', '.join(
            f'{k}={v}' for k, v in imported))

        # ---- 5. ETL ----
        emit('etl', '执行 ETL')
        schema_sql = s.semantic_schema_file.read_text(encoding='utf-8')
        i5 = schema_sql.index('-- ===== S5 ETL')
        i6 = schema_sql.index('-- ===== S6 指标视图')
        con.execute(schema_sql[i5:i6])

        # ---- 6. 质量门 ----
        emit('quality', '数据质量校验')
        i65 = schema_sql.index('-- ===== S6.5 数据质量校验')
        i7 = schema_sql.index('-- ===== S7 指标注册')
        con.execute(schema_sql[i65:i7])

        reg = SemanticRegistry(con)
        reg.load()
        passed, qmsg = reg.quality_passed('v1-full-refresh')
        if not passed:
            raise QualityGateError(qmsg)

        # ---- 7. 计算指标 ----
        emit('computing', '计算指标')
        tr: DateRange | None = None
        if time_range_text:
            tr = parse_range(time_range_text)

        renderer = SQLRenderer(reg)
        targets = metric_ids or ['M01', 'M03', 'M05', 'M06', 'M07',
                                 'M13', 'M14', 'M15', 'M17']

        metrics: list[dict] = []
        footnotes: list[str] = []
        warns: list[str] = []

        for mid in targets:
            m = reg.get(mid)
            if m is None:
                continue
            try:
                q = renderer.render(metric_id=mid, time_range=tr)
                row = con.execute(q.sql, q.params).fetchone()
                value = row[0] if row else None
                metrics.append({
                    'metric_id': mid,
                    'metric_name': m.metric_name,
                    'value': _serialize(value),
                    'unit': q.unit,
                    'formula': m.formula_expression,
                    'uncertainty': m.uncertainty.level.value,
                })
                if q.footnote:
                    footnotes.append(q.footnote)
                warns.extend(q.warnings)
            except (RenderError, CaliberConflict) as e:
                warns.append(f'{mid} 计算失败: {e}')

        emit('reporting', '生成报告')

        # ---- 8. 趋势与归因数据（供报告画图与定位问题）----
        trend = _build_trend(con, tr)
        products = _build_product_breakdown(con, tr)
        anomalies = _detect_anomaly_days(trend)
        weekly = _build_weekly_compare(con, tr)
        caliber_compare = _build_caliber_compare(metrics)
        caliber_snap = _build_caliber_snapshot(
            con, metrics, reg.disclaimer_for(targets))

        # ---- 9. 生成报告 ----
        report_blob = json.dumps({
            'diagnosis_id': did,
            'time_range': tr.to_dict() if tr else None,
            'source': {
                'name': primary.original_name or primary.filename,
                'row_count': sum(v for _k, v in imported),
                'files': [
                    {'name': (s.original_name or s.filename)
                            + (f' / {s.sheet_name}' if s.sheet_name else ''),
                     'type': s.source_type,
                     'rows': dict(imported).get(
                         get_spec(s.source_type).key, 0)}
                    for s in srcs
                ],
                'file_count': len(srcs),
            },
            'metrics': metrics,
            'trend': trend,
            'products': products,
            'anomaly_days': anomalies,
            'weekly': weekly,
            'caliber_compare': caliber_compare,
            'caliber': caliber_snap,
            'footnote': reg.disclaimer_for(targets),
            'warnings': sorted(set(warns)),
            'generated_at': _now(),
        }, ensure_ascii=False, indent=2).encode('utf-8')

        aid, _ap = tn.put_artifact(tenant_id, 'report', report_blob)

        duration = int((dt.datetime.now() - t0).total_seconds() * 1000)

        with db.transaction() as conn2:
            conn2.execute(
                'UPDATE diagnoses SET status = ?, progress_phase = ?, '
                'report_ref = ?, quality_passed = ?, finished_at = ? WHERE id = ?',
                ('succeeded', 'done', aid, 1, _now(), did))

        return DiagnosisResult(
            diagnosis_id=did, tenant_id=tenant_id, status='succeeded',
            metrics=metrics,
            footnote=reg.disclaimer_for(targets),
            warnings=sorted(set(warns)),
            quality_passed=True, report_ref=aid, duration_ms=duration)

    except Exception as e:
        # ★ 失败退还额度（用户不该为失败买单）；admin 未扣过，不退
        if not is_admin:
            try:
                billing.refund_credit(tenant_id, did,
                                       note=f'诊断失败退还: {type(e).__name__}')
            except Exception:
                pass
        _fail_diagnosis(did, str(e))
        if isinstance(e, DiagnosisError):
            raise
        raise DiagnosisError(str(e), type(e).__name__) from e
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _fail_diagnosis(did: str, error: str) -> None:
    try:
        with db.transaction() as conn:
            conn.execute(
                'UPDATE diagnoses SET status = ?, error = ?, finished_at = ? '
                'WHERE id = ?', ('failed', error[:1000], _now(), did))
    except Exception:
        pass


def _audit_admin_free(tenant_id: str, user_id: str, diagnosis_id: str) -> None:
    """管理员免费诊断的审计记录（docs/12：admin 只免额度，动作必须留痕）。"""
    try:
        with db.transaction() as conn:
            conn.execute(
                'INSERT INTO audit_logs (id,tenant_id,user_id,action,'
                'target_type,target_id,detail,created_at) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex, tenant_id, user_id, 'admin_free_diagnosis',
                 'diagnosis', diagnosis_id,
                 json.dumps({'note': '管理员免费诊断（不限次数）'},
                            ensure_ascii=False), _now()))
    except Exception:                                   # noqa: BLE001
        pass                                            # 审计失败不阻断诊断


def _serialize(v: Any) -> Any:
    """把 DuckDB 返回值转成 JSON 可序列化类型。"""
    if v is None:
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, float):
        return round(v, 6)
    try:
        import decimal
        if isinstance(v, decimal.Decimal):
            return float(v)
    except ImportError:
        pass
    return v


# ===========================================================================
# 5. 查询
# ===========================================================================

def get_diagnosis(tenant_id: str, diagnosis_id: str) -> dict:
    with db.get_conn() as conn:
        row = conn.execute(
            'SELECT id,status,progress_phase,report_ref,quality_passed,error,'
            'cost_usd,created_at,finished_at FROM diagnoses '
            'WHERE id = ? AND tenant_id = ?',
            (diagnosis_id, tenant_id)).fetchone()
    if row is None:
        raise DiagnosisError(f'诊断不存在: {diagnosis_id}', 'DIAGNOSIS_NOT_FOUND')

    return {
        'diagnosis_id': _rg(row, 'id', 0),
        'status': _rg(row, 'status', 1),
        'phase': _rg(row, 'progress_phase', 2) or '',
        'report_ref': _rg(row, 'report_ref', 3) or '',
        'quality_passed': bool(_rg(row, 'quality_passed', 4)),
        'error': _rg(row, 'error', 5) or '',
        'cost_usd': round(_rg(row, 'cost_usd', 6) or 0.0, 4),
        'created_at': _rg(row, 'created_at', 7),
        'finished_at': _rg(row, 'finished_at', 8) or '',
    }


def list_diagnoses(tenant_id: str, limit: int = 50) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            'SELECT id,status,progress_phase,quality_passed,created_at,finished_at '
            'FROM diagnoses WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?',
            (tenant_id, int(limit))).fetchall()
    return [{
        'diagnosis_id': _rg(r, 'id', 0),
        'status': _rg(r, 'status', 1),
        'phase': _rg(r, 'progress_phase', 2) or '',
        'quality_passed': bool(_rg(r, 'quality_passed', 3)),
        'created_at': _rg(r, 'created_at', 4),
        'finished_at': _rg(r, 'finished_at', 5) or '',
    } for r in rows]


def list_sources(tenant_id: str, limit: int = 50) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            'SELECT id,original_name,filename,sheet_name,size_bytes,row_count,'
            'status,created_at '
            'FROM sources WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?',
            (tenant_id, int(limit))).fetchall()
    return [{
        'source_id': _rg(r, 'id', 0),
        'name': (_rg(r, 'original_name', 1) or _rg(r, 'filename', 2))
                + (f' / {_rg(r, "sheet_name", 3)}' if _rg(r, 'sheet_name', 3) else ''),
        'source_type': '',
        'sheet_name': _rg(r, 'sheet_name', 3) or '',
        'size_bytes': _rg(r, 'size_bytes', 4),
        'row_count': _rg(r, 'row_count', 5) or 0,
        'status': _rg(r, 'status', 6),
        'created_at': _rg(r, 'created_at', 7),
    } for r in rows]


def get_report_json(tenant_id: str, diagnosis_id: str) -> dict:
    """取报告内容。"""
    d = get_diagnosis(tenant_id, diagnosis_id)
    ref = d.get('report_ref')
    if not ref:
        raise DiagnosisError('报告尚未生成', 'REPORT_NOT_READY')
    path = tn.get_artifact_path(tenant_id, 'report', ref)
    return json.loads(path.read_text(encoding='utf-8'))


def render_report_html(tenant_id: str, diagnosis_id: str) -> str:
    """把报告渲染成 HTML。"""
    data = get_report_json(tenant_id, diagnosis_id)
    return _build_html(data)


def _fmt_value(v: Any, unit: str) -> str:
    if v is None:
        return '—'
    if unit == 'percent' and isinstance(v, (int, float)):
        return f'{v * 100:.2f}%'
    if unit == 'currency' and isinstance(v, (int, float)):
        return f'¥{v:,.2f}'
    if isinstance(v, (int, float)):
        return f'{v:,.4g}' if isinstance(v, float) else f'{v:,}'
    return str(v)


# ---------------------------------------------------------------------------
# 趋势与 HTML 报告
# ---------------------------------------------------------------------------

def _build_trend(con, tr) -> list[dict]:
    """按日聚合净销售额与流量，供报告画趋势图。

    数据不进上下文 —— 这里只取聚合序列（每天 3 个数字），
    与"数据不进上下文"的原则一致。
    """
    try:
        sales = con.execute(
            'SELECT stat_date, SUM(net_sales) FROM v_sales_daily '
            'GROUP BY 1 ORDER BY 1').fetchall()
        traffic = con.execute(
            'SELECT stat_date, SUM(visitors), SUM(pay_buyers) '
            'FROM v_traffic_daily GROUP BY 1 ORDER BY 1').fetchall()
    except Exception:                       # noqa: BLE001
        return []

    tmap = {str(r[0]): (r[1] or 0, r[2] or 0) for r in traffic}
    out = []
    for d, net in sales:
        ds = str(d)
        v, b = tmap.get(ds, (0, 0))
        out.append({
            'date': ds,
            'net_sales': round(float(net or 0), 2),
            'visitors': int(v),
            'pay_cvr': round(b / v, 4) if v else None,
        })
    if tr and tr.start:
        out = [t for t in out if str(tr.start) <= t['date'] <= str(tr.end)]
    return out


def _tr_where(tr, col: str = 'stat_date') -> str:
    """时间范围过滤子句（参数化留白，由调用方传参）。"""
    if tr and tr.start:
        return f' WHERE {col} BETWEEN ? AND ? '
    return ' '


def _tr_params(tr) -> list:
    if tr and tr.start:
        return [str(tr.start), str(tr.end)]
    return []


def _build_product_breakdown(con, tr) -> list[dict]:
    """商品维度拆解 —— 回答"问题出在哪个商品"。

    全部来自既有视图的确定性聚合（不调 LLM）：
    v_sales_daily（销售/退款）+ v_profit_daily（毛利）+ v_refund_daily（原因）
    + stg_trade_orders（商品标题，DWD 未保留故回 STG 取）。
    """
    try:
        w, p = _tr_where(tr), _tr_params(tr)
        rows = con.execute(
            f'''SELECT s.item_id, s.merchant_code,
                       SUM(s.pay_amount), SUM(s.net_sales), SUM(s.refund_amount),
                       SUM(s.quantity)
                FROM v_sales_daily s {w}
                GROUP BY 1, 2 ORDER BY SUM(s.pay_amount) DESC NULLS LAST''',
            p).fetchall()

        gm = {}
        for r in con.execute(
                f'''SELECT item_id, SUM(gross_margin), SUM(net_sales)
                    FROM v_profit_daily {w} GROUP BY 1''', p).fetchall():
            gm[str(r[0])] = (float(r[1] or 0), float(r[2] or 0))

        reasons: dict[str, dict[str, float]] = {}
        for r in con.execute(
                f'''SELECT item_id, reason_category, SUM(refund_amount)
                    FROM v_refund_daily {w} GROUP BY 1, 2''', p).fetchall():
            reasons.setdefault(str(r[0]), {})[str(r[1])] = float(r[2] or 0)

        titles = {}
        try:
            for r in con.execute(
                    'SELECT item_id, ANY_VALUE(item_title) '
                    'FROM stg_trade_orders '
                    "WHERE COALESCE(item_title, '') != '' GROUP BY 1").fetchall():
                titles[str(r[0])] = str(r[1])
        except Exception:                   # noqa: BLE001  STG 可能未导入
            pass
    except Exception:                       # noqa: BLE001
        return []

    total_pay = sum(float(r[2] or 0) for r in rows) or 1.0
    total_refund = sum(float(r[4] or 0) for r in rows)
    total_net = sum(float(r[3] or 0) for r in rows) or 1.0
    total_gm = sum(gm.get(str(r[0]), (0, 0))[0] for r in rows)
    overall_refund_rate = (total_refund / total_pay) if total_pay else 0
    overall_margin = (total_gm / total_net) if total_net else 0

    out = []
    for item_id, mcode, pay, net, refund, qty in rows:
        item_id = str(item_id or '')
        pay, net, refund = float(pay or 0), float(net or 0), float(refund or 0)
        gmv, gnet = gm.get(item_id, (0.0, 0.0))
        refund_rate = refund / pay if pay else 0
        margin_rate = (gmv / gnet) if gnet else None

        flags = []
        # 阈值用"整体×1.25 且绝对值≥3%"：既防整体均值稀释（第 3 周 22% 摊平到
        # 4 周后只有 8%+，×1.5 会被整体拉高而漏报），也防小基数误报
        if pay >= total_pay * 0.03 and refund_rate > max(overall_refund_rate * 1.25, 0.03):
            flags.append('退款率偏高')
        if margin_rate is not None and gnet >= total_net * 0.03 \
                and overall_margin and margin_rate < overall_margin * 0.7:
            flags.append('毛利率偏低')
        top_reason = ''
        rs = reasons.get(item_id)
        if rs:
            top_reason = max(rs, key=rs.get)      # type: ignore[arg-type]

        out.append({
            'item_id': item_id,
            'merchant_code': str(mcode or ''),
            'title': titles.get(item_id, ''),
            'pay_amount': round(pay, 2),
            'share': round(pay / total_pay, 4),
            'net_sales': round(net, 2),
            'quantity': int(qty or 0),
            'refund_amount': round(refund, 2),
            'refund_rate': round(refund_rate, 4),
            'gross_margin': round(gmv, 2),
            'margin_rate': round(margin_rate, 4) if margin_rate is not None else None,
            'top_refund_reason': top_reason,
            'flags': flags,
        })
    return out


_ANOMALY_REL_THRESHOLD = 0.35      # 相对前 7 日基线的偏离比例


def _detect_anomaly_days(trend: list[dict]) -> list[dict]:
    """异常日检测 —— 回答"哪天不对劲"。

    基线 = 前 7 日（不足则用已有的全部）均值；对净销售额 / 访客 / 转化率
    三条序列独立检测。只描述**观察到的事实特征**（确定性模板），
    不臆测原因 —— 原因提示统一用"建议核对当日投放/活动"话术。
    """
    out: list[dict] = []
    n = len(trend)
    for i in range(n):
        t = trend[i]
        base_window = trend[max(0, i - 7):i]
        if len(base_window) < 3:
            continue                      # 前几天基线不足，不判
        feats: list[str] = []

        def _rel(cur, key):
            vals = [x[key] for x in base_window if x.get(key) is not None]
            if not vals:
                return None
            avg = sum(vals) / len(vals)
            if avg <= 0:
                return None
            return (cur - avg) / avg if cur is not None else None

        for key, label, fmt in (
                ('net_sales', '净销售额', _pct),
                ('visitors', '访客数', _pct),
                ('pay_cvr', '转化率', _pct)):
            r = _rel(t.get(key), key)
            if r is not None and abs(r) >= _ANOMALY_REL_THRESHOLD:
                feats.append(f'{label}{"上升" if r > 0 else "下降"} {fmt(abs(r))}（对比前 7 日均值）')

        if not feats:
            continue

        # 组合特征 → 确定性"关注点"模板
        notes = []
        up_vis = any('访客数上升' in f for f in feats)
        down_cvr = any('转化率下降' in f for f in feats)
        down_net = any('净销售额下降' in f for f in feats)
        up_net = any('净销售额上升' in f for f in feats)
        if up_vis and (down_cvr or down_net):
            notes.append('流量放大但销售未同步 —— 建议核对当日投放计划与人群定向')
        if up_vis and up_net:
            notes.append('量额齐升 —— 若当日有大促/上新，可作为后续对比基准日')
        if down_net and not up_vis:
            notes.append('销售回落 —— 建议核对当日是否有缺货、下架或差评集中')
        if down_cvr:
            notes.append('转化率走低 —— 建议核对当日主图/价格/评价变化')

        out.append({
            'date': t['date'],
            'features': feats,
            'note': '；'.join(notes) if notes else '建议核对该日与前后日的经营动作差异',
        })
    return out


def _pct(v: float) -> str:
    return f'{v * 100:.0f}%'


def _build_weekly_compare(con, tr) -> dict:
    """周度对比 —— 把"整体数字"变成"第几周、什么在变"。

    返回 {weeks: [...], biggest: {metric, last, base, delta}}。
    """
    try:
        w, p = _tr_where(tr), _tr_params(tr)
        sales = con.execute(
            f'''SELECT DATE_TRUNC('week', stat_date)::DATE wk,
                       SUM(pay_amount), SUM(net_sales), SUM(refund_amount),
                       COUNT(DISTINCT stat_date)
                FROM v_sales_daily {w} GROUP BY 1 ORDER BY 1''', p).fetchall()
        traffic = con.execute(
            f'''SELECT DATE_TRUNC('week', stat_date)::DATE wk,
                       SUM(visitors), SUM(pay_buyers)
                FROM v_traffic_daily {w} GROUP BY 1 ORDER BY 1''', p).fetchall()
        ads = con.execute(
            f'''SELECT DATE_TRUNC('week', stat_date)::DATE wk, SUM(spend)
                FROM dwd_ad_daily {w} GROUP BY 1 ORDER BY 1''', p).fetchall()
    except Exception:                       # noqa: BLE001
        return {}

    tmap = {str(r[0]): (r[1] or 0, r[2] or 0) for r in traffic}
    amap = {str(r[0]): float(r[1] or 0) for r in ads}

    weeks = []
    for wk, pay, net, refund, days in sales:
        ks = str(wk)
        vis, buyers = tmap.get(ks, (0, 0))
        spend = amap.get(ks, 0.0)
        weeks.append({
            'label': ks,
            'days': int(days or 0),
            'pay_amount': round(float(pay or 0), 2),
            'net_sales': round(float(net or 0), 2),
            'visitors': int(vis or 0),
            'pay_cvr': round((buyers / vis), 4) if vis else None,
            'refund_rate': round(float(refund or 0) / float(pay or 1), 4),
            'ad_spend': round(spend, 2),
            'roi': round(float(net or 0) / spend, 2) if spend else None,
        })
    if len(weeks) < 2:
        return {'weeks': weeks, 'biggest': None}

    # 变化最大的维度：末个完整周 vs 之前完整周均值（残周数据不完整，不参与）
    full = [w for w in weeks if w['days'] >= 7]
    if len(full) >= 2:
        last, prev = full[-1], full[:-1]
    else:
        last, prev = weeks[-1], weeks[:-1]

    def _avg(key):
        vals = [w[key] for w in prev if w.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    cands = []
    for key, label, kind in (
            ('visitors', '访客数', 'abs'),
            ('pay_cvr', '转化率', 'pct'),
            ('net_sales', '净销售额', 'abs'),
            ('refund_rate', '退款率', 'pct'),
            ('roi', 'ROI', 'pct')):
        lv, bv = last.get(key), _avg(key)
        if lv is None or bv in (None, 0):
            continue
        delta = (lv - bv) / bv
        cands.append({'metric': label, 'last': lv, 'base': bv,
                      'delta': round(delta, 4), 'kind': kind})
    biggest = max(cands, key=lambda c: abs(c['delta'])) if cands else None
    return {'weeks': weeks, 'biggest': biggest}


def _build_caliber_compare(metrics: list[dict]) -> dict:
    """L1 口径对比：财务口径 vs 推广口径毛利率并排呈现。

    两种口径本来就在指标集里（M13/M14），本函数只做"配对 + 差异解释"，
    不新增计算 —— 与"口径对比不动定义"的 L1 设计一致。
    """
    by_id = {m['metric_id']: m for m in metrics}
    m13, m14 = by_id.get('M13'), by_id.get('M14')
    if not m13 or not m14 or m13.get('value') is None or m14.get('value') is None:
        return {}
    return {
        'label': '毛利率',
        'items': [
            {'metric_id': 'M13', 'name': '财务口径',
             'desc': '扣商品成本 + 平台佣金 + 技术服务费',
             'value': m13['value'], 'unit': m13.get('unit', 'percent')},
            {'metric_id': 'M14', 'name': '推广口径',
             'desc': '财务口径基础上再扣广告花费',
             'value': m14['value'], 'unit': m14.get('unit', 'percent')},
        ],
        'diff': round(float(m13['value']) - float(m14['value']), 4),
        'note': (f'两口径相差 {_pct(max(float(m13["value"]), float(m14["value"])) - min(float(m13["value"]), float(m14["value"])))}，'
                 '即广告花费占销售的比重。看"店铺本质赚钱能力"用财务口径，'
                 '看"含投放的综合效益"用推广口径。'),
    }


def _build_caliber_snapshot(con, metrics: list[dict], disclaimer: str) -> dict:
    """口径版本快照 —— 让半年后的"为什么和上期不一样"可回答。"""
    version = ''
    try:
        row = con.execute(
            'SELECT MAX(version) FROM sem_metric_registry').fetchone()
        version = str(row[0]) if row and row[0] else ''
    except Exception:                       # noqa: BLE001
        pass
    return {
        'registry_version': version,
        'defaults': '财务口径退款按申请日归属；成本为移动加权平均（详见口径说明）',
        'metric_uncertainty': {m['metric_id']: m.get('uncertainty', '')
                               for m in metrics},
        'disclaimer': disclaimer,
    }


def _svg_trend(trend: list[dict], anomaly_dates: set[str] | None = None) -> str:
    """纯字符串生成趋势 SVG（双轴：净销售额柱/线 + 访客数线 + 异常日标记）。

    不引外部 JS —— 报告是自包含的，可直接打印/转发。
    """
    if not trend or len(trend) < 2:
        return ''

    anomaly_dates = anomaly_dates or set()
    W, H = 860, 200
    PL, PR, PT, PB = 50, 56, 18, 30          # 边距
    iw, ih = W - PL - PR, H - PT - PB

    nets = [t['net_sales'] for t in trend]
    viss = [t['visitors'] for t in trend]
    nmax = max(nets) * 1.1 or 1
    vmax = max(viss) * 1.1 or 1
    n = len(trend)

    def xy(i, v, vmax_):
        x = PL + i * (iw / (n - 1))
        y = PT + ih - (v / vmax_) * ih
        return f'{x:.1f},{y:.1f}'

    # 净销售额（面积 + 折线，绿色）
    pts = ' '.join(xy(i, v, nmax) for i, v in enumerate(nets))
    area = (f'{PL},{PT + ih} ' + pts + f' {PL + iw},{PT + ih}')
    # 访客数（虚线，蓝色）
    vpts = ' '.join(xy(i, v, vmax) for i, v in enumerate(viss))

    # 异常日标记（竖虚线 + 顶部圆点）
    marks = []
    for i, t in enumerate(trend):
        if t['date'] not in anomaly_dates:
            continue
        x = PL + i * (iw / (n - 1))
        marks.append(
            f'<line x1="{x:.1f}" y1="{PT}" x2="{x:.1f}" y2="{PT + ih}" '
            f'stroke="#d46b08" stroke-width="1" stroke-dasharray="3,3" opacity="0.7"/>'
            f'<circle cx="{x:.1f}" cy="{PT - 4}" r="3" fill="#d46b08"/>')

    # x 轴标签（最多 8 个）
    step = max(1, n // 8)
    labels = []
    for i in range(0, n, step):
        x = PL + i * (iw / (n - 1))
        labels.append(
            f'<text x="{x:.0f}" y="{H - 8}" text-anchor="middle" '
            f'font-size="10" fill="#8c959f">{trend[i]["date"][5:]}</text>')

    # y 轴刻度
    yl = []
    for k in (0, 0.5, 1.0):
        y = PT + ih - k * ih
        val = nmax * k
        yl.append(f'<text x="{PL - 6}" y="{y + 4:.0f}" text-anchor="end" '
                  f'font-size="10" fill="#2c6e49">¥{val:,.0f}</text>')
        yl.append(f'<line x1="{PL}" y1="{y:.0f}" x2="{PL + iw}" y2="{y:.0f}" '
                  f'stroke="#eef1f4"/>')
    vy = PT + ih * 0.5
    yl.append(f'<text x="{PL + iw + 8}" y="{vy + 4:.0f}" font-size="10" '
              f'fill="#3b82f6">{int(vmax * 0.5):,}</text>')
    yl.append(f'<text x="{PL + iw + 8}" y="{PT + 12}" font-size="10" '
              f'fill="#3b82f6">{int(vmax):,}</text>')

    return f'''<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">
      <polygon points="{area}" fill="rgba(44,110,73,0.10)"/>
      <polyline points="{pts}" fill="none" stroke="#2c6e49" stroke-width="2.5"/>
      <polyline points="{vpts}" fill="none" stroke="#3b82f6" stroke-width="1.8"
                stroke-dasharray="5,4" opacity="0.85"/>
      {''.join(marks)}
      {''.join(yl)}
      {''.join(labels)}
    </svg>'''


def _metric_cards(metrics: list[dict]) -> str:
    """顶部四张统计卡片（挑最有故事性的指标）。"""
    by_id = {m['metric_id']: m for m in metrics}
    order = [('M06', '支付金额'), ('M07', '净销售额'),
             ('M13', '毛利率（财务）'), ('M03', '支付转化率')]
    cards = []
    for mid, label in order:
        m = by_id.get(mid)
        if not m or m.get('value') is None:
            continue
        warn = m.get('uncertainty') == 'high'
        cls = 'card warn' if warn else 'card'
        cards.append(
            f'<div class="{cls}"><div class="label">{label}</div>'
            f'<div class="value">{_fmt_value(m["value"], m.get("unit", ""))}</div>'
            f'<div class="formula">{m.get("formula", "") or "&nbsp;"}</div></div>')
    return f'<div class="cards">{"".join(cards)}</div>' if cards else ''


def _caliber_compare_html(cc: dict) -> str:
    """L1 口径对比区块：双口径并排 + 差异解释。"""
    if not cc:
        return ''
    items = ''.join(
        f'<div class="cc-item"><div class="cc-name">{it["name"]}</div>'
        f'<div class="cc-value">{_fmt_value(it["value"], it.get("unit", ""))}</div>'
        f'<div class="cc-desc">{it.get("desc", "")}</div></div>'
        for it in cc['items'])
    return f'''
<h2>口径对比 · {cc['label']}</h2>
<div class="cc">{items}</div>
<p class="cc-note">{cc.get('note', '')}</p>'''


def _anomaly_html(anomalies: list[dict]) -> str:
    """异常日区块：日期 + 观察到的特征 + 建议核对方向。"""
    if not anomalies:
        return ''
    rows = []
    for a in anomalies[:8]:                 # 最多展示 8 天
        feats = '；'.join(a['features'])
        rows.append(
            f'<tr><td class="mono">{a["date"][5:]}</td>'
            f'<td>{feats}</td>'
            f'<td class="note">{a.get("note", "")}</td></tr>')
    return f'''
<h2>异常日定位</h2>
<p class="sec-desc">基线 = 前 7 日均值；偏离 ≥35% 的日子。特征是观察到的事实，
核对方向是建议 —— 系统不猜测原因，请结合当日实际经营动作确认。</p>
<table><thead><tr><th style="width:70px">日期</th><th>观察到的特征</th>
<th>建议核对方向</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>'''


def _products_html(products: list[dict]) -> str:
    """商品维度区块：拆解到商品，异常项标"重点关注"。"""
    if not products:
        return ''
    rows = []
    for p in products[:12]:
        flags = ('<span class="flag">重点关注</span>' if p['flags'] else '')
        reasons = (f'<span class="reason">{p["top_refund_reason"]}</span>'
                   if p['top_refund_reason'] and p['refund_amount'] > 0 else '—')
        title = p['title'] or p['merchant_code'] or p['item_id']
        rows.append(
            f'<tr><td>{title}{flags}</td>'
            f'<td class="num">{_fmt_value(p["share"], "percent")}</td>'
            f'<td class="num">¥{p["pay_amount"]:,.0f}</td>'
            f'<td class="num">¥{p["net_sales"]:,.0f}</td>'
            f'<td class="num">{_fmt_value(p["refund_rate"], "percent")}</td>'
            f'<td>{reasons}</td>'
            f'<td class="num">{_fmt_value(p["margin_rate"], "percent")}</td></tr>')
    flagged = [p for p in products if p['flags']]
    hint = ''
    if flagged:
        names = '、'.join((p['title'] or p['merchant_code'] or p['item_id'])
                          for p in flagged)
        reasons_txt = '；'.join(sorted({f for p in flagged for f in p['flags']}))
        hint = (f'<p class="sec-desc">⚠ 重点关注：{names} —— {reasons_txt}。'
                f'退款原因与商品明细见「退款售后明细」数据源。</p>')
    return f'''
<h2>商品维度拆解</h2>{hint}
<table><thead><tr><th>商品</th><th style="text-align:right">销售占比</th>
<th style="text-align:right">支付金额</th><th style="text-align:right">净销售额</th>
<th style="text-align:right">退款率</th><th>主要退款原因</th>
<th style="text-align:right">毛利率</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>'''


def _weekly_html(weekly: dict) -> str:
    """周度对比区块：末周 vs 之前均值，突出变化最大的维度。"""
    weeks = (weekly or {}).get('weeks') or []
    if len(weeks) < 2:
        return ''
    biggest = (weekly or {}).get('biggest')

    def wk_label(iso: str, days: int) -> str:
        suffix = f'（{days} 天，不完整）' if days < 7 else ''
        return f'{iso[5:]} 起{suffix}'

    rows = []
    for w in weeks:
        cvr = (f'{w["pay_cvr"] * 100:.2f}%' if w.get('pay_cvr') is not None else '—')
        roi = f'{w["roi"]:.1f}' if w.get('roi') is not None else '—'
        rows.append(
            f'<tr><td class="mono">{wk_label(w["label"], w.get("days", 7))}</td>'
            f'<td class="num">{w["visitors"]:,}</td>'
            f'<td class="num">{cvr}</td>'
            f'<td class="num">¥{w["net_sales"]:,.0f}</td>'
            f'<td class="num">{w["refund_rate"] * 100:.2f}%</td>'
            f'<td class="num">¥{w["ad_spend"]:,.0f}</td>'
            f'<td class="num">{roi}</td></tr>')

    summary = ''
    if biggest:
        k = biggest['kind']
        if k == 'pct':
            last_s = f'{biggest["last"] * 100:.2f}%'
            base_s = f'{biggest["base"] * 100:.2f}%'
        else:
            last_s = (f'{biggest["last"]:,}')
            base_s = f'{biggest["base"]:,}'
        arrow = '↑' if biggest['delta'] > 0 else '↓'
        summary = (f'<p class="sec-desc">变化最大的维度：<b>{biggest["metric"]}</b> '
                   f'（末周 {last_s} vs 之前均值 {base_s}，'
                   f'{arrow} {abs(biggest["delta"]) * 100:.0f}%）</p>')
    return f'''
<h2>周度对比</h2>{summary}
<table><thead><tr><th>周（周一起）</th><th style="text-align:right">访客</th>
<th style="text-align:right">转化率</th><th style="text-align:right">净销售额</th>
<th style="text-align:right">退款率</th><th style="text-align:right">广告花费</th>
<th style="text-align:right">ROI</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>'''


def _build_html(data: dict) -> str:
    """生成 HTML 报告（v1.2：归因区块 + 口径对比 + 异常日 + 商品/周拆解）。"""
    metrics = data.get('metrics', [])
    trend = data.get('trend') or []
    anomalies = data.get('anomaly_days') or []
    products = data.get('products') or []
    weekly = data.get('weekly') or {}
    cc = data.get('caliber_compare') or {}
    caliber = data.get('caliber') or {}

    # 指标表
    rows = []
    for m in metrics:
        lvl = m.get('uncertainty', 'low')
        badge = ('<span class="badge warn">待确认</span>' if lvl == 'high' else
                 ('<span class="badge mid">部分待确认</span>'
                  if lvl == 'medium' else
                  '<span class="badge ok">已核实</span>'))
        rows.append(
            f'<tr><td>{m.get("metric_name","")}</td>'
            f'<td class="num">{_fmt_value(m.get("value"), m.get("unit",""))}</td>'
            f'<td class="formula">{m.get("formula","")}</td>'
            f'<td>{badge}</td></tr>')

    warns = ''.join(f'<li>{w}</li>' for w in data.get('warnings', []))
    footnote = (data.get('footnote', '') or
                '所有指标口径均已核实。').replace('\n', '<br>')
    src = data.get('source', {})
    tr = data.get('time_range') or {}
    period = (f"{tr.get('start', '')} ~ {tr.get('end', '')}"
              if tr.get('start') else '全部数据')
    anomaly_dates = {a['date'] for a in anomalies}
    trend_svg = _svg_trend(trend, anomaly_dates)
    cards = _metric_cards(metrics)
    files = '、'.join(f"{f['name']}({f['rows']}行)" for f in src.get('files', [])[:6])

    caliber_line = ''
    if caliber.get('registry_version'):
        caliber_line = (f'<span>口径版本 <b>{caliber["registry_version"]}</b>'
                        f'（本次计算快照已随报告存档）</span>')

    legend_extra = ('<span><span class="k a">▍</span>异常日</span>'
                    if anomaly_dates else '')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>经营诊断报告</title>
<style>
 *{{box-sizing:border-box}}
 body{{font-family:-apple-system,"Microsoft YaHei","PingFang SC",sans-serif;
      max-width:960px;margin:0 auto;padding:32px 20px;color:#1f2328;line-height:1.65;
      background:#fafbfc}}
 .sheet{{background:#fff;border-radius:14px;padding:32px 36px;
        box-shadow:0 1px 4px rgba(0,0,0,.06)}}
 h1{{font-size:22px;margin:0 0 4px;color:#1a3d2b}}
 .sub{{color:#8c959f;font-size:13px;margin-bottom:22px}}
 .meta{{display:flex;flex-wrap:wrap;gap:12px 28px;font-size:13px;color:#57606a;
       background:#f6f8fa;padding:14px 18px;border-radius:8px;margin-bottom:24px}}
 .meta b{{color:#1f2328}}
 .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
        gap:14px;margin:0 0 28px}}
 .card{{border:1px solid #e5e7eb;border-radius:10px;padding:16px 18px}}
 .card.warn{{border-color:#f3c8c2;background:#fffaf9}}
 .card .label{{font-size:12px;color:#8c959f;margin-bottom:4px}}
 .card .value{{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums;
              color:#1a3d2b}}
 .card.warn .value{{color:#b3412f}}
 .card .formula{{font-family:ui-monospace,Consolas,monospace;font-size:11px;
                color:#8c959f;margin-top:6px;min-height:14px}}
 .cc{{display:flex;gap:14px;margin:6px 0 4px;flex-wrap:wrap}}
 .cc-item{{flex:1;min-width:220px;border:1px solid #d4e8da;background:#f0f9f1;
          border-radius:10px;padding:14px 18px}}
 .cc-item .cc-name{{font-size:13px;color:#2c6e49;font-weight:600}}
 .cc-item .cc-value{{font-size:30px;font-weight:650;color:#1a3d2b;
                    font-variant-numeric:tabular-nums;margin:2px 0}}
 .cc-item .cc-desc{{font-size:12px;color:#8c959f}}
 .cc-note{{font-size:13px;color:#57606a;margin:8px 0 0}}
 h2{{margin:34px 0 12px;color:#1a3d2b;font-size:17px;
    display:flex;align-items:center;gap:8px}}
 h2::before{{content:"";display:inline-block;width:4px;height:18px;
            background:#2c6e49;border-radius:2px}}
 .sec-desc{{font-size:13px;color:#57606a;margin:0 0 10px}}
 .legend{{font-size:12px;color:#8c959f;margin:-6px 0 10px;display:flex;gap:20px;
         flex-wrap:wrap}}
 .legend .k{{display:inline-block;width:22px;height:0;border-top:3px solid #2c6e49;
            vertical-align:middle;margin-right:6px}}
 .legend .k.b{{border-top:2px dashed #3b82f6}}
 .legend .k.a{{border:none;width:auto;height:auto;color:#d46b08;font-weight:700;
               vertical-align:baseline}}
 table{{width:100%;border-collapse:collapse;margin:14px 0}}
 th,td{{padding:10px 12px;border-bottom:1px solid #eceef1;text-align:left;
       font-size:14px}}
 th{{background:#f6f8fa;font-weight:600;font-size:13px;color:#57606a}}
 td.num{{font-variant-numeric:tabular-nums;font-weight:650;text-align:right;
        font-size:15px}}
 td.mono{{font-family:ui-monospace,Consolas,monospace;font-size:13px}}
 td.formula{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;
             color:#8c959f}}
 td.note{{font-size:13px;color:#57606a}}
 .badge{{padding:2px 9px;border-radius:10px;font-size:11px;white-space:nowrap}}
 .badge.warn{{background:#fff1f0;color:#cf1322}}
 .badge.mid{{background:#fff7e6;color:#d46b08}}
 .badge.ok{{background:#f0f9f1;color:#2c6e49}}
 .flag{{background:#fff1f0;color:#cf1322;font-size:11px;border-radius:9px;
       padding:1px 8px;margin-left:8px;white-space:nowrap}}
 .reason{{font-size:12px;color:#8c959f}}
 .disclaimer{{background:#fffbe6;border-left:4px solid #faad14;padding:16px 20px;
             border-radius:0 8px 8px 0;font-size:13.5px}}
 footer{{margin-top:36px;padding-top:16px;border-top:1px solid #eceef1;
         font-size:12px;color:#8c959f;display:flex;justify-content:space-between;
         flex-wrap:wrap;gap:8px}}
 @media print{{body{{background:#fff;padding:0}} .sheet{{box-shadow:none;
   border-radius:0;padding:0}}}}
</style></head><body>
<div class="sheet">
<h1>经营诊断报告</h1>
<div class="sub">淘宝店铺 · 自动生成</div>

<div class="meta">
  <span>统计周期 <b>{period}</b></span>
  <span>数据源 <b>{src.get('file_count', 0)}</b> 个 / {src.get('row_count', 0):,} 行</span>
  <span>报告编号 <b>{data.get('diagnosis_id', '')[:12]}</b></span>
  <span>生成时间 {data.get('generated_at', '')[:19].replace('T', ' ')}</span>
</div>

{cards}

{_caliber_compare_html(cc)}

{f'<h2>净销售额与访客数趋势</h2><div class="legend">'
 f'<span><span class="k"></span>净销售额（左轴）</span>'
 f'<span><span class="k b"></span>访客数（右轴）</span>{legend_extra}</div>{trend_svg}'
 if trend_svg else ''}

{_anomaly_html(anomalies)}

{_products_html(products)}

{_weekly_html(weekly)}

<h2>核心指标</h2>
<table>
<thead><tr><th>指标</th><th style="text-align:right">数值</th>
<th>计算方式</th><th>口径状态</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="4">无可用指标</td></tr>'}</tbody>
</table>

{f'<h2>计算提示</h2><ul style="font-size:13px;color:#57606a">{warns}</ul>'
 if warns else ''}

<div class="disclaimer">
<strong>口径说明与待确认项</strong><br>{footnote}
</div>

<footer>
  <span>本报告由数据分析 Agent 自动生成；标注「待确认」的口径建议核对后再做经营决策。</span>
  <span>数据源：{files}</span>
  {caliber_line}
</footer>
</div>
</body></html>"""
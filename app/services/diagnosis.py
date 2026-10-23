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

模块拆分（docs/16 P2-25，原 1860 行拆出三类独立职责）：
  - diag_errors.py    错误类型（路由按 code 映射 HTTP 状态）
  - semantic_admin.py 语义库打开 / 版本化迁移 / schema.sql 命名分段
  - analytics.py      趋势 / 商品拆解 / 异常日 / 周对比 / 口径（qa_engine、
                      agent_engine 直接依赖，不再反向 import 本模块）
  - report_render.py  HTML 报告渲染
  本模块保留：上传 / 画像 / 确认映射 / 诊断编排 / ETL 导入 / 查询。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.adapters import taobao
from app.adapters.taobao_fields import get_spec
from app.services import analytics, billing, locks, semantic_admin
from app.services import tenant as tn
from app.services.diag_errors import (            # noqa: F401  路由/测试经 dg.* 使用
    DiagnosisError, MappingNotConfirmedError, QualityGateError,
    SourceNotFoundError, SourceTypeUnknownError,
)
from app.services.report_render import build_html
from app.semantic.models import CaliberConflict
from app.semantic.registry import SemanticRegistry
from app.semantic.renderer import SQLRenderer, RenderError
from app.semantic.timerange import DateRange, parse_range
from app.storage import db

logger = logging.getLogger(__name__)


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
    tn.ensure_tenant_active(tenant_id)      # P2-20：停用租户拒绝上传
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
        needs_confirmation=bool(prepared.missing_required
                                or prepared.read.truncated
                                or any(m['needs_review'] for m in mappings)),
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

def _import_stg(con, prepared, mapping: dict[str, str],
                table: str) -> tuple[int, dict[str, dict]]:
    """把源文件数据导入标准 STG 表。

    返回 (导入行数, 数值清洗失败汇总)。失败值已置 NULL —— 但也必须
    记录到 dwd_etl_errors(BAD_AMOUNT)，不能静默（docs/16 P1-10）。
    """
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
        return 0, {}

    con.execute(f'DELETE FROM {table}')

    targets = sorted(col_of)
    placeholders = ','.join(['?'] * len(targets))
    cols = ','.join(targets)
    sql = f'INSERT INTO {table} ({cols}) VALUES ({placeholders})'

    rows = []
    bad_values: dict[str, dict] = {}
    for raw in read.rows:
        vals = []
        for t in targets:
            v = raw[col_of[t]] if col_of[t] < len(raw) else None
            # 类型驱动转换（按目标表的列类型，不靠字段名猜）
            vals.append(taobao.coerce_for_table(table, t, v, bad_values))
        if all(x is None for x in vals):
            continue
        rows.append(vals)

    if rows:
        con.executemany(sql, rows)
    return len(rows), bad_values


def _record_bad_amounts(con, bad_amounts: dict[str, dict]) -> None:
    """把导入期数值清洗失败写入 dwd_etl_errors（docs/16 P1-10）。

    必须在 S5 ETL 之后调用：S5 开头会清空本次 run 的错误表。
    QC-10(WARN) 再把它汇总进质量结果。
    """
    n = 0
    for table, fields in bad_amounts.items():
        for field, info in fields.items():
            n += 1
            samples = '、'.join(info.get('samples') or []) or '—'
            con.execute(
                'INSERT INTO dwd_etl_errors VALUES (?,?,?,?,?,?,?,?,?)',
                (f'E-BAD-{n}', 'v1-full-refresh', 'import', 'WARN',
                 'BAD_AMOUNT',
                 f'数值清洗失败已置 NULL: {table}.{field} × '
                 f'{info.get("count", 0)} 行（示例: {samples}）',
                 table, field, dt.datetime.now()))


def _etl_warning_messages(con) -> list[str]:
    """把本次 ETL/导入的 WARN 转成报告「计算提示」。"""
    try:
        rows = con.execute(
            "SELECT error_code, message FROM dwd_etl_errors "
            "WHERE run_id = 'v1-full-refresh' AND severity = 'WARN' "
            "ORDER BY error_code").fetchall()
    except Exception:                       # noqa: BLE001
        return []
    out: list[str] = []
    seen: set[str] = set()
    for code, msg in rows:
        text = f'[{code}] {msg}'
        if text not in seen:                # 同一错误码多条时只保留示例级去重
            seen.add(text)
            out.append(text)
        if len(out) >= 20:
            out.append('（更多 ETL 提示已省略，详见 dwd_etl_errors）')
            break
    return out


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
    tn.ensure_tenant_active(tenant_id)      # P2-20：停用租户拒绝诊断
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

    # ★ 同类型多数据源显式报错（docs/16 P0-6）：_import_stg 每次导入前
    #   先整表 DELETE，同类型第二份文件会静默清掉第一份的数据 ——
    #   报告只基于最后一份却把所有文件都算进 imported，数字与数据对不上。
    _seen_tables: dict[str, str] = {}        # target_table -> 文件名
    for x in srcs:
        _spec = get_spec(x.source_type)
        _name = x.original_name or x.filename
        if _spec.target_table in _seen_tables:
            raise DiagnosisError(
                f'包含多个「{_spec.display_name}」类型的数据源'
                f'（{_seen_tables[_spec.target_table]} 与 {_name}）。'
                f'同类型文件只能导入一份 —— 请把它们合并成一个文件后重新上传',
                'DUPLICATE_SOURCE_TYPE')
        _seen_tables[_spec.target_table] = _name

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
    # ★ P0-7：同租户互斥 —— 持锁期间独占 semantic.duckdb（导入/ETL/算指标
    #   全程），防止与并发诊断或对话交错执行（详见 app/services/locks.py）。
    #   锁在 finally 里随连接一起释放。
    tenant_lk = locks.tenant_lock(tenant_id)
    tenant_lk.acquire()
    try:
        # ---- 3. 打开语义层库（内部已按版本迁移，不再每次重放 DDL）----
        emit('loading', '加载语义层')
        con = semantic_admin.open_semantic_db(tenant_id)

        # ---- 4. 逐个导入（★ 多文件）----
        p = tn.tenant_paths(tenant_id)
        imported: list[tuple[str, int]] = []
        bad_amounts: dict[str, dict] = {}
        import_warnings: list[str] = []

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

            n, bad = _import_stg(con, prepared, mapping, spec.target_table)
            if bad:
                bad_amounts[spec.target_table] = bad
            if prepared.read.truncated:
                import_warnings.append(
                    f'{src.original_name or src.filename}: 超过 '
                    f'{taobao.MAX_READ_ROWS:,} 行上限，报告基于截断后的数据')
            imported.append((spec.key, n))
            emit('importing', f'{spec.display_name}: {n} 行')

        emit('importing', '导入完成: ' + ', '.join(
            f'{k}={v}' for k, v in imported))

        # ★ 诊断-数据源血缘落库（docs/16 P2-29）：diagnoses.source_id 只存
        #   首份文件，多源诊断的真实输入集合在这里可查证（报告“基于哪些
        #   文件”不再只活在报告 JSON 里）。
        with db.transaction() as conn_l:
            for src, (_spec_key, n) in zip(srcs, imported):
                conn_l.execute(
                    'INSERT OR REPLACE INTO diagnosis_sources '
                    '(diagnosis_id, source_id, source_type, imported_rows) '
                    'VALUES (?,?,?,?)',
                    (did, src.id, _spec_key, n))

        # ---- 5. ETL ----
        emit('etl', '执行 ETL')
        # ★ P2-26：命名分段加载（锚点缺失会立刻抛错，不再魔法字符串切片）
        con.execute(semantic_admin.load_schema_segment('etl'))

        # ★ 导入期数值清洗失败落在 ETL 之后写入：S5 开头会清空本次 run
        #   的错误表（docs/16 P1-10）。
        _record_bad_amounts(con, bad_amounts)

        # ---- 6. 质量门 ----
        emit('quality', '数据质量校验')
        con.execute(semantic_admin.load_schema_segment('quality'))

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
        warns: list[str] = list(import_warnings)
        # ★ ETL/导入告警进报告（docs/16 P1-10）：缺成本批次、数值清洗失败、
        #   账房未映射类型等不再只躺在 dwd_etl_errors 里。
        warns.extend(_etl_warning_messages(con))

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
            except Exception as e:                       # noqa: BLE001
                # ★ docs/16 P2-5：单指标的 DuckDB/数据错误只降级该指标
                #   （记入报告「计算提示」），不能炸掉整个诊断 ——
                #   否则一个坏指标让其余 8 个全丢，与趋势/商品区块的
                #   降级纪律一致。
                logger.exception('指标计算失败 metric=%s', mid)
                warns.append(f'{mid} 计算失败: {type(e).__name__}: {e}')

        emit('reporting', '生成报告')

        # ---- 8. 趋势与归因数据（供报告画图与定位问题）----
        trend = analytics.build_trend(con, tr)
        products = analytics.build_product_breakdown(con, tr)
        anomalies = analytics.detect_anomaly_days(trend)
        weekly = analytics.build_weekly_compare(con, tr)
        caliber_compare = analytics.build_caliber_compare(metrics)
        caliber_snap = analytics.build_caliber_snapshot(
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
        # ★ 失败退还额度（用户不该为失败买单）；admin 未扣过，不退。
        #   request_refund 内部幂等、失败落 refund_pending —— 绝不静默吞掉
        #   （docs/16 P1-3）。
        if not is_admin:
            billing.request_refund(
                tenant_id, did, note=f'诊断失败退还: {type(e).__name__}')
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
        tenant_lk.release()


def _fail_diagnosis(did: str, error: str) -> None:
    try:
        with db.transaction() as conn:
            conn.execute(
                'UPDATE diagnoses SET status = ?, error = ?, finished_at = ? '
                'WHERE id = ?', ('failed', error[:1000], _now(), did))
    except Exception:                                   # noqa: BLE001
        logger.exception('诊断状态落库失败 diagnosis=%s', did)


def reconcile_on_startup() -> dict:
    """启动对账（docs/16 P1-3）。

    进程重启会留下两类烂摊子：
      1. status=queued/running 的诊断永远卡死（daemon 线程已随进程消失），
         前端无限转圈，已扣额度也没有退款路径；
      2. refund_pending 里等待重试的退款。

    处理：遗留诊断标记 failed 并幂等补退款（request_refund 依赖
    consume/refund 流水判定，重复执行不会重复加余额）；再重试补偿表。
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, tenant_id FROM diagnoses "
            "WHERE status IN ('queued', 'running')").fetchall()

    interrupted = 0
    for row in rows:
        did = _rg(row, 'id', 0)
        tid = _rg(row, 'tenant_id', 1)
        _fail_diagnosis(did, '服务重启，诊断中断（额度已自动退还）')
        billing.request_refund(tid, did, note='服务重启对账退还')
        interrupted += 1

    refund_stats = billing.process_pending_refunds()
    if interrupted or any(refund_stats.values()):
        logger.info('启动对账：中断诊断 %d 个，补偿退款 %s',
                    interrupted, refund_stats)
    return {'interrupted': interrupted, **refund_stats}


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
    """把报告渲染成 HTML（渲染逻辑在 report_render.py）。"""
    data = get_report_json(tenant_id, diagnosis_id)
    return build_html(data)

"""API 请求/响应模型（Pydantic）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    tenant_name: str = Field(default='', max_length=100)
    contact_phone: str = Field(default='', max_length=30)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    # ★ 可选：新版前端依赖 httpOnly cookie，body 仅兼容旧客户端（P2-16）
    refresh_token: str | None = Field(default=None, min_length=8)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = 'Bearer'
    expires_in: int


class UserResponse(BaseModel):
    user_id: str
    tenant_id: str
    email: str
    role: str
    display_name: str = ''
    email_verified: bool = False


class AuthResponse(BaseModel):
    user: UserResponse
    token: TokenResponse
    verification_required: bool = False
    verification_sent: bool = False


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class VerifyEmailResponse(BaseModel):
    message: str
    user: UserResponse


# ---------------------------------------------------------------------------
# 数据源
# ---------------------------------------------------------------------------

class SourceResponse(BaseModel):
    source_id: str
    name: str
    source_type: str = ''
    sheet_name: str = ''
    size_bytes: int = 0
    row_count: int = 0
    status: str = ''
    created_at: str = ''


class MappingItem(BaseModel):
    source_column: str
    target_field: str
    confidence: float
    confidence_label: str = ''
    needs_review: bool = False
    reason: str = ''
    confirmed: bool = False


class ProfileResponse(BaseModel):
    source_id: str
    source_type: str
    display_name: str
    row_count: int
    column_count: int
    columns: list[str]
    mappings: list[MappingItem]
    missing_required: list[str]
    needs_confirmation: bool
    schema_fingerprint: str
    warnings: list[str] = []


class ConfirmMappingRequest(BaseModel):
    mapping: dict[str, str] = Field(
        description='目标字段 -> 源列名，如 {"order_id": "订单编号"}')


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------

class StartDiagnosisRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1,
                                  description='一组数据源 ID（订单+流量+账房…）')
    time_range: str = Field(default='', description='如 本月 / 2026-10-01~2026-10-03')
    metric_ids: list[str] | None = Field(default=None)


class MetricValue(BaseModel):
    metric_id: str
    metric_name: str
    value: Any = None
    unit: str = 'count'
    formula: str = ''
    uncertainty: str = 'low'


class DiagnosisResponse(BaseModel):
    diagnosis_id: str
    status: str
    phase: str = ''
    quality_passed: bool = False
    report_url: str = ''
    error: str = ''
    cost_usd: float = 0.0
    created_at: str = ''
    finished_at: str = ''


# ---------------------------------------------------------------------------
# 对话式分析（v1.4）
# ---------------------------------------------------------------------------

class ChatSessionResponse(BaseModel):
    session_id: str
    title: str = ''
    created_at: str = ''
    updated_at: str = ''


class ChatMessageResponse(BaseModel):
    message_id: str
    role: str
    content: str
    route: str = ''
    meta: dict = {}
    created_at: str = ''


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ChatAnswerResponse(BaseModel):
    answer: str
    route: str = Field(description='qa（形态A 查数）| agent（形态B LLM循环）| ...')
    meta: dict = {}


class ChatContextResponse(BaseModel):
    data: dict = Field(description='数据上下文：available/files/time_range/stale...')
    agent_quota: dict = Field(description='Agent 对话配额：quota/used/unlimited')


class DiagnosisResultResponse(BaseModel):
    diagnosis_id: str
    status: str
    metrics: list[MetricValue] = []
    footnote: str = ''
    warnings: list[str] = []
    quality_passed: bool = False
    report_url: str = ''
    error: str = ''
    duration_ms: int = 0
    cost_usd: float = 0.0
    trend: list[dict] = Field(default_factory=list,
                              description='按日趋势：{date, net_sales, visitors, pay_cvr}')
    products: list[dict] = Field(default_factory=list,
                                 description='商品维度拆解（v1.2 归因）')
    anomaly_days: list[dict] = Field(default_factory=list,
                                     description='异常日（基线=前7日均值，偏离≥35%）')
    weekly: dict = Field(default_factory=dict,
                         description='周度对比 {weeks, biggest}')
    caliber_compare: dict = Field(
        default_factory=dict,
        description='L1 口径对比：毛利率 财务口径 vs 推广口径')
    caliber: dict = Field(default_factory=dict,
                          description='口径版本快照（registry 版本 + 默认口径）')


# ---------------------------------------------------------------------------
# 额度与订单
# ---------------------------------------------------------------------------

class AccountResponse(BaseModel):
    tenant_id: str
    balance: int
    total_purchased: int
    total_used: int
    trial_available: bool
    cost_used_usd: float
    next_expiry_at: str = ''
    prices: dict[str, int] = {}


class PlanResponse(BaseModel):
    sku: str
    name: str
    price_cents: int
    price_yuan: float
    quantity: int
    per_unit_yuan: float
    badge: str = ''
    desc: str = ''


class CreateOrderRequest(BaseModel):
    sku: str = Field(description='trial | single | pack5')


class OrderResponse(BaseModel):
    order_no: str
    sku: str
    quantity: int
    amount_cents: int
    amount_yuan: float
    status: str
    provider: str
    created_at: str
    paid_at: str = ''
    pay_proof_ref: str = ''      # 付款凭证（P2-19，空 = 未上传）


class LedgerEntry(BaseModel):
    id: str = ''                 # 行主键（P3-22）
    delta: int
    balance_after: int
    reason: str
    note: str = ''
    created_at: str
    expires_at: str = ''


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str = 'ok'
    version: str = ''
    database: dict = {}
    sandbox: dict = {}


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: Any = None
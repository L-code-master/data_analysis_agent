/** 与后端 API 对应的类型定义。 */

export interface UserInfo {
  user_id: string
  tenant_id: string
  email: string
  role: string
  display_name: string
}

export interface TokenInfo {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface AuthResponse {
  user: UserInfo
  token: TokenInfo
}

export interface SourceInfo {
  source_id: string
  name: string
  source_type?: string
  sheet_name?: string
  size_bytes: number
  row_count: number
  status: string
  created_at: string
}

export interface MappingItem {
  source_column: string
  target_field: string
  confidence: number
  confidence_label: string
  needs_review: boolean
  reason: string
  confirmed: boolean
}

export interface ProfileInfo {
  source_id: string
  source_type: string
  display_name: string
  row_count: number
  column_count: number
  columns: string[]
  mappings: MappingItem[]
  missing_required: string[]
  needs_confirmation: boolean
  schema_fingerprint: string
  warnings: string[]
}

export interface MetricValue {
  metric_id: string
  metric_name: string
  value: number | string | null
  unit: string
  formula: string
  uncertainty: 'low' | 'medium' | 'high'
}

export interface DiagnosisInfo {
  diagnosis_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled'
  phase: string
  quality_passed: boolean
  report_url: string
  error: string
  cost_usd: number
  created_at: string
  finished_at: string
}

export interface TrendPoint {
  date: string
  net_sales: number
  visitors: number
  pay_cvr: number | null
}

/** 商品维度拆解（v1.2 归因） */
export interface ProductRow {
  item_id: string
  merchant_code: string
  title: string
  pay_amount: number
  share: number
  net_sales: number
  quantity: number
  refund_amount: number
  refund_rate: number
  gross_margin: number
  margin_rate: number | null
  top_refund_reason: string
  flags: string[]
}

/** 异常日（基线=前7日均值，偏离≥35%） */
export interface AnomalyDay {
  date: string
  features: string[]
  note: string
}

/** 周度对比 */
export interface WeekRow {
  label: string
  days?: number
  pay_amount: number
  net_sales: number
  visitors: number
  pay_cvr: number | null
  refund_rate: number
  ad_spend: number
  roi: number | null
}

export interface WeeklyCompare {
  weeks: WeekRow[]
  biggest: {
    metric: string
    last: number
    base: number
    delta: number
    kind: 'abs' | 'pct'
  } | null
}

/** L1 口径对比：毛利率 财务 vs 推广 */
export interface CaliberCompare {
  label: string
  items: { metric_id: string; name: string; desc: string; value: number; unit: string }[]
  diff: number
  note: string
}

export interface DiagnosisResult extends DiagnosisInfo {
  metrics: MetricValue[]
  footnote: string
  warnings: string[]
  duration_ms: number
  trend?: TrendPoint[]
  products?: ProductRow[]
  anomaly_days?: AnomalyDay[]
  weekly?: WeeklyCompare
  caliber_compare?: CaliberCompare
  caliber?: { registry_version?: string; defaults?: string }
}

export interface AccountInfo {
  tenant_id: string
  balance: number
  total_purchased: number
  total_used: number
  trial_available: boolean
  cost_used_usd: number
  prices: Record<string, number>
}

export interface PlanInfo {
  sku: string
  name: string
  price_cents: number
  price_yuan: number
  quantity: number
  per_unit_yuan: number
  badge: string
  desc: string
}

export interface OrderInfo {
  order_no: string
  sku: string
  quantity: number
  amount_cents: number
  amount_yuan: number
  status: string
  provider: string
  created_at: string
  paid_at: string
}

export interface LedgerEntry {
  delta: number
  balance_after: number
  reason: string
  note: string
  created_at: string
}

/** SSE 进度事件 */
export interface ProgressEvent {
  type: 'ready' | 'progress' | 'done' | 'error' | 'end'
  phase?: string
  message?: string
  code?: string
  result?: DiagnosisResult
  status?: string
}

export const PHASE_LABELS: Record<string, string> = {
  checking: '检查额度与映射',
  loading: '加载语义层',
  importing: '导入数据',
  etl: '执行 ETL',
  quality: '数据质量校验',
  computing: '计算指标',
  reporting: '生成报告',
  done: '完成',
}

export const UNCERTAINTY_LABELS: Record<string, { text: string; color: string }> = {
  low: { text: '已核实', color: 'green' },
  medium: { text: '部分待确认', color: 'orange' },
  high: { text: '关键口径待确认', color: 'red' },
}

export function formatMetricValue(v: number | string | null, unit: string): string {
  if (v === null || v === undefined) return '—'
  const n = typeof v === 'string' ? parseFloat(v) : v
  if (Number.isNaN(n)) return String(v)
  if (unit === 'percent') return `${(n * 100).toFixed(2)}%`
  if (unit === 'currency') return `¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  if (Number.isInteger(n)) return n.toLocaleString('zh-CN')
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
}
/** 对话式分析（v1.4） */
export interface ChatSession {
  session_id: string
  title: string
  created_at: string
  updated_at?: string
}

export interface ChatMessage {
  message_id: string
  role: 'user' | 'assistant'
  content: string
  route: string
  meta: Record<string, unknown>
  created_at: string
}

export interface ChatAnswer {
  answer: string
  route: string
  meta: Record<string, unknown>
}

export interface ChatDataContext {
  available: boolean
  message?: string
  files?: string[]
  file_count?: number
  row_count?: number
  time_range?: string
  diagnosed_at?: string
  stale?: boolean
  newer_uploads?: number
}

export interface ChatContext {
  data: ChatDataContext
  agent_quota: { quota: number; used: number; unlimited: boolean }
}

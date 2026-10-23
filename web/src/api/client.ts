/**
 * API 客户端。
 *
 * 关键设计：
 *  1. Axios 拦截器注入 JWT
 *  2. 401 自动尝试刷新令牌，失败则跳登录
 *  3. 并发 401 只刷新一次（避免刷新风暴）
 *  4. SSE / 报告下载用带 Authorization 头的 fetch 流式实现
 *     （EventSource 无法自定义 header —— docs/16 P0-2/3：
 *      曾经因此前端 SSE 根本过不了鉴权，进度页永久转圈）
 */

import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import type {
  AccountInfo, AuthResponse, DiagnosisInfo, DiagnosisResult, LedgerEntry,
  OrderInfo, PlanInfo, ProfileInfo, ProgressEvent, SourceInfo, UserInfo,
} from './types'

const ACCESS_KEY = 'daa_access_token'        // 旧版残留键（清理用）
const REFRESH_KEY = 'daa_refresh_token'      // 旧版残留键（清理用）
const SESSION_KEY = 'daa_session'            // 仅表示"可能已有 cookie 会话"

// 一次性清理旧版明文令牌（docs/16 P2-16）
localStorage.removeItem(ACCESS_KEY)
localStorage.removeItem(REFRESH_KEY)

// ---------------------------------------------------------------------------
// 令牌存取
// ---------------------------------------------------------------------------
// ★ access token 只放内存（刷新即丢，XSS 也拿不到持久凭证）；
//   refresh token 在 httpOnly cookie，JS 完全不可读（P2-16）。
//   SESSION_KEY 只是一个非敏感标记，用于页面重载后触发 cookie 续期。

let accessToken = ''

export const tokenStore = {
  get access() { return accessToken },
  /** @deprecated refresh 已移入 httpOnly cookie，保留空值兼容旧调用 */
  get refresh() { return '' },
  set(access: string, _refresh?: string) {
    accessToken = access
    localStorage.setItem(SESSION_KEY, '1')
  },
  clear() {
    accessToken = ''
    localStorage.removeItem(SESSION_KEY)
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
  get hasToken() {
    return Boolean(accessToken) || localStorage.getItem(SESSION_KEY) === '1'
  },
}

// ---------------------------------------------------------------------------
// 实例
// ---------------------------------------------------------------------------

export const http: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  withCredentials: true,      // refresh cookie 需要（P2-16）
})

http.interceptors.request.use((cfg: InternalAxiosRequestConfig) => {
  const t = tokenStore.access
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

// 刷新并发控制
let refreshing: Promise<string> | null = null

async function doRefresh(): Promise<string> {
  // refresh token 在 httpOnly cookie 中，这里只发空 body + credentials
  // 用裸 axios，避免拦截器递归
  const { data } = await axios.post('/api/v1/auth/refresh', {},
                                    { withCredentials: true })
  tokenStore.set(data.access_token)
  return data.access_token
}

// 认证端点自身的 401 = 密码错误/未注册等业务结果，不是会话过期
// （docs/16 P2-31）—— 不能触发"刷新令牌 + 跳登录页"，否则输错一次
// 密码就被无谓地重定向。
const AUTH_ENDPOINTS = ['/auth/login', '/auth/register', '/auth/verify-email',
  '/auth/refresh', '/auth/resend-verification']

http.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as InternalAxiosRequestConfig & { _retried?: boolean }
    const isAuthEndpoint = AUTH_ENDPOINTS.some((p) => original?.url?.includes(p))

    if (error.response?.status === 401 && original && !original._retried
        && !isAuthEndpoint) {
      original._retried = true
      try {
        // ★ 并发 401 只刷新一次
        refreshing = refreshing || doRefresh()
        const newToken = await refreshing
        refreshing = null
        original.headers.Authorization = `Bearer ${newToken}`
        return http(original)
      } catch {
        refreshing = null
        tokenStore.clear()
        if (!location.pathname.startsWith('/login')) {
          location.href = '/login'
        }
      }
    }
    return Promise.reject(error)
  },
)

/** 从错误对象提取后端返回的 message。 */
export function errMsg(e: unknown): string {
  const ax = e as AxiosError<any>
  const d = ax?.response?.data
  if (d?.message) return d.message
  if (typeof d === 'string') return d
  if (ax?.message) return ax.message
  return '请求失败'
}

// ---------------------------------------------------------------------------
// 带 JWT 的原生 fetch（SSE / 报告下载用；axios 拦截器管不到原生 fetch）
// ---------------------------------------------------------------------------

async function authFetch(path: string, init?: RequestInit): Promise<Response> {
  const req = (token: string) => fetch(path, {
    ...init,
    credentials: 'include',     // cookie 会话（P2-16）
    headers: { ...(init?.headers || {}), Authorization: `Bearer ${token}` },
  })
  let resp = await req(tokenStore.access)
  if (resp.status === 401 && tokenStore.hasToken) {
    refreshing = refreshing || doRefresh()
    try {
      const token = await refreshing
      refreshing = null
      resp = await req(token)
    } catch {
      refreshing = null
      tokenStore.clear()
      if (!location.pathname.startsWith('/login')) location.href = '/login'
    }
  }
  return resp
}

// ---------------------------------------------------------------------------
// 认证
// ---------------------------------------------------------------------------

export const authApi = {
  async register(email: string, password: string, tenantName: string) {
    const { data } = await http.post<AuthResponse>('/auth/register', {
      email, password, tenant_name: tenantName,
    })
    tokenStore.set(data.token.access_token, data.token.refresh_token)
    return data
  },
  async login(email: string, password: string) {
    const { data } = await http.post<AuthResponse>('/auth/login', { email, password })
    tokenStore.set(data.token.access_token, data.token.refresh_token)
    return data
  },
  async me() {
    const { data } = await http.get<UserInfo>('/auth/me')
    return data
  },
  async logout() {
    try { await http.post('/auth/logout') } finally { tokenStore.clear() }
  },
  async changePassword(oldPassword: string, newPassword: string) {
    const { data } = await http.post('/auth/change-password', {
      old_password: oldPassword, new_password: newPassword,
    })
    tokenStore.clear()
    return data
  },
  /** 邮箱验证（验证成功后发放注册赠送额度） */
  async verifyEmail(token: string) {
    const { data } = await http.post<{ message: string; user: UserInfo }>(
      '/auth/verify-email', { token })
    return data
  },
  async resendVerification() {
    const { data } = await http.post('/auth/resend-verification')
    return data
  },
}

// ---------------------------------------------------------------------------
// 数据源
// ---------------------------------------------------------------------------

export const sourceApi = {
  /**
   * 上传文件。★ 多工作表 Excel 会被自动拆分为多个数据源（返回数组）。
   * 大文件不走全局 60s 超时（docs/16 P2-32），并支持上传进度回调。
   */
  async upload(file: File, onProgress?: (percent: number) => void) {
    const fd = new FormData()
    fd.append('file', file)
    const { data } = await http.post<SourceInfo[]>('/sources', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,                       // 上传不限时（原受 60s 全局超时威胁）
      onUploadProgress: onProgress
        ? (e) => {
            if (e.total) onProgress(Math.round((e.loaded / e.total) * 100))
          }
        : undefined,
    })
    return data
  },
  async list(limit = 50) {
    const { data } = await http.get<SourceInfo[]>('/sources', { params: { limit } })
    return data
  },
  async profile(sourceId: string) {
    const { data } = await http.get<ProfileInfo>(`/sources/${sourceId}/profile`)
    return data
  },
  async confirmMapping(sourceId: string, mapping: Record<string, string>) {
    const { data } = await http.post(`/sources/${sourceId}/mapping`, { mapping })
    return data
  },
}

// ---------------------------------------------------------------------------
// 诊断
// ---------------------------------------------------------------------------

export const diagnosisApi = {
  async start(sourceIds: string[], timeRange: string) {
    const { data } = await http.post<DiagnosisInfo>('/diagnoses', {
      source_ids: sourceIds, time_range: timeRange,
    })
    return data
  },
  async list(limit = 50) {
    const { data } = await http.get<DiagnosisInfo[]>('/diagnoses', { params: { limit } })
    return data
  },
  async get(diagnosisId: string) {
    const { data } = await http.get<DiagnosisResult>(`/diagnoses/${diagnosisId}`)
    return data
  },
  /**
   * 在新标签页打开 HTML 报告（打印 / 导出 PDF）。
   *
   * ★ 带 Authorization 头 fetch 后用 blob URL 打开 —— 浏览器直接
   *   window.open 无法携带 JWT，后端报告接口需要鉴权（docs/16 P0-3）。
   */
  async openReport(diagnosisId: string) {
    const resp = await authFetch(`/api/v1/diagnoses/${diagnosisId}/report`)
    if (!resp.ok) throw new Error(`报告加载失败（HTTP ${resp.status}）`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const w = window.open(url, '_blank')
    if (w) {
      // 给新窗口留出加载时间再回收
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } else {
      URL.revokeObjectURL(url)
      throw new Error('弹出窗口被浏览器拦截，请允许弹窗后重试')
    }
  },
  /**
   * 订阅 SSE 进度（fetch 流式解析，可携带 Authorization 头）。
   *
   * - 收到 done / error 终态事件后自动停止，不再重连
   * - 网络中断自动重连（最多 3 次，间隔 1.5s）
   * - 401 先尝试刷新令牌再重试；403/404 等视为致命错误直接上报
   *
   * @returns 取消订阅函数
   */
  subscribe(
    diagnosisId: string,
    onEvent: (ev: ProgressEvent) => void,
    onError?: (e: { fatal: boolean; status?: number; message: string }) => void,
  ): () => void {
    const ctrl = new AbortController()
    let cancelled = false
    let finished = false   // 已收到终态（done/error）

    const handle = (ev: ProgressEvent) => {
      if (ev.type === 'done' || ev.type === 'error') finished = true
      onEvent(ev)
    }

    // 从缓冲区切出完整 SSE 帧，逐行取 data: 载荷
    const consumeFrames = (buf: string): string => {
      const frames = buf.split('\n\n')
      const rest = frames.pop() ?? ''
      for (const frame of frames) {
        for (const line of frame.split('\n')) {
          if (!line.startsWith('data: ')) continue
          try { handle(JSON.parse(line.slice(6)) as ProgressEvent) } catch { /* 非 JSON 忽略 */ }
        }
      }
      return rest
    }

    const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

    async function connect(retriesLeft: number): Promise<void> {
      if (cancelled || finished) return
      try {
        const resp = await authFetch(
          `/api/v1/diagnoses/${diagnosisId}/events`,
          { headers: { Accept: 'text/event-stream' }, signal: ctrl.signal },
        )
        if (!resp.ok || !resp.body) {
          // 403（越权）/ 404（不存在）等：重试无意义，直接报致命错误
          onError?.({
            fatal: true, status: resp.status,
            message: resp.status === 404 ? '诊断任务不存在' : `进度连接失败（HTTP ${resp.status}）`,
          })
          return
        }
        const reader = resp.body.getReader()
        const dec = new TextDecoder()
        let buf = ''
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buf = consumeFrames(buf + dec.decode(value, { stream: true }))
          if (finished || cancelled) { ctrl.abort(); return }
        }
        // 流被服务端/网络断开但还没到终态 → 重连
        if (!cancelled && !finished) {
          if (retriesLeft > 0) {
            await sleep(1500)
            return connect(retriesLeft - 1)
          }
          onError?.({ fatal: false, message: '进度连接中断，请点击"刷新状态"重试' })
        }
      } catch {
        if (cancelled) return
        if (retriesLeft > 0 && !finished) {
          await sleep(1500)
          return connect(retriesLeft - 1)
        }
        if (!finished) {
          onError?.({ fatal: false, message: '网络中断，请点击"刷新状态"重试' })
        }
      }
    }

    void connect(3)
    return () => { cancelled = true; ctrl.abort() }
  },
}

// ---------------------------------------------------------------------------
// 计费
// ---------------------------------------------------------------------------

export const billingApi = {
  async plans() {
    const { data } = await http.get<PlanInfo[]>('/billing/plans')
    return data
  },
  async account() {
    const { data } = await http.get<AccountInfo>('/billing/account')
    return data
  },
  async orders(limit = 50) {
    const { data } = await http.get<OrderInfo[]>('/billing/orders', { params: { limit } })
    return data
  },
  async createOrder(sku: string) {
    const { data } = await http.post<OrderInfo>('/billing/orders', { sku })
    return data
  },
  async cancelOrder(orderNo: string) {
    const { data } = await http.post<OrderInfo>(`/billing/orders/${orderNo}/cancel`)
    return data
  },
  /** 上传付款凭证（转账截图/PDF，≤5MB），供平台管理员核销核对（P2-19） */
  async attachProof(orderNo: string, file: File) {
    const fd = new FormData()
    fd.append('file', file)
    const { data } = await http.post<{ order_no: string; pay_proof_ref: string }>(
      `/billing/orders/${orderNo}/proof`, fd,
      { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 0 })
    return data
  },
  /** 带 JWT 下载付款凭证 blob（订单归属方/管理员） */
  async openProof(orderNo: string) {
    const resp = await authFetch(`/api/v1/billing/orders/${orderNo}/proof`)
    if (!resp.ok) throw new Error(`凭证加载失败（HTTP ${resp.status}）`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const w = window.open(url, '_blank')
    if (w) setTimeout(() => URL.revokeObjectURL(url), 60_000)
    else URL.revokeObjectURL(url)
  },
  async confirmOrder(orderNo: string) {
    const { data } = await http.post<AccountInfo>(`/billing/orders/${orderNo}/confirm`)
    return data
  },
  async ledger(limit = 100) {
    const { data } = await http.get<LedgerEntry[]>('/billing/ledger', { params: { limit } })
    return data
  },
}

export const healthApi = {
  async check() {
    const { data } = await http.get('/health')
    return data
  },
  /** 运维详情（DB 路径/表/沙箱），仅平台管理员（docs/16 P2-13） */
  async detail() {
    const { data } = await http.get('/admin/health')
    return data
  },
}
// ---------------------------------------------------------------------------
// 对话式分析（v1.4：形态 A 查数 + 形态 B Agent 归因）
// ---------------------------------------------------------------------------

export const chatApi = {
  async createSession(title = '') {
    const { data } = await http.post<import('./types').ChatSession>(
      '/chat/sessions', null, { params: { title } })
    return data
  },
  async sessions(limit = 30) {
    const { data } = await http.get<import('./types').ChatSession[]>(
      '/chat/sessions', { params: { limit } })
    return data
  },
  async messages(sessionId: string) {
    const { data } = await http.get<import('./types').ChatMessage[]>(
      `/chat/sessions/${sessionId}/messages`)
    return data
  },
  async ask(sessionId: string, question: string) {
    // 问题走 JSON body，不进 URL（docs/16 P2-18）；Agent 循环最长 5 分钟
    const { data } = await http.post<import('./types').ChatAnswer>(
      `/chat/sessions/${sessionId}/ask`, { question },
      { timeout: 180_000 })
    return data
  },
}

export const chatContextApi = {
  /** 数据上下文（对话基于哪份数据 + Agent 配额余量） */
  async get() {
    const { data } = await http.get<import('./types').ChatContext>('/chat/context')
    return data
  },
}

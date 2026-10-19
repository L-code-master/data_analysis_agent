/**
 * API 客户端。
 *
 * 关键设计：
 *  1. Axios 拦截器注入 JWT
 *  2. 401 自动尝试刷新令牌，失败则跳登录
 *  3. 并发 401 只刷新一次（避免刷新风暴）
 *  4. SSE 用原生 EventSource（不走 axios）
 */

import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import type {
  AccountInfo, AuthResponse, DiagnosisInfo, DiagnosisResult, LedgerEntry,
  OrderInfo, PlanInfo, ProfileInfo, SourceInfo, UserInfo,
} from './types'

const ACCESS_KEY = 'daa_access_token'
const REFRESH_KEY = 'daa_refresh_token'

// ---------------------------------------------------------------------------
// 令牌存取
// ---------------------------------------------------------------------------

export const tokenStore = {
  get access() { return localStorage.getItem(ACCESS_KEY) || '' },
  get refresh() { return localStorage.getItem(REFRESH_KEY) || '' },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_KEY, access)
    localStorage.setItem(REFRESH_KEY, refresh)
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
  get hasToken() { return Boolean(localStorage.getItem(ACCESS_KEY)) },
}

// ---------------------------------------------------------------------------
// 实例
// ---------------------------------------------------------------------------

export const http: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
})

http.interceptors.request.use((cfg: InternalAxiosRequestConfig) => {
  const t = tokenStore.access
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

// 刷新并发控制
let refreshing: Promise<string> | null = null

async function doRefresh(): Promise<string> {
  const rt = tokenStore.refresh
  if (!rt) throw new Error('no refresh token')
  // 用裸 axios，避免拦截器递归
  const { data } = await axios.post('/api/v1/auth/refresh', { refresh_token: rt })
  tokenStore.set(data.access_token, data.refresh_token)
  return data.access_token
}

http.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as InternalAxiosRequestConfig & { _retried?: boolean }

    if (error.response?.status === 401 && original && !original._retried) {
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
}

// ---------------------------------------------------------------------------
// 数据源
// ---------------------------------------------------------------------------

export const sourceApi = {
  /** 上传文件。★ 多工作表 Excel 会被自动拆分为多个数据源（返回数组）。 */
  async upload(file: File) {
    const fd = new FormData()
    fd.append('file', file)
    const { data } = await http.post<SourceInfo[]>('/sources', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
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
  reportUrl(diagnosisId: string) {
    return `/api/v1/diagnoses/${diagnosisId}/report`
  },
  /**
   * 订阅 SSE 进度。
   *
   * ⚠️ 用原生 EventSource —— 它无法自定义 header，
   *    因此后端 SSE 也接受 ?token= 查询参数（见下方实现说明）。
   *    当前实现：token 放在 query，由后端 deps 兼容读取。
   */
  subscribe(
    diagnosisId: string,
    onEvent: (ev: any) => void,
    onError?: (e: Event) => void,
  ): () => void {
    const url = `/api/v1/diagnoses/${diagnosisId}/events`
    const es = new EventSource(url)

    es.onmessage = (m) => {
      try { onEvent(JSON.parse(m.data)) } catch { /* 忽略非 JSON */ }
    }
    es.onerror = (e) => { onError?.(e) }
    return () => es.close()
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
    const { data } = await http.post<import('./types').ChatAnswer>(
      `/chat/sessions/${sessionId}/ask`, null, { params: { question } })
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

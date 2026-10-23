/**
 * 认证状态。
 *
 * 用一个轻量的 React Context 实现，不引入额外状态库 ——
 * 这里只有「当前用户」一份状态，Context 足够且少一个依赖。
 */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from 'react'
import { authApi, tokenStore } from '../api/client'
import type { AuthResponse, UserInfo } from '../api/types'

interface AuthContextValue {
  user: UserInfo | null
  loading: boolean
  initialized: boolean
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string,
             tenantName: string) => Promise<AuthResponse>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

// ★ P3-25：模块级去重 —— dev StrictMode 双挂载会让初始化 effect 跑两次，
//   原先每次都发 /auth/me。复用同一 Promise 只发一次。
let _initPromise: Promise<void> | null = null

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserInfo | null>(null)
  const [loading, setLoading] = useState(false)
  const [initialized, setInitialized] = useState(false)

  const refresh = useCallback(async () => {
    if (!tokenStore.hasToken) {
      setUser(null)
      setInitialized(true)
      return
    }
    setLoading(true)
    // ★ 401 才清 token；网络瞬断保留 token 并重试一次（docs/16 P1-19）
    const load = async (retry: boolean): Promise<void> => {
      try {
        setUser(await authApi.me())
      } catch (e) {
        const status = (e as { response?: { status?: number } })?.response?.status
        if (status === 401) {
          tokenStore.clear()
          setUser(null)
        } else if (retry) {
          await new Promise((r) => setTimeout(r, 1200))
          return load(false)
        } else {
          setUser(null)          // 保留 token，网络恢复后刷新即可
        }
      }
    }
    try {
      await load(true)
    } finally {
      setLoading(false)
      setInitialized(true)
    }
  }, [])

  useEffect(() => {
    if (!initialized) {
      _initPromise = _initPromise ?? refresh()
      void _initPromise
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    setLoading(true)
    try {
      const res = await authApi.login(email, password)
      setUser(res.user)
      setInitialized(true)
    } finally {
      setLoading(false)
    }
  }, [])

  const register = useCallback(async (email: string, password: string,
                                     tenantName: string) => {
    setLoading(true)
    try {
      const res = await authApi.register(email, password, tenantName)
      setUser(res.user)
      setInitialized(true)
      return res
    } finally {
      setLoading(false)
    }
  }, [])

  const logout = useCallback(async () => {
    // ★ 本地登出必须成功：接口失败（网络/401）不能把用户卡在登录态
    try {
      await authApi.logout()
    } catch {
      /* tokenStore 已在 authApi.logout 的 finally 中清空 */
    } finally {
      setUser(null)
    }
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ user, loading, initialized, login, register, logout, refresh }),
    [user, loading, initialized, login, register, logout, refresh],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth 必须在 <AuthProvider> 内使用')
  return ctx
}
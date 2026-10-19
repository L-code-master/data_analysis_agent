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
import type { UserInfo } from '../api/types'

interface AuthContextValue {
  user: UserInfo | null
  loading: boolean
  initialized: boolean
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string, tenantName: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

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
    try {
      setUser(await authApi.me())
    } catch {
      tokenStore.clear()
      setUser(null)
    } finally {
      setLoading(false)
      setInitialized(true)
    }
  }, [])

  useEffect(() => {
    if (!initialized) void refresh()
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
    } finally {
      setLoading(false)
    }
  }, [])

  const logout = useCallback(async () => {
    await authApi.logout()
    setUser(null)
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
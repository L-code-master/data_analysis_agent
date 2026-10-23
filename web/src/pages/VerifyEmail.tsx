/**
 * 邮箱验证落地页（docs/16 P1-7）。
 *
 * 验证邮件里的链接形如 /verify-email?token=...
 * 页面加载后 POST /auth/verify-email，成功后刷新当前用户状态。
 * 后端对「已验证用户重复提交同一 token」是幂等的，
 * 因此 StrictMode 双请求 / 刷新页面都不会误报。
 */
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Button, Card, Result, Spin } from 'antd'
import { useAuth } from '../stores/auth'
import { authApi, errMsg } from '../api/client'

type State = 'loading' | 'ok' | 'error'

export default function VerifyEmailPage() {
  const [params] = useSearchParams()
  const token = params.get('token') || ''
  const { user, refresh } = useAuth()
  const [state, setState] = useState<State>('loading')
  const [message, setMessage] = useState('')

  useEffect(() => {
    let cancelled = false
    if (!token) {
      setState('error')
      setMessage('验证链接缺少 token，请从邮件中重新打开')
      return
    }
    authApi.verifyEmail(token)
      .then(async () => {
        await refresh()
        if (!cancelled) setState('ok')
      })
      .catch((e) => {
        if (!cancelled) {
          setState('error')
          setMessage(errMsg(e))
        }
      })
    return () => { cancelled = true }
    // refresh 稳定；token 变化才需要重跑
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  const target = user ? '/dashboard' : '/login'

  return (
    <div className="auth-wrap">
      <Card className="auth-card" variant="borderless">
        {state === 'loading' && (
          <div style={{ textAlign: 'center', padding: '48px 0' }}>
            <Spin size="large" />
            <div style={{ marginTop: 16, color: '#8c959f' }}>正在验证邮箱…</div>
          </div>
        )}
        {state === 'ok' && (
          <Result
            status="success"
            title="邮箱验证成功"
            subTitle="注册赠送的诊断次数已发放，现在可以上传数据开始诊断了。"
            extra={(
              <Link to={target}>
                <Button type="primary">{user ? '进入工作台' : '去登录'}</Button>
              </Link>
            )}
          />
        )}
        {state === 'error' && (
          <Result
            status="warning"
            title="验证失败"
            subTitle={message || '验证链接无效或已过期'}
            extra={(
              <Link to={target}>
                <Button type="primary">
                  {user ? '进入设置页重新发送' : '去登录'}
                </Button>
              </Link>
            )}
          />
        )}
      </Card>
    </div>
  )
}
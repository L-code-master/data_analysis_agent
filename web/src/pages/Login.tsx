import { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { Button, Card, Form, Input, Typography, App as AntApp } from 'antd'
import { LockOutlined, MailOutlined } from '@ant-design/icons'
import { useAuth } from '../stores/auth'
import { errMsg } from '../api/client'

export default function LoginPage() {
  const { login, loading } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const { message } = AntApp.useApp()
  const [form] = Form.useForm()

  const onFinish = async (v: { email: string; password: string }) => {
    try {
      await login(v.email, v.password)
      message.success('登录成功')
      // ★ P3-20：回到登录前想访问的页面（RequireAuth 存在 state.from），
      //   原先记录后弃用一律跳工作台
      const from = (location.state as { from?: string } | null)?.from
      navigate(from && /^\/[^/]/.test(from) ? from : '/dashboard',
              { replace: true })
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  return (
    <div className="auth-wrap">
      <Card className="auth-card" variant="borderless">
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <div className="brand" style={{ fontSize: 24 }}>经营诊断</div>
          <Typography.Text type="secondary">
            淘宝店铺数据分析 · 告诉你为什么涨、为什么跌
          </Typography.Text>
        </div>

        <Form form={form} layout="vertical" onFinish={onFinish} size="large">
          <Form.Item name="email" label="邮箱"
                     rules={[{ required: true, message: '请输入邮箱' },
                             { type: 'email', message: '邮箱格式不正确' }]}>
            <Input prefix={<MailOutlined />} placeholder="you@shop.com"
                   autoComplete="username" />
          </Form.Item>

          <Form.Item name="password" label="密码"
                     rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="密码"
                            autoComplete="current-password" />
          </Form.Item>

          <Button type="primary" htmlType="submit" block loading={loading}
                  style={{ height: 44 }}>
            登录
          </Button>
        </Form>

        <div style={{ textAlign: 'center', marginTop: 20 }}>
          <Typography.Text type="secondary">还没有账号？</Typography.Text>
          <Link to="/register" style={{ marginLeft: 6 }}>立即注册</Link>
        </div>
      </Card>
    </div>
  )
}
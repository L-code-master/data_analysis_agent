import { Link, useNavigate } from 'react-router-dom'
import { Button, Card, Form, Input, Typography, App as AntApp } from 'antd'
import { LockOutlined, MailOutlined, ShopOutlined } from '@ant-design/icons'
import { useAuth } from '../stores/auth'
import { errMsg } from '../api/client'

/** ★ P3-21：与后端 validate_password_strength 同口径 ——
 * 至少 10 位，且 小写/大写/数字/符号 四类中至少两类。 */
const twoKinds = (v: string) =>
  (/[a-z]/.test(v) ? 1 : 0) + (/[A-Z]/.test(v) ? 1 : 0)
  + (/\d/.test(v) ? 1 : 0) + (/[^A-Za-z0-9]/.test(v) ? 1 : 0)

export default function RegisterPage() {
  const { register, loading } = useAuth()
  const navigate = useNavigate()
  const { message } = AntApp.useApp()

  const onFinish = async (v: {
    email: string; password: string; confirm: string; tenant_name: string,
  }) => {
    try {
      const res = await register(v.email, v.password, v.tenant_name)
      if (res.verification_required && !res.user.email_verified) {
        if (res.verification_sent) {
          message.success('注册成功，验证邮件已发送，请查收（验证后赠送次数到账）')
        } else {
          message.warning('注册成功，但验证邮件发送失败，请在「设置」页重新发送')
        }
      } else {
        message.success('注册成功，已自动登录')
      }
      navigate('/dashboard', { replace: true })
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  return (
    <div className="auth-wrap">
      <Card className="auth-card" variant="borderless">
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <div className="brand" style={{ fontSize: 24 }}>创建账号</div>
          <Typography.Text type="secondary">
            注册后即可上传店铺报表，开始经营诊断
          </Typography.Text>
        </div>

        <Form layout="vertical" onFinish={onFinish} size="large">
          <Form.Item name="tenant_name" label="店铺名称"
                     rules={[{ required: true, message: '请输入店铺名称' }]}>
            <Input prefix={<ShopOutlined />} placeholder="如：XX旗舰店" />
          </Form.Item>

          <Form.Item name="email" label="邮箱"
                     rules={[{ required: true, message: '请输入邮箱' },
                             { type: 'email', message: '邮箱格式不正确' }]}>
            <Input prefix={<MailOutlined />} placeholder="you@shop.com"
                   autoComplete="username" />
          </Form.Item>

          <Form.Item name="password" label="密码"
                     extra="至少 10 位，且小写字母 / 大写字母 / 数字 / 符号中至少两类"
                     rules={[{ required: true, message: '请输入密码' },
                             { min: 10, message: '密码至少 10 位' },
                             { validator: (_, v: string) =>
                                 !v || twoKinds(v) >= 2
                                   ? Promise.resolve()
                                   : Promise.reject(new Error(
                                       '需包含小写/大写/数字/符号中的至少两类')) }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="设置密码"
                            autoComplete="new-password" />
          </Form.Item>

          <Form.Item name="confirm" label="确认密码" dependencies={['password']}
                     rules={[{ required: true, message: '请再次输入密码' },
                             ({ getFieldValue }) => ({
                               validator(_, v: string) {
                                 return !v || v === getFieldValue('password')
                                   ? Promise.resolve()
                                   : Promise.reject(new Error('两次输入的密码不一致'))
                               },
                             })]}>
            <Input.Password prefix={<LockOutlined />} placeholder="再输入一次"
                            autoComplete="new-password" />
          </Form.Item>

          <Button type="primary" htmlType="submit" block loading={loading}
                  style={{ height: 44, marginTop: 8 }}>
            注册
          </Button>
        </Form>

        <div style={{ textAlign: 'center', marginTop: 20 }}>
          <Typography.Text type="secondary">已有账号？</Typography.Text>
          <Link to="/login" style={{ marginLeft: 6 }}>去登录</Link>
        </div>
      </Card>
    </div>
  )
}
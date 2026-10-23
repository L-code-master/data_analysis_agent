import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, Button, Card, Descriptions, Form, Input, Space, Tag, Typography,
  App as AntApp,
} from 'antd'
import { useAuth } from '../stores/auth'
import { authApi, billingApi, errMsg, healthApi } from '../api/client'
import type { AccountInfo } from '../api/types'

export default function SettingsPage() {
  const { user, logout, refresh } = useAuth()
  const navigate = useNavigate()
  const { message } = AntApp.useApp()
  const [form] = Form.useForm()
  const [account, setAccount] = useState<AccountInfo | null>(null)
  const [health, setHealth] = useState<any>(null)
  const [healthDetail, setHealthDetail] = useState<any>(null)
  const [saving, setSaving] = useState(false)
  const [resending, setResending] = useState(false)
  const [loadError, setLoadError] = useState('')

  const loadAccount = () => {
    // ★ P2-33：账户信息加载失败不再被吞掉（否则"剩余次数"永远显示 —）
    billingApi.account().then((a) => {
      setAccount(a)
      setLoadError('')
    }).catch((e) => setLoadError(errMsg(e)))
  }

  useEffect(() => {
    loadAccount()
    healthApi.check().then(setHealth).catch(() => setHealth(null))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (user?.role === 'admin') {
      healthApi.detail().then(setHealthDetail).catch(() => {})
    }
  }, [user?.role])

  const changePassword = async (v: { old_password: string; new_password: string }) => {
    setSaving(true)
    try {
      await authApi.changePassword(v.old_password, v.new_password)
      message.success('密码已修改，请重新登录')
      form.resetFields()
      setTimeout(() => { logout().finally(() => navigate('/login')) }, 800)
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  const resendVerification = async () => {
    setResending(true)
    try {
      await authApi.resendVerification()
      message.success('验证邮件已重新发送，请查收')
      await refresh()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setResending(false)
    }
  }

  return (
    <div className="page-narrow">
      {user && user.email_verified === false && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="邮箱尚未验证"
          description={(
            <Space direction="vertical" size={8}>
              <span>完成邮箱验证后才会发放注册赠送的诊断次数。</span>
              <Button size="small" type="primary" loading={resending}
                      onClick={resendVerification}>
                重新发送验证邮件
              </Button>
            </Space>
          )}
        />
      )}

      <Card variant="borderless" title="账户信息">
        {loadError && (
          <Alert
            type="error" showIcon closable style={{ marginBottom: 12 }}
            message="账户信息加载失败"
            description={(
              <Space>
                <span>{loadError}</span>
                <Button size="small" onClick={loadAccount}>重试</Button>
              </Space>
            )}
          />
        )}
        <Descriptions column={1} size="small">
          <Descriptions.Item label="邮箱">
            {user?.email}
            {user?.email_verified === false
              ? <Tag color="orange" style={{ marginLeft: 8 }}>未验证</Tag>
              : <Tag color="green" style={{ marginLeft: 8 }}>已验证</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="店铺">{user?.display_name || '—'}</Descriptions.Item>
          <Descriptions.Item label="角色">
            <Tag color={user?.role === 'owner' ? 'green' : 'default'}>
              {user?.role === 'owner' ? '管理员' : user?.role}
            </Tag>
          </Descriptions.Item>
          <Descriptions.Item label="租户 ID">
            <code style={{ fontSize: 12 }}>{user?.tenant_id}</code>
          </Descriptions.Item>
          <Descriptions.Item label="剩余次数">
            {account?.balance ?? '—'} 次
          </Descriptions.Item>
          <Descriptions.Item label="算力成本">
            ${account?.cost_used_usd?.toFixed(4) ?? '—'}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Card variant="borderless" title="修改密码" style={{ marginTop: 16 }}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="修改密码后所有设备将需要重新登录"
        />
        <Form form={form} layout="vertical" onFinish={changePassword}>
          <Form.Item name="old_password" label="当前密码"
                     rules={[{ required: true, message: '请输入当前密码' }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="new_password" label="新密码"
                     extra="至少 10 位，需包含字母、数字、符号中的至少两类"
                     rules={[{ required: true, message: '请输入新密码' },
                             { min: 10, message: '密码至少 10 位' }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={saving}>
            修改密码
          </Button>
        </Form>
      </Card>

      <Card variant="borderless" title="系统状态" style={{ marginTop: 16 }}>
        {health ? (
          <Descriptions column={1} size="small">
            <Descriptions.Item label="服务状态">
              <Tag color={health.status === 'ok' ? 'green' : 'orange'}>
                {health.status}
              </Tag>
              <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
                v{health.version}
              </Typography.Text>
            </Descriptions.Item>
            {healthDetail ? (
              <>
                <Descriptions.Item label="数据库">
                  {healthDetail.database?.tables ?? '—'} 张表 ·
                  WAL={String(healthDetail.database?.journal_mode ?? '—')}
                </Descriptions.Item>
                <Descriptions.Item label="沙箱网络隔离">
                  {healthDetail.sandbox?.network_isolated
                    ? <Tag color="green">已隔离</Tag>
                    : <Tag color="red">未隔离</Tag>}
                </Descriptions.Item>
              </>
            ) : (
              <Descriptions.Item label="运维详情">
                <Typography.Text type="secondary">
                  仅平台管理员可见
                </Typography.Text>
              </Descriptions.Item>
            )}
          </Descriptions>
        ) : (
          <Typography.Text type="secondary">加载中…</Typography.Text>
        )}
      </Card>

      <Card variant="borderless" title="数据与隐私" style={{ marginTop: 16 }}>
        <Space direction="vertical" size={8}>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            · 上传的店铺数据仅存放在你的租户目录内，<strong>不会跨租户共享</strong>
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            · 分析过程默认<strong>不联网</strong>，数据不会外发
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            · 所有额度变动都有流水记录，可随时核对
          </Typography.Text>
        </Space>
      </Card>
    </div>
  )
}
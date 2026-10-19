import { useEffect, useState } from 'react'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Badge, Button, Layout, Menu, Tag, Typography, App as AntApp } from 'antd'
import {
  CloudUploadOutlined, DashboardOutlined, FileTextOutlined,
  LogoutOutlined, MessageOutlined, PayCircleOutlined, SettingOutlined,
} from '@ant-design/icons'
import { useAuth } from '../stores/auth'
import { billingApi } from '../api/client'

const { Header, Sider, Content } = Layout

export default function AppLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const { message } = AntApp.useApp()
  const [balance, setBalance] = useState<number | null>(null)

  const loadBalance = () => {
    billingApi.account()
      .then((a) => setBalance(a.balance))
      .catch(() => setBalance(null))
  }

  useEffect(() => {
    loadBalance()
    // 额度变化后刷新（简单起见按时轮询）
    const t = setInterval(loadBalance, 15000)
    return () => clearInterval(t)
  }, [location.pathname])

  const selectedKey = (() => {
    const p = location.pathname
    if (p.startsWith('/upload') || p.startsWith('/mapping')) return 'upload'
    if (p.startsWith('/progress') || p.startsWith('/report')) return 'diagnoses'
    if (p.startsWith('/chat')) return 'chat'
    if (p.startsWith('/billing')) return 'billing'
    if (p.startsWith('/settings')) return 'settings'
    return 'dashboard'
  })()

  const items = [
    { key: 'dashboard', icon: <DashboardOutlined />, label: <Link to="/dashboard">工作台</Link> },
    { key: 'upload', icon: <CloudUploadOutlined />, label: <Link to="/upload">上传数据</Link> },
    { key: 'diagnoses', icon: <FileTextOutlined />, label: <Link to="/dashboard">诊断记录</Link> },
    { key: 'chat', icon: <MessageOutlined />, label: <Link to="/chat">对话分析</Link> },
  { key: 'billing', icon: <PayCircleOutlined />, label: <Link to="/billing">购买次数</Link> },
    { key: 'settings', icon: <SettingOutlined />, label: <Link to="/settings">账户设置</Link> },
  ]

  const onLogout = async () => {
    await logout()
    message.success('已登出')
    navigate('/login')
  }

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="light" width={212} style={{ borderRight: '1px solid #eee' }}>
        <div style={{ padding: '20px 20px 12px' }}>
          <div className="brand">经营诊断</div>
          <div style={{ fontSize: 12, color: '#8c959f', marginTop: 4 }}>
            淘宝店铺数据分析
          </div>
        </div>
        <Menu mode="inline" selectedKeys={[selectedKey]} items={items}
              style={{ borderInlineEnd: 'none' }} />
      </Sider>

      <Layout>
        <Header style={{
          background: '#fff', borderBottom: '1px solid #eee',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          paddingInline: 24, height: 60,
        }}>
          <Typography.Text type="secondary">
            {user?.display_name || user?.email}
          </Typography.Text>

          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            {user?.role === 'admin' ? (
              <Tag color="purple"
                   style={{ fontSize: 13, padding: '2px 10px' }}>
                管理员 · 不限次
              </Tag>
            ) : (
              <Link to="/billing">
                <Badge
                  count={balance === 0 ? '需购买' : null}
                  style={{ backgroundColor: '#faad14' }}
                >
                  <Tag color={balance && balance > 0 ? 'green' : 'default'}
                       style={{ fontSize: 13, padding: '2px 10px', cursor: 'pointer' }}>
                    剩余 {balance === null ? '—' : balance} 次
                  </Tag>
                </Badge>
              </Link>
            )}
            <Button type="text" icon={<LogoutOutlined />} onClick={onLogout}>
              登出
            </Button>
          </div>
        </Header>

        <Content>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
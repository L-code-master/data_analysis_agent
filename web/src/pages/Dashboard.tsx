import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Alert, Button, Card, Col, Empty, Row, Space, Statistic, Table, Tag,
  Typography, App as AntApp,
} from 'antd'
import { CloudUploadOutlined, FileTextOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { billingApi, diagnosisApi, errMsg, sourceApi } from '../api/client'
import type { AccountInfo, DiagnosisInfo, SourceInfo } from '../api/types'

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  queued: { color: 'default', text: '排队中' },
  running: { color: 'processing', text: '诊断中' },
  succeeded: { color: 'success', text: '已完成' },
  failed: { color: 'error', text: '失败' },
  canceled: { color: 'default', text: '已取消' },
}

export default function DashboardPage() {
  const navigate = useNavigate()
  const { message } = AntApp.useApp()
  const [account, setAccount] = useState<AccountInfo | null>(null)
  const [diagnoses, setDiagnoses] = useState<DiagnosisInfo[]>([])
  const [sources, setSources] = useState<SourceInfo[]>([])
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try {
      const [a, d, s] = await Promise.all([
        billingApi.account(), diagnosisApi.list(20), sourceApi.list(20),
      ])
      setAccount(a); setDiagnoses(d); setSources(s)
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const succeeded = diagnoses.filter((d) => d.status === 'succeeded').length

  return (
    <div className="page">
      <Row gutter={16}>
        <Col span={6}>
          <Card variant="borderless">
            <Statistic title="剩余诊断次数" value={account?.balance ?? 0}
                       suffix="次"
                       valueStyle={{
                         color: (account?.balance ?? 0) > 0 ? '#2c6e49' : '#cf1322',
                       }} />
            {(account?.balance ?? 0) === 0 && (
              <Link to="/billing">
                <Button type="link" size="small" style={{ padding: 0 }}>
                  去购买 →
                </Button>
              </Link>
            )}
          </Card>
        </Col>
        <Col span={6}>
          <Card variant="borderless">
            <Statistic title="累计诊断" value={account?.total_used ?? 0} suffix="次" />
          </Card>
        </Col>
        <Col span={6}>
          <Card variant="borderless">
            <Statistic title="已完成报告" value={succeeded} suffix="份" />
          </Card>
        </Col>
        <Col span={6}>
          <Card variant="borderless">
            <Statistic title="累计算力成本" value={account?.cost_used_usd ?? 0}
                       precision={4} prefix="$" />
          </Card>
        </Col>
      </Row>

      <Card
        variant="borderless"
        style={{ marginTop: 16 }}
        title="开始诊断"
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            上传千牛导出的报表（订单 / 生意参谋 / 账房 / 退款 / 广告 / 采购成本），
            系统会生成经营诊断报告。
          </Typography.Text>
          <Space>
            <Button type="primary" icon={<CloudUploadOutlined />}
                    onClick={() => navigate('/upload')} size="large">
              上传数据
            </Button>
            <Button icon={<FileTextOutlined />} onClick={() => navigate('/billing')}>
              购买次数
            </Button>
          </Space>
        </Space>
      </Card>

      <Card
        variant="borderless"
        style={{ marginTop: 16 }}
        title="诊断记录"
        extra={<Button size="small" onClick={load}>刷新</Button>}
      >
        {diagnoses.length === 0 && !loading ? (
          <Empty description="还没有诊断记录，先上传数据试试" />
        ) : (
          <Table<DiagnosisInfo>
            rowKey="diagnosis_id"
            loading={loading}
            dataSource={diagnoses}
            pagination={false}
            size="small"
            columns={[
              {
                title: '诊断编号', dataIndex: 'diagnosis_id', width: 140,
                render: (v: string) => <code>{v.slice(0, 10)}</code>,
              },
              {
                title: '状态', dataIndex: 'status', width: 110,
                render: (v: string) => {
                  const t = STATUS_TAG[v] ?? { color: 'default', text: v }
                  return <Tag color={t.color}>{t.text}</Tag>
                },
              },
              {
                title: '质量门', dataIndex: 'quality_passed', width: 100,
                render: (v: boolean, r) =>
                  r.status === 'succeeded'
                    ? (v ? <Tag color="green">通过</Tag> : <Tag color="orange">未过</Tag>)
                    : '—',
              },
              {
                title: '创建时间', dataIndex: 'created_at', width: 170,
                render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm') : '—'),
              },
              {
                title: '操作', width: 130,
                render: (_, r) => (
                  <Space>
                    {r.status === 'succeeded' && (
                      <Button type="link" size="small"
                              onClick={() => navigate(`/report/${r.diagnosis_id}`)}>
                        查看报告
                      </Button>
                    )}
                    {(r.status === 'running' || r.status === 'queued') && (
                      <Button type="link" size="small"
                              onClick={() => navigate(`/progress/${r.diagnosis_id}`)}>
                        查看进度
                      </Button>
                    )}
                  </Space>
                ),
              },
            ]}
          />
        )}
      </Card>

      {sources.length > 0 && (
        <Card variant="borderless" style={{ marginTop: 16 }} title="已上传文件">
          <Table<SourceInfo>
            rowKey="source_id"
            dataSource={sources}
            pagination={false}
            size="small"
            columns={[
              { title: '文件名', dataIndex: 'name' },
              {
                title: '类型', dataIndex: 'source_type', width: 150,
                render: (v: string) => v ? <Tag>{v}</Tag> : '—',
              },
              {
                title: '行数', dataIndex: 'row_count', width: 90,
                render: (v: number) => v ? v.toLocaleString() : '—',
              },
              {
                title: '大小', dataIndex: 'size_bytes', width: 100,
                render: (v: number) => `${(v / 1024).toFixed(1)} KB`,
              },
              {
                title: '上传时间', dataIndex: 'created_at', width: 170,
                render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm') : '—'),
              },
              {
                title: '操作', width: 110,
                render: (_, r) => (
                  <Button type="link" size="small"
                          onClick={() => navigate(`/mapping/${r.source_id}`)}>
                    查看映射
                  </Button>
                ),
              },
            ]}
          />
        </Card>
      )}

      {account && !account.trial_available && account.balance === 0 && (
        <Alert
          style={{ marginTop: 16 }}
          type="info"
          showIcon
          message="体验价已使用"
          description="体验价每人限购一次。如需继续使用，请购买标准价或 5 次包。"
          action={<Link to="/billing"><Button size="small">去购买</Button></Link>}
        />
      )}
    </div>
  )
}
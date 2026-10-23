import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert, Button, Card, Empty, Progress, Select, Space, Table, Tag,
  Typography, App as AntApp,
} from 'antd'
import { ArrowLeftOutlined, WarningOutlined } from '@ant-design/icons'
import { errMsg, sourceApi } from '../api/client'
import type { MappingItem, ProfileInfo } from '../api/types'

/**
 * 字段映射确认页 —— 产品信任的建立点。
 *
 * 设计要点：
 *  - 显示置信度，低置信度必须高亮（用户要能一眼看出哪条不确定）
 *  - 默认映射可修改：每个目标字段都能重新选源列
 *  - 确认后按 schema 指纹缓存，下次同结构文件免确认
 */
export default function MappingPage() {
  const { sourceId = '' } = useParams()
  const navigate = useNavigate()
  const { message } = AntApp.useApp()

  const [profile, setProfile] = useState<ProfileInfo | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    // ★ ignore 旧请求：快速切换数据源时，慢响应不能覆盖新画像（P1-20）
    let cancelled = false
    setLoading(true)
    setProfile(null)
    sourceApi.profile(sourceId)
      .then((p) => {
        if (cancelled) return
        setProfile(p)
        const m: Record<string, string> = {}
        p.mappings.forEach((x) => { m[x.target_field] = x.source_column })
        setMapping(m)
      })
      .catch((e) => { if (!cancelled) message.error(errMsg(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [sourceId])

  const save = async () => {
    if (!profile) return
    const missing = profile.missing_required.filter((f) => !mapping[f])
    if (missing.length) {
      message.error(`还有必填字段未映射：${missing.join('、')}`)
      return
    }
    setSaving(true)
    try {
      const res = await sourceApi.confirmMapping(sourceId, mapping)
      message.success(res.message || '映射已确认')
      navigate('/upload')
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="page"><Card loading variant="borderless" /></div>
  if (!profile) return <div className="page"><Empty description="数据源不存在" /></div>

  const reviewCount = profile.mappings.filter((m) => m.needs_review).length
  const requiredSet = new Set(profile.missing_required)

  const columns = [
    {
      title: '语义字段', dataIndex: 'target_field', width: 200,
      render: (v: string) => (
        <Space>
          <code style={{ fontSize: 13 }}>{v}</code>
          {requiredSet.has(v) && <Tag color="red">必填</Tag>}
        </Space>
      ),
    },
    {
      title: '← 源文件列名', width: 280,
      render: (_: unknown, r: MappingItem) => (
        <Select
          style={{ width: '100%' }}
          size="small"
          value={mapping[r.target_field] || undefined}
          placeholder="选择源列"
          allowClear
          status={requiredSet.has(r.target_field) && !mapping[r.target_field] ? 'error' : undefined}
          onChange={(val) => setMapping((m) => ({ ...m, [r.target_field]: val }))}
          options={profile.columns.map((c) => ({ label: c, value: c }))}
        />
      ),
    },
    {
      title: '置信度', width: 170,
      render: (_: unknown, r: MappingItem) => {
        const pct = Math.round(r.confidence * 100)
        const color = r.confidence >= 0.9 ? '#52c41a'
          : r.confidence >= 0.8 ? '#faad14' : '#ff4d4f'
        return (
          <Space>
            <span className="confidence-bar">
              <span className="confidence-fill"
                    style={{ width: `${pct}%`, background: color }} />
            </span>
            <Typography.Text style={{ color, fontSize: 13 }}>
              {pct}%
            </Typography.Text>
            {r.needs_review && <WarningOutlined style={{ color: '#faad14' }} />}
          </Space>
        )
      },
    },
    {
      title: '说明', dataIndex: 'reason', ellipsis: true,
      render: (v: string) => <Typography.Text type="secondary" style={{ fontSize: 12 }}>{v || '—'}</Typography.Text>,
    },
  ]

  return (
    <div className="page">
      <Button icon={<ArrowLeftOutlined />} type="text"
              onClick={() => navigate('/upload')} style={{ marginBottom: 8 }}>
        返回上传
      </Button>

      <Card
        variant="borderless"
        title={`确认字段映射 · ${profile.display_name}`}
        extra={<Tag>{profile.row_count.toLocaleString()} 行 · {profile.column_count} 列</Tag>}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="为什么要确认？"
          description={
            <>
              同一个列名在不同店铺含义可能不同。确认一次后，
              <strong>下次上传同结构的文件会自动应用，无需再确认</strong>。
              系统识别出的口径会写进报告，方便你核对。
            </>
          }
        />

        {reviewCount > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message={`有 ${reviewCount} 个字段匹配置信度较低，建议核对后再开始诊断`}
          />
        )}

        {profile.missing_required.length > 0 && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message={`缺少 ${profile.missing_required.length} 个必填字段的映射`}
            description={profile.missing_required.join('、')}
          />
        )}

        <Progress
          percent={Math.round((Object.keys(mapping).filter((k) => mapping[k]).length
            / profile.mappings.length) * 100)}
          size="small"
          style={{ marginBottom: 12 }}
        />

        <Table<MappingItem>
          rowKey="target_field"
          dataSource={profile.mappings}
          columns={columns as never}
          pagination={false}
          size="small"
          scroll={{ y: 520 }}
        />

        <div style={{ marginTop: 16 }}>
          <Space>
            <Button type="primary" loading={saving} onClick={save}>
              确认映射
            </Button>
            <Button onClick={() => navigate('/upload')}>稍后再说</Button>
          </Space>
        </div>
      </Card>

      {profile.warnings.length > 0 && (
        <Card variant="borderless" title="识别提示" style={{ marginTop: 16 }}>
          {profile.warnings.map((w, i) => (
            <Typography.Paragraph key={i} type="secondary" style={{ fontSize: 13 }}>
              · {w}
            </Typography.Paragraph>
          ))}
        </Card>
      )}
    </div>
  )
}
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, Button, Card, Descriptions, List, Modal, Progress, Space, Steps,
  Table, Tag, Typography, Upload, App as AntApp,
} from 'antd'
import { CloudUploadOutlined, CheckCircleOutlined, DownloadOutlined, InboxOutlined } from '@ant-design/icons'
import type { UploadFile } from 'antd'
import { diagnosisApi, errMsg, sourceApi } from '../api/client'
import type { SourceInfo } from '../api/types'

const { Dragger } = Upload

const MAX_UPLOAD_MB = 50

/** 支持的数据源类型及其导出路径提示。 */
const SOURCE_HINTS = [
  { key: 'trade_orders', name: '订单明细', path: '千牛 → 交易 → 已卖出的宝贝 → 批量导出', required: true },
  { key: 'sycm_traffic', name: '生意参谋流量', path: '千牛 → 生意参谋 → 流量 → 下载', required: false },
  { key: 'account_flow', name: '账房收支明细', path: '千牛 → 财务 → 账房 → 收支明细 → 导出', required: false },
  { key: 'refunds', name: '退款售后', path: '千牛 → 退款售后 → 退款管理 → 导出', required: false },
  { key: 'ad_reports', name: '推广报表', path: '万相台 / 直通车 → 报表 → 导出', required: false },
  { key: 'purchase_batches', name: '采购成本（自录）', path: '无平台导出，按模板自录', required: false },
]

export default function UploadPage() {
  const navigate = useNavigate()
  const { message } = AntApp.useApp()
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [uploaded, setUploaded] = useState<SourceInfo[]>([])
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const [listError, setListError] = useState('')
  const [starting, setStarting] = useState(false)
  const [timeRange, setTimeRange] = useState('本月')

  useEffect(() => {
    // ★ P2-33：列表加载失败不再被吞掉 —— 给出错误提示与重试
    sourceApi.list(30).then((rows) => {
      setUploaded(rows)
      setListError('')
    }).catch((e) => setListError(errMsg(e)))
  }, [])

  const doUpload = async (file: File) => {
    setUploading(true)
    setProgress(0)
    try {
      const list = await sourceApi.upload(file, setProgress)
      // ★ 多工作表 Excel 返回数组；单文件也是数组
      const added = list.filter(
        (s) => !uploaded.some((x) => x.source_id === s.source_id))
      setUploaded((prev) => [...added, ...prev.filter(
        (x) => !added.some((a) => a.source_id === x.source_id))])
      if (list.length > 1) {
        message.success(`已自动拆分 ${list.length} 个数据源（${list.map((s) => s.source_type).join('、')}）`)
      } else if (list.length === 1) {
        const s = list[0]
        message.success(`已识别：${s.source_type || '未知类型'} · ${s.row_count} 行`)
      }
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setUploading(false)
      setProgress(0)
    }
  }

  const doStart = async () => {
    const ids = uploaded.map((s) => s.source_id)
    setStarting(true)
    try {
      const info = await diagnosisApi.start(ids, timeRange)
      navigate(`/progress/${info.diagnosis_id}`)
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setStarting(false)
    }
  }

  /** ★ P2-34：诊断要扣 1 次额度，且默认携带**全部**已上传数据源 ——
   *  必须先弹窗列出将使用的数据，让用户确认，避免误扣费。 */
  const confirmStart = () => {
    if (uploaded.length === 0) {
      message.warning('请先上传至少一个数据源')
      return
    }
    Modal.confirm({
      title: '开始诊断？',
      content: (
        <div>
          <p>将使用以下 <strong>{uploaded.length}</strong> 个数据源（全部已上传文件）：</p>
          <ul style={{ maxHeight: 180, overflow: 'auto', paddingLeft: 20, margin: '4px 0' }}>
            {uploaded.map((s) => (
              <li key={s.source_id}>
                {s.name}（{s.row_count?.toLocaleString() ?? '—'} 行）
              </li>
            ))}
          </ul>
          <p>统计时间范围：{timeRange}。本次诊断将消耗 <strong>1 次</strong> 额度。</p>
        </div>
      ),
      okText: '开始诊断',
      cancelText: '再想想',
      onOk: doStart,
    })
  }

  const hasOrders = uploaded.some((s) => s.source_type === 'trade_orders')

  return (
    <div className="page">
      <Steps
        current={0}
        size="small"
        style={{ marginBottom: 24 }}
        items={[
          { title: '上传数据' },
          { title: '确认字段映射' },
          { title: '诊断分析' },
          { title: '查看报告' },
        ]}
      />

      <Card variant="borderless" title="上传千牛导出文件">
        {listError && (
          <Alert
            type="error" showIcon closable
            style={{ marginBottom: 16 }}
            message="已上传文件列表加载失败"
            description={listError}
          />
        )}
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="支持 .xlsx / .csv（.xls 请先另存为 .xlsx），单文件不超过 50MB"
          description={
            <>
              直接拖入千牛导出的原始文件即可，系统会自动识别类型并建议字段映射。
              <strong>一个 Excel 含多张报表也会自动拆分</strong>（如"说明"页会被跳过）。
            </>
          }
        />

        <div style={{ marginBottom: 16, padding: 14, background: '#f0f9f1',
                      border: '1px solid #d4e8da', borderRadius: 8 }}>
          <Space wrap>
            <DownloadOutlined style={{ color: '#2c6e49', fontSize: 18 }} />
            <Typography.Text strong>没有数据？先试试我们的样例</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              一个 Excel，含 6 张报表 + 使用说明；上传后自动拆分。
              里面藏着一个"第 3 周流量涨但转化率掉"的经营故事。
            </Typography.Text>
            <Button type="primary" ghost icon={<DownloadOutlined />}
                    href="/api/v1/samples/download" target="_blank">
              下载样例数据
            </Button>
          </Space>
        </div>

        <Dragger
          multiple
          accept=".xlsx,.csv"
          fileList={fileList}
          showUploadList={{ showRemoveIcon: true }}
          beforeUpload={(file) => {
            const f = file as unknown as File
            // ★ P2-32：客户端先查大小，超限直接拒（不再等 50MB 白传后才 413）
            if (f.size > MAX_UPLOAD_MB * 1024 * 1024) {
              message.error(
                `文件 ${(f.size / 1024 / 1024).toFixed(0)}MB 超过 ${MAX_UPLOAD_MB}MB 上限，请拆分后再上传`)
              return Upload.LIST_IGNORE
            }
            void doUpload(f)
            return false   // 阻止 antd 自动上传，交给 axios
          }}
          onChange={({ fileList: fl }) => setFileList(fl)}
          disabled={uploading}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽文件到此处上传</p>
          <p className="dragger-hint">
            订单明细为必需项；流量、账房、退款、广告、采购成本可选，但齐全时诊断更准确
          </p>
        </Dragger>

        {uploading && (
          <Progress percent={progress} size="small"
                    style={{ maxWidth: 360, marginTop: 8 }} />
        )}
      </Card>

      <Card variant="borderless" title="各文件的导出位置" style={{ marginTop: 16 }}>
        <List
          size="small"
          dataSource={SOURCE_HINTS}
          renderItem={(it) => (
            <List.Item>
              <Space>
                <Tag color={it.required ? 'red' : 'default'}>
                  {it.required ? '必需' : '可选'}
                </Tag>
                <Typography.Text strong style={{ width: 130, display: 'inline-block' }}>
                  {it.name}
                </Typography.Text>
                <Typography.Text type="secondary">{it.path}</Typography.Text>
              </Space>
            </List.Item>
          )}
        />
      </Card>

      {uploaded.length > 0 && (
        <Card
          variant="borderless"
          style={{ marginTop: 16 }}
          title={`已上传 ${uploaded.length} 个文件`}
          extra={
            hasOrders ? (
              <Tag icon={<CheckCircleOutlined />} color="success">
                已包含订单明细
              </Tag>
            ) : (
              <Tag color="warning">缺少订单明细</Tag>
            )
          }
        >
          <Table<SourceInfo>
            rowKey="source_id"
            dataSource={uploaded}
            pagination={false}
            size="small"
            columns={[
              { title: '文件名', dataIndex: 'name', ellipsis: true },
              {
                title: '识别类型', dataIndex: 'source_type', width: 170,
                render: (v: string) => (v ? <Tag color="blue">{v}</Tag> : <Tag>未识别</Tag>),
              },
              {
                title: '行数', dataIndex: 'row_count', width: 100,
                render: (v: number) => (v ? v.toLocaleString() : '—'),
              },
              {
                title: '操作', width: 120,
                render: (_, r) => (
                  <Button type="link" size="small"
                          onClick={() => navigate(`/mapping/${r.source_id}`)}>
                    确认字段映射
                  </Button>
                ),
              },
            ]}
          />
        </Card>
      )}

      {uploaded.length > 0 && (
        <Card variant="borderless" style={{ marginTop: 16 }}>
          <Descriptions column={1} size="small">
            <Descriptions.Item label="统计时间范围">
              <Space wrap>
                {['今天', '本周', '本月', '上月', '最近30天'].map((t) => (
                  <Button
                    key={t}
                    size="small"
                    type={timeRange === t ? 'primary' : 'default'}
                    onClick={() => setTimeRange(t)}
                  >
                    {t}
                  </Button>
                ))}
              </Space>
            </Descriptions.Item>
          </Descriptions>

          <Button
            type="primary"
            size="large"
            loading={starting}
            disabled={!hasOrders}
            onClick={confirmStart}
            style={{ marginTop: 8 }}
          >
            开始诊断
          </Button>
          {!hasOrders && (
            <Typography.Text type="danger" style={{ marginLeft: 12 }}>
              请先上传订单明细（必需）
            </Typography.Text>
          )}
        </Card>
      )}
    </div>
  )
}
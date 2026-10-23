import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert, Button, Card, Col, Descriptions, Drawer, Empty, Row, Space, Table, Tag,
  Typography, App as AntApp,
} from 'antd'
import { ArrowLeftOutlined, InfoCircleOutlined, PrinterOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { diagnosisApi, errMsg } from '../api/client'
import {
  formatMetricValue, UNCERTAINTY_LABELS,
  type AnomalyDay, type DiagnosisResult, type MetricValue, type ProductRow,
  type TrendPoint, type WeekRow,
} from '../api/types'

/** 趋势图（纯 SVG，不引图表库）。异常日用橙色竖虚线标记。 */
function TrendChart({ trend, anomalyDates }: {
  trend: TrendPoint[]; anomalyDates?: Set<string>
}) {
  const W = 860, H = 210
  const PL = 52, PR = 60, PT = 18, PB = 32
  const iw = W - PL - PR, ih = H - PT - PB
  const nets = trend.map((t) => t.net_sales)
  const viss = trend.map((t) => t.visitors)
  const nMax = Math.max(...nets) * 1.1 || 1
  const vMax = Math.max(...viss) * 1.1 || 1
  const n = trend.length

  const xy = (i: number, v: number, m: number) =>
    `${(PL + i * (iw / (n - 1))).toFixed(1)},${(PT + ih - (v / m) * ih).toFixed(1)}`

  const netPts = nets.map((v, i) => xy(i, v, nMax)).join(' ')
  const area = `${PL},${PT + ih} ${netPts} ${PL + iw},${PT + ih}`
  const visPts = viss.map((v, i) => xy(i, v, vMax)).join(' ')

  const marks: React.ReactNode[] = []
  trend.forEach((t, i) => {
    if (!anomalyDates?.has(t.date)) return
    const x = PL + i * (iw / (n - 1))
    marks.push(
      <g key={t.date}>
        <line x1={x} y1={PT} x2={x} y2={PT + ih} stroke="#d46b08"
              strokeWidth={1} strokeDasharray="3,3" opacity={0.7} />
        <circle cx={x} cy={PT - 3} r={3} fill="#d46b08" />
      </g>)
  })

  const step = Math.max(1, Math.floor(n / 8))
  const labels: React.ReactNode[] = []
  for (let i = 0; i < n; i += step) {
    const x = PL + i * (iw / (n - 1))
    labels.push(
      <text key={i} x={x} y={H - 10} textAnchor="middle" fontSize={10} fill="#8c959f">
        {trend[i].date.slice(5)}
      </text>)
  }

  const gridLines: React.ReactNode[] = []
  for (const k of [0, 0.5, 1]) {
    const y = PT + ih - k * ih
    gridLines.push(
      <g key={k}>
        <line x1={PL} y1={y} x2={PL + iw} y2={y} stroke="#eef1f4" />
        <text x={PL - 6} y={y + 4} textAnchor="end" fontSize={10} fill="#2c6e49">
          ¥{(nMax * k).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}
        </text>
      </g>)
  }

  return (
    <div>
      <div style={{ fontSize: 12, color: '#8c959f', marginBottom: 8 }}>
        <span style={{ display: 'inline-block', width: 22, borderTop: '3px solid #2c6e49',
                       verticalAlign: 'middle', marginRight: 6 }} />
        净销售额（左轴）
        <span style={{ display: 'inline-block', width: 22,
                       borderTop: '2px dashed #3b82f6', verticalAlign: 'middle',
                       margin: '0 6px 0 20px' }} />
        访客数（右轴）
        {marks.length > 0 && (
          <span style={{ marginLeft: 20, color: '#d46b08', fontWeight: 600 }}>▍ 异常日</span>
        )}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto' }}>
        <polygon points={area} fill="rgba(44,110,73,0.10)" />
        <polyline points={netPts} fill="none" stroke="#2c6e49" strokeWidth={2.5} />
        <polyline points={visPts} fill="none" stroke="#3b82f6" strokeWidth={1.8}
                  strokeDasharray="5,4" opacity={0.85} />
        {marks}
        {gridLines}
        <text x={PL + iw + 8} y={PT + 12} fontSize={10} fill="#3b82f6">
          {Math.round(vMax).toLocaleString('zh-CN')}
        </text>
        <text x={PL + iw + 8} y={PT + ih} fontSize={10} fill="#3b82f6">0</text>
        {labels}
      </svg>
    </div>
  )
}

const pctText = (v: number | null | undefined) =>
  (v === null || v === undefined) ? '—' : `${(v * 100).toFixed(2)}%`

/**
 * 报告页 —— 把后端的技术保证变成客户能看见的信任。
 *
 * 每个数字可点开看溯源：口径 / 公式 / 时间范围 / 待确认项。
 */
export default function ReportPage() {
  const { diagnosisId = '' } = useParams()
  const navigate = useNavigate()
  const { message } = AntApp.useApp()

  const [result, setResult] = useState<DiagnosisResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [trace, setTrace] = useState<MetricValue | null>(null)
  const [printing, setPrinting] = useState(false)

  useEffect(() => {
    // ★ 切换报告时丢弃旧请求结果，避免慢响应覆盖新数据（docs/16 P1-20）
    let cancelled = false
    setLoading(true)
    setResult(null)
    diagnosisApi.get(diagnosisId)
      .then((r) => { if (!cancelled) setResult(r) })
      .catch((e) => { if (!cancelled) message.error(errMsg(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [diagnosisId])

  if (loading) return <div className="page"><Card loading variant="borderless" /></div>
  if (!result) return <div className="page"><Empty description="报告不存在" /></div>

  if (result.status !== 'succeeded') {
    return (
      <div className="page">
        <Card variant="borderless">
          <Empty description={`诊断状态：${result.status}${result.error ? ' · ' + result.error : ''}`} />
          <Button onClick={() => navigate('/dashboard')}>回工作台</Button>
        </Card>
      </div>
    )
  }

  const uncertainCount = result.metrics.filter((m) => m.uncertainty !== 'low').length

  return (
    <div className="page">
      <Button icon={<ArrowLeftOutlined />} type="text"
              onClick={() => navigate('/dashboard')} style={{ marginBottom: 8 }}>
        返回工作台
      </Button>

      {uncertainCount > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={`有 ${uncertainCount} 个指标的口径尚未与您确认`}
          description="这些指标的数字按系统默认口径计算，建议核对报告底部的口径说明后再做经营决策。"
        />
      )}

      {result.metrics.length > 0 && (
        <Card variant="borderless" style={{ marginBottom: 16 }}>
          <Row gutter={16}>
            {(['M06', 'M07', 'M13', 'M03'] as const).map((mid) => {
              const m = result.metrics.find((x) => x.metric_id === mid)
              if (!m || m.value === null) return null
              return (
                <Col span={6} key={mid}>
                  <div style={{
                    padding: '14px 18px', border: '1px solid #e5e7eb',
                    borderRadius: 10,
                    background: m.uncertainty === 'high' ? '#fffaf9' : undefined,
                    borderColor: m.uncertainty === 'high' ? '#f3c8c2' : undefined,
                  }}>
                    <div style={{ fontSize: 12, color: '#8c959f' }}>{m.metric_name}</div>
                    <div style={{
                      fontSize: 26, fontWeight: 650,
                      fontVariantNumeric: 'tabular-nums',
                      color: m.uncertainty === 'high' ? '#b3412f' : '#1a3d2b',
                    }}>
                      {formatMetricValue(m.value, m.unit)}
                    </div>
                    <div style={{
                      fontSize: 11, color: '#8c959f',
                      fontFamily: 'ui-monospace, Consolas, monospace',
                    }}>
                      {m.formula || '\u00A0'}
                    </div>
                  </div>
                </Col>
              )
            })}
          </Row>
        </Card>
      )}

      {result.caliber_compare && result.caliber_compare.items?.length === 2 && (
        <Card variant="borderless" title={`口径对比 · ${result.caliber_compare.label}`}
              style={{ marginBottom: 16 }}>
          <Row gutter={16}>
            {result.caliber_compare.items.map((it) => (
              <Col span={12} key={it.metric_id}>
                <div style={{ padding: '14px 18px', border: '1px solid #d4e8da',
                              borderRadius: 10, background: '#f0f9f1' }}>
                  <div style={{ fontSize: 13, color: '#2c6e49', fontWeight: 600 }}>
                    {it.name}
                    <Tag style={{ marginLeft: 8, fontSize: 11 }}>{it.metric_id}</Tag>
                  </div>
                  <div style={{ fontSize: 30, fontWeight: 650,
                                fontVariantNumeric: 'tabular-nums', color: '#1a3d2b' }}>
                    {formatMetricValue(it.value, it.unit)}
                  </div>
                  <div style={{ fontSize: 12, color: '#8c959f' }}>{it.desc}</div>
                </div>
              </Col>
            ))}
          </Row>
          <Typography.Paragraph type="secondary" style={{ fontSize: 13, margin: '10px 0 0' }}>
            {result.caliber_compare.note}
          </Typography.Paragraph>
        </Card>
      )}

      {(result.trend && result.trend.length >= 2 || (result.anomaly_days?.length ?? 0) > 0) && (
        <Card variant="borderless" title="净销售额与访客数趋势"
              style={{ marginBottom: 16 }}>
          {result.trend && result.trend.length >= 2 && (
            <TrendChart trend={result.trend}
                        anomalyDates={new Set((result.anomaly_days ?? []).map((a) => a.date))} />
          )}
          {(result.anomaly_days?.length ?? 0) > 0 && (
            <>
              <Typography.Paragraph type="secondary"
                                    style={{ fontSize: 12.5, margin: '14px 0 8px' }}>
                基线 = 前 7 日均值；偏离 ≥35% 的日子。特征是观察到的事实，核对方向是建议
                —— 系统不猜测原因，请结合当日实际经营动作（促销 / 投放 / 上新）确认。
              </Typography.Paragraph>
              <Table<AnomalyDay>
                rowKey="date"
                dataSource={result.anomaly_days}
                pagination={false}
                size="small"
                columns={[
                  { title: '日期', dataIndex: 'date', width: 100,
                    render: (v: string) => <code>{v.slice(5)}</code> },
                  { title: '观察到的特征',
                    render: (_, r) => (
                      <span style={{ fontSize: 13 }}>
                        {r.features.map((f, i) => (
                          <div key={i}>
                            <span style={{ color: f.includes('上升') ? '#2c6e49' : '#b3412f' }}>
                              {f.includes('上升') ? '▲' : '▼'}
                            </span>{' '}{f}
                          </div>
                        ))}
                      </span>
                    ) },
                  { title: '建议核对方向', dataIndex: 'note', width: 320,
                    render: (v: string) => (
                      <span style={{ fontSize: 12.5, color: '#57606a' }}>{v}</span>
                    ) },
                ]}
              />
            </>
          )}
        </Card>
      )}

      {(result.products?.length ?? 0) > 0 && (
        <Card variant="borderless" title="商品维度拆解" style={{ marginBottom: 16 }}>
          {result.products!.some((p) => p.flags.length > 0) && (
            <Alert
              type="warning" showIcon style={{ marginBottom: 12 }}
              message={`重点关注：${
                result.products!.filter((p) => p.flags.length > 0)
                  .map((p) => p.title || p.merchant_code || p.item_id).join('、')}`}
              description={Array.from(new Set(
                result.products!.flatMap((p) => p.flags))).join('；') +
                '。请结合退款原因与商品明细进一步核查。'}
            />
          )}
          <Table<ProductRow>
            rowKey="item_id"
            dataSource={result.products}
            pagination={false}
            size="small"
            columns={[
              { title: '商品', width: 220,
                render: (_, r) => (
                  <Space>
                    <Typography.Text strong ellipsis style={{ maxWidth: 150 }}>
                      {r.title || r.merchant_code || r.item_id}
                    </Typography.Text>
                    {r.flags.length > 0 && <Tag color="red">重点关注</Tag>}
                  </Space>
                ) },
              { title: '销售占比', dataIndex: 'share', width: 100, align: 'right',
                render: (v: number) => pctText(v) },
              { title: '支付金额', dataIndex: 'pay_amount', width: 110, align: 'right',
                render: (v: number) => `¥${v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` },
              { title: '净销售额', dataIndex: 'net_sales', width: 110, align: 'right',
                render: (v: number) => `¥${v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` },
              { title: '退款率', dataIndex: 'refund_rate', width: 95, align: 'right',
                render: (v: number) => (
                  <span style={{ color: v > 0.05 ? '#b3412f' : undefined }}>
                    {pctText(v)}
                  </span>
                ) },
              { title: '主要退款原因', dataIndex: 'top_refund_reason', width: 130,
                render: (v: string) => v || '—' },
              { title: '毛利率', dataIndex: 'margin_rate', width: 95, align: 'right',
                render: (v: number | null) => pctText(v) },
            ]}
          />
        </Card>
      )}

      {result.weekly && result.weekly.weeks?.length >= 2 && (
        <Card variant="borderless" title="周度对比" style={{ marginBottom: 16 }}>
          {result.weekly.biggest && (
            <Typography.Paragraph style={{ fontSize: 13.5, margin: '0 0 8px' }}>
              变化最大的维度：<b>{result.weekly.biggest.metric}</b>
              （末周{' '}
              {result.weekly.biggest.kind === 'pct'
                ? pctText(result.weekly.biggest.last)
                : result.weekly.biggest.last.toLocaleString('zh-CN')}
              {' '}vs 之前均值{' '}
              {result.weekly.biggest.kind === 'pct'
                ? pctText(result.weekly.biggest.base)
                : result.weekly.biggest.base.toLocaleString('zh-CN')}
              ，<span style={{ color: result.weekly.biggest.delta > 0 ? '#2c6e49' : '#b3412f' }}>
                {result.weekly.biggest.delta > 0 ? '↑' : '↓'}
                {Math.abs(result.weekly.biggest.delta * 100).toFixed(0)}%
              </span>）
            </Typography.Paragraph>
          )}
          <Table<WeekRow>
            rowKey="label"
            dataSource={result.weekly.weeks}
            pagination={false}
            size="small"
            columns={[
              { title: '周（周一起）', dataIndex: 'label', width: 130,
                render: (v: string, r) => (
                  <code>
                    {v.slice(5)}
                    {(r.days ?? 7) < 7 && (
                      <span style={{ color: '#8c959f' }}> ·{r.days}天</span>
                    )}
                  </code>
                ) },
              { title: '访客', dataIndex: 'visitors', width: 90, align: 'right',
                render: (v: number) => v.toLocaleString('zh-CN') },
              { title: '转化率', dataIndex: 'pay_cvr', width: 90, align: 'right',
                render: (v: number | null) => pctText(v) },
              { title: '净销售额', dataIndex: 'net_sales', width: 110, align: 'right',
                render: (v: number) => `¥${v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` },
              { title: '退款率', dataIndex: 'refund_rate', width: 90, align: 'right',
                render: (v: number) => pctText(v) },
              { title: '广告花费', dataIndex: 'ad_spend', width: 100, align: 'right',
                render: (v: number) => `¥${v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` },
              { title: 'ROI', dataIndex: 'roi', width: 80, align: 'right',
                render: (v: number | null) => (v === null ? '—' : v.toFixed(1)) },
            ]}
          />
        </Card>
      )}

      <Card
        variant="borderless"
        title="核心指标"
        extra={
          <Space>
            <Button size="small" icon={<PrinterOutlined />} loading={printing}
                    onClick={() => {
                      // ★ 带 JWT fetch 后用 blob 打开：window.open 无法携带
                      //   Authorization 头，直接开 URL 会 401（docs/16 P0-3）
                      setPrinting(true)
                      diagnosisApi.openReport(diagnosisId)
                        .catch((e: Error) => message.error(e.message))
                        .finally(() => setPrinting(false))
                    }}>
              打印 / 导出 PDF
            </Button>
          </Space>
        }
      >
        <Table<MetricValue>
          rowKey="metric_id"
          dataSource={result.metrics}
          pagination={false}
          size="middle"
          columns={[
            {
              title: '指标', dataIndex: 'metric_name', width: 190,
              render: (v: string, r) => (
                <Space>
                  <Typography.Text strong>{v}</Typography.Text>
                  <Tag style={{ fontSize: 11 }}>{r.metric_id}</Tag>
                </Space>
              ),
            },
            {
              title: '数值', width: 200, align: 'right',
              render: (_, r) => (
                <Space>
                  <span className="metric-value">
                    {formatMetricValue(r.value, r.unit)}
                  </span>
                  <Button
                    type="text"
                    size="small"
                    icon={<InfoCircleOutlined style={{ color: '#8c959f' }} />}
                    onClick={() => setTrace(r)}
                    title="查看口径与溯源"
                  />
                </Space>
              ),
            },
            {
              title: '口径状态', width: 150,
              render: (_, r) => {
                const u = UNCERTAINTY_LABELS[r.uncertainty] ?? UNCERTAINTY_LABELS.low
                return <Tag color={u.color}>{u.text}</Tag>
              },
            },
            {
              title: '计算方式', dataIndex: 'formula',
              render: (v: string) => <span className="metric-formula">{v || '—'}</span>,
            },
          ]}
        />
      </Card>

      {result.warnings.length > 0 && (
        <Card variant="borderless" title="计算提示" style={{ marginTop: 16 }}>
          {result.warnings.map((w, i) => (
            <Typography.Paragraph key={i} type="secondary" style={{ fontSize: 13, marginBottom: 6 }}>
              · {w}
            </Typography.Paragraph>
          ))}
        </Card>
      )}

      {result.footnote && (
        <Card variant="borderless" title="口径说明与待确认项" style={{ marginTop: 16 }}>
          <div style={{ whiteSpace: 'pre-wrap', fontSize: 13, lineHeight: 1.9 }}>
            {result.footnote}
          </div>
        </Card>
      )}

      <Card variant="borderless" style={{ marginTop: 16 }}>
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="诊断编号">
            <code>{result.diagnosis_id.slice(0, 16)}</code>
          </Descriptions.Item>
          <Descriptions.Item label="质量门">
            {result.quality_passed
              ? <Tag color="green">通过</Tag>
              : <Tag color="orange">未通过</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="耗时">
            {result.duration_ms ? `${(result.duration_ms / 1000).toFixed(1)}s` : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="算力成本">
            ${result.cost_usd.toFixed(4)}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 溯源抽屉 */}
      <Drawer
        title={trace ? `${trace.metric_name} · 口径与溯源` : ''}
        open={Boolean(trace)}
        onClose={() => setTrace(null)}
        width={440}
      >
        {trace && (
          <div className="trace-row">
            <div>
              <span className="trace-label">指标 ID</span>
              <code>{trace.metric_id}</code>
            </div>
            <div>
              <span className="trace-label">数值</span>
              <strong>{formatMetricValue(trace.value, trace.unit)}</strong>
            </div>
            <div>
              <span className="trace-label">计算方式</span>
              <span className="metric-formula">{trace.formula || '—'}</span>
            </div>
            <div>
              <span className="trace-label">口径状态</span>
              <Tag color={UNCERTAINTY_LABELS[trace.uncertainty]?.color}>
                {UNCERTAINTY_LABELS[trace.uncertainty]?.text}
              </Tag>
            </div>
            <div>
              <span className="trace-label">生成时间</span>
              {dayjs(result.finished_at || undefined).format('YYYY-MM-DD HH:mm:ss')}
            </div>

            {trace.uncertainty !== 'low' && (
              <Alert
                type="warning"
                showIcon
                style={{ marginTop: 16 }}
                message="该口径尚未与您确认"
                description="数字按系统默认口径计算。请核对报告底部的口径说明，或联系服务方确认。"
              />
            )}
          </div>
        )}
      </Drawer>
    </div>
  )
}
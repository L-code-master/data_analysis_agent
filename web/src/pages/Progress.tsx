import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Alert, Button, Card, Result, Space, Spin, Typography } from 'antd'
import { CheckCircleFilled, LoadingOutlined } from '@ant-design/icons'
import { diagnosisApi, errMsg } from '../api/client'
import { PHASE_LABELS, type ProgressEvent } from '../api/types'

/**
 * 诊断进度页 —— SSE 实时推送。
 *
 * ⚠️ 后端会回放订阅前已发生的事件（见 routes/diagnoses.py），
 *    因此即使诊断很快跑完，用户也能看到完整阶段。
 */
export default function ProgressPage() {
  const { diagnosisId = '' } = useParams()
  const navigate = useNavigate()

  const [phases, setPhases] = useState<{ phase: string; message: string }[]>([])
  const [current, setCurrent] = useState<string>('')
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const closedRef = useRef(false)

  useEffect(() => {
    if (!diagnosisId) return
    closedRef.current = false

    // 先查一次状态：可能已经跑完了
    diagnosisApi.get(diagnosisId).then((info) => {
      if (info.status === 'succeeded') { setDone(true); return }
      if (info.status === 'failed') { setError(info.error || '诊断失败'); return }
    }).catch(() => {})

    const close = diagnosisApi.subscribe(
      diagnosisId,
      (ev: ProgressEvent) => {
        if (ev.type === 'ready' || ev.type === 'end') return
        if (ev.type === 'progress') {
          setCurrent(ev.phase || '')
          setPhases((prev) => {
            const last = prev[prev.length - 1]
            if (last && last.phase === ev.phase && last.message === ev.message) return prev
            return [...prev, { phase: ev.phase || '', message: ev.message || '' }]
          })
        } else if (ev.type === 'done') {
          setDone(true)
          close()
        } else if (ev.type === 'error') {
          setError(ev.message || '诊断失败')
          close()
        }
      },
      () => { /* EventSource 自动重连，忽略瞬时错误 */ },
    )

    return () => { close() }
  }, [diagnosisId])

  const allPhases = Object.keys(PHASE_LABELS).filter((p) => p !== 'done')

  if (error) {
    return (
      <div className="page-narrow">
        <Card variant="borderless">
          <Result
            status="error"
            title="诊断失败"
            subTitle={error}
            extra={[
              <Button key="back" type="primary" onClick={() => navigate('/upload')}>
                返回上传
              </Button>,
              <Button key="dash" onClick={() => navigate('/dashboard')}>
                回工作台
              </Button>,
            ]}
          />
          <Alert
            type="info"
            showIcon
            message="额度已自动退还"
            description="诊断失败不会消耗你的诊断次数。"
          />
        </Card>
      </div>
    )
  }

  if (done) {
    return (
      <div className="page-narrow">
        <Card variant="borderless">
          <Result
            status="success"
            title="诊断完成"
            subTitle="已生成经营诊断报告"
            extra={[
              <Button key="view" type="primary" size="large"
                      onClick={() => navigate(`/report/${diagnosisId}`)}>
                查看报告
              </Button>,
              <Button key="dash" onClick={() => navigate('/dashboard')}>
                回工作台
              </Button>,
            ]}
          />
        </Card>
      </div>
    )
  }

  return (
    <div className="page-narrow">
      <Card variant="borderless" title="正在诊断…">
        <div style={{ textAlign: 'center', padding: '12px 0 20px' }}>
          <Spin indicator={<LoadingOutlined style={{ fontSize: 32 }} spin />} />
          <div style={{ marginTop: 12 }}>
            <Typography.Text strong>
              {PHASE_LABELS[current] || '准备中…'}
            </Typography.Text>
          </div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            诊断通常需要 10 秒到 2 分钟，请勿关闭页面
          </Typography.Text>
        </div>

        <div className="phase-timeline">
          {allPhases.map((p) => {
            const idx = phases.findIndex((x) => x.phase === p)
            const reached = idx >= 0
            const isCurrent = current === p
            return (
              <div key={p} className="phase-line">
                {reached && !isCurrent ? (
                  <CheckCircleFilled style={{ color: '#52c41a', fontSize: 14 }} />
                ) : (
                  <span className={`phase-dot ${isCurrent ? 'active' : ''}`} />
                )}
                <Typography.Text
                  type={reached ? undefined : 'secondary'}
                  strong={isCurrent}
                >
                  {PHASE_LABELS[p]}
                </Typography.Text>
                {isCurrent && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {(phases.filter((x) => x.phase === p).slice(-1)[0]?.message) || ''}
                  </Typography.Text>
                )}
              </div>
            )
          })}
        </div>

        {phases.length > 0 && (
          <div style={{ marginTop: 16, maxHeight: 180, overflowY: 'auto' }}>
            {phases.slice(-12).map((x, i) => (
              <div key={i} style={{ fontSize: 12, color: '#8c959f', lineHeight: 1.9 }}>
                [{PHASE_LABELS[x.phase] || x.phase}] {x.message}
              </div>
            ))}
          </div>
        )}

        <Space style={{ marginTop: 16 }}>
          <Button onClick={() => navigate('/dashboard')}>回工作台</Button>
          <Button type="link" onClick={() => window.location.reload()}>
            刷新状态
          </Button>
        </Space>
      </Card>
    </div>
  )
}
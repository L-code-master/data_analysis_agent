import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert, Button, Card, Empty, Input, Space, Spin, Tag, Typography, App as AntApp,
} from 'antd'
import {
  AimOutlined, DatabaseOutlined, MessageOutlined, RobotOutlined, SendOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { chatApi, chatContextApi, errMsg } from '../api/client'
import type { ChatContext, ChatMessage, ChatSession } from '../api/types'

/** 路由标签（回答来自哪条路径） */
const ROUTE_TAG: Record<string, { text: string; color: string }> = {
  qa: { text: '查数', color: 'green' },
  agent: { text: 'Agent 分析', color: 'purple' },
  agent_error: { text: 'Agent 出错', color: 'red' },
  no_llm: { text: '未配置 LLM', color: 'orange' },
}

/**
 * 对话式分析（v1.4）。
 *
 * 形态 A（查数）：问题 → 语义层 → SQL，零 LLM，毫秒级；
 * 形态 B（归因）：LLM 循环 + 工具调用（带溯源与成本）。
 * 路由在后端自动完成 —— 前端只展示回答与来源标签。
 */
export default function ChatPage() {
  const { message } = AntApp.useApp()
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [active, setActive] = useState<ChatSession | null>(null)
  const [msgs, setMsgs] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [asking, setAsking] = useState(false)
  const [ctx, setCtx] = useState<ChatContext | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const refreshCtx = () => {
    chatContextApi.get().then(setCtx).catch((e) => {
      // eslint-disable-next-line no-console
      console.warn('[chat] context 加载失败:', e?.response?.status, e?.message)
      setCtx(null)
    })
  }

  useEffect(() => {
    refreshCtx()
    chatApi.sessions().then(async (list) => {
      setSessions(list)
      if (list.length > 0) {
        setActive(list[0])
        setMsgs(await chatApi.messages(list[0].session_id))
      }
    }).catch(() => {})
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [msgs, asking])

  const newSession = async () => {
    try {
      const s = await chatApi.createSession()
      setSessions((prev) => [s, ...prev])
      setActive(s)
      setMsgs([])
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const selectSession = async (s: ChatSession) => {
    setActive(s)
    try {
      setMsgs(await chatApi.messages(s.session_id))
    } catch {
      setMsgs([])
    }
  }

  const send = async () => {
    const q = input.trim()
    if (!q || !active || asking) return
    setInput('')
    setAsking(true)
    setMsgs((prev) => [...prev, {
      message_id: `local-${Date.now()}`, role: 'user', content: q,
      route: '', meta: {}, created_at: new Date().toISOString(),
    }])
    try {
      const ans = await chatApi.ask(active.session_id, q)
      setMsgs((prev) => [...prev, {
        message_id: `local-a-${Date.now()}`, role: 'assistant',
        content: ans.answer, route: ans.route, meta: ans.meta,
        created_at: new Date().toISOString(),
      }])
      refreshCtx()
    } catch (e: unknown) {
      // 402 = Agent 配额用尽（形态 A 永不限）—— 展示引导而不是报错样式
      const err = e as { response?: { status?: number; data?: { message?: string } } }
      if (err?.response?.status === 402) {
        message.warning(err.response.data?.message || 'Agent 追问次数已用完')
      } else {
        message.error(errMsg(e))
      }
    } finally {
      setAsking(false)
    }
  }

  const suggestions = [
    '这个月毛利率多少？',
    '哪个商品退款率最高？',
    '每周对比一下销售情况',
    '为什么转化率会跌？帮我分析一下',
  ]

  return (
    <div className="page">
      <Card
        variant="borderless"
        title={<Space><MessageOutlined /> 对话式分析</Space>}
        extra={
          <Button size="small" onClick={newSession}>新对话</Button>
        }
      >
        {/* 数据上下文条：对话基于哪份数据（docs/15） */}
        {ctx && !ctx.data.available && (
          <Alert type="info" showIcon icon={<DatabaseOutlined />}
                 style={{ marginBottom: 12 }}
                 message={ctx.data.message || '尚无数据'} />
        )}
        {ctx && ctx.data.available && (
          <Alert
            type={ctx.data.stale ? 'warning' : 'success'}
            showIcon icon={<DatabaseOutlined />}
            style={{ marginBottom: 12 }}
            message={
              <span style={{ fontSize: 13 }}>
                当前数据：<b>{ctx.data.file_count} 个数据源</b>
                （{(ctx.data.files || []).slice(0, 3).join('、')}
                {(ctx.data.file_count || 0) > 3 ? ' 等' : ''}）
                · 周期 <b>{ctx.data.time_range}</b>
                {' '}{(ctx.data.row_count || 0).toLocaleString()} 行
                · 诊断于 {String(ctx.data.diagnosed_at || '').slice(0, 10)}
                {ctx.agent_quota.unlimited
                  ? ' · 追问不限次（管理员）'
                  : ctx.agent_quota.quota >= 0
                    ? ` · Agent 追问剩余 ${Math.max(0, ctx.agent_quota.quota - ctx.agent_quota.used)} 次`
                    : ''}
              </span>
            }
            description={ctx.data.stale ? (
              <span>
                有 {ctx.data.newer_uploads} 个新上传未诊断 —— 当前回答基于上一次诊断的旧数据。{' '}
                <Link to="/upload">去重新上传并诊断 →</Link>
              </span>
            ) : undefined}
          />
        )}
        <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 12 }}>
          查数类问题直接语义层计算（零 AI 成本、数字可溯源）；
          「为什么/怎么办」类问题进入 Agent 循环（LLM + 工具，回答里的数字全部来自工具返回）。
          需要先上传数据并完成一次诊断。
        </Typography.Paragraph>

        {sessions.length > 1 && (
          <Space wrap style={{ marginBottom: 12 }}>
            {sessions.slice(0, 6).map((s) => (
              <Tag.CheckableTag
                key={s.session_id}
                checked={active?.session_id === s.session_id}
                onChange={() => selectSession(s)}
              >
                {s.title || '新对话'}
              </Tag.CheckableTag>
            ))}
          </Space>
        )}
      </Card>

      <Card variant="borderless" style={{ marginTop: 16 }}>
        <div style={{ minHeight: 320 }}>
          {msgs.length === 0 && !asking && (
            <Empty description="开始提问吧" style={{ paddingTop: 60 }}>
              <Space wrap>
                {suggestions.map((s) => (
                  <Button key={s} size="small" onClick={() => setInput(s)}>
                    {s}
                  </Button>
                ))}
              </Space>
            </Empty>
          )}

          {msgs.map((m) => (
            <div key={m.message_id}
                 style={{ display: 'flex', gap: 10, marginBottom: 16 }}>
              {m.role === 'user'
                ? <UserOutlined style={{ fontSize: 18, color: '#57606a', marginTop: 4 }} />
                : <RobotOutlined style={{ fontSize: 18, color: '#2c6e49', marginTop: 4 }} />}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  background: m.role === 'user' ? '#f6f8fa' : '#f0f9f1',
                  border: `1px solid ${m.role === 'user' ? '#eceef1' : '#d4e8da'}`,
                  borderRadius: 10, padding: '10px 14px',
                  whiteSpace: 'pre-wrap', fontSize: 14, lineHeight: 1.7,
                }}>
                  {m.content}
                </div>
                {m.role === 'assistant' && m.route && ROUTE_TAG[m.route] && (
                  <Space size={8} style={{ marginTop: 4 }}>
                    <Tag color={ROUTE_TAG[m.route].color} style={{ fontSize: 11 }}>
                      <AimOutlined /> {ROUTE_TAG[m.route].text}
                    </Tag>
                    {m.route === 'agent' && m.meta?.turns !== undefined && (
                      <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                        {String(m.meta.turns)} 轮 · 工具 {Array.isArray(m.meta.tool_trace)
                          ? (m.meta.tool_trace as unknown[]).length : 0} 次 ·
                        成本 ${Number(m.meta.cost_usd ?? 0).toFixed(4)}
                      </Typography.Text>
                    )}
                  </Space>
                )}
              </div>
            </div>
          ))}

          {asking && (
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <RobotOutlined style={{ fontSize: 18, color: '#2c6e49' }} />
              <Spin size="small" />
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                分析中（Agent 循环可能需要数秒到数十秒）…
              </Typography.Text>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        <Space.Compact style={{ width: '100%', marginTop: 8 }}>
          <Input
            size="large"
            placeholder="例如：这个月毛利率多少？ / 为什么第 3 周转化率跌了？"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onPressEnter={send}
            disabled={!active || asking}
          />
          <Button type="primary" size="large" icon={<SendOutlined />}
                  onClick={send} loading={asking} disabled={!active}>
            提问
          </Button>
        </Space.Compact>
      </Card>
    </div>
  )
}

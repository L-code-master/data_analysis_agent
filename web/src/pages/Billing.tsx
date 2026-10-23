import { useEffect, useState } from 'react'
import {
  Alert, Button, Card, Col, Modal, Row, Space, Table, Tag, Typography,
  App as AntApp,
} from 'antd'
import { CheckOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { billingApi, errMsg } from '../api/client'
import type { AccountInfo, LedgerEntry, OrderInfo, PlanInfo } from '../api/types'

const REASON_LABEL: Record<string, string> = {
  purchase: '购买',
  consume: '诊断消耗',
  refund: '失败退还',
  grant: '赠送',
  expire: '过期',
}

export default function BillingPage() {
  const { message, modal } = AntApp.useApp()
  const [plans, setPlans] = useState<PlanInfo[]>([])
  const [account, setAccount] = useState<AccountInfo | null>(null)
  const [orders, setOrders] = useState<OrderInfo[]>([])
  const [ledger, setLedger] = useState<LedgerEntry[]>([])
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try {
      const [p, a, o, l] = await Promise.all([
        billingApi.plans(), billingApi.account(),
        billingApi.orders(20), billingApi.ledger(50),
      ])
      setPlans(p); setAccount(a); setOrders(o); setLedger(l)
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const buy = (plan: PlanInfo) => {
    modal.confirm({
      title: `购买「${plan.name}」`,
      width: 460,
      content: (
        <div>
          <p>
            金额：<strong>¥{plan.price_yuan.toFixed(0)}</strong> ·
            获得 <strong>{plan.quantity}</strong> 次诊断
          </p>
          <Alert
            type="info"
            showIcon
            message="当前为人工核销"
            description={
              <>
                下单后请联系服务方完成付款。付款时请<strong>备注订单号</strong>，
                管理员确认收款后额度自动到账。
              </>
            }
          />
        </div>
      ),
      okText: '生成订单',
      onOk: async () => {
        try {
          const o = await billingApi.createOrder(plan.sku)
          await load()
          Modal.info({
            title: '订单已创建',
            width: 480,
            content: (
              <div>
                <p>订单号：<code style={{ fontSize: 15 }}>{o.order_no}</code></p>
                <p>金额：<strong>¥{o.amount_yuan.toFixed(0)}</strong></p>
                <Alert
                  type="warning"
                  showIcon
                  message="请把订单号提供给服务方并完成付款"
                  description="付款备注订单号可加快核销速度。"
                />
              </div>
            ),
          })
        } catch (e) {
          message.error(errMsg(e))
        }
      },
    })
  }

  /** ★ P2-34：取消订单加确认弹窗 + 防重复点击（原直接发请求可连点）。 */
  const cancel = (orderNo: string) => {
    modal.confirm({
      title: '取消该订单？',
      content: `订单号 ${orderNo}，取消后如需购买请重新下单。`,
      okText: '取消订单',
      okButtonProps: { danger: true },
      cancelText: '保留',
      onOk: async () => {
        try {
          await billingApi.cancelOrder(orderNo)
          message.success('订单已取消')
          await load()
        } catch (e) {
          message.error(errMsg(e))
        }
      },
    })
  }

  return (
    <div className="page">
      <Card variant="borderless">
        <Row gutter={16} align="middle">
          <Col flex="auto">
            <Typography.Title level={4} style={{ margin: 0 }}>
              剩余 <span style={{ color: '#2c6e49' }}>{account?.balance ?? 0}</span> 次诊断
            </Typography.Title>
            <Typography.Text type="secondary">
              累计购买 {account?.total_purchased ?? 0} 次 ·
              已使用 {account?.total_used ?? 0} 次
              {account?.next_expiry_at && (
                <> · 最近到期 {dayjs(account.next_expiry_at).format('YYYY-MM-DD')}</>
              )}
            </Typography.Text>
          </Col>
          <Col>
            <Button onClick={load} loading={loading}>刷新</Button>
          </Col>
        </Row>
      </Card>

      <Row gutter={16} style={{ marginTop: 16 }}>
        {plans.map((p) => {
          const disabled = p.sku === 'trial' && !account?.trial_available
          return (
            <Col span={8} key={p.sku}>
              <Card
                variant="borderless"
                style={{
                  textAlign: 'center',
                  border: p.sku === 'pack5' ? '2px solid #2c6e49' : undefined,
                }}
              >
                <Typography.Title level={5} style={{ marginTop: 0 }}>
                  {p.name}
                </Typography.Title>
                <div style={{ fontSize: 30, fontWeight: 600, color: '#2c6e49' }}>
                  ¥{p.price_yuan.toFixed(0)}
                </div>
                <Typography.Text type="secondary">
                  {p.quantity} 次 · 约 ¥{p.per_unit_yuan.toFixed(0)}/次
                </Typography.Text>
                <div style={{ margin: '10px 0', minHeight: 24 }}>
                  {p.badge && <Tag color="gold">{p.badge}</Tag>}
                </div>
                <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                  {p.desc}
                </Typography.Paragraph>
                <Button
                  type={p.sku === 'pack5' ? 'primary' : 'default'}
                  block
                  disabled={disabled}
                  onClick={() => buy(p)}
                >
                  {disabled ? '已使用过' : '购买'}
                </Button>
              </Card>
            </Col>
          )
        })}
      </Row>

      <Card variant="borderless" title="我的订单" style={{ marginTop: 16 }}>
        <Table<OrderInfo>
          rowKey="order_no"
          dataSource={orders}
          loading={loading}
          pagination={false}
          size="small"
          locale={{ emptyText: '暂无订单' }}
          columns={[
            {
              title: '订单号', dataIndex: 'order_no', width: 200,
              render: (v: string) => <code style={{ fontSize: 12 }}>{v}</code>,
            },
            { title: '套餐', dataIndex: 'sku', width: 90 },
            {
              title: '金额', dataIndex: 'amount_yuan', width: 100,
              render: (v: number) => `¥${v.toFixed(0)}`,
            },
            {
              title: '次数', dataIndex: 'quantity', width: 70,
            },
            {
              title: '状态', dataIndex: 'status', width: 100,
              render: (v: string) => {
                const map: Record<string, [string, string]> = {
                  pending: ['orange', '待付款'],
                  paid: ['green', '已完成'],
                  canceled: ['default', '已取消'],
                  refunded: ['blue', '已退款'],
                  failed: ['red', '失败'],
                }
                const [c, t] = map[v] ?? ['default', v]
                return <Tag color={c}>{t}</Tag>
              },
            },
            {
              title: '创建时间', dataIndex: 'created_at', width: 160,
              render: (v: string) => (v ? dayjs(v).format('MM-DD HH:mm') : '—'),
            },
            {
              title: '操作', width: 90,
              render: (_, r) => r.status === 'pending' ? (
                <Button type="link" size="small" danger
                        onClick={() => cancel(r.order_no)}>
                  取消
                </Button>
              ) : null,
            },
          ]}
        />
      </Card>

      <Card variant="borderless" title="额度流水" style={{ marginTop: 16 }}>
        <Table<LedgerEntry>
          rowKey="id"
          dataSource={ledger}
          loading={loading}
          pagination={{ pageSize: 10, size: 'small' }}
          size="small"
          locale={{ emptyText: '暂无流水' }}
          columns={[
            {
              title: '变动', dataIndex: 'delta', width: 90,
              render: (v: number) => (
                <span style={{ color: v > 0 ? '#52c41a' : '#cf1322', fontWeight: 600 }}>
                  {v > 0 ? `+${v}` : v}
                </span>
              ),
            },
            {
              title: '余额', dataIndex: 'balance_after', width: 80,
            },
            {
              title: '类型', dataIndex: 'reason', width: 110,
              render: (v: string) => <Tag>{REASON_LABEL[v] ?? v}</Tag>,
            },
            { title: '说明', dataIndex: 'note', ellipsis: true },
            {
              title: '到期', dataIndex: 'expires_at', width: 110,
              render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD') : '—'),
            },
            {
              title: '时间', dataIndex: 'created_at', width: 160,
              render: (v: string) => (v ? dayjs(v).format('MM-DD HH:mm') : '—'),
            },
          ]}
        />
      </Card>

      <Alert
        style={{ marginTop: 16 }}
        type="info"
        showIcon
        message="关于计费"
        description={
          <Space direction="vertical" size={4}>
            <span>· 诊断失败会自动退还次数，不会消耗额度</span>
            <span>· 单次诊断的算力成本约 ¥0.36，远低于售价</span>
            <span>· 当前为人工核销；支付宝商户号就绪后将支持自动支付</span>
          </Space>
        }
      />
    </div>
  )
}
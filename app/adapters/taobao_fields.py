"""淘宝（千牛）导出文件字段映射表。

这是语义层的**平台适配层**：把千牛导出的中文列名，映射到标准 STG 表字段。

设计原则：
  - 别名驱动：一个标准字段可以有多个候选源列名（不同版本千牛导出列名有差异）
  - 置信度分级：完全匹配 = 1.0，模糊匹配 = 0.5~0.9，需人工确认
  - 不确定即标注：每个映射带 note，说明为什么可能不准

数据结构：
    SOURCE_SPECS[table_name] = SourceSpec(...)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldRule:
    """一条字段映射规则。"""
    target: str                     # 标准 STG 字段名
    aliases: tuple[str, ...]        # 候选源列名（按优先级）
    required: bool = False          # 缺失是否算错误
    note: str = ''                  # 不确定说明
    confidence: float = 1.0         # 默认置信度（别名精确匹配时）


@dataclass(frozen=True)
class SourceSpec:
    """一种导出文件的规格。"""
    key: str                        # 内部标识，如 'sycm_traffic'
    display_name: str               # 给用户看
    target_table: str               # 标准 STG 表名
    export_path: str                # 千牛导出路径（给用户的操作指引）
    rules: tuple[FieldRule, ...]    # 字段映射规则
    required_sheets: tuple[str, ...] = ()   # 需要的 sheet 名（Excel 多 sheet 时）
    header_row_candidates: tuple[int, ...] = (0, 1, 2)  # 表头可能在第几行
    notes: tuple[str, ...] = field(default=())      # 整体说明/不确定项

    @property
    def field_count(self) -> int:
        return len(self.rules)

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(r.target for r in self.rules if r.required)

    def find_rule(self, target: str) -> FieldRule | None:
        for r in self.rules:
            if r.target == target:
                return r
        return None


# ============================================================================
# ① 生意参谋 · 流量
# ============================================================================
SYCM_TRAFFIC = SourceSpec(
    key='sycm_traffic',
    display_name='生意参谋 · 流量数据',
    target_table='stg_sycm_traffic',
    export_path='千牛工作台 → 生意参谋 → 流量 → 流量看板 / 商品效果 → 下载',
    header_row_candidates=(0, 1, 2, 3),
    rules=(
        FieldRule('stat_date', ('日期', '统计日期', '时间', 'date'), required=True),
        FieldRule('stat_scope', ('统计范围', '维度', '范围'), confidence=0.8,
                  note='部分导出无此列，缺失时按 SHOP 处理'),
        FieldRule('item_id', ('商品ID', '宝贝ID', '商品id', 'item_id'),
                  confidence=0.9, note='店铺级导出无此列'),
        FieldRule('item_title', ('商品名称', '宝贝名称', '商品标题', '标题')),
        FieldRule('visitors', ('访客数', '访客', 'UV', '访客量'), required=True),
        FieldRule('page_views', ('浏览量', 'PV', '浏览次数'), required=True),
        FieldRule('pay_buyers', ('支付买家数', '支付人数', '成交买家数'), required=True),
        FieldRule('pay_items', ('支付件数', '支付子订单数', '成交件数')),
        FieldRule('pay_amount', ('支付金额', '成交金额', '支付金额(元)'), required=True),
        FieldRule('order_buyers', ('下单买家数', '下单人数')),
        FieldRule('order_items', ('下单件数', '下单子订单数')),
        FieldRule('order_amount', ('下单金额',)),
        FieldRule('bounce_rate', ('跳失率',)),
        FieldRule('stay_time', ('平均停留时长', '停留时长', '人均停留时长')),
        FieldRule('source_channel', ('流量来源', '来源', '渠道'), confidence=0.85,
                  note='只在「流量来源」报表中有此列'),
        FieldRule('cart_buyers', ('加购人数', '加购买家数')),
        FieldRule('favorite_buyers', ('收藏人数', '收藏买家数')),
    ),
    notes=(
        '[UNCERTAIN-1] 生意参谋基础版/专业版导出的列不一致；'
        '本规格按「流量看板」常见列设计，接入真实文件后请核对字段映射页。',
        '「支付金额」按淘宝官方定义：未剔除事后退款、预售付清尾款当天计入。',
    ),
)


# ============================================================================
# ② 已卖出的宝贝 · 订单
# ============================================================================
TRADE_ORDERS = SourceSpec(
    key='trade_orders',
    display_name='已卖出的宝贝 · 订单明细',
    target_table='stg_trade_orders',
    export_path='千牛工作台 → 交易 → 已卖出的宝贝 → 批量导出 → 导出Excel',
    header_row_candidates=(0, 1, 2),
    rules=(
        FieldRule('order_id', ('订单编号', '主订单编号', '订单号'), required=True),
        FieldRule('sub_order_id', ('子订单编号', '子订单号'), required=True),
        FieldRule('create_time', ('订单创建时间', '创建时间', '下单时间'), required=True),
        FieldRule('pay_time', ('买家付款时间', '付款时间', '支付时间'), required=True),
        FieldRule('ship_time', ('发货时间',)),
        FieldRule('confirm_time', ('确认收货时间', '成交时间')),
        FieldRule('order_status', ('订单状态', '交易状态'), required=True),
        FieldRule('refund_status', ('退款状态',)),
        FieldRule('buyer_nick', ('买家会员名', '买家昵称', '会员名')),
        FieldRule('item_id', ('商品ID', '宝贝ID'), required=True),
        FieldRule('item_title', ('商品标题', '宝贝标题', '商品名称')),
        FieldRule('merchant_code', ('商家编码', '商家编码(SKU)', '外部编码'),
                  required=True,
                  note='★成本匹配的关键字段；缺失则无法计算 COGS'),
        FieldRule('sku_id', ('SKU ID', 'skuId', '规格ID')),
        FieldRule('sku_props', ('sku属性', '规格', '商品属性', '销售属性')),
        FieldRule('price', ('单价', '商品单价')),
        FieldRule('quantity', ('购买数量', '数量', '商品数量'), required=True),
        FieldRule('pay_amount', ('买家实际支付金额', '实付金额', '买家实付'),
                  required=True,
                  note='[UNCERTAIN] 是否含运费因导出报表而异，需核对'),
        FieldRule('goods_amount', ('商品金额', '货款金额'),
                  note='部分导出无此列，缺失时用「实付-运费」回推'),
        FieldRule('shipping_fee', ('运费', '邮费', '物流费用')),
        FieldRule('discount', ('优惠金额', '店铺优惠', '平台优惠')),
        FieldRule('seller_income', ('商家实收金额', '实收金额', '应结金额'),
                  note='[UNCERTAIN-3] 部分导出无此列'),
        FieldRule('buyer_message', ('买家留言', '买家备注')),
        FieldRule('seller_memo', ('商家备注', '卖家备注', '旗帜备注')),
        FieldRule('receiver_province', ('收货省份', '收货地址', '省份')),
    ),
    notes=(
        '[UNCERTAIN-2] 「已卖出的宝贝」与「订单报表」下载的列名略有差异，'
        '接入后请核对字段映射页。',
        '★「商家编码」是成本匹配的唯一键，必须保证所有订单都有填写。',
    ),
)


# ============================================================================
# ③ 退款 / 售后
# ============================================================================
REFUNDS = SourceSpec(
    key='refunds',
    display_name='退款售后 · 退款明细',
    target_table='stg_refunds',
    export_path='千牛工作台 → 退款售后 → 退款管理 → 导出',
    header_row_candidates=(0, 1, 2),
    rules=(
        FieldRule('refund_id', ('退款编号', '退款单号', '售后编号'), required=True),
        FieldRule('order_id', ('订单编号', '主订单编号')),
        FieldRule('sub_order_id', ('子订单编号', '子订单号')),
        FieldRule('apply_time', ('退款申请时间', '申请时间', '创建时间'), required=True,
                  note='★退款归属日期以此为准'),
        FieldRule('refund_success_time', ('退款成功时间', '退款完成时间')),
        FieldRule('refund_status', ('退款状态', '售后状态'), required=True),
        FieldRule('refund_type', ('退款类型', '售后类型', '服务类型')),
        FieldRule('item_id', ('商品ID', '宝贝ID')),
        FieldRule('merchant_code', ('商家编码', '外部编码')),
        FieldRule('quantity', ('退款数量', '退货数量', '数量')),
        FieldRule('refund_amount', ('退款金额', '退款总额', '实退金额'), required=True),
        FieldRule('refund_reason', ('退款原因', '退款理由', '售后原因')),
        FieldRule('refund_stage', ('退款阶段', '售后阶段'),
                  note='[UNCERTAIN-4] 部分导出无此列；缺失时按发货时间推断'),
    ),
    notes=(
        '【已核实】淘宝官方口径：退款金额按「退款申请时间」归属统计周期。',
        '[UNCERTAIN-4] 售中退款（未发货）与售后退款（已收货）的区分，'
        '对归因结论影响很大，请确认导出是否含此列。',
    ),
)


# ============================================================================
# ④ 账房 · 收支明细
# ============================================================================
ACCOUNT_FLOW = SourceSpec(
    key='account_flow',
    display_name='账房 · 收支明细',
    target_table='stg_account_flow',
    export_path='千牛工作台 → 财务 → 账房 → 收支明细 → 选择周期 → 导出Excel',
    header_row_candidates=(0, 1, 2, 3),
    rules=(
        FieldRule('flow_id', ('流水号', '账务流水号', '记账流水号'), confidence=0.9,
                  note='部分导出无此列，可留空'),
        FieldRule('posted_date', ('记账日期', '入账日期', '日期', '发生时间'),
                  required=True),
        FieldRule('trade_time', ('交易时间', '交易创建时间'), confidence=0.9),
        FieldRule('order_id', ('订单编号', '关联订单号'), confidence=0.9,
                  note='月度服务费等无关联订单'),
        FieldRule('biz_type', ('业务类型', '业务场景', '费用类型', '摘要'),
                  required=True,
                  note='★费用归类的关键字段'),
        FieldRule('income_expense', ('收支类型', '收入/支出', '方向'), required=True),
        FieldRule('amount', ('金额', '发生金额', '金额(元)'), required=True),
        FieldRule('balance', ('余额', '账户余额'), confidence=0.9),
        FieldRule('remark', ('备注', '说明', '摘要说明')),
        FieldRule('counterparty', ('对方', '交易对方', '对方账户'), confidence=0.8),
    ),
    notes=(
        '★[UNCERTAIN-5] 账房「业务类型」的实际取值未知 —— '
        '这是本项目最大的不确定项。',
        'ETL 会把未映射的业务类型记入 dwd_etl_errors（WARN）并归入 OTHER，'
        '不会静默丢弃。接入真实文件后请补全 dim_fee_mapping。',
    ),
)


# ============================================================================
# ⑤ 广告报表
# ============================================================================
AD_REPORTS = SourceSpec(
    key='ad_reports',
    display_name='推广报表 · 直通车/万相台',
    target_table='stg_ad_reports',
    export_path='万相台无界版 / 直通车 → 报表 → 选择日期 → 导出',
    header_row_candidates=(0, 1, 2),
    rules=(
        FieldRule('stat_date', ('日期', '时间', '统计日期'), required=True),
        FieldRule('channel', ('渠道', '推广渠道', '来源'), confidence=0.85,
                  note='部分导出无此列，按文件名/用户选择判定'),
        FieldRule('campaign_id', ('计划ID', '推广计划ID', '单元ID'), confidence=0.9),
        FieldRule('campaign_name', ('计划名称', '推广计划', '单元名称')),
        FieldRule('item_id', ('商品ID', '宝贝ID', '推广商品ID')),
        FieldRule('impressions', ('展现量', '曝光量', '展现次数')),
        FieldRule('clicks', ('点击量', '点击次数'), required=True),
        FieldRule('ctr', ('点击率', 'CTR')),
        FieldRule('spend', ('花费', '消耗', '花费(元)', '总花费'), required=True),
        FieldRule('cpc', ('平均点击花费', '点击单价', 'CPC')),
        FieldRule('attributed_orders', ('成交笔数', '总成交笔数', '成交订单数'),
                  note='归因窗口视渠道而定'),
        FieldRule('attributed_items', ('成交件数', '总成交件数')),
        FieldRule('attributed_amount', ('成交金额', '总成交金额', '直接成交金额'),
                  note='[UNCERTAIN-6] 是否含间接成交待确认'),
        FieldRule('attributed_gmv', ('总成交金额', '成交金额(含间接)'),
                  confidence=0.8),
        FieldRule('cart_adds', ('加购数', '总加购数', '宝贝收藏数')),
        FieldRule('favorites', ('收藏数', '总收藏数')),
    ),
    notes=(
        '[UNCERTAIN-6] 直通车与万相台报表列不同；'
        '归因窗口可能不同（当日/3天/15天），跨渠道汇总会失真。',
        '★接入前请确认各渠道的归因窗口天数，写入 M15 的 caliber_rules。',
    ),
)


# ============================================================================
# ⑥ 采购成本（自录，非导出）
# ============================================================================
PURCHASE_BATCHES = SourceSpec(
    key='purchase_batches',
    display_name='采购成本（自录）',
    target_table='stg_purchase_batches',
    export_path='无平台导出 —— 由商家提供 Excel 模板',
    header_row_candidates=(0, 1),
    rules=(
        FieldRule('batch_id', ('批次号', '批次ID'), confidence=0.9),
        FieldRule('merchant_code', ('商家编码', 'SKU编码', '货号'), required=True,
                  note='★必须与订单的商家编码一致'),
        FieldRule('item_id', ('商品ID', '宝贝ID'), confidence=0.8),
        FieldRule('purchase_date', ('采购日期', '下单日期')),
        FieldRule('arrival_date', ('到仓日期', '入库日期', '到货日期')),
        FieldRule('quantity', ('数量', '采购数量', '入库数量'), required=True),
        FieldRule('unit_cost', ('采购单价', '单价', '进货价'), required=True),
        FieldRule('freight_total', ('运费', '总运费', '头程运费')),
        FieldRule('other_cost', ('其他成本', '包装费', '杂费')),
        FieldRule('supplier', ('供应商', '厂家')),
        FieldRule('currency', ('币种', '货币'), confidence=0.9,
                  note='淘宝为 CNY，保留列以备多平台'),
    ),
    notes=(
        '★成本计价方法（移动加权/FIFO）尚未确认 —— 见 M11 的 caliber_rules。',
        '必须保证「商家编码」与订单一致，否则 COGS 无法计算。',
    ),
)


# ============================================================================
# ⑦ 库存（自录或千牛导出）
# ============================================================================
INVENTORY = SourceSpec(
    key='inventory',
    display_name='库存数据',
    target_table='stg_inventory',
    export_path='千牛工作台 → 库存 → 库存管理 → 导出（或商家自录）',
    header_row_candidates=(0, 1, 2),
    rules=(
        FieldRule('stat_date', ('日期', '统计日期'), required=True),
        FieldRule('item_id', ('商品ID', '宝贝ID'), confidence=0.9),
        FieldRule('merchant_code', ('商家编码', '外部编码')),
        FieldRule('sku_id', ('SKU ID', '规格ID'), confidence=0.8),
        FieldRule('sellable_qty', ('可售库存', '库存', '可用库存'), required=True),
        FieldRule('locked_qty', ('锁定库存', '占用库存'), confidence=0.8),
        FieldRule('inbound_qty', ('在途库存', '在途数量'), confidence=0.8),
    ),
)


# ============================================================================
# 注册表
# ============================================================================
SOURCE_SPECS: dict[str, SourceSpec] = {
    s.key: s for s in (
        SYCM_TRAFFIC, TRADE_ORDERS, REFUNDS,
        ACCOUNT_FLOW, AD_REPORTS, PURCHASE_BATCHES, INVENTORY,
    )
}

# 按 STG 表名索引
BY_TABLE: dict[str, SourceSpec] = {s.target_table: s for s in SOURCE_SPECS.values()}


def get_spec(key: str) -> SourceSpec:
    """按 key 取规格，不存在则报错（不返回 None，避免静默跳过）。"""
    spec = SOURCE_SPECS.get(key)
    if spec is None:
        raise KeyError(
            f'未知的数据源类型: {key!r}。'
            f'可用: {", ".join(sorted(SOURCE_SPECS))}')
    return spec


def all_specs() -> list[SourceSpec]:
    return list(SOURCE_SPECS.values())


def spec_summary() -> list[dict]:
    """给前端展示的数据源清单。"""
    return [
        {
            'key': s.key,
            'display_name': s.display_name,
            'target_table': s.target_table,
            'export_path': s.export_path,
            'field_count': s.field_count,
            'required_fields': list(s.required_fields),
            'notes': list(s.notes),
        }
        for s in SOURCE_SPECS.values()
    ]
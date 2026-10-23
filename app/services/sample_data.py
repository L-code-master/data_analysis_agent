"""样例数据生成器 —— 让新用户 5 分钟内看到一份完整诊断报告。

设计目标：
  1. **讲一个经营故事**（4 周，2 个商品）：
     周 1-2 稳定 → 周 3 加大直通车投放 + 连衣裙出现质量退款 →
     周 4 收手。诊断报告应能"看出"：转化率下滑、退款集中在连衣裙、
     广告 ROI 从 ~9 掉到 ~3.5。
  2. **数字自洽**：流量表 pay_amount = 当日订单合计（逐日核对）；
     账房佣金/技术服务费 = 当日支付金额 × 固定费率。
  3. **列名与适配器规格精确匹配**（置信度 1.0，无需人工映射）。
  4. **确定性**：random.Random(42)，每次生成结果一致，便于测试。

产出：
  - build_files()  -> [(文件名, CSV bytes)]  6 份数据 + 说明
  - build_xlsx()   -> 一个 Excel（7 个工作表：说明 + 6 份数据）
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import random
from pathlib import Path

START = dt.date(2026, 9, 1)
DAYS = 28
# ★ 种子在 _gen_dataset 内部重建：模块级全局 Random 会在同进程第二次
#   调用时漂移（随机流被上次调用消耗），导致"每次生成一致"被破坏。
SEED = 42

# 商品定义：(商家编码, 商品ID, 标题, 单价, 成本批次)
PRODUCTS = [
    ('MC-A', 'ITEM-A', '秋季连衣裙 灯芯绒收腰', 199.0,
     ('BAT-A', 500, 85.0, 2000.0)),
    ('MC-B', 'ITEM-B', '针织半身裙 高腰A字', 129.0,
     ('BAT-B', 400, 58.0, 1600.0)),
]

# 各周参数：访客基数 / 支付转化率 / A 退款率 / 直通车花费 / ROI
WEEKS = [
    {'visitors': 1500, 'cvr': 0.031, 'refund_a': 0.03, 'spend': 600, 'roi': 9.2},
    {'visitors': 1560, 'cvr': 0.030, 'refund_a': 0.03, 'spend': 650, 'roi': 8.8},
    {'visitors': 2600, 'cvr': 0.019, 'refund_a': 0.22, 'spend': 1400, 'roi': 3.5},
    {'visitors': 1700, 'cvr': 0.026, 'refund_a': 0.05, 'spend': 900, 'roi': 5.2},
]

PROVINCES = ['浙江', '江苏', '广东', '上海', '北京', '山东', '四川', '福建']
REFUND_REASONS_A = ['质量问题-起球', '质量问题-色差', '尺寸不符']
REFUND_REASONS_B = ['尺寸不符', '不想要了']

# 标准列名（与 app/adapters/taobao_fields.py 的别名精确对应 → 置信度 1.0）
HEADERS = {
    'orders': ['订单编号', '子订单编号', '订单创建时间', '买家付款时间', '发货时间',
               '确认收货时间', '订单状态', '退款状态', '买家会员名', '商品ID',
               '商品标题', '商家编码', 'SKU ID', 'sku属性', '单价', '购买数量',
               '买家实际支付金额', '商品金额', '运费', '优惠金额', '商家实收金额',
               '收货省份'],
    'traffic': ['日期', '访客数', '浏览量', '支付买家数', '支付件数', '支付金额',
                '下单买家数', '下单件数', '下单金额', '跳失率', '平均停留时长',
                '加购人数', '收藏人数'],
    'account': ['流水号', '记账日期', '交易时间', '订单编号', '业务类型',
                '收支类型', '金额', '余额', '备注', '对方'],
    'refunds': ['退款编号', '订单编号', '子订单编号', '退款申请时间', '退款成功时间',
                '退款状态', '退款类型', '商品ID', '商家编码', '退款数量',
                '退款金额', '退款原因', '退款阶段'],
    'ads': ['日期', '渠道', '计划ID', '计划名称', '商品ID', '展现量', '点击量',
            '点击率', '花费', '平均点击花费', '成交笔数', '成交件数',
            '成交金额', '总成交金额', '加购数', '收藏数'],
    'purchase': ['批次号', '商家编码', '商品ID', '采购日期', '到仓日期', '数量',
                 '采购单价', '运费', '其他成本', '供应商', '币种'],
}

SHEET_NAMES = {
    '说明': 'readme',
    'orders': '订单明细',
    'traffic': '生意参谋流量',
    'account': '账房收支明细',
    'refunds': '退款售后明细',
    'ads': '推广报表',
    'purchase': '采购成本',
}


def _gen_dataset() -> dict[str, list[list]]:
    """生成全部数据，返回 {类型: 行列表（不含表头）}。

    每次调用都从固定种子开始 —— 同一进程内多次调用结果一致（可测试）。
    """
    R = random.Random(SEED)
    orders: list[list] = []
    refunds: list[list] = []
    account: list[list] = []
    traffic: list[list] = []
    ads: list[list] = []

    seq = 0
    rseq = 0
    fseq = 0
    balance = 50000.0

    for i in range(DAYS):
        d = START + dt.timedelta(days=i)
        w = WEEKS[i // 7]

        visitors = int(w['visitors'] * R.uniform(0.92, 1.08))
        cvr = w['cvr'] * R.uniform(0.94, 1.06)
        buyers = max(1, round(visitors * cvr))

        day_orders = []
        day_pay = 0.0
        day_qty = 0
        for _ in range(buyers):
            # 60% 买 A，40% 买 B
            code, item_id, title, price, _bat = (
                PRODUCTS[0] if R.random() < 0.6 else PRODUCTS[1])
            qty = 2 if R.random() < 0.12 else 1
            pay = round(price * qty, 2)
            seq += 1
            oid = f'TB2609{i + 1:02d}{seq:05d}'
            pay_t = f'{d.isoformat()} {R.randint(9, 21):02d}:{R.randint(10, 59):02d}:00'
            create_t = pay_t
            ship_t = f'{(d + dt.timedelta(days=1)).isoformat()} 18:00:00'
            confirm_t = f'{(d + dt.timedelta(days=3)).isoformat()} 12:00:00'
            reason = R.choice(REFUND_REASONS_A if code == 'MC-A' else REFUND_REASONS_B)
            refunded = R.random() < (w['refund_a'] if code == 'MC-A' else 0.02)

            orders.append([
                oid, f'{oid}-1', create_t, pay_t, ship_t, confirm_t,
                '交易成功', '退款成功' if refunded else '无退款',
                f'buyer{seq:05d}', item_id, title, code, 'SKU-1',
                '默认', price, qty, f'{pay:.2f}', f'{pay:.2f}', '0.00',
                '0.00', f'{pay:.2f}', R.choice(PROVINCES),
            ])
            day_orders.append((oid, code, item_id, pay, qty, refunded, reason))
            day_pay += pay
            day_qty += qty

        # 退款行：申请时间 = 付款日 + 2~6 天（体现"按申请日归属"的口径故事）
        for oid, code, item_id, pay, qty, refunded, reason in day_orders:
            if not refunded:
                continue
            rseq += 1
            delay = R.randint(2, 6)
            apply_d = min(d + dt.timedelta(days=delay), START + dt.timedelta(days=DAYS - 1))
            refunds.append([
                f'RF2609{rseq:06d}', oid, f'{oid}-1',
                f'{apply_d.isoformat()} 20:00:00',
                f'{(apply_d + dt.timedelta(days=1)).isoformat()} 10:00:00',
                '退款成功', '仅退款', item_id, code, qty,
                f'{pay:.2f}', reason, '',
            ])

        # 账房：当日佣金 5% + 技术服务费 2%
        for ftype, rate in (('天猫佣金', 0.05), ('技术服务费', 0.02)):
            fseq += 1
            amt = -round(day_pay * rate, 2)
            balance += amt
            account.append([
                f'F2609{fseq:06d}', d.isoformat(), '', '',
                ftype, '支出', f'{amt:.2f}', f'{balance:.2f}', '', '淘宝',
            ])

        # 生意参谋流量（与订单逐日自洽）
        pv = int(visitors * R.uniform(2.3, 2.6))
        traffic.append([
            d.isoformat(), visitors, pv, buyers, day_qty, f'{day_pay:.2f}',
            int(buyers * R.uniform(1.05, 1.15)), int(day_qty * 1.1),
            f'{day_pay * 1.03:.2f}', f'{R.uniform(0.40, 0.48):.2%}',
            f'{R.randint(55, 75)}', int(visitors * R.uniform(0.08, 0.10)),
            int(visitors * R.uniform(0.04, 0.05)),
        ])

        # 推广报表（直通车）
        spend = w['spend'] * R.uniform(0.95, 1.05)
        roi = w['roi'] * R.uniform(0.92, 1.08)
        attributed = spend * roi
        clicks = int(spend / 0.75)
        imps = int(clicks / R.uniform(0.015, 0.018))
        ads.append([
            d.isoformat(), '直通车', 'CMP-001', '秋季新品引流计划',
            '', imps, clicks, f'{clicks / imps:.2%}', f'{spend:.2f}',
            '0.75', int(attributed / 170), int(attributed / 170 * 1.1),
            f'{attributed:.2f}', f'{attributed * 1.15:.2f}',
            int(visitors * 0.06), int(visitors * 0.03),
        ])

    # 采购成本（静态批次）
    purchase = []
    for code, item_id, _t, _p, (bid, qty, unit, freight) in PRODUCTS:
        purchase.append([
            bid, code, item_id, '2026-08-05', '2026-08-20',
            qty, f'{unit:.2f}', f'{freight:.2f}', '0.00',
            '杭州四季服饰', 'CNY',
        ])

    return {'orders': orders, 'traffic': traffic, 'account': account,
            'refunds': refunds, 'ads': ads, 'purchase': purchase}


README_SHEET = [
    ['样例数据使用说明（本表不会被识别为数据源）'],
    [],
    ['这是为演示「经营诊断」准备的一套仿真淘宝店铺数据。'],
    ['时间范围：2026-09-01 ~ 2026-09-28（4 周），2 个商品。'],
    [],
    ['使用步骤：'],
    ['1. 直接把本文件拖到"上传数据"区域（无需解压）'],
    ['2. 系统会自动识别 6 张数据表，拆分为 6 个数据源'],
    ['3. 点击"开始诊断"，选择统计时间范围（如 2026-09-01~2026-09-28）'],
    ['4. 等待约 10 秒，查看诊断报告'],
    [],
    ['数据里藏了一个经营故事，看报告能不能找出来：'],
    ['- 第 3 周（9/15-9/21）加大了直通车投放，流量上涨但转化率下滑'],
    ['- 连衣裙（MC-A）在第 3 周出现质量问题退款（起球/色差）'],
    ['- 广告 ROI 从第 1 周的 ~9 掉到第 3 周的 ~3.5'],
    [],
    ['各表对应的千牛导出位置：'],
    ['· 订单明细       千牛 → 交易 → 已卖出的宝贝 → 批量导出'],
    ['· 生意参谋流量   千牛 → 生意参谋 → 流量 → 下载'],
    ['· 账房收支明细   千牛 → 财务 → 账房 → 收支明细 → 导出'],
    ['· 退款售后明细   千牛 → 退款售后 → 退款管理 → 导出'],
    ['· 推广报表       万相台 / 直通车 → 报表 → 导出'],
    ['· 采购成本       无平台导出，按本表格式自录'],
]


def _csv_bytes(rows: list[list], header: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    # \u2605 docs/16 P2-4\uff1autf-8-sig \u7f16\u7801\u81ea\u5e26\u4e00\u4e2a BOM \u2014\u2014 \u539f\u5148\u518d\u624b\u5de5\u62fc
    #   '\ufeff' \u4f1a\u4ea7\u51fa\u53cc BOM\uff0c\u9996\u5217\u540d\u53d8\u6210 '\ufeff\u8ba2\u5355\u7f16\u53f7'\uff0c\u5217\u5339\u914d
    #   \u964d\u7ea7\u89e6\u53d1\u4eba\u5de5\u786e\u8ba4\u3002
    return buf.getvalue().encode('utf-8-sig')


def build_files() -> list[tuple[str, bytes]]:
    """6 份数据（CSV，带 BOM，Excel 可直接打开）。"""
    data = _gen_dataset()
    return [
        ('订单明细.csv', _csv_bytes(data['orders'], HEADERS['orders'])),
        ('生意参谋流量.csv', _csv_bytes(data['traffic'], HEADERS['traffic'])),
        ('账房收支明细.csv', _csv_bytes(data['account'], HEADERS['account'])),
        ('退款售后明细.csv', _csv_bytes(data['refunds'], HEADERS['refunds'])),
        ('推广报表.csv', _csv_bytes(data['ads'], HEADERS['ads'])),
        ('采购成本.csv', _csv_bytes(data['purchase'], HEADERS['purchase'])),
    ]


def build_xlsx() -> bytes:
    """单个 Excel：说明 + 6 张数据表（上传时自动拆分）。"""
    from openpyxl import Workbook

    data = _gen_dataset()
    wb = Workbook()

    ws = wb.active
    ws.title = SHEET_NAMES['说明']
    for row in README_SHEET:
        ws.append(row)

    for key in ('orders', 'traffic', 'account', 'refunds', 'ads', 'purchase'):
        ws = wb.create_sheet(SHEET_NAMES[key])
        ws.append(HEADERS[key])
        for row in data[key]:
            ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def story_summary() -> dict:
    """给 API 元数据用的简介。"""
    return {
        'period': f'{START.isoformat()} ~ {(START + dt.timedelta(days=DAYS - 1)).isoformat()}',
        'days': DAYS,
        'products': [
            {'merchant_code': c, 'item_id': i, 'title': t, 'price': p}
            for c, i, t, p, _ in PRODUCTS
        ],
        'story': (
            '第 1-2 周经营稳定（转化率 ~3%）；第 3 周加大直通车投放，'
            '访客涨到 ~2600/天但转化率降到 ~1.9%，同时连衣裙出现质量退款'
            '（起球/色差，退款率 ~22%）；第 4 周收手回落。'
            '报告应能定位：转化率下滑、退款集中在 MC-A、广告 ROI 9 → 3.5。'
        ),
        'sheets': [SHEET_NAMES[k] for k in
                   ('说明', 'orders', 'traffic', 'account', 'refunds', 'ads', 'purchase')],
    }


if __name__ == '__main__':
    # 独立运行时把样例写到当前目录（调试用）
    out = Path('sample-data.xlsx')
    out.write_bytes(build_xlsx())
    print(f'written {out} ({out.stat().st_size} bytes)')

"""模块 2 验证：语义层接口（Registry + Resolver + 质量门）。

用 SQLite 作为 DuckDB 的代理运行时（沙箱内无法安装 duckdb）。
semantic/schema.sql 里的 DuckDB 方言部分被剥离，只保留 SEM 层
（指标注册表 + 维度白名单 + 质量结果），这正是 Registry 依赖的部分。
"""
import sys, pathlib, re, sqlite3

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.semantic.registry import SemanticRegistry
from app.semantic.models import UncertaintyLevel

SCHEMA = ROOT / 'semantic' / 'schema.sql'
sql = SCHEMA.read_text(encoding='utf-8')

# 提取 SEM 层三段：建表 + 指标注册 + 维度白名单
def extract(start_marker, end_marker):
    i = sql.index(start_marker)
    j = sql.index(end_marker, i)
    return sql[i:j]

sem_ddl = extract('CREATE TABLE IF NOT EXISTS sem_metric_registry',
                  '-- ===== S5 ETL')
# 指标 INSERT
ins_i = sql.index('DELETE FROM sem_metric_registry;')
ins_j = sql.index('-- 维度白名单（单一真源）')
sem_insert = sql[ins_i:ins_j]
# 维度白名单
dim_i = sql.index('DELETE FROM sem_metric_dimension;')
dim_j = sql.index('-- 一致性断言')
dim_insert = sql[dim_i:dim_j]

con = sqlite3.connect(':memory:')
con.row_factory = sqlite3.Row

# 只用 DDL（三张 sem_ 表）+ 数据，跳过 DuckDB 专有语法
con.executescript(sem_ddl)
con.executescript(sem_insert)
con.executescript(dim_insert)

# 补质量结果测试数据（真实环境由 QC-xx 写入）
con.executescript('''
INSERT INTO sem_quality_result VALUES
 ('Q1','v1-full-refresh','M06','QC-01','ERROR',1,'>= 0','0','',''),
 ('Q2','v1-full-refresh','M13','QC-02','ERROR',1,'[-1,1]','0.51','',''),
 ('Q3','v1-full-refresh','M03','QC-04','WARN',0,'<= 1','1.2','支付转化率 > 100%',''),
 ('Q4','v1-full-refresh','M13','QC-08','ERROR',1,'分摊合计=账房总额','0','','');
''')

print('=' * 76)
print('模块 2 验证：语义层接口')
print('=' * 76)

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond: fails.append(label)

# ---------- 1. 加载 ----------
print('\n[1] Registry 加载')
reg = SemanticRegistry(con)
n = reg.load()
print(f'  加载指标数: {n}')
check('加载到 16 个指标', n == 16, n)

st = reg.stats()
print(f'  分类: {st["by_category"]}')
print(f'  优先级: {st["by_priority"]}')
print(f'  不确定度: {st["by_uncertainty"]}')
check('全部为 draft 状态', st['by_status']['draft'] == 16, st['by_status'])
check('有 high 级不确定项', st['by_uncertainty'].get('high', 0) > 0,
      st['by_uncertainty'].get('high'))
check('无 confirmed（待访谈）', st['by_status']['confirmed'] == 0)

# ---------- 2. 按 ID 查询 ----------
print('\n[2] 按 ID 查询')
m13 = reg.get('M13')
check('M13 存在', m13 is not None)
check('M13 名称正确', m13.metric_name == '毛利率（财务口径）', m13.metric_name)
check('M13 有别名', len(m13.aliases) > 0, list(m13.aliases)[:4])
check('M13 有公式', bool(m13.formula_expression), m13.formula_expression[:50])
check('M13 有维度白名单', 'item_id' in m13.allowed_dimensions,
      list(m13.allowed_dimensions))

# ---------- 3. 别名解析 ----------
print('\n[3] 别名解析')
for text in ['毛利率', '赚多少', '利润']:
    hits = reg.find(text)
    print(f'  {text!r} -> {[h.metric_id for h in hits]}')
check('毛利率命中 M13', any(h.metric_id == 'M13' for h in reg.find('毛利率')))
check('"赚多少"命中 M13', any(h.metric_id == 'M13' for h in reg.find('赚多少')))

# ---------- 4. ★ 口径歧义与透明提示（核心设计） ----------
print('\n[4] 口径歧义与透明提示')
from app.semantic.models import CaliberConflict

# 4a. 真正的歧义：一个词同时精确命中多个指标
#     构造场景：用"退款"同时精确命中 M17 与 M18 的别名
hits = reg.find('退款')
print(f'  "退款" 命中: {[h.metric_id for h in hits]}')
if len(hits) > 1:
    try:
        reg.resolve('退款')
        check('多命中时抛 CaliberConflict', False, '未报错！')
    except CaliberConflict as e:
        check('多命中时抛 CaliberConflict', True, f'候选={len(e.candidates)}')
        print(f'       错误文案: {e}')
else:
    print('       （"退款"当前只精确命中一个，改用严格模式测试见 4c）')

# 4b. 精确命中唯一时的透明提示（本轮新增设计）
#     "毛利率" 精确命中 M13，但也是 M14 别名"推广毛利率"的子串
primary, related = reg.find_with_related('毛利率')
print(f'\n  "毛利率" 直接命中: {[m.metric_id for m in primary]}')
print(f'  "毛利率" 相关未命中: {[m.metric_id for m in related]}')
check('精确命中 M13', len(primary) == 1 and primary[0].metric_id == 'M13')
check('相关提示含 M14', any(m.metric_id == 'M14' for m in related))

note = reg.ambiguity_note('毛利率')
print(f'\n  歧义提示文案: {note}')
check('生成歧义提示', bool(note))
check('提示含 M14', 'M14' in note)

# "推广毛利率" 精确命中 M14，不该再提示 M13
primary2, related2 = reg.find_with_related('推广毛利率')
check('"推广毛利率"精确命中 M14',
      len(primary2) == 1 and primary2[0].metric_id == 'M14',
      [m.metric_id for m in primary2])

# 4c. 严格消歧：显式指定
m = reg.resolve('毛利率', 'M13')
check('显式指定 M13 可解析', m.metric_id == 'M13')
m = reg.resolve('毛利率', 'M14')
check('显式指定 M14 可解析', m.metric_id == 'M14')

# 4d. 唯一命名的指标不该被干扰
m = reg.resolve('客单价')
check('"客单价"唯一命中 M05', m.metric_id == 'M05', m.metric_id)
note2 = reg.ambiguity_note('客单价')
check('"客单价"无歧义提示', note2 == '', repr(note2))

# 4e. 未知指标
try:
    reg.resolve('完全不存在的指标xyz')
    check('未知指标抛 KeyError', False)
except KeyError as e:
    check('未知指标抛 KeyError', True, str(e)[:50])

# ---------- 5. 维度白名单 ----------
print('\n[5] 维度白名单校验')
ok, reason = reg.check_dimension('M13', 'item_id')
check('M13 允许 item_id', ok)
ok, reason = reg.check_dimension('M13', 'campaign_id')
check('M13 拒绝 campaign_id', not ok)
print(f'       拒绝理由: {reason[:70]}')
ok, reason = reg.check_dimension('M15', 'campaign_id')
check('M15 允许 campaign_id', ok)
all_ok, errs = reg.validate_dimensions('M13', ['item_id', 'campaign_id'])
check('批量校验返回失败项', not all_ok and len(errs) == 1, f'{len(errs)} 错误')

# ---------- 6. 质量门 ----------
print('\n[6] 质量门（验证层门1）')
passed, msg = reg.quality_passed('v1-full-refresh')
check('质量门判定为通过（无阻塞失败）', passed, msg or '(无阻塞)')
blockers = reg.blocking_failures('v1-full-refresh')
check('阻塞失败为 0', len(blockers) == 0, len(blockers))
warns = reg.warnings('v1-full-refresh')
check('警告 1 条', len(warns) == 1, len(warns))
if warns:
    print(f'       警告: [{warns[0].rule_id}] {warns[0].message}')

# ---------- 7. 不确定项披露 ----------
print('\n[7] 不确定项披露')
high = reg.uncertain_metrics(UncertaintyLevel.HIGH)
print(f'  high 级指标: {[m.metric_id for m in high]}')
check('M13 为 high 级', any(m.metric_id == 'M13' for m in high))
check('M07 为 high 级', any(m.metric_id == 'M07' for m in high))
check('M01 为 low 级', reg.get('M01').uncertainty.level == UncertaintyLevel.LOW)

disc = reg.disclaimer_for(['M13', 'M14', 'M01', 'M07'])
print()
print('  ── 生成的报告口径声明 ──')
for line in disc.splitlines():
    print(f'  {line}')
print('  ──────────────────────')
check('声明含 M13', 'M13' in disc)
check('声明含待确认项', '待确认' in disc)
check('声明不含已核实的 M01', '**访客数**' not in disc)

print()
print('=' * 76)
print('模块 2: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 76)
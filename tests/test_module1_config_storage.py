"""模块 1 验证：配置加载 + 存储层。"""
import sys, pathlib, tempfile, os, io

# 控制台强制 UTF-8（Windows GBK 控制台无法输出部分字符）
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 用工作区内的临时目录（沙箱允许），避免污染真实 data/app.db
tmp = ROOT / '.testtmp' / f'run_{os.getpid()}'
tmp.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{tmp / "test.db"}'

from app.config import get_settings, reset_settings_cache
reset_settings_cache()

print('=' * 72)
print('模块 1 验证：配置 + 存储')
print('=' * 72)

# ---------- 1. 配置加载 ----------
print('\n[1] 配置加载')
s = get_settings()
print(f'  APP_ENV                = {s.app_env}')
print(f'  LLM_MODEL              = {s.llm_model}')
# ★ docs/16 测试项：不打印开发者真实 LLM_BASE_URL（端点属半敏感配置，
#   输出会进 CI 日志/截图），只报是否已配置
print(f'  LLM_BASE_URL           = {"SET (len=%d)" % len(s.llm_base_url) if s.llm_base_url else "EMPTY"}')
print(f'  LLM_API_KEY            = {"SET (len=%d)" % len(s.llm_api_key) if s.llm_api_key else "EMPTY"}')
print(f'  SANDBOX_NETWORK_ISOLATED = {s.sandbox_network_isolated}')
print(f'  PRICE_TRIAL_CENTS      = {s.price_trial_cents} (¥{s.price_trial_cents/100:.0f})')
print(f'  PRICE_SINGLE_CENTS     = {s.price_single_cents} (¥{s.price_single_cents/100:.0f})')
print(f'  PRICE_PACK5_CENTS      = {s.price_pack5_cents} (¥{s.price_pack5_cents/100:.0f})')
print(f'  BILLING_PROVIDER       = {s.billing_provider}')
print(f'  allowed_ext_set        = {s.allowed_ext_set}')

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond: fails.append(label)

check('模型名正确（非 qwen-plus）', s.llm_model != 'qwen-plus', s.llm_model)
check('沙箱网络隔离为 true', s.sandbox_network_isolated is True)
check('定价：试用 ¥49', s.price_trial_cents == 4900)
check('定价：单次 ¥199', s.price_single_cents == 19900)
check('定价：5次包 ¥799', s.price_pack5_cents == 79900)

# ---------- 2. 非法模型名被拒 ----------
print('\n[2] 配置校验：非法模型名')
from app.config import Settings, reset_settings_cache as _reset
try:
    _bad = pathlib.Path(tmp) / 'bad.env'
    _bad.write_text('LLM_MODEL=qwen-plus\n', encoding='utf-8')
    Settings.load(_bad)
    check('qwen-plus 被拒绝', False, '未拦截')
except ValueError as e:
    check('qwen-plus 被拒绝', '不存在' in str(e), str(e)[:70])

# ---------- 3. 启动自检 ----------
print('\n[3] 启动自检')
problems = s.validate_for_startup()
print(f'  问题数: {len(problems)}')
for p in problems:
    print(f'    - {p}')
check('无阻塞问题', len(problems) == 0, f'({len(problems)} issues)')

# ---------- 4. 数据库 ----------
print('\n[4] 存储层')
from app.storage import db
db.apply_schema()
h = db.health_check()
print(f'  表数量: {h["tables"]}')
print(f'  journal_mode: {h["journal_mode"]}')
print(f'  foreign_keys: {h["foreign_keys"]}')
check('建表 >= 15 张', h['tables'] >= 15, h['tables'])
check('WAL 模式已启用', h['journal_mode'] == 'wal', h['journal_mode'])
check('外键约束已启用', h['foreign_keys'] is True)

# ---------- 5. 事务回滚 ----------
print('\n[5] 事务回滚')
import uuid, datetime
now = datetime.datetime.now().isoformat()
tid = uuid.uuid4().hex
try:
    with db.transaction() as conn:
        conn.execute('INSERT INTO tenants (id,name,status,created_at,updated_at)'
                     ' VALUES (?,?,?,?,?)', (tid, '回滚测试', 'active', now, now))
        raise RuntimeError('故意失败')
except RuntimeError:
    pass
with db.get_conn() as c:
    n = c.execute('SELECT COUNT(*) FROM tenants WHERE id=?', (tid,)).fetchone()[0]
check('异常时回滚', n == 0, f'剩余 {n} 行')

# ---------- 6. 事务提交 ----------
with db.transaction() as conn:
    conn.execute('INSERT INTO tenants (id,name,status,created_at,updated_at)'
                 ' VALUES (?,?,?,?,?)', (tid, '提交测试', 'active', now, now))
with db.get_conn() as c:
    n = c.execute('SELECT COUNT(*) FROM tenants WHERE id=?', (tid,)).fetchone()[0]
check('正常时提交', n == 1, f'{n} 行')

# ---------- 7. 外键约束生效 ----------
print('\n[6] 外键约束')
try:
    with db.transaction() as conn:
        conn.execute('INSERT INTO users (id,tenant_id,email,password_hash,'
                     'created_at,updated_at) VALUES (?,?,?,?,?,?)',
                     ('x', 'no-such-tenant', 'z@z.com', 'h', now, now))
    check('外键拦截无效租户', False, '未拦截')
except Exception as e:
    check('外键拦截无效租户', 'FOREIGN KEY' in str(e).upper(), type(e).__name__)

print()
print('=' * 72)
print('模块 1: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 72)

import shutil
shutil.rmtree(tmp, ignore_errors=True)
"""模块 6a 验证：认证服务（注册/登录/令牌/锁定）。"""
import sys, pathlib, os, datetime as dt

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / '.testtmp' / f'auth_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "auth.db"}'
os.environ['APP_SECRET_KEY'] = 'test-secret-key-for-jwt-signing-32chars-minimum'
os.environ['LOGIN_MAX_ATTEMPTS'] = '3'
os.environ['LOGIN_LOCKOUT_MINUTES'] = '15'

from app.config import reset_settings_cache
reset_settings_cache()
from app.storage import db
db.apply_schema()
from app.services import auth, security as sec

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)

print('=' * 76)
print('模块 6a 验证：认证服务')
print('=' * 76)

# ---------- 1. 注册 ----------
print('\n[1] 注册')
u, tok = auth.register('boss@shop.com', 'MyStr0ng!Pass',
                       tenant_name='测试旗舰店', ip='127.0.0.1')
print(f'  user_id:   {u.user_id[:12]}...')
print(f'  tenant_id: {u.tenant_id[:12]}...')
print(f'  role:      {u.role}')
print(f'  access:    {tok.access_token[:32]}...')
check('创建用户', bool(u.user_id))
check('创建租户', bool(u.tenant_id))
check('角色为 owner', u.role == 'owner')
check('返回令牌对', bool(tok.access_token) and bool(tok.refresh_token))
check('expires_in 合理', tok.expires_in > 0, tok.expires_in)

# 密码未存明文
with db.get_conn() as c:
    row = c.execute('SELECT password_hash FROM users WHERE email=?',
                    ('boss@shop.com',)).fetchone()
    ph = row[0]
check('密码为 bcrypt 哈希', ph.startswith('$2b$'), ph[:10])
check('密码非明文', 'MyStr0ng!Pass' not in ph)

# 自动创建额度账户
with db.get_conn() as c:
    acc = c.execute('SELECT balance FROM credit_accounts WHERE tenant_id=?',
                    (u.tenant_id,)).fetchone()
check('自动创建额度账户（赠送 1 次体验）', acc is not None and acc[0] == 1,
      acc[0] if acc else None)

# 重复邮箱
print('\n[2] 重复注册')
try:
    auth.register('boss@shop.com', 'AnotherPass123')
    check('重复邮箱被拒绝', False)
except auth.EmailExistsError as e:
    check('重复邮箱被拒绝', True, e.message)

# 弱密码
print('\n[3] 弱密码')
for pw, why in [('123', '太短'), ('password123', '常见'), ('alllowercase', '单类')]:
    try:
        auth.register(f'w{abs(hash(pw))%9999}@x.com', pw)
        check(f'弱密码 {why} 被拒绝', False)
    except auth.AuthError as e:
        check(f'弱密码 {why} 被拒绝', e.code == 'WEAK_PASSWORD', e.message)

# ---------- 4. 登录 ----------
print('\n[4] 登录')
u2, tok2 = auth.login('boss@shop.com', 'MyStr0ng!Pass', ip='127.0.0.1')
check('登录成功', u2.user_id == u.user_id)
check('返回新令牌', tok2.access_token != tok.access_token)
check('大小写不敏感邮箱', auth.login('BOSS@SHOP.COM', 'MyStr0ng!Pass')[0].user_id == u.user_id)

# 错误密码
print('\n[5] 错误密码与账号锁定（阈值 3）')
try:
    auth.login('boss@shop.com', 'WrongPass123')
    check('错误密码被拒绝', False)
except auth.InvalidCredentialsError as e:
    check('错误密码被拒绝', True, e.message)

try:
    auth.login('nonexist@x.com', 'Whatever123')
    check('不存在邮箱被拒绝', False)
except auth.InvalidCredentialsError as e:
    check('不存在邮箱返回同样错误（不泄露）', True, e.message)

# 连续失败锁定（阈值 3）
for i in range(2):                      # 第 2、3 次失败
    try:
        auth.login('boss@shop.com', 'Wrong')
    except auth.AuthError:
        pass
try:
    auth.login('boss@shop.com', 'MyStr0ng!Pass')   # 正确密码也被锁
    check('达阈值后锁定（正确密码也拒绝）', False)
except auth.InvalidCredentialsError as e:
    check('★ 达阈值后锁定且返回通用文案（不泄露状态）',
          e.code == 'INVALID_CREDENTIALS', e.message)

# 解锁以便后续测试
with db.transaction() as c:
    c.execute('UPDATE users SET locked_until=NULL, failed_attempts=0 WHERE id=?',
              (u.user_id,))

# ---------- 6. Access Token ----------
print('\n[6] Access Token 解析')
payload = auth.decode_access_token(tok2.access_token)
check('sub 为用户 ID', payload['sub'] == u.user_id)
check('tid 为租户 ID', payload['tid'] == u.tenant_id)
check('role 正确', payload['role'] == 'owner')
check('typ 为 access', payload['typ'] == 'access')

for bad in ['garbage', tok2.access_token[:-4] + 'xxxx', '']:
    try:
        auth.decode_access_token(bad)
        check(f'非法 token {bad[:14]!r} 被拒绝', False)
    except auth.AuthError:
        check(f'非法 token {bad[:14]!r} 被拒绝', True)

# ---------- 7. Refresh Token 轮换 ----------
print('\n[7] Refresh Token 轮换')
pair = auth.rotate_refresh_token(tok2.refresh_token)
check('轮换成功', bool(pair.access_token) and bool(pair.refresh_token))
check('新 refresh 与旧不同', pair.refresh_token != tok2.refresh_token)

try:
    auth.rotate_refresh_token(tok2.refresh_token)
    check('旧 refresh 已失效（防重放）', False)
except auth.AuthError as e:
    check('旧 refresh 已失效（防重放）', e.code == 'TOKEN_REVOKED', e.code)

# 库中存的是哈希
with db.get_conn() as c:
    rows = c.execute('SELECT token_hash FROM refresh_tokens').fetchall()
    hashes = [r[0] for r in rows]
check('库中存哈希不存原文', all(len(h) == 64 for h in hashes), f'{len(hashes)} 条')
check('原文不在库中', pair.refresh_token not in hashes)

# ---------- 8. 吊销 ----------
print('\n[8] 登出吊销全部令牌')
n = auth.revoke_all_tokens(u.user_id)
print(f'  吊销数量: {n}')
check('吊销生效', n > 0, n)
try:
    auth.rotate_refresh_token(pair.refresh_token)
    check('吊销后 refresh 不可用', False)
except auth.AuthError:
    check('吊销后 refresh 不可用', True)

# ---------- 9. 改密 ----------
print('\n[9] 修改密码')
u3, tok3 = auth.login('boss@shop.com', 'MyStr0ng!Pass')
try:
    auth.change_password(u.user_id, 'WrongOld123', 'NewStr0ng!Pass')
    check('旧密码错误被拒绝', False)
except auth.InvalidCredentialsError:
    check('旧密码错误被拒绝', True)

revoked = auth.change_password(u.user_id, 'MyStr0ng!Pass', 'NewStr0ng!Pass')
check('改密成功并吊销令牌', revoked >= 0, f'吊销 {revoked}')

try:
    auth.login('boss@shop.com', 'MyStr0ng!Pass')
    check('旧密码失效', False)
except auth.InvalidCredentialsError:
    check('旧密码失效', True)

u4, tok4 = auth.login('boss@shop.com', 'NewStr0ng!Pass')
check('新密码可登录', u4.user_id == u.user_id)

# ---------- 10. 用户查询 ----------
print('\n[10] 用户查询')
got = auth.get_user(u.user_id)
check('可查到用户', got is not None and got.email == 'boss@shop.com')
check('查不到不存在的用户', auth.get_user('nope') is None)

print()
print('=' * 76)
print('模块 6a: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 76)

import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
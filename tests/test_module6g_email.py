"""模块 6g：SMTP 邮箱验证 + 验证后发放赠送额度（docs/16 P1-7）。"""
import os
import re
import sys
import pathlib
import datetime as dt

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / '.testtmp' / f'email_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "e.db"}'
os.environ['TENANTS_ROOT'] = str(TMP / 'tenants')
os.environ['APP_SECRET_KEY'] = 'test-secret-key-for-jwt-signing-32chars-minimum'
os.environ['SIGNUP_FREE_CREDITS'] = '1'
os.environ['REQUIRE_EMAIL_VERIFICATION'] = 'true'
os.environ['SMTP_HOST'] = 'smtp.test.local'
os.environ['SMTP_PORT'] = '465'
os.environ['SMTP_FROM'] = 'no-reply@test.local'
os.environ['SMTP_USE_SSL'] = 'true'
os.environ['WEB_BASE_URL'] = 'http://127.0.0.1:5173'
os.environ['RL_REGISTER_PER_HOUR'] = '100'

from app.config import reset_settings_cache, Settings        # noqa: E402
reset_settings_cache()
from app.storage import db                                   # noqa: E402
db.apply_schema()

from app.services import email as mailer                     # noqa: E402

sent: list[dict] = []


def fake_send(to, subject, text, html=''):
    sent.append({'to': to, 'subject': subject, 'text': text, 'html': html})


mailer.send_email = fake_send

from fastapi.testclient import TestClient                    # noqa: E402
from app.main import create_app                              # noqa: E402

client = TestClient(create_app())
client.__enter__()

fails: list[str] = []


def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)


def token_from_last_email() -> str:
    m = re.search(r'token=([A-Za-z0-9._~%\-]+)', sent[-1]['text'])
    return m.group(1) if m else ''


def register(email: str, tenant: str = '验证店'):
    r = client.post('/api/v1/auth/register', json={
        'email': email, 'password': 'EmailTest!2026', 'tenant_name': tenant})
    return r


print('=' * 78)
print('模块 6g：SMTP 邮箱验证')
print('=' * 78)

# ---------------------------------------------------------------------------
print('\n[1] 启动自检：要求验证但缺 SMTP 必须拒绝启动')
bad = Settings(app_secret_key='x' * 40, email_verification_required=True,
               smtp_host='', smtp_from='')
problems = bad.validate_for_startup()
check('★ 缺 SMTP_HOST/FROM 被拦截',
      any('SMTP_HOST' in p for p in problems), problems)

# ---------------------------------------------------------------------------
print('\n[2] 注册：不立即发额度 + 发送验证邮件')
r = register('verify1@test.com')
check('注册 201', r.status_code == 201, r.status_code)
b = r.json()
check('★ 响应标记需要验证', b.get('verification_required') is True, b)
check('★ 验证邮件已发送', b.get('verification_sent') is True, b)
check('★ 邮箱未验证', b['user']['email_verified'] is False, b['user'])
check('发送了 1 封邮件', len(sent) == 1, len(sent))
check('邮件收件人正确', sent[-1]['to'] == 'verify1@test.com')
check('邮件含验证链接',
      'http://127.0.0.1:5173/verify-email?token=' in sent[-1]['text'])
TOKEN1 = token_from_last_email()
check('提取到 token', len(TOKEN1) >= 20, TOKEN1[:12])

H1 = {'Authorization': f"Bearer {b['token']['access_token']}"}
acc = client.get('/api/v1/billing/account', headers=H1).json()
check('★ 验证前余额为 0', acc['balance'] == 0, acc['balance'])

# 未验证也能登录（只限制额度，不锁账号）
r = client.post('/api/v1/auth/login', json={
    'email': 'verify1@test.com', 'password': 'EmailTest!2026'})
check('未验证仍可登录', r.status_code == 200, r.status_code)

# ---------------------------------------------------------------------------
print('\n[3] 验证：错误/过期 token 被拒')
r = client.post('/api/v1/auth/verify-email',
                json={'token': 'x' * 40})
check('无效 token 400', r.status_code == 400, r.status_code)
check('错误码 INVALID_VERIFICATION_TOKEN',
      r.json().get('code') == 'INVALID_VERIFICATION_TOKEN', r.text[:80])

r = client.post('/api/v1/auth/verify-email', json={'token': TOKEN1})
check('★ 正确 token 验证 200', r.status_code == 200, r.text[:80])
check('★ 验证后 email_verified=true',
      r.json()['user']['email_verified'] is True, r.json()['user'])
acc = client.get('/api/v1/billing/account', headers=H1).json()
check('★ 验证后赠送 1 次', acc['balance'] == 1, acc['balance'])
r = client.get('/api/v1/auth/me', headers=H1)
check('/me 返回已验证', r.json().get('email_verified') is True, r.json())

r = client.post('/api/v1/auth/verify-email', json={'token': TOKEN1})
check('★ 重复验证幂等（返回成功态）', r.status_code == 200, r.status_code)
acc = client.get('/api/v1/billing/account', headers=H1).json()
check('重复验证不重复发额度', acc['balance'] == 1, acc['balance'])
r = client.post('/api/v1/auth/resend-verification', headers=H1)
check('已验证后重发 400',
      r.status_code == 400 and r.json().get('code') == 'ALREADY_VERIFIED',
      r.text[:80])

# ---------------------------------------------------------------------------
print('\n[4] 重发：旧 token 失效，新 token 可用')
r = register('verify2@test.com')
H2 = {'Authorization': f"Bearer {r.json()['token']['access_token']}"}
TOKEN2_OLD = token_from_last_email()
r = client.post('/api/v1/auth/resend-verification', headers=H2)
check('重发 200', r.status_code == 200, r.text[:80])
TOKEN2_NEW = token_from_last_email()
check('生成了新 token', TOKEN2_NEW and TOKEN2_NEW != TOKEN2_OLD)
r = client.post('/api/v1/auth/verify-email', json={'token': TOKEN2_OLD})
check('★ 旧 token 已作废', r.status_code == 400, r.status_code)
r = client.post('/api/v1/auth/verify-email', json={'token': TOKEN2_NEW})
check('★ 新 token 验证成功', r.status_code == 200, r.status_code)
check('第二个用户余额 1',
      client.get('/api/v1/billing/account', headers=H2).json()['balance'] == 1)

# ---------------------------------------------------------------------------
print('\n[5] 过期 token 被拒')
register('verify3@test.com')
TOKEN3 = token_from_last_email()
with db.transaction() as c:
    c.execute(
        "UPDATE one_time_tokens SET expires_at=? WHERE user_id="
        "(SELECT id FROM users WHERE email='verify3@test.com') "
        "AND purpose='verify_email'",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(),))
r = client.post('/api/v1/auth/verify-email', json={'token': TOKEN3})
check('★ 过期 token 400',
      r.status_code == 400
      and r.json().get('code') == 'VERIFICATION_TOKEN_EXPIRED',
      r.text[:80])

# ---------------------------------------------------------------------------
print('\n[6] SMTP 发送失败：注册成功但标记未发送，可重发')
orig_send = mailer.send_email
mailer.send_email = lambda *a, **k: (_ for _ in ()).throw(
    mailer.EmailError('smtp down'))
r = register('verify4@test.com')
check('SMTP 失败仍注册 201', r.status_code == 201, r.status_code)
b4 = r.json()
check('★ 标记 verification_sent=false',
      b4.get('verification_sent') is False, b4)
check('★ 未发送就不发额度',
      client.get('/api/v1/billing/account', headers={
          'Authorization': f"Bearer {b4['token']['access_token']}"}
      ).json()['balance'] == 0)
mailer.send_email = fake_send
H4 = {'Authorization': f"Bearer {b4['token']['access_token']}"}
r = client.post('/api/v1/auth/resend-verification', headers=H4)
check('恢复后重发 200', r.status_code == 200, r.text[:80])
TOKEN4 = token_from_last_email()
r = client.post('/api/v1/auth/verify-email', json={'token': TOKEN4})
check('重发后验证成功', r.status_code == 200, r.text[:80])
check('余额到账 1',
      client.get('/api/v1/billing/account', headers=H4).json()['balance'] == 1)

# ---------------------------------------------------------------------------
print('\n[7] 赠送额度幂等（账目只有一条 grant）')
with db.get_conn() as c:
    grants = c.execute(
        "SELECT COUNT(*) FROM credit_ledger WHERE tenant_id = "
        "(SELECT tenant_id FROM users WHERE email='verify1@test.com') "
        "AND reason='grant'").fetchone()[0]
check('★ verify1 只有 1 条 grant 流水', grants == 1, grants)

print()
print('=' * 78)
print('模块 6g: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 78)

client.__exit__(None, None, None)
import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
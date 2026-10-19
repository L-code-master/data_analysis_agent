"""模块 5 验证：密码哈希与令牌（bcrypt 直用版）。

背景：实测 passlib 1.7.4 + bcrypt 5.0.0 不兼容，改用 bcrypt 直用。
本脚本验证替代方案的正确性与安全性。
"""
import sys, pathlib, time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import security as sec

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)

print('=' * 76)
print('模块 5 验证：密码哈希与令牌')
print('=' * 76)

# ---------- 1. 基本哈希 ----------
print('\n[1] 基本哈希与校验')
h = sec.hash_password('MySecretPass123')
print(f'  哈希: {h[:40]}...')
print(f'  长度: {len(h)}')
check('哈希以 $2b$ 开头（bcrypt 格式）', h.startswith('$2b$'), h[:7])
check('哈希含成本因子 12', '$12$' in h)
check('正确密码通过', sec.verify_password('MySecretPass123', h))
check('错误密码拒绝', not sec.verify_password('WrongPass123', h))
check('空密码拒绝', not sec.verify_password('', h))
check('空哈希拒绝', not sec.verify_password('x', ''))

# ---------- 2. 同一密码两次哈希不同（加盐）----------
print('\n[2] 加盐（同一密码两次哈希应不同）')
h2 = sec.hash_password('MySecretPass123')
check('两次哈希不同', h != h2)
check('两个哈希都能校验通过',
      sec.verify_password('MySecretPass123', h) and
      sec.verify_password('MySecretPass123', h2))

# ---------- 3. ★ 超过 72 字节的长密码 ----------
print('\n[3] 长密码（bcrypt 有 72 字节硬限制）')
long_pw = 'A' * 200 + 'UniqueTail!'
h_long = sec.hash_password(long_pw)
check('200+ 字符密码可哈希', bool(h_long))
check('长密码校验通过', sec.verify_password(long_pw, h_long))
# 关键：尾部的差异必须能被区分（说明没有静默截断）
check('尾部不同的长密码被拒绝',
      not sec.verify_password('A' * 200 + 'DifferentTail!', h_long))
# 前 72 字节相同、后面不同 —— 这正是 bcrypt 静默截断会出错的情况
base = 'B' * 72
check('前72字节相同+尾部不同 → 被拒绝（无静默截断）',
      not sec.verify_password(base + 'AAAA', sec.hash_password(base + 'BBBB')))

# ---------- 4. 中文密码 ----------
print('\n[4] 中文与 Unicode 密码')
for pw in ['我的密码很安全123', 'Pässwörd-Ünïcode-42', '🔐🔑 表情密码 42']:
    hh = sec.hash_password(pw)
    ok = sec.verify_password(pw, hh)
    check(f'Unicode 密码可用: {pw[:14]}...', ok)
    if not ok:
        break

# ---------- 5. 非法哈希不抛异常 ----------
print('\n[5] 非法输入不抛异常（fail-closed）')
for bad_hash in ['', 'not-a-hash', '$2b$99$xx', '$$$$', None]:
    try:
        r = sec.verify_password('x', bad_hash)
        check(f'非法哈希 {str(bad_hash)[:14]!r} 返回 False', r is False)
    except Exception as e:
        check(f'非法哈希 {bad_hash!r} 不抛异常', False, f'{type(e).__name__}')

# ---------- 6. 密码强度 ----------
print('\n[6] 密码强度校验')
cases = [
    ('123456', False, '长度不足'),
    ('password123', False, '常见密码'),
    ('1234567890', False, '常见密码'),
    ('alllowercase', False, '仅一类字符'),
    ('ALLUPPERCASE', False, '仅一类字符'),
    ('1234567890a', True, '字母+数字'),
    ('MyStr0ng!Pass', True, '字母+数字+符号'),
    ('我的密码Abc123', True, '中文+字母+数字'),
]
for pw, expect, why in cases:
    ok, reason = sec.validate_password_strength(pw)
    mark = 'PASS' if ok == expect else 'FAIL'
    if ok != expect:
        fails.append(f'强度校验 {pw!r}')
    print(f'  [{mark}] {pw!r:20} -> {ok} ({why}) {reason}')

# ---------- 7. 令牌 ----------
print('\n[7] 令牌生成与校验')
t1 = sec.generate_token()
t2 = sec.generate_token()
print(f'  令牌示例: {t1[:32]}...')
check('两次令牌不同', t1 != t2)
check('令牌长度合理（>=40）', len(t1) >= 40, len(t1))
check('令牌 URL 安全（无 +/=）',
      not any(c in t1 for c in '+/='), t1[:20])

th = sec.hash_token(t1)
check('令牌哈希为 64 位 hex', len(th) == 64 and all(c in '0123456789abcdef' for c in th))
check('正确令牌校验通过', sec.verify_token(t1, th))
check('错误令牌被拒绝', not sec.verify_token(t2, th))
check('空令牌被拒绝', not sec.verify_token('', th))

# ---------- 8. 订单号 ----------
print('\n[8] 订单号生成')
order_no = sec.generate_order_no()
print(f'  订单号: {order_no}')
check('订单号有前缀', order_no.startswith('TB'))
check('订单号唯一', sec.generate_order_no() != sec.generate_order_no())
check('订单号长度合理', 16 <= len(order_no) <= 30, len(order_no))

# ---------- 9. 性能与成本因子 ----------
print('\n[9] 性能（成本因子 12 的耗时）')
t0 = time.perf_counter()
sec.hash_password('BenchmarkPass123')
dt_ms = (time.perf_counter() - t0) * 1000
print(f'  单次哈希耗时: {dt_ms:.0f} ms')
check('耗时在 50-1000ms 之间（防爆破且不卡顿）', 50 <= dt_ms <= 1000, f'{dt_ms:.0f}ms')

# needs_rehash
h10 = sec.hash_password('x', rounds=10)
check('低成本因子哈希标记为需重算', sec.needs_rehash(h10, rounds=12))
check('同成本因子哈希不需重算', not sec.needs_rehash(h, rounds=12))

# ---------- 10. 与 passlib 对比说明 ----------
print('\n[10] 与 passlib 的对比（为什么弃用）')
import bcrypt
print(f'  bcrypt 版本: {bcrypt.__version__}')
try:
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=['bcrypt'], deprecated='auto')
    ctx.hash('test')
    print('  passlib: 可用')
except Exception as e:
    print(f'  passlib: 不可用 -> {type(e).__name__}')
    print('  → 确认弃用 passlib 是正确决定')

print()
print('=' * 76)
print('模块 5: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 76)
sys.exit(1 if fails else 0)
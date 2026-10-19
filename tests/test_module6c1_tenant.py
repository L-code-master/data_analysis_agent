"""模块 6c-1 验证：租户沙箱与文件隔离。"""
import sys, pathlib, os
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = ROOT / '.testtmp' / f'tenant_{os.getpid()}'
TMP.mkdir(parents=True, exist_ok=True)
os.environ['DATABASE_URL'] = f'sqlite:///{TMP / "t.db"}'
os.environ['APP_SECRET_KEY'] = 'x' * 40
os.environ['TENANTS_ROOT'] = str(TMP / 'tenants')

from app.config import reset_settings_cache
reset_settings_cache()
from app.services import tenant as tn

fails = []
def check(label, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {label} {detail}')
    if not cond:
        fails.append(label)

print('=' * 76)
print('模块 6c-1 验证：租户沙箱')
print('=' * 76)

# ---------- 1. 目录结构 ----------
print('\n[1] 目录结构')
p = tn.ensure_tenant_dirs('shop_a')
print(f'  根目录: {p.root}')
for name in ('inbox', 'work', 'reports'):
    d = getattr(p, name)
    print(f'    {name:9} exists={d.exists()}')
check('inbox 已创建', p.inbox.exists())
check('work 已创建', p.work.exists())
check('reports 已创建', p.reports.exists())
check('sessions 已创建', p.sessions.exists())
check('artifacts 已创建', p.artifacts.exists())
check('logs 已创建', p.logs.exists())

# ---------- 2. 租户 ID 校验 ----------
print('\n[2] 租户 ID 校验（防路径穿越）')
for bad in ['../evil', 'a/b', 'a\\b', '', 'x' * 100, 'a b', 'a;b']:
    try:
        tn.validate_tenant_id(bad)
        check(f'拒绝非法 ID {bad!r}', False, '未拒绝')
    except tn.TenantPathError:
        check(f'拒绝非法 ID {bad[:20]!r}', True)
for good in ['shop_a', 'T-123', 'abc123', 'A' * 64]:
    try:
        tn.validate_tenant_id(good)
        check(f'接受合法 ID {good[:12]!r}', True)
    except tn.TenantPathError as e:
        check(f'接受合法 ID {good[:12]!r}', False, str(e))

# ---------- 3. ★ 路径越界防护 ----------
print('\n[3] 路径越界防护（核心安全）')
attacks = [
    '../../etc/passwd',
    '../..',
    '/etc/passwd',
    'C:\\Windows\\System32',
    'a/../../../outside',
    '..\\..\\windows',
]
for atk in attacks:
    try:
        tn.assert_inside_tenant('shop_a', p.root / atk)
        check(f'拦截越界 {atk[:28]!r}', False, '未拦截！')
    except tn.TenantPathError:
        check(f'拦截越界 {atk[:28]!r}', True)

# 注：'....//....//etc' 不是穿越 —— '....' 只是普通目录名，
#     resolve 后仍在租户内。真正的 '../' 已被上面拦截。

# 正常路径应通过
ok_path = tn.assert_inside_tenant('shop_a', p.inbox / 'file.xlsx')
check('租户内路径通过', ok_path == (p.inbox / 'file.xlsx').resolve())

# ★ 符号链接逃逸（真实漏洞面）
link = p.work / 'escape_link'
try:
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(Path(os.environ.get('WINDIR', 'C:\\Windows')))
    try:
        tn.assert_inside_tenant('shop_a', link)
        check('★ 拦截符号链接逃逸', False, '未拦截！')
    except tn.TenantPathError:
        check('★ 拦截符号链接逃逸', True)
    # 链接下的文件也必须拦截
    try:
        tn.assert_inside_tenant('shop_a', link / 'system.ini')
        check('★ 拦截链接下的文件', False, '未拦截！')
    except tn.TenantPathError:
        check('★ 拦截链接下的文件', True)
except (OSError, NotImplementedError) as e:
    print(f'  [SKIP] 符号链接测试（环境不支持创建链接: {type(e).__name__}）')

# ★ 跨租户访问必须被拒绝
p_b = tn.ensure_tenant_dirs('shop_b')
try:
    tn.assert_inside_tenant('shop_a', p_b.inbox / 'secret.xlsx')
    check('★ 跨租户访问被拒绝', False, '未拦截！')
except tn.TenantPathError:
    check('★ 跨租户访问被拒绝', True)

# ---------- 4. 上传校验 ----------
print('\n[4] 上传校验')
cases = [
    ('报表.xlsx', 1024, True, '正常 xlsx'),
    ('data.csv', 1024, True, '正常 csv'),
    ('evil.exe', 1024, False, '可执行文件'),
    ('script.py', 1024, False, '脚本文件'),
    ('shell.sh', 1024, False, 'shell 脚本'),
    ('noext', 1024, False, '无扩展名'),
    ('big.xlsx', 60 * 1024 * 1024, False, '超过 50MB'),
    ('empty.xlsx', 0, False, '空文件'),
]
for fname, size, expect, why in cases:
    ok, reason = tn.validate_upload('shop_a', fname, size)
    mark = 'PASS' if ok == expect else 'FAIL'
    if ok != expect:
        fails.append(f'上传校验 {why}')
    print(f'  [{mark}] {why:12} {fname:12} -> {ok}  {reason[:44]}')

# ---------- 5. 保存上传 ----------
print('\n[5] 保存上传')
content = b'fake xlsx content for testing'
path, digest = tn.save_upload('shop_a', '客户报表.xlsx', content)
print(f'  落盘: {path.name}')
print(f'  sha256: {digest[:24]}...')
check('文件已落盘', path.exists())
check('文件名由服务端生成（UUID）', path.stem == digest[:0] or len(path.stem) == 32,
      path.stem)
check('★ 不使用客户端文件名', '客户报表' not in path.name, path.name)
check('扩展名保留', path.suffix == '.xlsx')
check('内容正确', path.read_bytes() == content)
check('落在 inbox 内', path.parent == p.inbox)

# 客户端文件名可穿越 —— 但服务端不用它
path2, _ = tn.save_upload('shop_a', '../../evil.xlsx', b'x')
check('恶意文件名不影响落盘位置', path2.parent == p.inbox, path2)
check('恶意文件名被清洗后的展示名安全',
      '/' not in tn.safe_filename('../../evil.xlsx'),
      tn.safe_filename('../../evil.xlsx'))

# ---------- 6. 产物存储（内容寻址去重）----------
print('\n[6] 产物存储')
aid1, ap1 = tn.put_artifact('shop_a', 'profile', b'{"rows": 100}')
aid2, ap2 = tn.put_artifact('shop_a', 'profile', b'{"rows": 100}')
aid3, ap3 = tn.put_artifact('shop_a', 'profile', b'{"rows": 200}')
print(f'  aid1={aid1[:16]}  aid2={aid2[:16]}  aid3={aid3[:16]}')
check('相同内容同一 ID', aid1 == aid2)
check('相同内容不重复写', ap1 == ap2)
check('不同内容不同 ID', aid1 != aid3)
check('产物可取回', tn.get_artifact_path('shop_a', 'profile', aid1).read_bytes()
      == b'{"rows": 100}')

try:
    tn.get_artifact_path('shop_a', 'profile', 'nonexistent0000')
    check('不存在的产物报错', False)
except FileNotFoundError:
    check('不存在的产物报错', True)

try:
    tn.get_artifact_path('shop_a', 'profile', '../../etc/passwd')
    check('非法产物 ID 被拒绝', False)
except tn.TenantPathError:
    check('非法产物 ID 被拒绝', True)

# ---------- 7. 报告路径 ----------
print('\n[7] 报告路径')
rp = tn.report_path('shop_a', 'diag001')
print(f'  {rp}')
check('报告路径正确', rp.parent == p.reports and rp.name == 'diag001.html')
try:
    tn.report_path('shop_a', '../evil')
    check('非法诊断 ID 被拒绝', False)
except tn.TenantPathError:
    check('非法诊断 ID 被拒绝', True)

# ---------- 8. 租户隔离实测 ----------
print('\n[8] 租户隔离实测')
(p_b.inbox / 'b_secret.txt').write_text('shop B secret', encoding='utf-8')
# A 想读 B 的文件 —— 通过租户 API 不可能拿到
try:
    tn.assert_inside_tenant('shop_a', p_b.inbox / 'b_secret.txt')
    check('★ A 无法访问 B 的文件', False, '未拦截！')
except tn.TenantPathError:
    check('★ A 无法访问 B 的文件', True)

# 各自目录互不可见
a_files = {f.name for f in p.inbox.iterdir()}
b_files = {f.name for f in p_b.inbox.iterdir()}
check('两租户文件不串', 'b_secret.txt' not in a_files, a_files)

# ---------- 9. 磁盘用量 ----------
print('\n[9] 磁盘用量')
usage = tn.tenant_usage('shop_a')
print(f'  {usage}')
check('统计到文件', usage['files'] > 0, usage['files'])
check('统计到字节', usage['bytes'] > 0)

print()
print('=' * 76)
print('模块 6c-1: ' + ('全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'))
print('=' * 76)

import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
"""全量回归：按顺序跑所有测试模块，汇总结果。

用法（在 data_agent 环境）:
    python tests/run_all.py
"""
import sys, pathlib, subprocess, time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable

TESTS = [
    ('模块 1  配置 + 存储', 'test_module1_config_storage.py'),
    ('模块 2  语义层接口', 'test_module2_semantic.py'),
    ('模块 3  淘宝适配器', 'test_module3_adapter.py'),
    ('模块 4  SQL 渲染器',  'test_module4_renderer.py'),
    ('模块 5  密码与令牌', 'test_module5_security.py'),
    ('模块 6a 认证服务',     'test_module6a_auth.py'),
    ('模块 6c1 租户沙箱',    'test_module6c1_tenant.py'),
    ('模块 6c 诊断服务',     'test_module6c_diagnosis.py'),
    ('模块 6d/e API 端到端', 'test_module6de_api.py'),
    ('模块 6f  并发互斥',    'test_module6f_concurrency.py'),
    ('模块 6b 额度订单',     'test_module6b_billing.py'),
    ('模块 6g 邮箱验证',     'test_module6g_email.py'),
    ('模块 8  样例全流程',   'test_module8_sample.py'),
    ('模块 9  四级压缩库',   'test_module9_compressor.py'),
    ('模块 10 超级管理员',   'test_module10_admin.py'),
    ('模块 11 对话查数(A)',  'test_module11_chat_qa.py'),
    ('模块 12 Agent循环(B)', 'test_module12_agent_loop.py'),
    ('全栈冒烟(经代理)',   'smoke_fullstack.py'),
    ('DuckDB  语义层方言', 'test_duckdb_schema.py'),
    ('DuckDB  端到端', 'test_duckdb_e2e.py'),
]

# 需要前后端服务在线的模块：服务未启动时记 SKIP 而非 FAIL
# （docs/16 测试/仓库项：原先 needs_duckdb 是死参数，冒烟连接拒绝
#   被当成 FAIL，与真实回归失败混在一起）
SERVICES_REQUIRED = {'smoke_fullstack.py'}

print('=' * 78)
print(f'全量回归  ({time.strftime("%Y-%m-%d %H:%M:%S")})')
print(f'解释器: {PY}')
print('=' * 78)

results = []
for name, fname in TESTS:
    path = ROOT / 'tests' / fname
    if not path.exists():
        results.append((name, 'MISSING', 0))
        print(f'\n▶ {name}  — 文件不存在')
        continue

    t0 = time.perf_counter()
    r = subprocess.run([PY, str(path)], capture_output=True, text=True,
                       encoding='utf-8', errors='replace',
                       cwd=str(ROOT))
    dt = time.perf_counter() - t0

    passed = r.returncode == 0
    # 从输出里统计 PASS/FAIL 数
    out = (r.stdout or '') + (r.stderr or '')
    n_pass = out.count('[PASS]')
    n_fail = out.count('[FAIL]')

    status = 'OK' if passed else 'FAIL'
    if (fname in SERVICES_REQUIRED and not passed
            and ('ConnectionRefusedError' in out or 'URLError' in out)):
        status = 'SKIP'
    print(f'\n▶ {name}')
    print(f'   退出码={r.returncode}  PASS={n_pass}  FAIL={n_fail}  '
          f'耗时={dt:.1f}s  -> {status}')
    if status == 'SKIP':
        print('   | 服务未启动（需先 .\\start.ps1 起前后端再复跑冒烟）')
    if not passed:
        # 打印失败行
        for line in out.splitlines():
            if '[FAIL]' in line or 'Error' in line or 'Traceback' in line:
                print(f'   | {line.strip()[:110]}')
    results.append((name, status, n_pass, dt))

print()
print('=' * 78)
print('汇总')
print('=' * 78)
total_pass = 0
n_ok = 0
n_skip = 0
for row in results:
    name, status, n_pass = row[0], row[1], row[2]
    dt = row[3] if len(row) > 3 else 0
    mark = '✓' if status == 'OK' else ('◌' if status == 'SKIP' else '✗')
    print(f'  {mark} {name:26} {status:6} PASS={n_pass:3}  {dt:5.1f}s')
    total_pass += n_pass
    if status == 'OK':
        n_ok += 1
    elif status == 'SKIP':
        n_skip += 1

print()
effective = len(results) - n_skip
print(f'  模块通过: {n_ok}/{effective}'
      + (f'   （{n_skip} 项 SKIP：需双服务在线）' if n_skip else '')
      + f'   断言总数: {total_pass}')
print('=' * 78)
sys.exit(0 if n_ok == effective else 1)




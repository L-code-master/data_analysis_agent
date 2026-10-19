"""DuckDB 真实验证：完整执行 semantic/schema.sql。

这是第一次在真实 DuckDB 上跑，可能暴露 SQLite 代理无法发现的方言问题。
"""
import sys, pathlib, traceback
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import duckdb

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / 'semantic' / 'schema.sql'
sql = SCHEMA.read_text(encoding='utf-8')

print('=' * 76)
print('DuckDB 真实验证：semantic/schema.sql')
print('=' * 76)
print(f'duckdb 版本: {duckdb.__version__}')
print(f'文件行数: {len(sql.splitlines())}')
print()

con = duckdb.connect(':memory:')

# 逐段执行，便于定位错误
print('[1] 执行全部 DDL + ETL + 视图')
try:
    con.execute(sql)
    print('    ✅ 全部执行成功')
except Exception as e:
    print(f'    ❌ 失败: {type(e).__name__}')
    print(f'       {e}')
    traceback.print_exc()
    sys.exit(1)

print()
print('[2] 表与视图清单')
tables = [r[0] for r in con.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_schema='main' "
    "ORDER BY table_name").fetchall()]
views = [r[0] for r in con.execute(
    "SELECT table_name FROM information_schema.views WHERE table_schema='main' "
    "ORDER BY table_name").fetchall()]
print(f'    表 ({len(tables)}): {", ".join(tables)}')
print(f'    视图 ({len(views)}): {", ".join(views)}')

print()
print('[3] 指标注册表')
n = con.execute('SELECT COUNT(*) FROM sem_metric_registry').fetchone()[0]
print(f'    指标数: {n}')
rows = con.execute(
    'SELECT metric_id, metric_name, priority FROM sem_metric_registry '
    'ORDER BY metric_id').fetchall()
for mid, name, pri in rows:
    print(f'      {mid}  {name:24} {pri}')

print()
print('[4] 维度白名单')
d = con.execute('SELECT COUNT(*) FROM sem_metric_dimension').fetchone()[0]
print(f'    维度记录数: {d}')

print()
print('[5] 一致性检查（v_registry_consistency）')
bad = con.execute(
    "SELECT * FROM v_registry_consistency WHERE check_result <> 'OK'").fetchall()
print(f'    不一致项: {len(bad)}')
for b in bad:
    print(f'      {b}')

print()
print('[6] 指标定义视图（v_metric_registry_json）')
mv = con.execute(
    "SELECT metric_id, metric_name, allowed_dimensions "
    "FROM v_metric_registry_json ORDER BY metric_id").fetchall()
for mid, name, dims in mv[:5]:
    print(f'      {mid}  {name:22} dims={dims}')
print(f'      ... 共 {len(mv)} 条')

print()
print('[7] 不确定项视图（v_uncertainty_report）')
un = con.execute('SELECT * FROM v_uncertainty_report').fetchall()
print(f'    high/medium 指标数: {len(un)}')
for r in un:
    print(f'      {r[0]}  {r[1]:22} level={r[2]}')

print()
print('=' * 76)
print('✅ DuckDB 方言全部通过')
print('=' * 76)
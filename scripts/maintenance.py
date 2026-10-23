"""运维维护脚本 —— 租户对账、WAL checkpoint、备份（docs/16 P2-20 / P2-23）。

用法（在项目根目录，conda data_agent 环境）：
    python scripts/maintenance.py check              # 只读对账报告
    python scripts/maintenance.py checkpoint         # WAL 落盘并收缩
    python scripts/maintenance.py backup             # checkpoint 后备份 app.db
    python scripts/maintenance.py purge-orphans      # 预览孤儿租户目录
    python scripts/maintenance.py purge-orphans --apply   # 实际删除

说明：
  - check / checkpoint / backup 全程只读或安全操作，可随时运行
  - purge-orphans 默认 dry-run（只打印将删除的目录），--apply 才动手；
    只删「磁盘上有目录但 DB 无租户行」的目录，DB 内数据永不删除
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings           # noqa: E402
from app.storage import db                    # noqa: E402


def _human(n: int) -> str:
    return f'{n / 1024 / 1024:.2f}MB' if n >= 1024 * 1024 else f'{n / 1024:.0f}KB'


def _dir_size(p: Path) -> int:
    total = 0
    for root, _dirs, names in os.walk(p):
        for n in names:
            try:
                total += (Path(root) / n).stat().st_size
            except OSError:
                pass
    return total


def cmd_check() -> int:
    s = get_settings()
    db.apply_schema()

    with db.get_conn() as conn:
        rows = conn.execute(
            'SELECT id, name, status, created_at FROM tenants '
            'ORDER BY created_at').fetchall()
    db_ids = {r[0] for r in rows}

    disk_ids = set()
    if s.tenants_path.exists():
        for d in s.tenants_path.iterdir():
            if d.is_dir():
                disk_ids.add(d.name)

    orphans = sorted(disk_ids - db_ids)
    missing = sorted(db_ids - disk_ids)

    wal = db._wal_sizes()
    print('=' * 64)
    print('租户对账报告')
    print('=' * 64)
    print(f'DB 租户数:        {len(db_ids)}')
    print(f'磁盘租户目录数:   {len(disk_ids)}')
    print(f'孤儿目录(无DB行): {len(orphans)}')
    for t in orphans:
        p = s.tenants_path / t
        print(f'    {t}  ({_human(_dir_size(p))})')
    print(f'缺目录的租户:     {len(missing)}')
    for t in missing:
        print(f'    {t}')
    print(f'app.db: {wal["db_bytes"]:,} bytes   wal: {wal["wal_bytes"]:,} bytes')
    print()
    print('状态分布:')
    from collections import Counter
    for st, n in Counter(r[2] for r in rows).most_common():
        print(f'    {st:10} {n}')
    if orphans:
        print()
        print('提示: 孤儿目录可用 `python scripts/maintenance.py purge-orphans --apply` 清理')
    return 0


def cmd_checkpoint() -> int:
    db.apply_schema()
    r = db.wal_checkpoint('TRUNCATE')
    print(f"checkpoint({r['mode']}) result={r['result']}")
    print(f"  before: db={r['before']['db_bytes']:,}  wal={r['before']['wal_bytes']:,}")
    print(f"  after:  db={r['after']['db_bytes']:,}  wal={r['after']['wal_bytes']:,}")
    return 0


def cmd_backup() -> int:
    db.apply_schema()
    db.wal_checkpoint('TRUNCATE')            # 先落盘再拷贝（P2-23）

    src = get_settings().database_path
    dest_dir = src.parent / 'backups'
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = dest_dir / f'{src.stem}-{ts}{src.suffix}'
    shutil.copy2(src, dest)
    print(f'已备份: {dest} ({dest.stat().st_size:,} bytes)')
    print('注意: 备份只含元数据/计费库；租户数据目录(tenants/)请另行打包。')
    return 0


def cmd_purge_orphans(apply: bool) -> int:
    s = get_settings()
    db.apply_schema()
    with db.get_conn() as conn:
        db_ids = {r[0] for r in conn.execute('SELECT id FROM tenants').fetchall()}

    if not s.tenants_path.exists():
        print('租户根目录不存在，无事可做')
        return 0

    orphans = [d for d in sorted(s.tenants_path.iterdir())
               if d.is_dir() and d.name not in db_ids]
    if not orphans:
        print('没有孤儿目录')
        return 0

    total = 0
    for d in orphans:
        size = _dir_size(d)
        total += size
        mark = 'DELETE' if apply else 'DRY-RUN'
        print(f'[{mark}] {d.name}  ({_human(size)})')
        if apply:
            shutil.rmtree(d)
    print(f'共 {len(orphans)} 个目录，{_human(total)}'
          + (' 已删除' if apply else '（加 --apply 才会实际删除）'))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('check', help='只读对账报告')
    sub.add_parser('checkpoint', help='WAL checkpoint(TRUNCATE)')
    sub.add_parser('backup', help='checkpoint 后备份 app.db')
    po = sub.add_parser('purge-orphans', help='清理孤儿租户目录（默认 dry-run）')
    po.add_argument('--apply', action='store_true', help='实际删除（默认只预览）')
    args = ap.parse_args()

    if args.cmd == 'check':
        return cmd_check()
    if args.cmd == 'checkpoint':
        return cmd_checkpoint()
    if args.cmd == 'backup':
        return cmd_backup()
    if args.cmd == 'purge-orphans':
        return cmd_purge_orphans(args.apply)
    return 2


if __name__ == '__main__':
    sys.exit(main())

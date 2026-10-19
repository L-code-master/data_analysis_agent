"""租户沙箱管理 —— 所有文件操作的边界。

目录结构：
    tenants/{tenant_id}/
    ├── inbox/            客户上传的原始文件（只读）
    ├── work/             Agent 工作区（读写）
    ├── reports/          产出报告（只写）
    └── .agent/
        ├── sessions/     会话 jsonl
        ├── artifacts/    产物（内容寻址）
        └── logs/         审计日志

安全纪律：
  1. 所有路径必须落在租户根内 —— 越界一律拒绝（resolve 后前缀比对）
  2. 上传文件名由服务端生成（UUID），绝不用客户端文件名
  3. inbox/ 只读，reports/ 只写
  4. 跨租户访问在文件系统层就不可能
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings


class TenantPathError(ValueError):
    """路径越界或非法。"""


# 允许的上传扩展名（与 settings.allowed_upload_ext 双重校验）
_DANGEROUS_EXT = frozenset({
    '.exe', '.dll', '.so', '.bat', '.cmd', '.ps1', '.sh', '.js',
    '.vbs', '.scr', '.msi', '.jar', '.py', '.php',
})

# 文件名安全字符
_SAFE_NAME = re.compile(r'[^A-Za-z0-9._\u4e00-\u9fff-]')


@dataclass(frozen=True)
class TenantPaths:
    """一个租户的全部路径。"""
    root: Path
    inbox: Path
    work: Path
    reports: Path
    sessions: Path
    artifacts: Path
    logs: Path
    semantic_db: Path

    def to_dict(self) -> dict:
        return {
            'root': str(self.root),
            'inbox': str(self.inbox),
            'work': str(self.work),
            'reports': str(self.reports),
            'sessions': str(self.sessions),
            'artifacts': str(self.artifacts),
            'logs': str(self.logs),
        }


def validate_tenant_id(tenant_id: str) -> str:
    """校验租户 ID 格式（防路径穿越）。"""
    t = (tenant_id or '').strip()
    if not t:
        raise TenantPathError('租户 ID 不能为空')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', t):
        raise TenantPathError(
            f'租户 ID 只能包含字母、数字、下划线、连字符（最长 64）: {t!r}')
    return t


def tenant_paths(tenant_id: str) -> TenantPaths:
    """取（并在首次调用时创建）租户目录结构。"""
    tid = validate_tenant_id(tenant_id)
    root = (get_settings().tenants_path / tid).resolve()

    p = TenantPaths(
        root=root,
        inbox=root / 'inbox',
        work=root / 'work',
        reports=root / 'reports',
        sessions=root / '.agent' / 'sessions',
        artifacts=root / '.agent' / 'artifacts',
        logs=root / '.agent' / 'logs',
        semantic_db=root / 'semantic.duckdb',
    )
    return p


def ensure_tenant_dirs(tenant_id: str) -> TenantPaths:
    """创建租户目录（幂等）。"""
    p = tenant_paths(tenant_id)
    for d in (p.inbox, p.work, p.reports, p.sessions, p.artifacts, p.logs):
        d.mkdir(parents=True, exist_ok=True)
    return p


def assert_inside_tenant(tenant_id: str, path: Path) -> Path:
    """确保路径落在该租户根内。

    这是**文件系统级的租户隔离** —— 不依赖 prompt 约束。

    防御三层：
      1. 词法归一化（resolve 掉 ../ 与符号链接）
      2. 前缀比对：必须在租户根之内
      3. 符号链接拦截：symlink 可指向租户外的文件
         （如 tenants/a/link -> /etc/passwd，词法上在 a 内，
          但读的是外部文件 —— 必须在解析前识别）
    """
    p = tenant_paths(tenant_id)

    # Layer 1: 符号链接拦截（在 resolve 之前判断，否则 resolve 会跟随）
    try:
        if path.is_symlink():
            raise TenantPathError(f'拒绝符号链接: {path}')
    except OSError:
        pass  # 路径不存在时 is_symlink 可能抛 OSError，继续后续检查

    # 逐级检查父目录是否含符号链接（防止多级 symlink 绕过）
    try:
        cur = path
        for _ in range(16):          # 深度上限，防异常长链
            if not cur.exists() or cur == cur.parent:
                break
            if cur.is_symlink():
                raise TenantPathError(f'路径中含符号链接: {cur}')
            if cur == p.root:
                break
            cur = cur.parent
    except OSError:
        pass

    # Layer 2: 词法归一化
    try:
        resolved = path.resolve()
    except OSError as e:
        raise TenantPathError(f'路径无法解析: {path}') from e

    # Layer 3: 前缀比对
    try:
        resolved.relative_to(p.root)
    except ValueError:
        raise TenantPathError(
            f'路径越出租户范围: {resolved} 不在 {p.root} 内') from None
    return resolved


def safe_filename(original: str, max_len: int = 80) -> str:
    """从客户端文件名生成安全的展示名（**不用于落盘**）。"""
    name = Path(original or 'file').name
    name = _SAFE_NAME.sub('_', name)
    name = name.strip('. _') or 'file'
    return name[:max_len]


def generate_stored_name(ext: str) -> str:
    """生成服务端存储文件名（UUID），绝不用客户端文件名。"""
    e = (ext or '').lower()
    if not e.startswith('.'):
        e = '.' + e if e else ''
    return f'{uuid.uuid4().hex}{e}'


def validate_upload(tenant_id: str, filename: str, size_bytes: int,
                    declared_ext: str = '') -> tuple[bool, str]:
    """上传前置校验。"""
    s = get_settings()

    ext = Path(filename or '').suffix.lower() or declared_ext.lower()

    if ext in _DANGEROUS_EXT:
        return False, f'不允许上传该类型文件: {ext}'

    allowed = s.allowed_ext_set
    if ext not in allowed:
        return False, (f'不支持的文件类型 {ext or "(无扩展名)"}，'
                       f'仅支持: {", ".join(sorted(allowed))}')

    if size_bytes <= 0:
        return False, '文件为空'

    max_bytes = s.max_upload_mb * 1024 * 1024
    if size_bytes > max_bytes:
        return False, (f'文件过大（{size_bytes / 1024 / 1024:.1f}MB），'
                       f'上限 {s.max_upload_mb}MB')

    return True, ''


def save_upload(tenant_id: str, filename: str, content: bytes) -> tuple[Path, str]:
    """保存上传文件到 tenants/{id}/inbox/。

    返回 (落盘路径, sha256)。
    """
    p = ensure_tenant_dirs(tenant_id)
    ext = Path(filename or '').suffix.lower()
    stored_name = generate_stored_name(ext)
    target = p.inbox / stored_name

    # 双重确认目标在租户内
    assert_inside_tenant(tenant_id, target)

    target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    return target, digest


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 产物存储（内容寻址）
# ---------------------------------------------------------------------------

def put_artifact(tenant_id: str, kind: str, data: bytes,
                 ext: str = '.json') -> tuple[str, Path]:
    """按内容哈希存产物（同一内容自动去重）。

    返回 (artifact_id, 路径)。artifact_id = sha256[:32]
    """
    p = ensure_tenant_dirs(tenant_id)
    aid = hashlib.sha256(data).hexdigest()[:32]
    target = p.artifacts / f'{kind}_{aid}{ext}'

    assert_inside_tenant(tenant_id, target)

    if not target.exists():          # 内容寻址 → 已存在则不重复写
        target.write_bytes(data)
    return aid, target


def get_artifact_path(tenant_id: str, kind: str, artifact_id: str,
                      ext: str = '.json') -> Path:
    """取产物路径（不存在则报错，不静默返回空）。"""
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', artifact_id or ''):
        raise TenantPathError(f'非法的产物 ID: {artifact_id!r}')
    if not re.fullmatch(r'[a-z]{1,32}', kind or ''):
        raise TenantPathError(f'非法的产物类型: {kind!r}')

    p = tenant_paths(tenant_id)
    target = p.artifacts / f'{kind}_{artifact_id}{ext}'
    assert_inside_tenant(tenant_id, target)

    if not target.exists():
        raise FileNotFoundError(f'产物不存在: {kind}_{artifact_id}')
    return target


def report_path(tenant_id: str, diagnosis_id: str, ext: str = '.html') -> Path:
    """报告输出路径。"""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', diagnosis_id or ''):
        raise TenantPathError(f'非法的诊断 ID: {diagnosis_id!r}')
    p = ensure_tenant_dirs(tenant_id)
    target = p.reports / f'{diagnosis_id}{ext}'
    assert_inside_tenant(tenant_id, target)
    return target


def tenant_usage(tenant_id: str) -> dict:
    """租户磁盘占用（运维用）。"""
    p = tenant_paths(tenant_id)
    if not p.root.exists():
        return {'exists': False, 'files': 0, 'bytes': 0}

    files = 0
    total = 0
    for root, _dirs, names in os.walk(p.root):
        for n in names:
            try:
                total += (Path(root) / n).stat().st_size
                files += 1
            except OSError:
                continue
    return {
        'exists': True,
        'files': files,
        'bytes': total,
        'mb': round(total / 1024 / 1024, 2),
    }
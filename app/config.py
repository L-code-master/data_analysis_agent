"""应用配置加载。

设计取舍：**不依赖 pydantic-settings**，自己解析 .env。
原因：本环境无法安装 pydantic-settings（沙箱限制），
且 .env 格式简单（KEY=VALUE），自解析只有 30 行，还省一个依赖。

所有配置项都有显式默认值；生产环境的关键项在 validate_for_startup() 中强校验。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 已知在该端点不存在的模型名（实测返回 model_not_found）
KNOWN_BAD_MODELS = {'qwen-plus', 'qwen-max', 'qwen-turbo', 'qwen-long'}


def _load_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与引号。"""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        key, sep, value = line.partition('=')
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        # 去掉行尾注释（仅当值未被引号包裹时）
        if value and value[0] not in ('"', "'") and ' #' in value:
            value = value.split(' #', 1)[0].strip()
        # 去引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def _get(env: dict[str, str], key: str, default: str = '') -> str:
    """优先取真实环境变量，其次 .env，最后默认值。"""
    return os.environ.get(key, env.get(key, default))


def _get_int(env: dict[str, str], key: str, default: int) -> int:
    raw = _get(env, key, '')
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f'配置 {key} 必须是整数，得到 {raw!r}') from e


def _get_float(env: dict[str, str], key: str, default: float) -> float:
    raw = _get(env, key, '')
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f'配置 {key} 必须是数字，得到 {raw!r}') from e


def _get_bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key, '').strip().lower()
    if not raw:
        return default
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    if raw in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(f'配置 {key} 必须是布尔值，得到 {raw!r}')


def _get_list(env: dict[str, str], key: str) -> list[str]:
    """逗号分隔列表 → 去空格、去空项、统一小写（邮箱比较用）。"""
    raw = _get(env, key, '')
    return [x.strip().lower() for x in raw.split(',') if x.strip()]


@dataclass(frozen=True)
class Settings:
    """全局配置（不可变）。"""

    # ---------------- 运行环境 ----------------
    app_env: str = 'development'
    app_debug: bool = True
    app_secret_key: str = ''
    app_base_url: str = 'http://127.0.0.1:8000'

    # ---------------- LLM ----------------
    llm_api_key: str = ''
    llm_base_url: str = ''
    llm_model: str = 'qwen3.8-flash'
    llm_context_window: int = 262144   # Agent 压缩预算按此推导（P3-13）
    llm_timeout_seconds: int = 60          # 单次 LLM 调用超时（P1-13）
    agent_max_duration_seconds: int = 300  # 一次 Agent 循环整体 deadline
    agent_max_cost_usd: float = 0.10       # 一次 Agent 循环成本上限

    # ---------------- 成本熔断 ----------------
    diagnosis_max_cost_usd: float = 0.05
    tenant_monthly_cost_usd: float = 5.00

    # ---------------- 存储 ----------------
    database_url: str = 'sqlite:///./data/app.db'
    semantic_schema_path: str = './semantic/schema.sql'
    tenants_root: str = './tenants'

    # ---------------- 上传 ----------------
    max_upload_mb: int = 50
    allowed_upload_ext: str = '.xlsx,.csv'    # .xls 不支持（docs/16 P2-8）

    # ---------------- 沙箱 ----------------
    sandbox_network_isolated: bool = True
    sandbox_mode: str = 'accept'
    upload_confirm_timeout: int = 30

    # ---------------- 认证 ----------------
    jwt_algorithm: str = 'HS256'
    jwt_access_token_expire_minutes: int = 15   # 短有效期 + 登出水位（P1-18）
    jwt_refresh_token_expire_days: int = 30
    password_min_length: int = 10
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15

    # ---------------- 计费 ----------------
    price_trial_cents: int = 4900
    price_single_cents: int = 19900
    price_pack5_cents: int = 79900
    trial_once_per_user: bool = True
    # ★ 额度批次化有效期（docs/16 P1-6）：0 = 永久有效
    pack5_valid_days: int = 180
    credit_expire_days: int = 365
    billing_provider: str = 'manual'
    # 同一租户 24h 内自动退款上限，超限转人工（防"永远失败"刷退款，P1-7）
    max_auto_refunds_per_day: int = 10

    # ---------------- 速率限制（docs/16 P1-7） ----------------
    # 进程内滑动窗口。单 worker 架构下足够；多实例部署需换 Redis。
    rate_limit_enabled: bool = True
    rl_register_per_hour: int = 20       # 每 IP（办公 NAT 共享出口留余量）
    rl_login_per_minute: int = 30        # 每 IP
    rl_ask_per_minute: int = 30          # 每租户（Agent 成本闸门）
    rl_upload_per_minute: int = 30       # 每租户
    rl_diagnosis_per_minute: int = 10    # 每租户

    # 注册即赠送的体验次数（获客钩子：让新用户能跑完整个演示流程）
    # 设为 0 可关闭（此时需先购买才能诊断）
    signup_free_credits: int = 1

    # 超级管理员邮箱（逗号分隔）：注册/登录命中即 role=admin，诊断不限次数。
    # 空 = 功能关闭（默认安全）。只免额度这一道闸，不免成本熔断/质量门/沙箱。
    super_admin_emails: tuple[str, ...] = ()

    # 每获得 1 次诊断额度（购买或赠送）附送的 Agent 对话次数（docs/15）。
    # 形态 A（查数）永远免费；0 = 关闭限制。
    agent_chats_per_credit: int = 5

    # ---------------- 报告 ----------------
    report_format: str = 'html'
    report_brand_name: str = '经营诊断报告'

    # ---------------- 观测 ----------------
    log_level: str = 'INFO'
    log_dir: str = './data/logs'

    # ---------------- CORS ----------------
    cors_origins: str = 'http://127.0.0.1:5173'

    # ---------------- 邮箱验证（SMTP，docs/16 P1-7） ----------------
    web_base_url: str = 'http://127.0.0.1:5173'   # 验证邮件里的前端链接
    email_verification_required: bool = False
    email_token_expire_hours: int = 24
    smtp_host: str = ''
    smtp_port: int = 465
    smtp_user: str = ''
    smtp_password: str = ''
    smtp_from: str = ''
    smtp_use_ssl: bool = True                     # true=465 SSL；false=587 STARTTLS
    smtp_timeout: int = 15

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, env_file: Path | None = None) -> Settings:
        env = _load_env_file(env_file or (PROJECT_ROOT / '.env'))
        s = cls(
            app_env=_get(env, 'APP_ENV', 'development'),
            app_debug=_get_bool(env, 'APP_DEBUG', True),
            app_secret_key=_get(env, 'APP_SECRET_KEY', ''),
            app_base_url=_get(env, 'APP_BASE_URL', 'http://127.0.0.1:8000'),

            llm_api_key=_get(env, 'LLM_API_KEY', ''),
            llm_base_url=_get(env, 'LLM_BASE_URL', ''),
            llm_model=_get(env, 'LLM_MODEL', 'qwen3.8-flash'),
            llm_context_window=_get_int(env, 'LLM_CONTEXT_WINDOW', 262144),
            llm_timeout_seconds=_get_int(env, 'LLM_TIMEOUT_SECONDS', 60),
            agent_max_duration_seconds=_get_int(
                env, 'AGENT_MAX_DURATION_SECONDS', 300),
            agent_max_cost_usd=_get_float(env, 'AGENT_MAX_COST_USD', 0.10),

            diagnosis_max_cost_usd=_get_float(env, 'DIAGNOSIS_MAX_COST_USD', 0.05),
            tenant_monthly_cost_usd=_get_float(env, 'TENANT_MONTHLY_COST_USD', 5.00),

            database_url=_get(env, 'DATABASE_URL', 'sqlite:///./data/app.db'),
            semantic_schema_path=_get(env, 'SEMANTIC_SCHEMA_PATH', './semantic/schema.sql'),
            tenants_root=_get(env, 'TENANTS_ROOT', './tenants'),

            max_upload_mb=_get_int(env, 'MAX_UPLOAD_MB', 50),
            allowed_upload_ext=_get(env, 'ALLOWED_UPLOAD_EXT', '.xlsx,.csv'),

            sandbox_network_isolated=_get_bool(env, 'SANDBOX_NETWORK_ISOLATED', True),
            sandbox_mode=_get(env, 'SANDBOX_MODE', 'accept'),
            upload_confirm_timeout=_get_int(env, 'UPLOAD_CONFIRM_TIMEOUT', 30),

            jwt_algorithm=_get(env, 'JWT_ALGORITHM', 'HS256'),
            jwt_access_token_expire_minutes=_get_int(
                env, 'JWT_ACCESS_TOKEN_EXPIRE_MINUTES', 15),
            jwt_refresh_token_expire_days=_get_int(
                env, 'JWT_REFRESH_TOKEN_EXPIRE_DAYS', 30),
            password_min_length=_get_int(env, 'PASSWORD_MIN_LENGTH', 10),
            login_max_attempts=_get_int(env, 'LOGIN_MAX_ATTEMPTS', 5),
            login_lockout_minutes=_get_int(env, 'LOGIN_LOCKOUT_MINUTES', 15),

            price_trial_cents=_get_int(env, 'PRICE_TRIAL_CENTS', 4900),
            price_single_cents=_get_int(env, 'PRICE_SINGLE_CENTS', 19900),
            price_pack5_cents=_get_int(env, 'PRICE_PACK5_CENTS', 79900),
            trial_once_per_user=_get_bool(env, 'TRIAL_ONCE_PER_USER', True),
            pack5_valid_days=_get_int(env, 'PACK5_VALID_DAYS', 180),
            credit_expire_days=_get_int(env, 'CREDIT_EXPIRE_DAYS', 365),
            billing_provider=_get(env, 'BILLING_PROVIDER', 'manual'),
            max_auto_refunds_per_day=_get_int(
                env, 'MAX_AUTO_REFUNDS_PER_DAY', 10),
            rate_limit_enabled=_get_bool(env, 'RATE_LIMIT_ENABLED', True),
            rl_register_per_hour=_get_int(env, 'RL_REGISTER_PER_HOUR', 20),
            rl_login_per_minute=_get_int(env, 'RL_LOGIN_PER_MINUTE', 30),
            rl_ask_per_minute=_get_int(env, 'RL_ASK_PER_MINUTE', 30),
            rl_upload_per_minute=_get_int(env, 'RL_UPLOAD_PER_MINUTE', 30),
            rl_diagnosis_per_minute=_get_int(
                env, 'RL_DIAGNOSIS_PER_MINUTE', 10),
            signup_free_credits=_get_int(env, 'SIGNUP_FREE_CREDITS', 1),
            super_admin_emails=tuple(_get_list(env, 'SUPER_ADMIN_EMAILS')),
            agent_chats_per_credit=_get_int(env, 'AGENT_CHATS_PER_CREDIT', 5),

            report_format=_get(env, 'REPORT_FORMAT', 'html'),
            report_brand_name=_get(env, 'REPORT_BRAND_NAME', '经营诊断报告'),

            log_level=_get(env, 'LOG_LEVEL', 'INFO'),
            log_dir=_get(env, 'LOG_DIR', './data/logs'),

            cors_origins=_get(env, 'CORS_ORIGINS', 'http://127.0.0.1:5173'),

            web_base_url=_get(env, 'WEB_BASE_URL', 'http://127.0.0.1:5173'),
            email_verification_required=_get_bool(
                env, 'REQUIRE_EMAIL_VERIFICATION', False),
            email_token_expire_hours=_get_int(
                env, 'EMAIL_TOKEN_EXPIRE_HOURS', 24),
            smtp_host=_get(env, 'SMTP_HOST', ''),
            smtp_port=_get_int(env, 'SMTP_PORT', 465),
            smtp_user=_get(env, 'SMTP_USER', ''),
            smtp_password=_get(env, 'SMTP_PASSWORD', ''),
            smtp_from=_get(env, 'SMTP_FROM', ''),
            smtp_use_ssl=_get_bool(env, 'SMTP_USE_SSL', True),
            smtp_timeout=_get_int(env, 'SMTP_TIMEOUT', 15),
        )
        s._validate()
        return s

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def _validate(self) -> None:
        m = self.llm_model.strip().lower()
        if m in KNOWN_BAD_MODELS:
            raise ValueError(
                f'模型 {self.llm_model!r} 在该端点不存在'
                f'（实测返回 model_not_found）。'
                f'请使用 qwen3.8-flash / qwen3.8-max / qwen3.7-plus。')

        if self.sandbox_mode not in {'ask', 'accept', 'bypass', 'plan'}:
            raise ValueError(
                f'sandbox_mode 必须是 ask/accept/bypass/plan，得到 {self.sandbox_mode!r}')

        if self.billing_provider not in {'manual', 'alipay', 'wechat'}:
            raise ValueError(
                f'billing_provider 必须是 manual/alipay/wechat，'
                f'得到 {self.billing_provider!r}')

        if self.report_format not in {'html', 'markdown', 'pdf'}:
            raise ValueError(
                f'report_format 必须是 html/markdown/pdf，得到 {self.report_format!r}')

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == 'production'

    @property
    def allowed_ext_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_upload_ext.split(',') if e.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(',') if o.strip()]

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def tenants_path(self) -> Path:
        return self._resolve(self.tenants_root)

    @property
    def database_path(self) -> Path:
        url = self.database_url
        if not url.startswith('sqlite:///'):
            raise ValueError(f'当前仅支持 sqlite:/// ，得到 {url!r}')
        return self._resolve(url[len('sqlite:///'):])

    @property
    def semantic_schema_file(self) -> Path:
        return self._resolve(self.semantic_schema_path)

    @property
    def log_path(self) -> Path:
        return self._resolve(self.log_dir)

    def tenant_dir(self, tenant_id: str) -> Path:
        """租户沙箱根目录 —— 所有文件操作的边界。"""
        return self.tenants_path / tenant_id

    def tenant_semantic_db(self, tenant_id: str) -> Path:
        return self.tenant_dir(tenant_id) / 'semantic.duckdb'

    def price_for_sku(self, sku: str) -> int:
        """按 SKU 取价格（分）。"""
        mapping = {
            'trial': self.price_trial_cents,
            'single': self.price_single_cents,
            'pack5': self.price_pack5_cents,
        }
        if sku not in mapping:
            raise ValueError(f'未知的 SKU: {sku!r}')
        return mapping[sku]

    # ------------------------------------------------------------------
    # 启动自检
    # ------------------------------------------------------------------
    def validate_for_startup(self) -> list[str]:
        """返回**阻塞启动**的问题清单（空 = 通过）。

        关键：**任何环境下 APP_SECRET_KEY 都不能为空**。
        它是 JWT 签名密钥；为空会让注册/登录在运行期抛
        `InvalidKeyError: HMAC key must not be empty`，
        而不是启动即失败。宁可起不来，也不要带病运行。

        ★ 注意 LLM_API_KEY **不在**这里 —— 见 warnings_for_startup()。
        理由：诊断主流程与查数问答（形态 A）是纯确定性计算，一个 LLM
        token 都不花；缺 key 只影响归因对话（形态 B），而形态 B 已有
        优雅降级（chat.py 检测到无 key → route='no_llm' + 引导话术）。
        因为一个可选功能而拒绝启动整站，是把可用功能一起关掉。
        """
        problems: list[str] = []

        # ★ 所有环境都要求非空且足够长
        if not self.app_secret_key:
            problems.append(
                'APP_SECRET_KEY 未配置（JWT 签名密钥）。'
                '生成方法：python -c "import secrets; print(secrets.token_hex(32))"')
        elif len(self.app_secret_key) < 32:
            problems.append(
                f'APP_SECRET_KEY 过短（{len(self.app_secret_key)} 字符），'
                f'至少需要 32 字符')

        if self.is_production:
            if self.app_debug:
                problems.append('生产环境 APP_DEBUG 必须为 false')
            if '*' in self.cors_origin_list:
                problems.append('生产环境 CORS_ORIGINS 不允许使用 *')

        # ★ 客户数据不得出网（缺陷 D9）
        if not self.sandbox_network_isolated:
            problems.append(
                'SANDBOX_NETWORK_ISOLATED 必须为 true —— '
                '否则客户经营数据可能外发')

        # ★ 要求邮箱验证但 SMTP 未配置 → 用户永远收不到验证邮件，
        #   也就永远拿不到赠送额度。宁可起不来，也不要带病运行。
        if self.email_verification_required:
            missing = []
            if not self.smtp_host:
                missing.append('SMTP_HOST')
            if not self.smtp_from:
                missing.append('SMTP_FROM')
            elif '@' not in self.smtp_from:
                missing.append('SMTP_FROM（格式应形如 no-reply@your-domain.com）')
            if missing:
                problems.append(
                    'REQUIRE_EMAIL_VERIFICATION=true 但 '
                    + '、'.join(missing) + ' 未配置（无法发送验证邮件）')

        return problems

    def warnings_for_startup(self) -> list[str]:
        """返回**非阻塞**提醒（功能降级，但不影响启动）。"""
        warns: list[str] = []

        if not self.llm_api_key:
            warns.append(
                'LLM_API_KEY 未配置 —— 归因对话（形态 B）将降级为提示信息；'
                '数据上传、诊断报告、查数问答（形态 A）不受影响。')

        if not self.email_verification_required:
            warns.append(
                'REQUIRE_EMAIL_VERIFICATION=false —— 注册即发放赠送次数；'
                '生产环境建议开启邮箱验证并配置 SMTP，避免批量注册刷额度。')

        if (self.smtp_host or self.smtp_user) and not self.smtp_password:
            warns.append('SMTP 已配置主机/账号但 SMTP_PASSWORD 为空 —— '
                         '发送验证邮件可能失败。')

        return warns

    @staticmethod
    def generate_secret_key() -> str:
        """生成一个合格的 APP_SECRET_KEY（64 位十六进制）。"""
        import secrets
        return secrets.token_hex(32)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例配置（进程内缓存）。"""
    return Settings.load()


def reset_settings_cache() -> None:
    """测试用：清空配置缓存。"""
    get_settings.cache_clear()
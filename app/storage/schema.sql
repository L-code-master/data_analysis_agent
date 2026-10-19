-- ============================================================================
-- 应用元数据 · SQLite schema v1.0
-- ============================================================================
-- 存放：租户 / 用户 / 认证 / 订阅 / 订单 / 数据源 / 诊断任务 / 用量 / 审计
-- 不放：业务数据（走 DuckDB）、大文件（走文件系统）
--
-- 运行：python -m app.storage.migrate
-- 并发：必须启用 WAL，见文件末尾 PRAGMA 段
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. 租户（付费主体）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,                    -- 公司/店铺名
    contact_name      TEXT,
    contact_phone     TEXT,
    status            TEXT NOT NULL DEFAULT 'active',   -- active | suspended | closed
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status);

-- ---------------------------------------------------------------------------
-- 2. 用户（登录主体，隶属租户）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    email             TEXT NOT NULL,
    phone             TEXT,
    password_hash     TEXT NOT NULL,                    -- bcrypt
    display_name      TEXT,
    role              TEXT NOT NULL DEFAULT 'owner',    -- owner | member
    email_verified    INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'active',   -- active | locked | disabled
    failed_attempts   INTEGER NOT NULL DEFAULT 0,
    locked_until      TEXT,
    last_login_at     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);

-- ---------------------------------------------------------------------------
-- 3. 刷新令牌（JWT refresh，可吊销）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(id),
    token_hash        TEXT NOT NULL,                    -- 存哈希不存原文
    expires_at        TEXT NOT NULL,
    revoked_at        TEXT,
    user_agent        TEXT,
    ip_address        TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refresh_hash ON refresh_tokens(token_hash);

-- ---------------------------------------------------------------------------
-- 4. 一次性令牌（邮箱验证 / 找回密码）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS one_time_tokens (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(id),
    purpose           TEXT NOT NULL,                    -- verify_email | reset_password
    token_hash        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    used_at           TEXT,
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ott_hash ON one_time_tokens(token_hash);

-- ---------------------------------------------------------------------------
-- 5. 额度账户（按次付费模型的核心）
-- ---------------------------------------------------------------------------
-- 设计：不用"订阅"概念，改为"次数额度"（credit）。
--   购买 → 加额度；消费 → 扣额度；额度有有效期。
--   比订阅简单得多：无周期续费、无按比例退款、无状态机。
CREATE TABLE IF NOT EXISTS credit_accounts (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL UNIQUE REFERENCES tenants(id),
    balance           INTEGER NOT NULL DEFAULT 0,   -- 可用次数
    total_purchased   INTEGER NOT NULL DEFAULT 0,   -- 累计购买
    total_used        INTEGER NOT NULL DEFAULT 0,   -- 累计使用
    trial_used        INTEGER NOT NULL DEFAULT 0,   -- 体验价是否已用（0/1）
    cost_used_usd     REAL NOT NULL DEFAULT 0,      -- 累计 LLM 成本（护栏用）
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 6. 额度流水（每次增减都留痕，可对账）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credit_ledger (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    order_id          TEXT REFERENCES orders(id),    -- 购买时关联订单
    -- ⚠️ 刻意不加 FOREIGN KEY：额度在「诊断开始时」就要扣，
    --    而诊断行可能尚未落库（或并发下顺序不定）。
    --    加 FK 会导致扣额度失败 → 用户看到"次数不足"但实际有余额。
    --    这里只存 id 作弱引用，由业务层保证一致性。
    diagnosis_id      TEXT,
    delta             INTEGER NOT NULL,              -- 正=购买/赠送，负=消费
    balance_after     INTEGER NOT NULL,              -- 变动后余额（对账用）
    reason            TEXT NOT NULL,                 -- purchase | consume | refund | grant | expire
    expires_at        TEXT,                          -- 该批额度的到期时间
    note              TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON credit_ledger(tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 7. 订单（支付流水）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    user_id           TEXT REFERENCES users(id),     -- 人工核销时可能为空
    order_no          TEXT NOT NULL,                 -- 对外订单号
    sku               TEXT NOT NULL,                 -- trial | single | pack5
    quantity          INTEGER NOT NULL DEFAULT 1,    -- 本次购买次数
    amount_cents      INTEGER NOT NULL,              -- 金额（分），避免浮点
    currency          TEXT NOT NULL DEFAULT 'CNY',
    status            TEXT NOT NULL,                 -- pending | paid | failed | refunded | canceled
    provider          TEXT NOT NULL,                 -- alipay | wechat | manual
    provider_trade_no TEXT,                          -- 第三方交易号
    pay_proof_ref     TEXT,                          -- 人工核销凭证（截图等）→ artifacts
    reviewed_by       TEXT,                          -- 人工核销审核人
    reviewed_at       TEXT,
    paid_at           TEXT,
    refunded_at       TEXT,
    refund_amount_cents INTEGER,
    remark            TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_no ON orders(order_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_trade ON orders(provider, provider_trade_no)
    WHERE provider_trade_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_tenant ON orders(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at DESC);

-- 体验价限购一次：同一租户只能有一条 trial 且已支付的订单
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_trial_once
    ON orders(tenant_id, sku) WHERE sku = 'trial' AND status = 'paid';

-- ---------------------------------------------------------------------------
-- 7. 支付回调原始记录（幂等 + 对账）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_events (
    id                TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    event_id          TEXT NOT NULL,                    -- 第三方通知 ID
    order_no          TEXT,
    payload           TEXT NOT NULL,                    -- 原始报文（审计）
    signature_valid   INTEGER NOT NULL,
    processed         INTEGER NOT NULL DEFAULT 0,
    process_result    TEXT,
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payevt_unique ON payment_events(provider, event_id);

-- ---------------------------------------------------------------------------
-- 8. 数据源（上传的导出文件）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    uploaded_by       TEXT NOT NULL REFERENCES users(id),
    filename          TEXT NOT NULL,                    -- 服务端生成名（防穿越）
    original_name     TEXT,                             -- 客户端原名（仅展示）
    sheet_name        TEXT DEFAULT '',                  -- ★Excel 工作表名（多表拆分用；CSV 为 ''）
    size_bytes        INTEGER NOT NULL,
    sha256            TEXT NOT NULL,                    -- 去重 + 完整性
    platform          TEXT,                             -- douyin | taobao | pdd | amazon | other
    row_count         INTEGER,
    profile_ref       TEXT,                             -- → artifacts/{hash}.json
    status            TEXT NOT NULL DEFAULT 'uploaded', -- uploaded | profiled | failed
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_tenant ON sources(tenant_id, created_at DESC);
-- ★ 去重索引由 db.py apply_schema 动态管理（含 sheet_name 列），
--   因为旧库迁移（ALTER TABLE）必须在建索引之前完成。

-- ---------------------------------------------------------------------------
-- 9. 字段映射确认（驱动"第二次免确认"）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS field_mappings (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    source_schema     TEXT NOT NULL,                    -- 列集合 hash，同构可复用
    platform          TEXT,
    mapping           TEXT NOT NULL,                    -- JSON: {源列名: 语义字段}
    confidence        TEXT,                             -- JSON: {列名: 置信度}
    confirmed_by      TEXT REFERENCES users(id),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_map_unique ON field_mappings(tenant_id, source_schema);

-- ---------------------------------------------------------------------------
-- 10. 诊断任务
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS diagnoses (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    created_by        TEXT NOT NULL REFERENCES users(id),
    source_id         TEXT NOT NULL REFERENCES sources(id),
    order_id          TEXT REFERENCES orders(id),        -- 单份付费报告关联订单
    status            TEXT NOT NULL,                     -- queued | running | succeeded | failed | canceled
    progress_phase    TEXT,                              -- profiling | mapping | computing | verifying | reporting
    session_id        TEXT,                              -- → 会话 jsonl
    report_ref        TEXT,                              -- → reports/{id}.html
    quality_passed    INTEGER,                           -- 门1/门2/门3 是否全通过
    error             TEXT,
    cost_usd          REAL NOT NULL DEFAULT 0,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,        -- 推理模型必须单独统计
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_diag_tenant ON diagnoses(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_diag_status ON diagnoses(status);

-- ---------------------------------------------------------------------------
-- 10.5 对话式分析（v1.4：形态 A 查数问答 + 形态 B Agent 归因）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    created_by        TEXT REFERENCES users(id),
    title             TEXT,                              -- 首条问题截断
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_sess_tenant
    ON chat_sessions(tenant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES chat_sessions(id),
    tenant_id         TEXT NOT NULL,
    role              TEXT NOT NULL,                     -- user | assistant
    content           TEXT NOT NULL,
    route             TEXT NOT NULL DEFAULT '',          -- qa | agent | ''（用户消息）
    meta              TEXT,                              -- JSON：{metric_id, time_range, tool_calls, cost_usd...}
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_session
    ON chat_messages(session_id, created_at);

-- ---------------------------------------------------------------------------
-- 11. 用量明细（计费与成本核算依据）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_records (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    -- 弱引用（不加 FK）：成本在诊断执行过程中就要记账，
    -- 此时诊断行可能尚未落库。同 credit_ledger.diagnosis_id。
    diagnosis_id      TEXT,
    model             TEXT NOT NULL,
    input_tokens      INTEGER NOT NULL,
    output_tokens     INTEGER NOT NULL,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant ON usage_records(tenant_id, created_at);

-- ---------------------------------------------------------------------------
-- 12. 审计日志（合规必需；只追加，不更新不删除）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT,
    user_id           TEXT,
    action            TEXT NOT NULL,                     -- login | upload | diagnose | export | pay ...
    target_type       TEXT,
    target_id         TEXT,
    ip_address        TEXT,
    user_agent        TEXT,
    detail            TEXT,                              -- JSON
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_logs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action, created_at DESC);

-- ---------------------------------------------------------------------------
-- 13. 幂等键（防重复提交诊断 / 重复扣费）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key               TEXT PRIMARY KEY,
    tenant_id         TEXT,
    endpoint          TEXT NOT NULL,
    request_hash      TEXT NOT NULL,
    response_body     TEXT,
    status_code       INTEGER,
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idem_expire ON idempotency_keys(expires_at);

-- ---------------------------------------------------------------------------
-- 14. 文件目录（报告/产物索引，便于统一清理与授权下载）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifacts (
    id                TEXT PRIMARY KEY,                  -- 内容 hash（寻址）
    tenant_id         TEXT NOT NULL REFERENCES tenants(id),
    kind              TEXT NOT NULL,                     -- profile | report | chart | session
    rel_path          TEXT NOT NULL,                     -- 相对 tenants/{id}/
    size_bytes        INTEGER NOT NULL,
    content_type      TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_tenant ON artifacts(tenant_id, kind, created_at DESC);

-- ============================================================================
-- 初始化 PRAGMA（每次连接都要执行，见 app/storage/db.py）
-- ============================================================================
-- PRAGMA journal_mode = WAL;        -- 读写并发
-- PRAGMA busy_timeout = 5000;       -- 避免瞬时 database is locked
-- PRAGMA foreign_keys = ON;         -- 外键约束
-- PRAGMA synchronous = NORMAL;      -- WAL 下的性能/安全平衡

-- ============================================================================
-- 初始数据（由 app/storage/migrate.py 写入，此处仅作文档）
-- ============================================================================
-- 首次部署需创建：
--   1. 一个 tenant（默认租户）
--   2. 一个 user（owner，密码由 migrate 参数或环境变量提供）
--   3. 一条 trial 订阅（TRIAL_DAYS 天）
-- 禁止在 SQL 里硬编码默认密码。
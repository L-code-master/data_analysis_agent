# 企业生产级数据分析 Agent —— 架构图

> 版本 **v1.0（已实现）** · 2026-10-19 · 配套 `01-DESIGN.md`、`语义层评审.md`
> 所有图使用 Mermaid，可在支持 Mermaid 的 Markdown 渲染器中直接查看
>
> **v0.2 新增**：前端层、接口层、存储层
> **v0.3 新增**：语义层实体关系、运行时选型（DuckDB + SQLite）、LLM 接入链路
> **v1.0 更新**：全部图为**已实现形态**；新增「实际目录结构」与「按次付费额度模型」；
> 开发中的架构修正见 `01-DESIGN.md` §8
> **v1.1 新增**：§15.5 样例数据与多工作表拆分（获客漏斗）；验证更新为 18 模块 / 688 断言（v1.4 对话式分析 + v1.4.1 上下文/配额）

---

## 0. 全景图（含前端与存储）

> ✅ **v1.0 已实现**：图中每个组件都已落地，标注了对应代码文件。

```mermaid
graph TB
    subgraph WEB["🖥 前端层 Web UI (Vite + React + TS)"]
        W1[① 登录]
        W2[② 上传文件]
        W3["③ 字段映射确认 ★"]
        W4[④ 诊断进度 SSE]
        W5["⑤ 报告 + 溯源 ★"]
        W1 --> W2 --> W3 --> W4 --> W5
    end

    subgraph API["🔌 接口层 API (FastAPI)"]
        A1[REST 路由]
        A2[SSE 事件流]
        A3[鉴权 / 租户绑定]
        A4[上传校验]
        A5["任务池<br/>ThreadPoolExecutor(4)"]
    end

    subgraph CORE["⚙️ Agent 内核 (harness_py_pro 改造)"]
        C1[⑥ 交付层]
        C2[⑤ 验证层]
        C3[④ 记忆层]
        C4[③ 上下文层]
        C5[② 工具层]
        C6[① 约束层]
        C7["★ 语义层"]
    end

    subgraph STORE["▣ 存储层"]
        S1["SQLite<br/>元数据/状态/计费<br/>5 张表"]
        S2["文件系统<br/>数据/产物/日志/jsonl"]
    end

    WEB -->|"HTTP / SSE"| API
    API -->|"进程内同步调用"| CORE
    API --> S1
    CORE --> S1
    CORE --> S2
    API --> S2

    style W3 fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style W5 fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style C7 fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style S1 fill:#d5e8d4,stroke:#82b366
    style S2 fill:#d5e8d4,stroke:#82b366
```

---

## 1. 六层总架构（含横向语义层）

```mermaid
graph TB
    subgraph L6["⑥ 交付层 Delivery"]
        D1[报告渲染<br/>MD/HTML/PDF]
        D2[多租户管理]
        D3[计费与配额]
        D4[审计日志]
    end

    subgraph L5["⑤ 验证层 Verification"]
        V1[门1 数据质量门]
        V2[门2 数值自洽门]
        V3[门3 归因可证门]
        V4[LoopGuard<br/>循环守卫]
    end

    subgraph L4["④ 记忆层 Memory"]
        M1[四级压缩<br/>Compressor]
        M2[租户记忆<br/>MemoryManager]
        M3[会话持久化<br/>artifact 引用制]
        M4[Token预算<br/>五区分配]
    end

    subgraph L3["③ 上下文层 Context"]
        C1[稳定段<br/>身份+纪律+指标目录]
        C2[cache 边界标记]
        C3[动态段<br/>计划+画像+任务]
        C4[注入威胁扫描]
    end

    subgraph L2["② 工具层 Tools"]
        T1[数据接入 x3]
        T2[字段映射 x2]
        T3[指标计算 x3]
        T4[归因分析 x2]
        T5[产出 x2]
    end

    subgraph L1["① 约束层 Constraint"]
        S1[沙箱<br/>sandbox.py]
        S2[权限<br/>permissions.py]
        S3[Hook<br/>hooks.py]
        S4[成本熔断<br/>CostTracker]
    end

    subgraph SEM["★ 语义层 Semantic（横向贯穿）"]
        SE1[指标定义<br/>MetricDefinition]
        SE2[口径冲突消解]
        SE3[依赖拓扑排序]
        SE4[数据质量规则]
    end

    L1 --> L2
    L2 --> L3
    L3 --> L4
    L4 --> L5
    L5 --> L6

    SEM -.约束.-> L2
    SEM -.注入.-> L3
    SEM -.校验依据.-> L5

    style SEM fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style L1 fill:#ffcccc,stroke:#cc0000
    style L5 fill:#d5e8d4,stroke:#82b366
```

---

## 2. 运行时数据流（一次诊断的完整链路）

```mermaid
flowchart TD
    A[客户上传导出文件<br/>tenants/id/inbox/] --> B{约束层检查}
    B -->|路径白名单<br/>不出网| C[工具层: list_sources]
    B -->|拦截| Z1[拒绝并审计]

    C --> D[工具层: profile_source<br/>只返回画像]
    D --> E[工具层: load_source<br/>数据入内存<br/>★不进上下文]

    E --> F[工具层: suggest_field_mapping]
    F --> G{字段映射确认}
    G -->|未确认| G2[向客户确认<br/>并写入 tenant_ 记忆]
    G -->|已确认| H
    G2 --> H[语义层: 解析指标口径]

    H --> I{口径是否明确?}
    I -->|歧义| Z2[抛 CaliberAmbiguous<br/>fail-closed]
    I -->|明确| J[语义层: 依赖拓扑排序]

    J --> K[验证门1: 数据质量]
    K -->|不通过| Z3[标记指标 unavailable<br/>★不出错数]
    K -->|通过| L[工具层: compute_metric<br/>按序计算闭包]

    L --> M[写入 computed_registry<br/>★旁路账本]
    M --> N[工具层: compare_periods<br/>+ detect_anomaly]
    N --> O[工具层: decompose_change]
    O --> P[工具层: test_hypothesis]

    P --> Q[验证门2: 数值自洽<br/>★每个数字可溯源]
    Q -->|不通过| Z4[退回重算]
    Q -->|通过| R[验证门3: 归因可证<br/>p值支撑]

    R -->|不通过| R2[降级为'疑似'<br/>标注未通过检验]
    R -->|通过| S[工具层: render_chart]
    R2 --> S
    S --> T[工具层: emit_report]
    T --> U[验证: 报告脱敏<br/>post_tool hook]
    U --> V[交付: 报告落盘<br/>tenants/id/reports/]
    V --> W[审计日志 + 成本记账]
```

---

## 3. 上下文装配：Prompt Cache 边界（关键设计）

```mermaid
graph LR
    subgraph STABLE["稳定段 —— 逐字节不变，跨请求命中 cache"]
        A1[身份定义]
        A2[分析纪律 ANALYST.md]
        A3[指标目录<br/>~400 token]
        A4[报告模板]
    end

    B["__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__<br/>★ 参考代码生产层缺失，需从教学层取回"]

    subgraph DYNAMIC["动态段 —— 每轮变化，不参与 cache"]
        C1[计划状态]
        C2[租户记忆包]
        C3[数据画像]
        C4[本次任务]
        C5[工具结果]
    end

    STABLE --> B --> DYNAMIC

    style STABLE fill:#d5e8d4,stroke:#82b366,stroke-width:2px
    style B fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style DYNAMIC fill:#dae8fc,stroke:#6c8ebf
```

**装配位置对比（这是与参考代码的关键偏离）**：

| 内容 | 参考代码 | 本设计 |
|---|---|---|
| 身份+纪律+指标目录 | `messages[0]` system | `messages[0]` system（稳定段） |
| plan_context | `messages[0]` system（**每轮重建**） | 首条 user 消息（动态段） |
| memory_bundle | `messages[0]` system（**每轮重建**） | 首条 user 消息（动态段） |

> 参考代码 `engine.py:556-563` 每轮压缩后重建整个 system prompt，把 plan/memory 塞在 `messages[0]` —— 这会让**稳定前缀每轮变化，cache 全量失效**。本设计必须把这两项移出去。

---

## 4. 四级压缩与 computed_registry 旁路账本

```mermaid
flowchart TD
    A[每轮开始<br/>estimate_tokens] --> B{tokens > 60%<br/>available_for_messages?}
    B -->|否| Z[继续]
    B -->|是| C[L1 Microcompact<br/>截断旧工具结果]

    C --> D{达标?}
    D -->|是| Z
    D -->|否| E[L2 Snip<br/>分级删除最老中段<br/>★修补 G3]

    E --> F{达标?}
    F -->|是| Z
    F -->|否| G[L3 Compact<br/>LLM 结构化摘要]

    G --> H{摘要有效?<br/>len > 30}
    H -->|无效| Z2[回退不压缩<br/>★修补 G2]
    H -->|有效| I[注入 computed_registry<br/>★修补 G1]

    I --> J[_fix_orphaned_tool_pairs<br/>修复 tool_calls 配对]
    Z --> J
    Z2 --> J
    J --> K[继续循环]

    L[API 报错<br/>context/token 类] --> M[L4 Reactive<br/>紧急压缩]
    M --> J

    style I fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style H fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style E fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
```

**computed_registry 的作用**：

```mermaid
graph LR
    A[compute_metric 返回] --> B[computed_registry<br/>metric_key → value]
    B --> C[压缩时随摘要注入<br/>防止重算]
    B --> D[验证门2 查证<br/>数字可溯源]
    B --> E[报告生成引用<br/>唯一权威源]
    B --> F[不占消息历史<br/>★独立于上下文]

    style B fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
```

---

## 5. 三段验证门

```mermaid
graph TB
    subgraph GATE1["门1 数据质量（计算前）"]
        A1[revenue > 0]
        A2[gross_profit <= revenue]
        A3[日期无断层/无未来]
        A4[关键维度缺失 < 20%]
    end

    subgraph GATE2["门2 数值自洽（计算后）"]
        B1[恒等式: 分项和 == 总计]
        B2[交叉校验: 环比公式误差 < 0.5%]
        B3[量级校验: 数字在合理区间]
        B4[★引用校验: 每个数字可溯源]
    end

    subgraph GATE3["门3 归因可证（出报告前）"]
        C1[每条归因有 p 值支撑]
        C2[p > 0.05 降级为'疑似']
        C3[禁止无依据的因果表述]
    end

    GATE1 -->|任一不通过| X1[指标标记 unavailable<br/>不输出错数]
    GATE1 -->|通过| CALC[执行计算]
    CALC --> GATE2
    GATE2 -->|不通过| X2[退回重算]
    GATE2 -->|通过| GATE3
    GATE3 -->|不通过| X3[降级为'疑似'并标注]
    GATE3 -->|通过| OUT[出报告]
    X3 --> OUT

    style B4 fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style X1 fill:#ffcccc,stroke:#cc0000
```

> **门 2 第 4 条是整个架构的"生产级"标志**：报告中每个数字都必须能在 `computed_registry` 里找到来源。这一条直接杀死 LLM 编数字。

---

## 6. 多租户目录隔离

```mermaid
graph TD
    ROOT[data_analysis_agent/]
    ROOT --> TEN[tenants/]
    ROOT --> SRC[src/ 框架代码]
    ROOT --> SEM[semantic/ 语义层定义]

    TEN --> T1["{tenant_a}/"]
    TEN --> T2["{tenant_b}/"]

    T1 --> I1[inbox/ 上传文件<br/>只读]
    T1 --> W1[work/ Agent 工作区<br/>读写]
    T1 --> R1[reports/ 产出报告<br/>只写]
    T1 --> H1[".harness/<br/>会话/记忆/审计"]

    H1 --> H11[sessions/]
    H1 --> H12[memory/]
    H1 --> H13[artifacts/ 内容寻址]

    style T1 fill:#d5e8d4,stroke:#82b366
    style T2 fill:#d5e8d4,stroke:#82b366
    style H13 fill:#ffe6cc,stroke:#d79b00
```

**隔离强度**：`allowed_roots = [tenants/{tenant_id}]` —— 跨租户读取在**文件系统层**就不可能，不依赖 prompt 约束。

---

## 7. 二期：多角色编排（swarm.py 复用）

```mermaid
graph LR
    subgraph R1["第1轮 Profiler"]
        P1[list_sources<br/>profile_source]
    end

    subgraph R2["第2轮 Analyst"]
        P2[compute_metric<br/>compare_periods<br/>detect_anomaly]
    end

    subgraph R3["第3轮 Attributor"]
        P3[decompose_change<br/>test_hypothesis]
    end

    subgraph R4["第4轮 Writer"]
        P4[emit_report]
    end

    R1 --> R2 --> R3 --> R4

    subgraph ISO["角色隔离（tool_filter 强制）"]
        N1["Profiler: 无计算权限"]
        N2["Writer: 看不到原始数据<br/>只能引用上游产出"]
    end

    style ISO fill:#fff2cc,stroke:#d6b656
```

对应 `swarm.py` 的 `round_plan`：

```python
round_plan = {
    1: ['Profiler'],
    2: ['Analyst'],
    3: ['Attributor'],
    4: ['Writer'],
}
```

`AgentRole` 的 `cwd` / `tool_filter` / `acceptance_commands` **原样复用**，天然实现隔离。

---

## 8. 模块目录结构（✅ v1.0 实际实现）

> 下图是**已落地的真实结构**（非目标态）。数字为实际行数。

```mermaid
graph TD
    ROOT["data_analysis_agent/"]

    ROOT --> SEM["semantic/ ★核心资产"]
    SEM --> SE1["schema.sql<br/>1,200 行<br/>STG→DWD→DIM→SEM + 16 指标 + 9 质量门"]

    ROOT --> APP["app/ 后端 4,400 行"]
    APP --> CFG["config.py 277<br/>三层配置 + 启动自检"]
    APP --> MAIN["main.py 112<br/>FastAPI 入口 + 统一错误"]
    APP --> ADP["adapters/ 889<br/>淘宝适配器 + 字段映射表"]
    APP --> SVC["services/ 1,717<br/>auth/billing/diagnosis/tenant/security"]
    APP --> APID["api/ 808<br/>deps/schemas/routes×4"]
    APP --> SEMD["semantic/ 1,126<br/>models/registry/renderer/timerange"]
    APP --> STO["storage/ 105<br/>db.py + schema.sql"]

    ROOT --> WEB["web/ 前端 2,100 行"]
    WEB --> W1["src/api/ 357<br/>client.ts + types.ts"]
    WEB --> W2["src/pages/ 1,404<br/>8 个页面"]
    WEB --> W3["src/components/ 91<br/>AppLayout"]
    WEB --> W4["src/stores/ 80<br/>auth.tsx (Context)"]

    ROOT --> TST["tests/ 3,300 行<br/>19 个测试文件 / 688 断言"]
    ROOT --> DOCS["docs/ 9 份文档 + 架构讲解.html"]

    style SEM fill:#ffe6cc,stroke:#d79b00,stroke-width:3px
    style APP fill:#dae8fc,stroke:#6c8ebf
    style WEB fill:#d5e8d4,stroke:#82b366
    style TST fill:#e1d5e7,stroke:#9673a6
```

### 后端模块职责表

| 模块 | 行数 | 职责 |
|---|---|---|
| `config.py` | 277 | 三层配置 + **启动自检**（密钥非空、沙箱隔离） |
| `main.py` | 112 | FastAPI 入口、lifespan、CORS、统一错误格式 |
| `storage/db.py` | 105 | SQLite 连接（**WAL + busy_timeout + 外键**）、事务上下文 |
| `storage/schema.sql` | — | **19 张表**（含 `refund_pending` / `credit_lots`）/ 26 索引 |
| `adapters/taobao_fields.py` | 302 | **7 种数据源**的字段映射规格（别名 + 置信度 + 类型） |
| `adapters/taobao.py` | 587 | 识别 / 读取 / 编码探测 / 表头定位 / 类型驱动转换 |
| `semantic/models.py` | 301 | `MetricDefinition` / `Uncertainty` / `CaliberConflict` |
| `semantic/registry.py` | 272 | 加载 / 别名解析 / **口径歧义** / 维度白名单 / 质量门 |
| `semantic/renderer.py` | 342 | **指标 + 维度 + 时间窗 → 参数化 SQL**（含注入防护） |
| `semantic/timerange.py` | 211 | 中文时间表达 → 日期区间 + 同环比 |
| `services/security.py` | 106 | **bcrypt 直用**（弃 passlib）+ 令牌 + 密码强度 |
| `services/auth.py` | 289 | 注册 / 登录 / **账号锁定** / 令牌轮换 / 吊销 |
| `services/billing.py` | 700 | 额度账户 / **订单** / 支付幂等 / 成本记录 / **退款幂等 + refund_pending + 月度熔断** |
| `services/locks.py` | 45 | **每租户 RLock**（DuckDB 写路径串行化） |
| `services/ratelimit.py` | 95 | 进程内滑动窗口限速（register/login/ask/upload/diagnosis） |
| `services/email.py` | 120 | **SMTP 邮件**（465 SSL / 587 STARTTLS）+ 验证邮件模板 |
| `services/tenant.py` | 216 | **租户沙箱**（三层路径防御）/ 产物寻址 |
| `services/diagnosis.py` | 1,100+ | **诊断编排**：上传→画像→映射→导入→ETL→门→报告；**启动对账 + 语义层版本迁移** |
| `api/deps.py` | 110 | 认证依赖 / 租户绑定 / 额度校验 / **限速入口（429）** |
| `api/routes/*.py` | 640 | auth / sources / diagnoses(**SSE**) / billing / chat / **admin（补偿退款）** |

### 前端页面

| 页面 | 文件 | 行数 |
|---|---|---|
| 登录 / 注册 | `Login.tsx` `Register.tsx` | 111 |
| 工作台 | `Dashboard.tsx` | 206 |
| 上传 | `Upload.tsx` | 198 |
| **字段映射确认** | `Mapping.tsx` | 176 |
| **诊断进度（SSE）** | `Progress.tsx` | 156 |
| **报告 + 溯源** | `Report.tsx` | 193 |
| 购买次数 | `Billing.tsx` | 248 |
| 账户设置 | `Settings.tsx` | 117 |

> **注**：原设计的 `src/` 顶层结构（`constraint/` `tools/` `context/` `memory/`
> `verify/` `delivery/` `engine.py`）**未按原样落地** —— 数据 Agent 不需要
> 文件编辑工具链，因此按职责合并进了 `app/services/` 与 `app/semantic/`。
> 架构分层**保留**，目录划分**按实际需要重组**。详见 `01-DESIGN.md` §8 偏差 1。

**另需 `web/` 前端工程**（独立于 `src/`）：

```mermaid
graph TD
    W["web/ ★已实现"]
    W --> W0["src/api/client.ts 213<br/>Axios 拦截器 + JWT + 401 刷新"]
    W --> W1["src/api/types.ts 144<br/>TS 类型 + 格式化"]
    W --> W2["src/stores/auth.tsx 80<br/>React Context"]
    W --> W3["src/pages/ 1,404<br/>Login/Register/Dashboard/Upload"]
    W --> W4["Mapping/Progress/Report ▲核心"]
    W --> W5["Billing/Settings"]
    W --> W6["src/components/AppLayout.tsx 91"]
    style W4 fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style W0 fill:#d5e8d4,stroke:#82b366
```

---

## 9. 关键时序：从上传到报告（✅ 已实现）

> 该时序已在**全栈冒烟测试**中逐步骤验证（经 Vite 代理，33 项断言）。

```mermaid
sequenceDiagram
    participant U as 客户浏览器
    participant W as 前端 Web UI
    participant A as 接口层 FastAPI
    participant DB as SQLite
    participant P as 后台线程池
    participant E as 诊断服务
    participant V as 验证层

    Note over U,A: ① 上传
    U->>W: 拖入 xlsx
    W->>A: POST /sources (multipart)
    A->>A: 扩展名 + 大小校验（拒绝 .exe）
    A->>A: 落盘 tenants/{tid}/inbox/{uuid}.csv
    A->>A: 识别类型（7 种规格打分）
    A->>DB: INSERT sources（按 sha256 去重）
    A-->>W: 201 {source_id, source_type, row_count}

    Note over W,A: ② 画像（同步，秒级）
    W->>A: GET /sources/{id}/profile
    A->>E: profile_source()
    E-->>A: 列结构 + 映射建议（★不含数据行）
    A-->>W: 22 字段 + 置信度

    Note over W,A: ③ 字段映射确认 ★人工把关
    U->>W: 核对低置信度项，可改选源列
    W->>A: POST /sources/{id}/mapping
    A->>DB: INSERT field_mappings（按 schema hash 复用）
    A-->>W: 已确认（下次同结构免确认）
    W->>A: GET /sources/{id}/mapping
    A->>E: suggest_field_mapping
    E-->>A: 候选映射 + 置信度
    A-->>W: 候选列表
    U->>W: 检查并修正低置信度项
    W->>A: POST /sources/{id}/mapping
    A->>DB: INSERT field_mappings (按 schema hash 复用)
    A-->>W: 200 已确认

    Note over W,E: ④ 诊断（异步长任务）
    W->>A: POST /diagnoses
    A->>DB: INSERT diagnoses (status=running)
    A->>P: ThreadPool 提交任务（预生成 diagnosis_id）
    A-->>W: 202 {diagnosis_id}

    W->>A: GET /diagnoses/{id}/events (SSE)
    Note over A,W: ★ 服务端先回放历史事件<br/>（诊断可能已跑完，不回放前端看不到进度）
    P->>E: run_diagnosis(tenant,<br/>source_ids 一组文件)
    E->>DB: 扣 1 次额度（credit_ledger 留痕）
    loop 每个文件
        E->>E: 导入 STG（类型驱动转换）
        E-->>A: progress: importing
        A-->>W: data: {type:progress, phase, message}
    end
    E->>E: ETL（STG→DWD）
    E->>E: 质量门（9 条 QC）
    E->>E: SQL 渲染器 → 计算 16 指标
    E->>E: 生成报告产物（内容寻址）
    E->>DB: UPDATE diagnoses SET status=succeeded
    E-->>A: _emit(done)
    A-->>W: data: {type:done, result:{metrics}}
    Note over A,W: 失败时自动退还额度

    Note over U,W: ⑤ 报告 + 溯源
    W->>A: GET /diagnoses/{id}/report
    A-->>W: HTML 报告
    U->>W: 点某个数字的 ⓘ
    W->>A: GET /diagnoses/{id}（含 metrics + uncertainty）
    A-->>W: 口径状态 / 公式 / 待确认项
```

> **实现差异**：原设计的溯源是单独的 `/trace` 端点；实际实现复用
> `GET /diagnoses/{id}`（已在报告中返回 `metric_spec` 与 `uncertainty`），
> 少一次往返。完整的 `v_metric_trace` 视图留在语义层备用。

### 9.1 为什么长任务必须任务化 + SSE

```mermaid
flowchart TD
    A[一次诊断耗时 10 秒 ~ 数分钟] --> B{直接用同步接口?}
    B -->|是| C[HTTP 超时<br/>浏览器超时<br/>网关超时]
    C --> D[❌ 用户看到失败<br/>但任务其实还在跑<br/>重复提交 → 重复扣额度]
    B -->|否| E[POST 立即返回 202 + diagnosis_id]
    E --> F[SSE 推送实时进度<br/>+ 历史事件回放]
    F --> G[✅ 用户看到阶段与已算指标]
    G --> H[断线可重连<br/>GET /diagnoses/id 轮询兜底]

    style D fill:#ffcccc,stroke:#cc0000
    style G fill:#d5e8d4,stroke:#82b366
```

### 9.2 SSE 事件时序的两个坑（实测踩过）

```mermaid
flowchart TD
    subgraph P1["坑 1：订阅前的进度全丢"]
        A1[诊断几百毫秒跑完] --> A2[客户端才连上 SSE]
        A2 --> A3[❌ 只能看到空的进度流]
        A3 --> A4[✅ 修法：服务端缓冲历史事件<br/>订阅时一并回放]
    end

    subgraph P2["坑 2：done 事件收不到"]
        B1["流程：先写 DB succeeded<br/>再 _emit(done)"]
        B1 --> B2["SSE 循环看到 DB 终态就 break"]
        B2 --> B3["❌ 抢在 done 入队前结束流"]
        B3 --> B4["✅ 修法：加 2 秒宽限期<br/>仍无则合成终态事件兜底"]
    end

    style A3 fill:#ffcccc,stroke:#cc0000
    style B3 fill:#ffcccc,stroke:#cc0000
    style A4 fill:#d5e8d4,stroke:#82b366
    style B4 fill:#d5e8d4,stroke:#82b366
```

---

## 10. 存储分工与 SQLite 并发（✅ 已实现）

```mermaid
graph TB
    subgraph DB["SQLite app.db —— 15 张表 / 23 索引"]
        T1["tenants + users<br/>+ refresh_tokens"]
        T2["credit_accounts + credit_ledger<br/>★按次额度"]
        T3["orders + payment_events<br/>订单与支付"]
        T4["sources + field_mappings"]
        T5["diagnoses + usage_records"]
    end

    subgraph FS["文件系统 tenants/{id}/ —— 大对象"]
        F1["inbox/ 上传原始文件<br/>（UUID 命名）"]
        F2["semantic.duckdb<br/>★每租户独立语义层库"]
        F3[".harness/artifacts/<br/>画像/报告（hash 去重）"]
        F4["reports/ 报告产物"]
    end

    T4 -.->|sha256 去重| F1
    T4 -.->|profile_ref| F3
    T5 -.->|report_ref| F3
    T2 -.->|额度变动留痕| T3

    style DB fill:#d5e8d4,stroke:#82b366
    style FS fill:#dae8fc,stroke:#6c8ebf
    style T2 fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style F2 fill:#ffe6cc,stroke:#d79b00
```

**两处 v1.0 的关键变更**：

| 变更 | 原设计 | 实际实现 | 原因 |
|---|---|---|---|
| **计费模型** | `subscriptions` 订阅表 | **`credit_accounts` + `credit_ledger`** | 按次付费只需"加/减"两个操作，订阅要处理续费/退款/升降级 |
| **语义层位置** | DuckDB 全局一个库 | **每租户一个 `semantic.duckdb`** | 租户数据隔离在**文件系统层**，不靠 WHERE 条件 |

### ★ 两处刻意的外键缺失（不是遗漏）

```mermaid
flowchart LR
    A["credit_ledger.diagnosis_id<br/>usage_records.diagnosis_id"] --> B{"加 FK 指向<br/>diagnoses(id)?"}
    B -->|加| C["❌ 额度在诊断开始时扣<br/>此时诊断行还没落库<br/>→ FK 失败<br/>→ 用户看到「次数不足」但实际有余额"]
    B -->|不加| D["✅ 弱引用<br/>由业务层保证一致性"]

    style C fill:#ffcccc,stroke:#cc0000
    style D fill:#d5e8d4,stroke:#82b366
```

**SQLite 并发纪律**（FastAPI 多线程下必须遵守）：

```mermaid
flowchart LR
    A[FastAPI 多线程] --> B["PRAGMA journal_mode=WAL<br/>读写可并发"]
    B --> C["PRAGMA busy_timeout=5000<br/>避免瞬时 locked"]
    C --> D["PRAGMA foreign_keys=ON<br/>外键约束生效"]
    D --> E["每线程独立连接<br/>threading.local"]
    E --> F["uvicorn --workers 1<br/>★SQLite 单写者 + 进程内任务调度"]
    F --> G[✅ 稳定]
    style G fill:#d5e8d4,stroke:#82b366
```

### 额度扣减的原子性（防超扣）

```sql
-- ★ 条件 UPDATE：余额不足时影响 0 行 —— 从根上杜绝负余额
UPDATE credit_accounts SET balance = balance - 1, total_used = total_used + 1
 WHERE tenant_id = ? AND balance >= 1;
```

**实测验证**：余额扣到 0 后继续扣，仍保持 0（不出现负数）。

---

## 11. 关键时序：从上传到报告（引擎内部细节）

```mermaid
sequenceDiagram
    participant U as 客户
    participant S as 沙箱
    participant T as 工具层
    participant M as 语义层
    participant E as 引擎
    participant V as 验证层

    U->>S: 上传 xlsx
    S->>S: 路径白名单校验
    E->>T: list_sources
    T-->>E: 文件清单
    E->>T: profile_source
    T-->>E: 画像(不含数据)
    E->>T: load_source
    T-->>E: 句柄ID + schema
    E->>T: suggest_field_mapping
    T-->>E: 候选映射+置信度
    E->>M: resolve(metric, caliber)
    M-->>E: 定义 或 CaliberAmbiguous
    E->>V: 门1 数据质量
    V-->>E: 通过/不可用
    E->>T: compute_metric(按拓扑序)
    T->>T: 写入 computed_registry
    T-->>E: 数值+血缘
    E->>T: compare_periods / detect_anomaly
    E->>T: decompose_change / test_hypothesis
    E->>V: 门2 数值自洽(含溯源)
    E->>V: 门3 归因可证
    V-->>E: 通过 / 降级
    E->>T: render_chart / emit_report
    T-->>U: 诊断报告(HTML)
```

---

## 12. 语义层实体关系（v0.3 新增，来自用户提供的 SQL）

```mermaid
erDiagram
    sem_metric_registry ||--o{ sem_metric_dimension : "维度白名单"
    sem_metric_registry ||--|| v_traffic_daily : "formula_view"
    sem_metric_registry ||--|| v_sales_daily : "formula_view"
    sem_metric_registry ||--|| v_profit_daily : "formula_view"
    sem_metric_registry ||--|| v_ads_daily : "formula_view"
    sem_metric_registry ||--|| v_tacos_daily : "formula_view"
    sem_metric_registry ||--|| v_refund_daily : "formula_view"

    stg_trade_orders ||--|| dwd_order_line : "ETL"
    stg_sycm_traffic ||--|| dwd_traffic_daily : "ETL"
    stg_account_flow ||--|| dwd_platform_fee : "ETL"
    stg_refunds ||--|| dwd_refund_line : "ETL"
    stg_ad_reports ||--|| dwd_ad_daily : "ETL"
    stg_purchase_batches ||--|| dwd_cost_batch : "ETL"
    stg_inventory ||--|| dwd_inventory_snapshot : "ETL"

    dwd_order_line ||--o{ v_sales_daily : ""
    dwd_refund_line ||--o{ v_sales_daily : ""
    dwd_traffic_daily ||--o{ v_traffic_daily : ""
    dwd_order_line ||--o{ v_traffic_daily : ""
    v_sales_daily ||--o{ v_profit_daily : ""
    dwd_cost_batch ||--o{ v_profit_daily : ""
    dwd_platform_fee ||--o{ v_profit_daily : ""
    dwd_ad_daily ||--o{ v_profit_daily : ""
    dwd_ad_daily ||--o{ v_ads_daily : ""
    v_sales_daily ||--o{ v_tacos_daily : ""
    dwd_refund_line ||--o{ v_refund_daily : ""
    v_profit_daily ||--o{ v_metric_trace : "溯源"

    sem_metric_registry {
        TEXT metric_id PK "M01..M18"
        TEXT metric_name
        TEXT category
        TEXT priority "P0/P1/P2"
        TEXT aliases "JSON 自然语言别名"
        TEXT formula_view "指向指标视图"
        TEXT formula_expression "人类可读公式"
        TEXT metric_spec "★机器可执行规格 JSON"
        TEXT caliber_rules "JSON 口径规则"
        TEXT footnote_template "回答自动附带"
        TEXT quality_rules "JSON 质量规则 ID"
        TEXT uncertainty "★JSON 不确定项"
        TEXT status "draft/confirmed"
    }
    sem_metric_dimension {
        TEXT metric_id FK
        TEXT dimension_name
        TEXT dim_table
        TEXT enabled
    }
```

> **本语义层四列最关键**：
> - `caliber_rules` —— 口径变更是 `UPDATE` 一行而非改代码
> - `footnote_template` —— 每条回答自动带口径说明
> - **`metric_spec`** —— 机器可执行规格，SQL 渲染器的唯一输入（v1.0 新增）
> - **`uncertainty`** —— 不确定项显式披露，报告自动加"待确认"提示（v1.0 新增）

### 指标视图（7 个，已实现）

| 视图 | 粒度 | 服务指标 |
|---|---|---|
| `v_traffic_daily` | 日期 × 店铺/商品 × 渠道 | M01-M05 |
| `v_sales_daily` | 日期 × 商品 × 商家编码 | M06-M07 |
| `v_profit_daily` | 日期 × 商品 × 商家编码 | M10-M14 |
| `v_ads_daily` | 日期 × 渠道 × 计划 | M15 |
| `v_tacos_daily` | 日期 | M16 |
| `v_refund_daily` | 日期 × 商品 × 原因 | M17-M18 |
| `v_metric_trace` | 日期 × 商品 | **数字溯源（门2 第4条）** |

### 从 STG 到指标的数据流（淘宝版）

```mermaid
graph LR
    subgraph STG["STG 千牛导出原样落表（7 张）"]
        S1[stg_trade_orders]
        S2[stg_sycm_traffic]
        S3[stg_account_flow]
        S4[stg_refunds]
        S5[stg_ad_reports]
        S6[stg_purchase_batches]
        S7[stg_inventory]
    end

    subgraph DWD["DWD 统一口径事实（7 张）"]
        D1[dwd_order_line]
        D2[dwd_traffic_daily]
        D3[dwd_platform_fee]
        D4[dwd_refund_line]
        D5[dwd_ad_daily]
        D6[dwd_cost_batch]
        D7[dwd_inventory_snapshot]
    end

    subgraph SEMV["SEM 指标视图（7 个）"]
        V1[v_traffic_daily]
        V2[v_sales_daily]
        V3[v_profit_daily]
        V4[v_ads_daily]
        V5[v_tacos_daily]
        V6[v_refund_daily]
        V7[v_metric_trace]
    end

    S1 --> D1 --> V2
    S2 --> D2 --> V1
    S3 --> D3 --> V3
    S4 --> D4 --> V2
    S5 --> D5 --> V4
    S6 --> D6 --> V3
    S7 --> D7

    V2 --> V3
    V3 --> V7

    style STG fill:#dae8fc,stroke:#6c8ebf
    style DWD fill:#d5e8d4,stroke:#82b366
    style SEMV fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
```

> **ETL 里修掉的一个真实 bug**：`S5.3 退款行` 曾引用 `confirm_time` / `ship_time`
> —— 这两个字段属于 `stg_trade_orders`，不在 `stg_refunds` 里。
> 用 SQLite 代理验证时**没跑到这段 ETL** 所以漏掉了，换真实 DuckDB 才暴露。
> 修法：`LEFT JOIN` 订单表取确认收货时间。

---

## 13. 运行时选型：DuckDB + SQLite 并存（v0.3 新增）

```mermaid
graph TB
    subgraph APP["应用元数据"]
        S1["SQLite · app.db<br/>租户/任务/计费/映射确认"]
    end

    subgraph SEMDB["语义层 + 数据分析"]
        S2["DuckDB · per-tenant<br/>tenants/id/semantic.duckdb<br/>STG / DWD / DIM / SEM"]
    end

    subgraph FS["文件系统"]
        F1["tenants/id/inbox/<br/>卖家导出文件"]
        F2["tenants/id/reports/<br/>HTML 报告"]
    end

    F1 -->|"DuckDB 直接读 Excel/CSV"| S2
    S2 -->|"指标结果"| F2
    S1 -.->|"租户配额/任务状态"| S2

    style S2 fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style S1 fill:#d5e8d4,stroke:#82b366
```

| 用途 | 选型 | 理由 |
|---|---|---|
| 语义层 + 数据分析 | **DuckDB** | 列存、**直接读 Excel/CSV**、`FULL OUTER JOIN`、分析函数 |
| 应用元数据 | **SQLite** | 单文件、零配置、事务写、小并发够用 |

> **不冲突**：DuckDB 每租户一个文件，SQLite 全局一个。**两者都不需要外部服务。**

---

## 14. LLM 接入链路（端点已实测，⚠️ 尚未进入分析循环）

> ⚠️ **v1.0 重要说明**：当前指标计算是**确定性 SQL 渲染**，**不调 LLM**。
> 好处是**数字 100% 可复现、零幻觉**；代价是**没有自然语言归因**。
> 下面的链路是**为二期归因功能准备的**，端点与能力已全部验证可用。

```mermaid
sequenceDiagram
    participant E as 二期：归因服务
    participant R as ProviderRouter（待实现）
    participant C as LLMClient（待实现）
    participant Q as LLM 服务商网关

    Note over E,Q: ⚠️ 以下链路尚未接入，仅端点已验证
    E->>R: complete(messages, tools)
    R->>C: 按优先级选 provider
    C->>C: _clean_messages() 规整格式
    C->>Q: POST /chat/completions
    Note over Q: model-flash<br/>tool calling 实测支持<br/>会临时限流 429
    alt 429 / 5xx
        C->>C: _jittered_backoff() 去相关抖动
        C->>Q: 重试（最多 3 次）
    end
    Q-->>C: content + tool_calls + usage
    C-->>R: 统一格式
    R-->>E: response + provider_name

    Note over E: reasoning tokens 需单独统计<br/>占 completion 绝大部分
```

**三条实测结论**（详见 `01-DESIGN.md` §7.5）：
1. 必须用 `model-flash`（`model-plus` 会 404）
2. **这是推理模型**：reasoning token 可达可见输出的 3–43 倍
3. 端点会限流 → 依赖 `client.py:122-125` 已有的抖动重试

---

## 15. 淘宝适配层（✅ 已实现）

> **v1.0 更新**：适配层已实现，且语义层**不是"零改动"** —— 已整体重写为淘宝结构。
> 原设计的"亚马逊 STG + 适配器映射"方案被放弃，改为**语义层直接对齐淘宝**。

```mermaid
graph LR
    subgraph KN["千牛导出（7 种）"]
        C1["已卖出的宝贝<br/>订单"]
        C2["生意参谋<br/>流量"]
        C3["账房<br/>收支明细"]
        C4["退款售后"]
        C5["直通车/万相台"]
        C6["采购成本<br/>（自录）"]
        C7["库存"]
    end

    subgraph ADP["适配器 app/adapters/ ★已实现"]
        A1["识别（7 规格打分）"]
        A2["读取（编码探测 + 表头定位）"]
        A3["字段映射（别名 + 置信度）"]
        A4["类型驱动转换"]
    end

    subgraph STD["标准 STG 层（淘宝结构）"]
        S1[stg_trade_orders]
        S2[stg_sycm_traffic]
        S3[stg_account_flow]
        S4[stg_refunds]
        S5[stg_ad_reports]
        S6[stg_purchase_batches]
        S7[stg_inventory]
    end

    C1 --> A1
    C2 --> A1
    C3 --> A1
    C4 --> A1
    C5 --> A1
    C6 --> A1
    C7 --> A1
    A1 --> A2 --> A3 --> A4
    A4 --> S1
    A4 --> S2
    A4 --> S3
    A4 --> S4
    A4 --> S5
    A4 --> S6
    A4 --> S7

    style ADP fill:#ffe6cc,stroke:#d79b00,stroke-width:2px
    style STD fill:#d5e8d4,stroke:#82b366
```

### 适配器的四道处理

```mermaid
flowchart TD
    F["客户上传的文件"] --> A["① 识别<br/>按列名打分选最匹配的规格"]
    A --> B{"识别置信度 > 0.5?"}
    B -->|否| X["422 请确认这是千牛报表"]
    B -->|是| C["② 读取<br/>编码探测 + 表头行定位"]
    C --> D["③ 字段映射<br/>别名精确匹配 → 子串匹配"]
    D --> E{"低置信度<br/>或缺必填?"}
    E -->|是| Y["前端高亮，要求人工确认"]
    E -->|否| Z["④ 类型驱动转换<br/>按目标表列类型转换"]
    Y --> Z
    Z --> W["写入 STG 表"]

    style X fill:#ffcccc,stroke:#cc0000
    style Y fill:#fff2cc,stroke:#d6b656
    style Z fill:#d5e8d4,stroke:#82b366
```

**四个实测能力**：

| 能力 | 实测 |
|---|---|
| **编码探测** | GBK / UTF-8-BOM 正确识别，中文不乱码 |
| **表头定位** | 跳过"订单导出报表 / 导出时间：xxx"等前置行（实测跳 2 行） |
| **数值归一化** | `1,234.56` / `¥1,234` / `45.0%` / `(123.45)` / `1.5万` 全对 |
| **日期归一化** | `2026/10/01`、`2026.10.01`、`2026年10月1日` → ISO |

> **类型驱动转换是 v1.0 的修正**（见 `01-DESIGN.md` §8 偏差 11）：
> 早期用字段名关键词（`amount`/`price`/`fee`）判断是否转数值，
> 结果 `seller_income` 不含任何关键词 → `'6,000.00'` 原样进库
> → DuckDB 转 DOUBLE 失败。改为按 `FIELD_TYPES` 表声明的类型转换。

---

## 15.5 样例数据与多工作表拆分（v1.1 新增）

### 获客漏斗：从"没有数据"到"看到报告"

```mermaid
flowchart LR
    A["陌生访客<br/>未注册"] -->|"GET /samples/download<br/>(公开)"| B["下载样例 Excel<br/>7 个工作表"]
    B -->|"注册<br/>(赠送 1 次体验)"| C["拖入上传区"]
    C -->|"prepare_all<br/>逐表嗅探"| D["自动拆分 6 个数据源<br/>(说明页跳过)"]
    D -->|"零人工映射<br/>(列名精确匹配)"| E["开始诊断"]
    E --> F["报告:卡片+趋势图<br/>看出第 3 周的故事"]
    F -->|"体验额度用完"| G["购买 ¥A/¥B/¥C"]

    style A fill:#ffcccc,stroke:#cc0000
    style D fill:#ffe6cc,stroke:#d79b00
    style F fill:#d5e8d4,stroke:#82b366
    style G fill:#d5e8d4,stroke:#82b366
```

### 多工作表拆分（`taobao.prepare_all`）

```mermaid
flowchart TD
    X["一个 xlsx 上传"] --> P["prepare_all(path)"]
    P --> S1["'说明' 页 → 认不出 → 跳过"]
    P --> S2["'订单明细' → trade_orders"]
    P --> S3["'生意参谋流量' → sycm_traffic"]
    P --> S4["其余 4 表 → 各自规格"]
    S2 & S3 & S4 --> R["sources 表 N 行<br/>(sha256+sheet_name 去重)"]
    S1 --> Q["全部认不出才 422"]

    style S1 fill:#fff2cc,stroke:#d6b656
    style R fill:#d5e8d4,stroke:#82b366
```

**样例数据自洽性约束**（`app/services/sample_data.py`）：
流量表 `支付金额` 与订单逐日合计一致；账房费用 = 当日支付 × 固定费率；
退款申请日 = 付款日 + 2~6 天（体现按申请日归属口径）。
确定性随机（`Random(42)`），测试可精确断言（模块 8，48 项）。

---

## 15.6 对话式分析数据流（v1.4：形态 A + B）

### 一次归因提问的完整链路（形态 B）

```mermaid
sequenceDiagram
    participant U as 用户
    participant C as chat.ask（路由）
    participant Q as 形态A qa_engine
    participant E as agent_engine（循环）
    participant L as LLM（服务商网关）
    participant D as 租户 DuckDB

    U->>C: "为什么第 3 周转化率跌了？"
    C->>Q: 先试查数（零 LLM）
    Q-->>C: NeedsAgentError（归因类）
    C->>C: 配额检查（×5/诊断额度；admin 免）
    C->>E: system prompt（纪律+数据上下文）+ 历史 + 问题

    loop 最多 8 轮（LoopGuard 3/3/8 / 超 60k 压缩）
        E->>L: chat(messages, tools=5个)
        L-->>E: tool_calls: weekly_compare
        E->>D: 确定性 SQL 聚合（★原始数据不进上下文）
        D-->>E: 聚合结果（每周 5 个数字）
        E->>L: tool 消息回填 → 模型看结果定下一步
    end
    L-->>E: 最终自然语言回答（数字全部来自工具返回）
    E->>C: 回答 + 工具轨迹 + tokens/成本
    C-->>U: 落库（route=agent）+ 前端标签（轮次/工具数/成本）
```

### ★ 上下文里有什么、没有什么（"数据不进大模型"的准确含义）

| 进 LLM 上下文 | 永不进上下文 |
|---|---|
| 消息文本（问题/历史/回答） | **原始数据行**（1299 行订单明细等） |
| 工具规格（5 个工具的 JSON schema，~600 token） | 完整数据画像 |
| **工具返回的聚合结果**（"第 3 周 访客 17,294 净销 ¥59,294…"，单个结果截断 4000 字符） | 任何明细级查询结果 |
| 数据上下文摘要（几个文件/周期/诊断时间） | 文件内容本身 |

数据访问的唯一通道：模型**只能通过工具**间接读库，工具只做确定性聚合 ——
这就是"数据不进上下文，模型拿到的是数"的落地方式（成本可控 + 数字可溯源 + 防注入）。

---

## 16. 验证结果（v1.1）

```mermaid
graph TB
    subgraph SUITE["18 个测试模块 / 688 项断言（全绿）"]
        M1["模块 1 配置+存储<br/>13 断言"]
        M2["模块 2 语义层接口<br/>34"]
        M3["模块 3 淘宝适配器<br/>28"]
        M4["模块 4 SQL 渲染器<br/>47"]
        M5["模块 5 密码与令牌<br/>41"]
        M6A["模块 6a 认证<br/>38"]
        M6B["模块 6b 额度订单<br/>46"]
        M6C1["模块 6c1 租户沙箱<br/>53"]
        M6C["模块 6c 诊断服务<br/>56"]
        M6DE["模块 6d/e API<br/>83"]
        M8["模块 8 样例全流程<br/>48"]
        SMOKE["全栈冒烟(经代理)<br/>33"]
        DDB["DuckDB 方言+端到端<br/>11"]
    end

    SUITE --> CMD["python tests/run_all.py"]

    style SUITE fill:#d5e8d4,stroke:#82b366,stroke-width:2px
```

**关键验证维度**：

| 维度 | 验证方式 |
|---|---|
| **数值正确性** | 手工核算 vs 系统输出逐项比对（M01=1900、M13=56.17% 等） |
| **口径正确性** | 淘宝官方口径（支付转化率、退款归属）逐条核对 |
| **安全** | 路径穿越 ×6、符号链接逃逸、SQL 注入 ×4、跨租户越权 ×7 |
| **隔离** | 两租户文件/额度/诊断/报告互不可见 |
| **原子性** | 额度扣到 0 不再变负；支付回调幂等；失败退还 |
| **全链路** | 经 Vite 代理跑完整用户流程（注册→购买→上传→诊断→报告） |
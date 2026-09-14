# SupplyAgent 数据契约（PostgreSQL DDL）

> **文档契约** · 类型：契约层 · 读取：实现某张表或改 schema 时按表名定点读，禁止通读
> 更新：表结构、约束、索引或灌数方向变更时由 Claude 写入
> 独占：PostgreSQL 表结构与约束正文、审计表的只插入强制方式、灌数方向、`tenant_id` 预留口径
> 不收录：业务规则条款（见 `docs/product/requirements.md`）、状态取值与迁移（见 `docs/spec/state-machine.md`）、工具契约（见 `docs/spec/interfaces.md`）、数据现状与缺口（见 `data/supplychain/normalized/projects.yaml`）、术语含义（见 `docs/product/GLOSSARY.md`）

本文是表结构的唯一正文来源。DDL 以 PostgreSQL 16 为目标，迁移由 Alembic 管理——**本文定义目标形态，`alembic/versions/` 定义如何到达**，两者冲突时以本文为准并修迁移脚本。

## 1. 全局约定

- **时间**：一律 `TIMESTAMPTZ`，存 UTC。禁止 `TIMESTAMP`（无时区）。
- **数量与金额**：一律 `NUMERIC`，禁止 `FLOAT` / `DOUBLE PRECISION`。BR-03 要求精确数值类型，浮点会在阶梯价与倍数计算中引入误差。数量 `NUMERIC(18,6)`，金额 `NUMERIC(18,6)`。
- **标识符**：业务主键用文本自然键（`line_id`、`component_id` 等，与 `normalized/` 一致）；运行时对象用 `UUID`。
- **多租户**：业务表一律带 `tenant_id TEXT NOT NULL DEFAULT 'default'`，**仅作架构预留**，MVP 不实现隔离逻辑（见 `DECISIONS.md` D10）。审计表同样带，便于将来按租户裁剪。
- **模拟数据标注**：任何合成数据的表带 `is_simulated BOOLEAN NOT NULL DEFAULT FALSE`。最终回答必须能区分模拟与真实来源（见 `productinfo.md` §7 第 7 条）。
- **证据引用**：`evidence_ref` 统一为 `JSONB`，形如 `{"kind": "tool_call", "id": "...", "retrieved_at": "..."}`。每个实质性判断都要能回指。
- **命名**：表名单数、蛇形；外键列名为 `<引用表>_id`；索引 `ix_<表>_<列>`，唯一索引 `uq_<表>_<列>`。

## 2. 分层与依赖方向

```text
① 权威静态层（元件身份与 BOM）   ← 由 normalized/ 单向灌入，不接受应用写入
② 业务层（需求/库存/在途/方案）  ← 应用读写
③ 审计层（run/tool_call/...）     ← 只插入，永不更新或删除
④ 规则层（business_rule）         ← 版本化，变更留痕
```

灌数方向只能是 `domdata/（只读原始层）→ normalize_domdata.py → normalized/ → PostgreSQL`，不得反向回写，也不得跳过规范化层直读原始 CSV（见 `ARCHITECTURE.md`）。

## 3. ① 权威静态层

列名与 `data/supplychain/normalized/` 的 CSV 表头逐一对应，不另起别名。

```sql
CREATE TABLE project (
    project_id      TEXT PRIMARY KEY,
    github_url      TEXT,
    kitspace_url    TEXT,
    source_file     TEXT        NOT NULL,
    bom_lines       INTEGER     NOT NULL,
    total_quantity  INTEGER     NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE component (
    component_id    TEXT PRIMARY KEY,             -- part_<hash>
    manufacturer    TEXT        NOT NULL,
    mpn             TEXT        NOT NULL,
    identity_status TEXT        NOT NULL,         -- 见下方 CHECK
    used_by_count   INTEGER     NOT NULL DEFAULT 0,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_component_identity CHECK (
        identity_status IN ('source_asserted', 'review_required', 'verified')
    ),
    CONSTRAINT uq_component_mfr_mpn UNIQUE (manufacturer, mpn)
);

CREATE TABLE bom_line (
    line_id          TEXT PRIMARY KEY,            -- <project_id>-NNN
    project_id       TEXT        NOT NULL REFERENCES project(project_id),
    source_row_index INTEGER     NOT NULL,
    reference        TEXT        NOT NULL,        -- 位号，空格分隔，可含多个
    quantity         NUMERIC(18,6) NOT NULL,      -- 单块板用量
    description      TEXT,
    qty_basis_verified BOOLEAN   NOT NULL DEFAULT FALSE,  -- BR-01：未核验不得乘生产数量
    CONSTRAINT ck_bom_line_qty CHECK (quantity > 0)
);

CREATE TABLE bom_line_candidate (
    line_id          TEXT        NOT NULL REFERENCES bom_line(line_id),
    candidate_seq    INTEGER     NOT NULL,
    component_id     TEXT        NOT NULL REFERENCES component(component_id),
    manufacturer     TEXT        NOT NULL,        -- 归一后
    manufacturer_raw TEXT        NOT NULL,        -- 原始层写法，归一不覆盖原值
    mpn              TEXT        NOT NULL,
    PRIMARY KEY (line_id, candidate_seq)
);

CREATE TABLE bom_line_distributor_sku (
    line_id         TEXT NOT NULL REFERENCES bom_line(line_id),
    distributor     TEXT NOT NULL,                -- digikey / mouser / farnell / newark / rs
    distributor_sku TEXT NOT NULL,
    PRIMARY KEY (line_id, distributor, distributor_sku)
);

CREATE INDEX ix_bom_line_project  ON bom_line (project_id);
CREATE INDEX ix_candidate_component ON bom_line_candidate (component_id);
```

**`bom_line_distributor_sku` 挂在行级不挂在候选级**，这是源数据的固有限制：多候选行无法判定 SKU 归属哪个候选（见 `projects.yaml`）。不得在灌数时臆断归属。

一条用料行可以没有任何候选（源 BOM 无 MPN），这是合法状态，保留为未解决项（BR-10），不用占位行填补。

## 4. ② 业务层

```sql
CREATE TABLE demand (
    demand_id       UUID PRIMARY KEY,
    tenant_id       TEXT        NOT NULL DEFAULT 'default',
    project_id      TEXT        NOT NULL REFERENCES project(project_id),
    product_version TEXT        NOT NULL,
    quantity        NUMERIC(18,6) NOT NULL,       -- 生产数量
    need_by_date    DATE        NOT NULL,
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_simulated    BOOLEAN     NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_demand_qty CHECK (quantity > 0)
);

-- BOM 展开后的逐行需求。只有 qty_basis_verified 的行才能展开（BR-01）。
CREATE TABLE demand_line (
    demand_id     UUID  NOT NULL REFERENCES demand(demand_id),
    line_id       TEXT  NOT NULL REFERENCES bom_line(line_id),
    component_id  TEXT  REFERENCES component(component_id),  -- 未选定候选时为 NULL
    required_qty  NUMERIC(18,6) NOT NULL,
    unresolved_reason TEXT,                       -- 无 MPN / 候选未选定 / 数量口径未核验
    PRIMARY KEY (demand_id, line_id),
    CONSTRAINT ck_demand_line_resolved CHECK (
        (component_id IS NOT NULL AND unresolved_reason IS NULL)
        OR (component_id IS NULL AND unresolved_reason IS NOT NULL)
    )
);

CREATE TABLE inventory (
    component_id   TEXT        NOT NULL REFERENCES component(component_id),
    tenant_id      TEXT        NOT NULL DEFAULT 'default',
    warehouse      TEXT        NOT NULL,
    on_hand_qty    NUMERIC(18,6) NOT NULL DEFAULT 0,  -- 可用实物
    quarantine_qty NUMERIC(18,6) NOT NULL DEFAULT 0,  -- 隔离/待检，BR-04 明确不计入
    snapshot_at    TIMESTAMPTZ NOT NULL,
    is_simulated   BOOLEAN     NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tenant_id, component_id, warehouse),
    CONSTRAINT ck_inventory_nonneg CHECK (on_hand_qty >= 0 AND quarantine_qty >= 0)
);

-- 库存占用。BR-04 要求唯一关联、禁止重复抵扣，故对 (demand, component) 唯一。
CREATE TABLE inventory_allocation (
    allocation_id UUID PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    component_id  TEXT NOT NULL REFERENCES component(component_id),
    demand_id     UUID NOT NULL REFERENCES demand(demand_id),
    allocated_qty NUMERIC(18,6) NOT NULL,
    allocated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_allocation_demand_component UNIQUE (demand_id, component_id),
    CONSTRAINT ck_allocation_pos CHECK (allocated_qty > 0)
);

CREATE TABLE in_transit (
    in_transit_id UUID PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    component_id  TEXT NOT NULL REFERENCES component(component_id),
    qty           NUMERIC(18,6) NOT NULL,
    is_confirmed  BOOLEAN NOT NULL DEFAULT FALSE,  -- 仅已确认的计入可分配量
    eta           DATE,                            -- NULL = 无到货日期，列作不确定项（BR-04）
    allocated_to  UUID REFERENCES demand(demand_id),  -- NULL = 未分配
    is_simulated  BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_in_transit_pos CHECK (qty > 0)
);

CREATE INDEX ix_in_transit_component ON in_transit (tenant_id, component_id, eta);
```

`eta IS NULL` 的在途**不计入可分配量**，必须在结果中显式列为不确定项，不得按乐观假设折算。

## 5. ③ 方案与审批

```sql
CREATE TABLE plan (
    plan_id      UUID PRIMARY KEY,
    tenant_id    TEXT        NOT NULL DEFAULT 'default',
    demand_id    UUID        NOT NULL REFERENCES demand(demand_id),
    version      INTEGER     NOT NULL,
    content_hash TEXT        NOT NULL,            -- BR-08 审批绑定对象
    is_single_source BOOLEAN NOT NULL DEFAULT FALSE,  -- BR-07 单源方案须显式标注
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_plan_demand_version UNIQUE (demand_id, version)
);

CREATE TABLE plan_line (
    plan_id        UUID NOT NULL REFERENCES plan(plan_id),
    component_id   TEXT NOT NULL REFERENCES component(component_id),
    shortage_qty   NUMERIC(18,6) NOT NULL,        -- 缺口，BR-04
    suggested_qty  NUMERIC(18,6) NOT NULL,        -- 经 MOQ 与倍数上调后，BR-06
    distributor    TEXT,
    distributor_sku TEXT,
    unit_price     NUMERIC(18,6),                 -- NULL = 无价格，不得伪装完整报价
    currency       CHAR(3),                       -- BR-06 不同币种不自动混合比较
    price_break_qty NUMERIC(18,6),                -- suggested_qty 所在阶梯的起始数量
    moq            NUMERIC(18,6),
    order_multiple NUMERIC(18,6),
    lead_time_days INTEGER,                       -- NULL = 交期不明，不得声称满足交付日期
    quote_retrieved_at TIMESTAMPTZ,
    evidence_ref   JSONB,
    PRIMARY KEY (plan_id, component_id),
    CONSTRAINT ck_plan_line_price_currency CHECK (
        (unit_price IS NULL AND currency IS NULL) OR
        (unit_price IS NOT NULL AND currency IS NOT NULL)
    )
);
```

`ck_plan_line_price_currency` 是 BR-06 的结构化落地：有金额就必须有币种，不允许出现无币种的裸数字被后续误当作可比值。税费与运费**不建模**——未知即排除，不设默认 0 的列，避免被聚合成"到岸总价"。

## 6. ④ 审计层（只插入）

```sql
CREATE TABLE run (
    run_id        UUID PRIMARY KEY,
    tenant_id     TEXT        NOT NULL DEFAULT 'default',
    demand_id     UUID        REFERENCES demand(demand_id),
    trigger_kind  TEXT        NOT NULL,           -- user / monitor
    state         TEXT        NOT NULL,           -- 见 docs/spec/state-machine.md §1
    budget_total  NUMERIC(18,6),
    parent_run_id UUID        REFERENCES run(run_id),  -- 子任务预算从父预算扣除
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- run 的状态变化用事件表追加，不在 run 上原地改写，保证迁移过程可回溯。
CREATE TABLE run_state_event (
    event_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      UUID        NOT NULL REFERENCES run(run_id),
    from_state  TEXT,                             -- NULL = 初始
    to_state    TEXT        NOT NULL,
    reason      TEXT        NOT NULL,
    evidence_ref JSONB      NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tool_call (
    tool_call_id UUID PRIMARY KEY,
    run_id       UUID        NOT NULL REFERENCES run(run_id),
    trace_id     TEXT        NOT NULL,
    tool_name    TEXT        NOT NULL,
    request      JSONB       NOT NULL,
    response     JSONB,
    status       TEXT        NOT NULL,            -- ok / partial / not_found / error
    error_code   TEXT,
    duration_ms  INTEGER,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE llm_call (
    llm_call_id   UUID PRIMARY KEY,
    run_id        UUID        NOT NULL REFERENCES run(run_id),
    trace_id      TEXT        NOT NULL,
    worker        TEXT        NOT NULL,           -- supervisor / query / detail / research / summary / action
    model         TEXT        NOT NULL,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    duration_ms   INTEGER,
    route_rule    TEXT,                           -- 触发本次升级路由的显式规则
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE permission_decision (
    decision_id   UUID PRIMARY KEY,
    run_id        UUID        NOT NULL REFERENCES run(run_id),
    plan_id       UUID        NOT NULL REFERENCES plan(plan_id),
    plan_version  INTEGER     NOT NULL,
    content_hash  TEXT        NOT NULL,           -- 批准时绑定的哈希，BR-08
    action_scope  JSONB       NOT NULL,
    decision      TEXT        NOT NULL,           -- approved / rejected / changes_requested / expired
    decided_by    TEXT,                           -- expired 时为 NULL
    decided_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_decision_value CHECK (
        decision IN ('approved','rejected','changes_requested','expired')
    )
);

-- 跨系统写入的幂等边界。BR-11：同一逻辑动作最终只能对应一张草稿。
CREATE TABLE external_action (
    logical_action_id TEXT PRIMARY KEY,
    run_id        UUID        NOT NULL REFERENCES run(run_id),
    plan_id       UUID        NOT NULL REFERENCES plan(plan_id),
    target_system TEXT        NOT NULL,
    external_id   TEXT,                           -- 目标草稿 ID，未知时 NULL
    status        TEXT        NOT NULL,           -- pending / created / unknown / reconciled / failed
    attempt_count INTEGER     NOT NULL DEFAULT 0,
    first_sent_at TIMESTAMPTZ,
    reconciled_at TIMESTAMPTZ
);

CREATE TABLE shortage_snapshot (
    snapshot_id   UUID PRIMARY KEY,
    run_id        UUID        NOT NULL REFERENCES run(run_id),
    component_id  TEXT        NOT NULL REFERENCES component(component_id),
    required_qty  NUMERIC(18,6) NOT NULL,
    allocatable_qty NUMERIC(18,6) NOT NULL,
    shortage_qty  NUMERIC(18,6) NOT NULL,
    breakdown     JSONB       NOT NULL,           -- 实物/占用/在途各项的抵扣明细
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE metrics_snapshot (
    snapshot_id  UUID PRIMARY KEY,
    tenant_id    TEXT        NOT NULL DEFAULT 'default',
    metric_name  TEXT        NOT NULL,            -- 库存周转率 / 缺货率 / 物流异常
    metric_value NUMERIC(18,6) NOT NULL,
    threshold    NUMERIC(18,6),
    breached     BOOLEAN     NOT NULL,
    rule_version INTEGER,                         -- 取自 business_rule
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE operator_log (
    log_id      UUID PRIMARY KEY,
    tenant_id   TEXT        NOT NULL DEFAULT 'default',
    operator    TEXT        NOT NULL,
    action      TEXT        NOT NULL,             -- approve / reject / request_changes / update_threshold
    target_ref  JSONB       NOT NULL,
    before_value JSONB,
    after_value JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 只插入的强制方式

审计表不靠约定，靠权限强制。应用角色只授 `INSERT` 与 `SELECT`：

```sql
CREATE ROLE supplyagent_app;
GRANT SELECT, INSERT ON run_state_event, tool_call, llm_call, permission_decision,
      shortage_snapshot, metrics_snapshot, operator_log TO supplyagent_app;
-- 显式不授 UPDATE / DELETE。迁移由独立的 owner 角色执行。
REVOKE UPDATE, DELETE ON run_state_event, tool_call, llm_call, permission_decision,
      shortage_snapshot, metrics_snapshot, operator_log FROM supplyagent_app;
```

`run` 与 `external_action` 是例外，它们持有当前状态需要更新；**状态变化的历史由 `run_state_event` 追加保存**，所以即便 `run.state` 被改写也不丢审计链。

## 7. ⑤ 规则层

```sql
CREATE TABLE business_rule (
    rule_id     TEXT        NOT NULL,             -- 如 inventory.alert_threshold
    version     INTEGER     NOT NULL,
    tenant_id   TEXT        NOT NULL DEFAULT 'default',
    value       JSONB       NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL,
    changed_by  TEXT        NOT NULL,
    change_note TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, rule_id, version)
);

CREATE INDEX ix_business_rule_current
    ON business_rule (tenant_id, rule_id, effective_from DESC);
```

版本只增不改：改阈值是插入新版本，不是 `UPDATE`。读取时取 `effective_from <= now()` 的最大版本。每次变更必须同时写一条 `operator_log`（谁改的、改前改后值、生效时间）。

**本表存的是可变业务参数**（库存告警线、审批期限、提醒频率、D13 的扇出升级阈值）；Agent 的 system prompt／模型／工具列表走 YAML + Git，两者不混放（见 `DECISIONS.md` D07）。

## 8. 未决与已知缺口

| 项 | 状态 |
|---|---|
| 合成时间序列数据的表结构（库存/出入库/在途事件的时间维） | 未定。`inventory` 当前是快照而非事件流，Monitor 计算库存周转率需要事件级历史，schema 待 T04 设计 |
| `component` 的技术规格字段（参数、分类、规格书链接） | 不建模。D12 移除 RAG 后，技术信息在运行时由供应查询工具返回，不预先建库 |
| 目标业务系统（ERP）侧的表 | 不在本项目库内。Agent 自有 PostgreSQL 与 ERP 是两个独立系统 |
| `identity_status` 的 `verified` 取值 | CHECK 已允许，但核验流程未建（T12），当前数据全部为 `source_asserted` |
| 税费与运费 | 有意不建模，见 §5 |

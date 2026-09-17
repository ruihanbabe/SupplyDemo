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

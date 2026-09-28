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
- **多租户**：业务表一律带 `tenant_id TEXT NOT NULL DEFAULT 'default'`，**仅作架构预留**，MVP 不实现隔离逻辑（见 `docs/product/requirements.md` §8；这是已决事项，不在 `docs/OPEN-QUESTIONS.md` 之列）。审计表同样带，便于将来按租户裁剪。
- **模拟数据标注**：任何合成数据的表带 `is_simulated BOOLEAN NOT NULL DEFAULT FALSE`。最终回答必须能区分模拟与真实来源（见 `docs/product/requirements.md` §5 BR-09）。
- **证据引用**：`evidence_ref` 统一为 `JSONB`，形如 `{"kind": "tool_call", "id": "...", "retrieved_at": "..."}`。每个实质性判断都要能回指。
- **命名**：表名单数、蛇形；外键列名为 `<引用表>_id`；索引 `ix_<表>_<列>`，唯一索引 `uq_<表>_<列>`。

## 2. 分层与依赖方向

```text
① 权威静态层（元件身份与 BOM）      ← 由 normalized/ 单向灌入，不接受应用写入
② 业务层（需求/库存/在途）           ← 应用读写
③ 方案与审批（plan/permission）      ← 版本化，审批绑定 content_hash
④ 审计层（run/tool_call/...）        ← 只插入，永不更新或删除
⑤ 规则层（business_rule）            ← 版本化，变更留痕
⑥ 证据层（evidence/official_document）← 只插入，取代用 superseded_by 回填
⑦ 预警层（monitor_target/alert）      ← alert 持当前状态，历史在 alert_event
⑧ 运行时层（artifact/context_bundle/node_checkpoint）← 产物与上下文只插入，checkpoint 持当前尝试状态
⑨ 语义层（metric_definition）         ← 业务术语的冻结口径，版本化
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
    required_qty  NUMERIC(18,6),                 -- NULL = 数量口径未核验，尚未计算
    unresolved_reason TEXT,                       -- 无 MPN / 候选未选定 / 数量口径未核验
    PRIMARY KEY (demand_id, line_id),
    CONSTRAINT ck_demand_line_resolved CHECK (
        (component_id IS NOT NULL AND unresolved_reason IS NULL
         AND required_qty IS NOT NULL AND required_qty > 0)
        OR (component_id IS NULL AND unresolved_reason IS NOT NULL
            AND (required_qty IS NULL OR required_qty > 0))
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
    worker        TEXT        NOT NULL,           -- supervisor / spec_check / evidence_check / proposal
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

**本表存的是可变业务参数**（证据有效期、审批期限、候选选择策略等）；模型、工具和工作流配置走版本化运行配置，两者不混放（见 `DECISIONS.md` D18）。

## 8. ⑥ 证据层（F12 / F17）

> **本层分两期落地。**F12 迁移 `evidence` 与 `evidence_field_locator`（字段型观察，`sourcing` 产出）。`evidence_document_locator`、`official_document`、`document_resolution` 三张表随 **F17** 一并迁移——它们服务于厂商资料的寻址、取回与缓存，而那正是 F17 的职责；先建会让外键指向不存在的表。

Evidence 是全系统的事实单位，契约见 `docs/spec/interfaces.md`「Evidence」。本节只定义表结构。

只插入语义，唯一例外是 `superseded_by` 的窄列回填。取代一条观察是**插入新行并回填旧行**，不是覆盖：审批当时知道什么，事后必须仍能回答。

```sql
CREATE TABLE evidence (
    evidence_id     UUID PRIMARY KEY,
    run_id          UUID        NOT NULL REFERENCES run(run_id),
    tenant_id       TEXT        NOT NULL DEFAULT 'default',
    kind            TEXT        NOT NULL,          -- stock / price / lead_time / lifecycle / replacement / parameter
    subject_ref     TEXT        NOT NULL,          -- component_id 或 mpn
    attribute       TEXT        NOT NULL,
    value_raw       TEXT        NOT NULL,          -- 来源原始形态，不归一化
    value_normalized TEXT,                         -- 归一化失败必须留 NULL，不得猜测
    locator_kind    TEXT        NOT NULL,          -- field | document
    match_mode      TEXT,                          -- 仅 document：enumerated | decoded
    retrieved_at    TIMESTAMPTZ NOT NULL,          -- 新鲜度唯一依据；缓存命中保留原值不刷新
    observed_at     TIMESTAMPTZ,                   -- 仅来源显式声明快照时间时填写
    provenance      TEXT        NOT NULL,          -- real / cache / sample / replay
    tool_call_id    UUID        REFERENCES tool_call(tool_call_id),
    content_hash    TEXT        NOT NULL,
    superseded_by   UUID        REFERENCES evidence(evidence_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_evidence_locator CHECK (locator_kind IN ('field','document')),
    CONSTRAINT ck_evidence_match_mode CHECK (
        (locator_kind = 'document' AND match_mode IN ('enumerated','decoded'))
        OR (locator_kind = 'field' AND match_mode IS NULL)),
    CONSTRAINT ck_evidence_provenance CHECK (provenance IN ('real','cache','sample','replay')),
    CONSTRAINT ck_evidence_not_self_superseded CHECK (
        superseded_by IS NULL OR superseded_by <> evidence_id)
);

-- 定位块与 evidence 一对一，按 locator_kind 二选一，不合表以免半数列恒为 NULL。
CREATE TABLE evidence_field_locator (
    evidence_id     UUID PRIMARY KEY REFERENCES evidence(evidence_id),
    source_name     TEXT        NOT NULL,          -- digikey / mouser / ...
    request_digest  TEXT        NOT NULL,          -- 可复现该次查询的参数摘要
    field_path      TEXT        NOT NULL,          -- 响应中的字段路径
    response_status TEXT        NOT NULL           -- ok / partial / not_found
);

CREATE TABLE evidence_document_locator (
    evidence_id       UUID PRIMARY KEY REFERENCES evidence(evidence_id),
    document_id       UUID     NOT NULL REFERENCES official_document(document_id),
    page              INTEGER  NOT NULL,
    span_start        INTEGER  NOT NULL,
    span_end          INTEGER  NOT NULL,
    quoted_text       TEXT     NOT NULL,
    extractor_name    TEXT     NOT NULL,
    extractor_version TEXT     NOT NULL,           -- 必填：抽取器升级会整体位移字符偏移
    CONSTRAINT ck_span_order CHECK (span_end > span_start)
);

-- 原始文件按内容寻址存本地文件系统，DB 只存路径与哈希。
-- 130MB 级的 PDF 不进 PostgreSQL；重启后必须仍在，否则违反「已采集不重复采集」。
CREATE TABLE official_document (
    document_id       UUID PRIMARY KEY,
    manufacturer      TEXT        NOT NULL,
    document_kind     TEXT        NOT NULL,        -- datasheet / product_page / pcn
    source_url        TEXT        NOT NULL,
    doc_revision      TEXT,                        -- 如 DocID2572 Rev 38；无法识别时 NULL
    coverage_kind     TEXT,                        -- single / enumerated / parametric；首次探测后缓存
    decoder_page      INTEGER,                     -- parametric 时编码器表所在页
    content_sha256    TEXT        NOT NULL,        -- 文件内容寻址键
    blob_path         TEXT        NOT NULL,        -- 原始字节落点
    text_path         TEXT,                        -- 抽取文本落点；无文字层时 NULL
    extractor_name    TEXT,
    extractor_version TEXT,
    page_count        INTEGER,
    retrieved_at      TIMESTAMPTZ NOT NULL,
    provenance        TEXT        NOT NULL,
    superseded_by     UUID        REFERENCES official_document(document_id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_content UNIQUE (content_sha256),
    CONSTRAINT ck_document_provenance CHECK (provenance IN ('real','cache','sample','replay')),
    CONSTRAINT ck_document_coverage CHECK (
        coverage_kind IS NULL OR coverage_kind IN ('single','enumerated','parametric'))
);

-- 型号到文档的映射是多对一：12 个 RC0402FR-* 指向同一份 Yageo 系列文档。
-- 并发采集按 content_sha256 单次取回（single-flight），不重复抓取。
CREATE TABLE document_resolution (
    resolution_id   UUID PRIMARY KEY,
    mpn             TEXT        NOT NULL,
    manufacturer    TEXT        NOT NULL,
    document_kind   TEXT        NOT NULL,
    status          TEXT        NOT NULL,          -- resolved / ambiguous / unresolvable
    document_id     UUID        REFERENCES official_document(document_id),
    candidates      JSONB,                         -- ambiguous 时列出全部命中，不择一
    reason          TEXT,                          -- ambiguous / unresolvable 必填
    strategy_used   TEXT,                          -- 命中的策略；未命中时记已尝试的全部
    resolved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_resolution_status CHECK (status IN ('resolved','ambiguous','unresolvable')),
    CONSTRAINT ck_resolution_resolved CHECK (
        (status = 'resolved' AND document_id IS NOT NULL)
        OR (status <> 'resolved' AND reason IS NOT NULL))
);

CREATE INDEX ix_evidence_subject ON evidence(subject_ref, retrieved_at DESC);
CREATE INDEX ix_resolution_mpn ON document_resolution(mpn, manufacturer);
```

### 三层存储分工

| 层 | 落点 | 理由 |
|---|---|---|
| 原始文件字节 | 本地文件系统，`sha256` 作键 | 单份 1.6MB 量级；进 DB 会拖垮备份与恢复 |
| 抽取文本 | 本地文件系统 | span 偏移必须相对一个确定的文本版本 |
| Evidence 与定位 | PostgreSQL | 需要事务、外键与审计权限 |

**不得改为进程内缓存。**重启即清空会使「已成功采集的观察不重复采集」失效（`docs/product/requirements.md` §9），并让 F20 的冻结证据缓存无从建立。

### 修订检查而非重复下载

复核时先查文档修订（产品页或 HEAD），与 `doc_revision` 一致则复用缓存；不一致才重新取回，并回填旧行的 `superseded_by`。一次修订变更会使指向该文档的全部 Evidence 同时失效——12:1 扇入下这是 12 条。

## 9. ⑦ 预警层（F18）

> **待重塑。**2026-09-22 产品形态重定后，预警从主线降为支流，告警改以 `RiskEvent` 表达（`DECISIONS.md` D10、`docs/spec/interfaces.md`「RiskEvent」）。本节仍是旧的 `monitor_target` / `alert` / `alert_event` 形态，**尚未迁移**，重塑随 F18 实现一并进行。在此之前不要按本节建表。下方内容保留的是仍然成立的那部分约束：去重靠数据库部分唯一索引而非应用自觉、判定时的规则版本必须落库、三个时刻互不替代。

`alert` 的部分唯一索引是 BR-15「同一对象同一类型同时最多一条活动告警」的机械化落点——**靠数据库强制，不靠应用代码自觉**。

```sql
CREATE TABLE monitor_target (
    target_id      UUID PRIMARY KEY,
    tenant_id      TEXT        NOT NULL DEFAULT 'default',
    component_id   TEXT        REFERENCES component(component_id),
    line_id        TEXT        REFERENCES bom_line(line_id),
    policy_ref     TEXT        NOT NULL,          -- business_rule.rule_id，阈值与复核策略
    enabled        BOOLEAN     NOT NULL DEFAULT TRUE,
    last_checked_at TIMESTAMPTZ,                  -- 上次成功复核时刻；采集失败不更新
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_target_subject CHECK (
        (component_id IS NOT NULL) OR (line_id IS NOT NULL))
);

CREATE TABLE alert (
    alert_id          UUID PRIMARY KEY,
    tenant_id         TEXT        NOT NULL DEFAULT 'default',
    target_id         UUID        NOT NULL REFERENCES monitor_target(target_id),
    alert_type        TEXT        NOT NULL,       -- stock_low / eol / lifecycle_change
    severity          TEXT        NOT NULL,
    status            TEXT        NOT NULL,       -- active / closed
    policy_version    TEXT        NOT NULL,       -- 判定时采用的阈值版本；缺它则告警不可复现
    trigger_evidence  JSONB       NOT NULL,       -- 触发本告警的 evidence_id 列表，非空
    first_seen_at     TIMESTAMPTZ NOT NULL,       -- 首次发现；重复触发不改写
    last_confirmed_at TIMESTAMPTZ NOT NULL,       -- 最近一次证据仍成立；采集失败不更新
    closed_at         TIMESTAMPTZ,
    closed_reason     TEXT,                       -- 状态回退的依据；不得因采集失败而关闭
    CONSTRAINT ck_alert_status CHECK (status IN ('active','closed')),
    CONSTRAINT ck_alert_closed CHECK (
        (status = 'closed' AND closed_at IS NOT NULL AND closed_reason IS NOT NULL)
        OR (status = 'active' AND closed_at IS NULL))
);

-- BR-15 的强制点：同一 (target, type) 至多一条 active。
-- 部分唯一索引让重复告警在数据库层就写不进去，而不是靠应用先查再插。
CREATE UNIQUE INDEX uq_alert_active
    ON alert(target_id, alert_type) WHERE status = 'active';

-- 告警的每次变化追加一行，只插入。alert 行持有当前状态，历史在此。
CREATE TABLE alert_event (
    event_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alert_id      UUID        NOT NULL REFERENCES alert(alert_id),
    run_id        UUID        NOT NULL REFERENCES run(run_id),
    kind          TEXT        NOT NULL,           -- opened / reconfirmed / escalated / closed
    evidence_ref  JSONB       NOT NULL,
    policy_version TEXT       NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_alert_event_kind CHECK (
        kind IN ('opened','reconfirmed','escalated','closed'))
);

-- 评测运行的身份。run.eval_run_id 指向这里，使评测轨迹与生产轨迹可分离。
CREATE TABLE eval_run (
    eval_run_id    UUID PRIMARY KEY,
    fixture_set    TEXT        NOT NULL,          -- 冻结证据缓存的标识
    backend        TEXT        NOT NULL,
    policy_version TEXT        NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ
);

CREATE INDEX ix_alert_active ON alert(status, severity, last_confirmed_at DESC);
CREATE INDEX ix_alert_event_alert ON alert_event(alert_id, event_id);
```

### 对既有审计表的追加

`run` 与 `llm_call` 属于 `0001` 已冻结的审计层，加列走 ALTER，**不改写 §6 的冻结 DDL**（同 F03 在 `0002` 的先例）：

```sql
ALTER TABLE llm_call ADD COLUMN bundle_id UUID REFERENCES context_bundle(bundle_id);
ALTER TABLE llm_call ADD COLUMN backend   TEXT;
ALTER TABLE run      ADD COLUMN eval_run_id UUID REFERENCES eval_run(eval_run_id);
```

- `llm_call.bundle_id`：回答「这次调用模型看到了什么」。`docs/product/requirements.md` §2（T07） 已承诺回答此问题，缺它则无法回答。
- `llm_call.backend`：与 `model` 分开——同一模型可经不同后端，跨后端比较要按后端而非模型分组。
- `run.eval_run_id`：非空即该 Run 属于某次评测，使评测轨迹与生产轨迹可分离。

三者都属于**写入时不记就补不回来**的字段，与 trace 导出格式无关；格式可随时从关系型数据导出，因此不在本期冻结。

### 为什么 `policy_version` 不能省

不记录判定时采用的阈值版本，就永远无法回答「按新阈值这条告警还会不会触发」——而这正是给告警系统做回归测试的核心动作（F20）。它属于**写入时不记就补不回来**的一类，与格式无关。

### 三个时刻的分工

`first_seen_at` 重复触发时不改写，`last_confirmed_at` 每次证据仍成立时更新，`closed_at` 只由状态回退写入。**采集失败只影响 `last_confirmed_at` 的停滞，既不产生告警也不关闭告警**（FR-11、EV-48）——失败意味着不知道，不意味着恢复正常。

## 10. ⑧ 运行时层（F14/F15）

Harness 与 Runtime 的三张表：一次模型调用的上下文投影、节点的版本化产出、节点尝试的检查点。契约见 `docs/spec/interfaces.md`「ContextBundle」「Artifact」与 `docs/spec/state-machine.md` §4，本节只定义表结构。

`context_bundle` 与 `artifact` 是只插入语义，`artifact` 的唯一例外是 `superseded_by` 的窄列回填（同 ⑥ 层）。`node_checkpoint` 持当前尝试的状态，需要窄列更新（同 `run` 与 `external_action`）；历史由 `attempt` 序列与 `run_state_event` 承担。角色授权随本层迁移一并定义。

**迁移顺序**：⑦ 层「对既有审计表的追加」中的 `llm_call.bundle_id` 引用本层的 `context_bundle`，本层必须先于该 ALTER 建出。

```sql
-- 一次模型调用实际看到了什么。Context 是权威状态的可重建投影，不是聊天记录。
CREATE TABLE context_bundle (
    bundle_id        UUID PRIMARY KEY,
    run_id           UUID        NOT NULL REFERENCES run(run_id),
    tenant_id        TEXT        NOT NULL DEFAULT 'default',
    schema_version   TEXT        NOT NULL,
    purpose          TEXT        NOT NULL,        -- 本次调用的有界任务
    business_state_refs JSONB    NOT NULL,
    evidence_refs    JSONB       NOT NULL,
    artifact_refs    JSONB       NOT NULL,
    human_input_refs JSONB       NOT NULL,
    instructions     TEXT        NOT NULL,        -- 已渲染的指令；凭据与模型隐藏思维不得进入
    manifest         JSONB       NOT NULL,        -- 见下方两个 CHECK
    content_hash     TEXT        NOT NULL,        -- 重复编译的等价性判据（F14）
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_bundle_manifest_keys CHECK (jsonb_path_exists(manifest,
        '$ ? (exists(@.compiler_version) && exists(@.token_estimate) && exists(@.included)
              && exists(@.excluded) && exists(@.summaries))')),
    -- excluded 的五个取值不得合并：expired 要求重查或转人工，budget 只是本次上下文没放下。
    CONSTRAINT ck_bundle_excluded_reason CHECK (NOT jsonb_path_exists(manifest,
        '$.excluded[*].reason ? (@ != "expired" && @ != "budget" && @ != "permission"
              && @ != "superseded" && @ != "irrelevant")'))
);

-- 节点产出的版本化结果。不覆盖旧版本：新版本插入新行并回填旧行的 superseded_by。
CREATE TABLE artifact (
    artifact_id    UUID PRIMARY KEY,
    run_id         UUID        NOT NULL REFERENCES run(run_id),
    tenant_id      TEXT        NOT NULL DEFAULT 'default',
    kind           TEXT        NOT NULL,          -- risk_finding_set / investigation_summary / proposal_explanation
    version        INTEGER     NOT NULL,          -- 同 (run_id, kind) 内递增
    schema_version TEXT        NOT NULL,
    content        JSONB       NOT NULL,
    content_hash   TEXT        NOT NULL,
    input_refs     JSONB       NOT NULL,          -- evidence_id / snapshot_id / artifact_id；回指的起点
    producer       TEXT        NOT NULL,          -- 产出该产物的 worker 或确定性节点
    bundle_id      UUID        REFERENCES context_bundle(bundle_id),  -- 模型产物指向其上下文；确定性产物为 NULL
    llm_call_id    UUID        REFERENCES llm_call(llm_call_id),      -- 使重试关系按调用尝试可辨（EV-37）
    -- 取代在同一事务内完成：先回填旧行，再插入新行；自引用外键因此必须可延迟。
    superseded_by  UUID        REFERENCES artifact(artifact_id) DEFERRABLE INITIALLY DEFERRED,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_artifact_version UNIQUE (run_id, kind, version),
    CONSTRAINT ck_artifact_version CHECK (version > 0),
    CONSTRAINT ck_artifact_not_self_superseded CHECK (
        superseded_by IS NULL OR superseded_by <> artifact_id)
);

-- EV-37 的强制点：同一 (run, kind) 同时只能有一个未被取代的版本。
CREATE UNIQUE INDEX uq_artifact_current ON artifact(run_id, kind) WHERE superseded_by IS NULL;

-- 节点尝试的检查点。恢复只读本表与 run_state_event，不读模型聊天记录。
CREATE TABLE node_checkpoint (
    checkpoint_id    UUID PRIMARY KEY,
    run_id           UUID        NOT NULL REFERENCES run(run_id),
    node_id          TEXT        NOT NULL,
    node_kind        TEXT        NOT NULL,        -- deterministic / tool / model / human
    attempt          INTEGER     NOT NULL,        -- 同 (run_id, node_id) 内递增
    workflow_version TEXT        NOT NULL,        -- 节点选择所依据的 workflow 定义版本
    input_refs       JSONB       NOT NULL,
    status           TEXT        NOT NULL,        -- running / committed / failed
    result_ref       JSONB,                       -- artifact_id / evidence_id / tool_call_id
    error            JSONB,                       -- 错误分类与原因
    next_node        TEXT,
    side_effect      TEXT        NOT NULL DEFAULT 'none',  -- none / pending / confirmed / unknown
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    CONSTRAINT uq_node_attempt UNIQUE (run_id, node_id, attempt),
    CONSTRAINT ck_checkpoint_attempt CHECK (attempt > 0),
    CONSTRAINT ck_checkpoint_kind CHECK (node_kind IN ('deterministic','tool','model','human')),
    CONSTRAINT ck_checkpoint_status CHECK (status IN ('running','committed','failed')),
    CONSTRAINT ck_checkpoint_finished CHECK (
        (status = 'running' AND finished_at IS NULL)
        OR (status <> 'running' AND finished_at IS NOT NULL)),
    CONSTRAINT ck_checkpoint_committed_result CHECK (status <> 'committed' OR result_ref IS NOT NULL),
    CONSTRAINT ck_checkpoint_failed_error CHECK (status <> 'failed' OR error IS NOT NULL),
    CONSTRAINT ck_checkpoint_side_effect CHECK (
        side_effect IN ('none','pending','confirmed','unknown')),
    -- 只有工具节点会产生外部副作用；其他节点恒为 none，恢复时可安全重算。
    CONSTRAINT ck_checkpoint_side_effect_kind CHECK (node_kind = 'tool' OR side_effect = 'none')
);

CREATE INDEX ix_context_bundle_run ON context_bundle(run_id, created_at DESC);
CREATE INDEX ix_artifact_run ON artifact(run_id, kind, version DESC);
CREATE INDEX ix_checkpoint_run ON node_checkpoint(run_id, started_at DESC);
```

### 上下文的两条 CHECK 不是装饰

`manifest` 少一个键，`/api/runs/{run_id}/context/{bundle_id}` 就答不出"这次调用看到了什么"，而这是 `docs/product/requirements.md` §2（T07） 已承诺回答的问题。`excluded` 的五个取值同理：`expired`（证据超出新鲜度窗口）要求重查或转人工，`budget`（预算不足被裁剪）只是本次上下文没放下——合并成一个"被裁掉了"，恢复时就无法判断该重查还是该继续。取值表在 `interfaces.md`「ContextBundle」，此处只做机械化强制。

### 取代必须在同一事务内先回填后插入

`uq_artifact_current` 是 EV-37 的强制点：模型已返回、产物未落库时崩溃，重试会产生第二份内容，部分唯一索引让它必须先回填旧行的 `superseded_by` 才写得进去，重试关系留在 `llm_call_id` 上。手法与 `uq_alert_active` 相同。

由此产生一个顺序约束：回填在前，新行尚不存在，所以自引用外键声明为 `DEFERRABLE INITIALLY DEFERRED`，在 COMMIT 时才校验。延迟不等于放弃——指向不存在行的取代仍会在 COMMIT 失败。

### `side_effect` 不能由 `status` 推断

`failed` 不区分"请求没发出去"和"发出去了但结果未知"。恢复时前者可以按相同输入重算，后者只能核对（`state-machine.md` §4、`ARCHITECTURE.md` 不变量 6）。这属于**写入时不记就补不回来**的字段，与 `alert.policy_version` 同类。

### `content_hash` 承担"可重建"的判据

F14 要求同一 Run 状态重复编译得到等价 bundle。没有内容哈希就只能逐字段比对 JSONB，摘要或裁剪策略一变就误报差异，等价性无从断言。

`workflow_version` 同理：节点选择依据的 workflow 定义版本必须与 checkpoint 同行保存，否则定义演进后无法判断旧 Run 还能不能按原路恢复（`state-machine.md` §3「created → analyzing」要求工作流定义版本）。定义采用何种格式仍见 `ARCHITECTURE.md` A-TBD-01，本列只存版本标识，不预设格式。

## 11. ⑨ 语义层（F21）

业务术语不靠模型理解，靠冻结的注册表选择。Intake agent 做的是**枚举分类**（选哪个指标 + 填参数），不是生成 SQL。

```sql
CREATE TABLE metric_definition (
    metric_id     TEXT        NOT NULL,           -- 如 allocatable_qty
    version       INTEGER     NOT NULL,
    display_name  TEXT        NOT NULL,           -- 可分配量
    aliases       JSONB       NOT NULL,           -- ["可用量","可分配库存"]；别名不跨指标复用
    expression    TEXT        NOT NULL,           -- on_hand - quarantine - allocated_to_other_demand
    unit          TEXT        NOT NULL,
    applies_to    TEXT        NOT NULL,           -- component / line / demand
    caveat        TEXT,                           -- 口径陷阱说明，进 prompt
    effective_from TIMESTAMPTZ NOT NULL,
    created_by    TEXT        NOT NULL,
    PRIMARY KEY (metric_id, version)
);
```

`MOQ` / `MPQ` / `SPQ` 必须是三条独立记录，别名互不通用——这是 LLM 最常混淆的一组，也是 `caveat` 字段存在的理由。

指标未命中注册表时返回 `unknown_metric` 并转人工，**不得由模型即兴定义口径**。

## 12. 未决与已知缺口

| 项 | 状态 |
|---|---|
| ⑥ 证据层分两期 | F12 已迁移字段型两张表；文档型三张表随 F17 迁移。迁移表数 22 → 24 |
| ⑩ 硬件设计原始层与料号表 | 迁移 0004 建原始层 4 张表（§13），0005 建料号表 1 张（§14）。契约表数 35 → 40，迁移表数 24 → 29 |
| ⑦ 预警层待重塑 | 形态重定后告警改以 `RiskEvent` 表达，本文 §9 仍是旧形态且未迁移；重塑随 F18 进行，届时表数会变 |
| 契约 35 表 vs 迁移 22 表 | 有意分期：`0001` 冻结 ①～⑤ 层 22 表；⑥ 证据层（F12）、⑦ 预警层（F18）、⑧ 运行时层（F14/F15）、⑨ 语义层（F21）已写契约、迁移待建。`tests/test_schema.py` 对两个数字分别断言，任何一侧漂移都会失败 |
| `llm_call.worker` 与新 Worker 名册 | 注释与 `ARCHITECTURE.md` Worker 名册一致：`supervisor / spec_check / evidence_check / proposal`；`internal`、`sourcing`、`action` 是确定性服务，不产生 llm_call |
| `llm_call.worker` 与 `metrics_snapshot` | 遗留兼容结构。前者暂写 node_id，后者当前不读写；删除或改名必须通过迁移并同步测试 |
| `component` 的技术规格字段（参数、分类、规格书链接） | 不预建通用知识库；当前任务需要时由工具产生 Evidence |
| 目标业务系统（ERP）侧的表 | 不在本项目库内。Agent 自有 PostgreSQL 与 ERP 是两个独立系统 |
| `identity_status` 的 `verified` 取值 | CHECK 已允许，但核验流程未建（T12），当前数据全部为 `source_asserted` |
| 税费与运费 | 有意不建模，见 §5 |

## 13. ⑩ 硬件设计原始层（知识库，迁移 0004）

**原样保留，不做统一字段。**各项目自己的属性名（`MPN` / `Part Number` / `PartNumber` …）原封不动存在 `properties` 里；拆分只沿 KiCad 文件自身的结构（文件 → 图纸页 → 符号），不合并多单元、不过滤电源符号、不解释 DNP。统一字段与业务含义在后续加工层按"相近字段 + 业务功能"确定，届时从本层重新派生，本层不改。

`hw_source_file.content` 保存文件全文，是最终依据：`hw_raw_sheet` / `hw_raw_symbol` 只是为了查询方便从它解析出来的，二者不一致时以全文为准。同一项目换提交 = 新增一个快照，旧快照保留，可对比版本差异。应用角色只有 SELECT / INSERT。

```sql
CREATE TABLE hw_source_snapshot (
    snapshot_id      UUID PRIMARY KEY,
    tenant_id        TEXT        NOT NULL DEFAULT 'default',
    project_id       TEXT        NOT NULL,
    repo             TEXT        NOT NULL,
    commit_sha       TEXT        NOT NULL,
    source_path      TEXT        NOT NULL,
    root_schematic   TEXT        NOT NULL,
    license          TEXT        NOT NULL,
    manifest_sha256  TEXT        NOT NULL,
    fetched_at       TIMESTAMPTZ NOT NULL,
    imported_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_hw_source_snapshot_project_commit UNIQUE (project_id, commit_sha)
);

CREATE TABLE hw_source_file (
    snapshot_id      UUID        NOT NULL REFERENCES hw_source_snapshot(snapshot_id),
    path             TEXT        NOT NULL,           -- 仓库内路径
    sha256           TEXT        NOT NULL,
    git_blob         TEXT        NOT NULL,           -- 与上游 git 对象一致的校验
    byte_size        INTEGER     NOT NULL,
    content          TEXT        NOT NULL,           -- 文件全文，逐字
    PRIMARY KEY (snapshot_id, path)
);

CREATE TABLE hw_raw_sheet (
    snapshot_id      UUID        NOT NULL REFERENCES hw_source_snapshot(snapshot_id),
    sheet_path       TEXT        NOT NULL,           -- 实例路径；同一文件被引用两次即两行
    parent_path      TEXT,                           -- 根页为 NULL
    file_path        TEXT        NOT NULL,           -- 相对根原理图目录
    sheet_uuid       TEXT,
    properties       JSONB       NOT NULL,           -- 父页中该图纸块的属性，原样
    PRIMARY KEY (snapshot_id, sheet_path)
);

CREATE TABLE hw_raw_symbol (
    snapshot_id      UUID        NOT NULL REFERENCES hw_source_snapshot(snapshot_id),
    sheet_path       TEXT        NOT NULL,
    symbol_uuid      TEXT        NOT NULL,
    file_path        TEXT        NOT NULL,
    reference        TEXT,                           -- 来自实例表；解析不到为 NULL
    reference_source TEXT        NOT NULL,
    lib_id           TEXT        NOT NULL,
    unit             INTEGER,
    attributes       JSONB       NOT NULL,           -- in_bom / on_board / dnp … 原始取值
    properties       JSONB       NOT NULL,           -- 全部属性，原属性名
    PRIMARY KEY (snapshot_id, sheet_path, symbol_uuid),
    CONSTRAINT ck_hw_raw_symbol_reference_source CHECK (reference_source IN ('instance', 'property'))
);
CREATE INDEX ix_hw_raw_symbol_properties ON hw_raw_symbol USING GIN (properties);
```

## 14. 硬件料号表（迁移 0005）

从 §13 原始层派生的唯一一张加工表：每个项目、每个要采购的料号一行。只收在 BOM 内、有料号、要贴装的元件；替代料说明按设计者原文保存，不解析。表内容可由原始层完整重建，重建时按快照整体替换。

```sql
CREATE TABLE hw_part (
    snapshot_id      UUID        NOT NULL REFERENCES hw_source_snapshot(snapshot_id),
    mpn              TEXT        NOT NULL,
    manufacturer     TEXT,
    description      TEXT,
    quantity         INTEGER     NOT NULL,           -- 单板用量
    alternates       TEXT,                           -- 设计者替代料说明，原文
    PRIMARY KEY (snapshot_id, mpn),
    CONSTRAINT ck_hw_part_quantity CHECK (quantity > 0)
);
```

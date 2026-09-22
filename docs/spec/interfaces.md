# SupplyAgent Harness 与工具接口契约

> **文档契约** · 类型：接口契约 SSOT · 读取：实现相应端口时按名称定点读取
> 更新：Harness 端口、工具 schema、返回信封或错误语义变化时
> 独占：ModelBackend、ContextBundle、工具元数据和 ToolResult 契约
> 不收录：业务规则、表结构、HTTP 路由和供应平台能力

## 1. Harness 核心端口

### ModelBackend

ModelBackend 提供 `complete(ModelRequest) -> ModelResult` 和 `cancel(call_id)`。请求必须包含 call_id、model、context_bundle_id、输出 schema、允许工具、预算和超时；结果统一表示文本、结构化输出、工具请求、完成原因、用量、延迟和后端原始引用。

ModelCapabilities 显式声明结构化输出、工具调用、流式、取消和用量报告能力。能力不存在时返回 `CAPABILITY_UNSUPPORTED`，不得静默删掉约束。

后端按**形态**而非协议划分：云端 API、本地部署（Ollama / vLLM，同为 OpenAI 兼容）、回放（`ReplayBackend`）。

`ModelCapabilities` 必须是**探测结果**而非手写声明：对每个 endpoint 跑固定探针（带 tool 的请求、并行 tool call、`response_format`、流式 usage），把实测值落成该后端的能力矩阵。十个后端都返回同一串 `True`，能力协商就无物可协商（D05、F27）。

### ContextBundle

ContextBundle 至少包含 schema_version、bundle_id、run_id、purpose、business_state_refs、evidence_refs、artifact_refs、human_input_refs、instructions 和 manifest。

manifest 必须记录 compiler_version、created_at、token_estimate、included、excluded 和 summaries，说明内容的来源与版本、为何纳入或排除、是否经过摘要或截断。凭据、无关全量历史和模型隐藏思维不得进入 ContextBundle。

`excluded` 的每一项必须带明确理由，取值为 `expired | budget | permission | superseded | irrelevant`，**不得合并**。`expired`（证据超出新鲜度窗口）与 `budget`（预算不足被裁剪）在采购语义上后果完全不同：前者要求重查或转人工，后者只是本次上下文没放下。`ui-contract.md` 与 `/api/runs/{run_id}/context/{bundle_id}` 按此取值呈现。

### Artifact

Artifact 至少包含 artifact_id、run_id、kind、schema_version、version、content、input_refs、producer、created_at 和 content_hash。Artifact 不覆盖旧版本。

### RiskEvent

核验过程中发现的异常，结构化、异步投递，不阻塞主流程。至少包含：

```text
risk_event_id, run_id
source,            -- rule | model_judgment | collection_failed
severity,
rule_ref,          -- source=rule 时必填：触发的 business_rule 及其版本
evidence_refs,     -- 非空：触发它的证据或核验调用
subject_ref,       -- 关联元件或用料行
detected_at
```

**`source` 必须留在事件上**：阈值判定是确定的，模型判断是有概率的，采集失败意味着「不知道」而不是「没问题」。三者可信度不同，审批时要向人展示差异（D10、D20）。

响应动作由声明式配置决定（matcher → actions），新增响应不改代码。带 `RiskEvent` 的建议抬高审批级别；`RiskEvent` 强制把会话从 `Auto` 档降到 `Ask`。

### NodeResult

Graph 中一个节点的产出，跨节点只传它，不传自然语言段落：

```text
node_id, worker, subject_ref,
status,            -- ok | partial | not_found | error，四态不得合并
content,           -- 类型化结果或其引用
evidence_refs,
usage, duration_ms,
error_code
```

**`not_found` 与 `error` 分开**：「查了没有」与「没查成」对采购决策是两件事。fan-in 处任一非 `ok` 分支不得被当作成功（D24）。

### Evidence（证据）

Evidence 是全系统的事实单位。任何告警、结论、报告文字与外部写入都必须能追溯到至少一条 Evidence（BR-17、`docs/product/requirements.md` §5）。

Evidence 按**定位方式**分两类，共用信封、定位块互斥：

| | 字段型 `field` | 文档型 `document` |
|---|---|---|
| 来源 | 分销商等结构化接口 | 制造商官方资料 |
| 典型内容 | 库存、阶梯价、交期、MOQ、包装、`ProductStatus` | 生命周期阶段、推荐替代、封装、额定值 |
| 定位 | 响应字段路径 + 原始值 | 文档修订 + 页码 + 字符区间 + 原文片段 |
| 校验 | 值须等于该字段路径处的原始值 | 原文片段须在该区间逐字命中 |
| 失效 | 按 `retrieved_at` 与策略版本在**读取时**判定 | 文档出新修订即被 supersede |

分类不以"值是不是数字"判断：`ProductStatus: Active` 是字段型，datasheet 中的 `0.063W` 是文档型。

**共用信封**

```text
evidence_id, run_id, kind, subject_ref, attribute,
value_raw,              -- 来源处的原始形态，字符串，不做归一化
value_normalized,       -- 可空；归一化失败必须留空而非猜测
locator_kind,           -- field | document
retrieved_at,           -- 该值实际取自来源的时刻；新鲜度的唯一依据
provenance,             -- real | cache | sample | replay
observed_at,            -- 可空、罕见：仅当来源显式声明快照时间时填写
content_hash, superseded_by
```

**`retrieved_at` 是唯一承重的时间戳。** 落库时刻由标准审计列 `created_at` 承担，不作为独立概念。

**缓存命中必须保留原始 `retrieved_at` 并标 `provenance=cache`，不得刷新为当前时间。** 这样单个时间戳即可表达"这个值有多旧"，跨缓存层依然成立。

`observed_at` 只在来源显式声明快照时间时填写（如夜间库存快照、`inventory.snapshot_at`）。多数分销商接口不提供，此时**必须为 NULL——不得用 `retrieved_at` 顶替**，那构成"把未知当作已知"，违反 `docs/product/requirements.md` §5。文档型证据的快照时间由 `doc_revision` 承载，不重复填 `observed_at`。

**新鲜度在读取时判定，不在写入时烤死。** Evidence 不存 `expires_at`；是否新鲜由「`retrieved_at` + 策略版本 + 判定时刻」在读取时计算。理由：阈值策略未冻结（Q-05），写入时固化会使策略变更需要回填历史；且 Replay/Eval 必须能以新策略重新评估旧证据（F20）。告警决策必须记录判定时采用的策略版本，否则不可复现。

**定位块（互斥）**

```text
field:     source_name, request_digest, field_path, response_status
document:  document_ref, doc_revision, page, span_start, span_end,
           quoted_text, extractor_name, extractor_version
```

`extractor_version` 是必填项。文本抽取器升级会使字符偏移整体位移，存量 span 将静默失效；不记录版本就无法判定一条旧 Evidence 的 span 是否仍然可信。span 校验时抽取器版本不匹配，该条 Evidence 标记为 `needs_reverify`，不得直接采信，也不得直接丢弃。

### 模型输出的证据隔离（强制）

`ARCHITECTURE.md` 不变量 1 与 BR-17 在本层落成契约，不依赖 prompt 约束：

1. **模型节点的输出 schema 中不得出现权威事实字段。** 数量、金额、缺口、MOQ、订购倍数、阶梯价、库存、交期天数、型号、生命周期枚举一律不作为模型可填写的 number/string 位。模型只能填 `evidence_ref`、`plan_line_id`、枚举标签和自由文本说明。
2. **权威值由 Ledger 回填。** 渲染面向人的说明时，由确定性代码按 `evidence_ref` / `plan_line_id` 取值代入模板位，模型不经手。
3. **输出后校验，按证据类型分派。** 字段型：引用的值须等于该 `field_path` 处的 `value_raw`。文档型：引用的原文片段须在 `[span_start, span_end)` 处逐字命中，且 `extractor_version` 与当前一致。模型自由文本中出现的任何数字，必须能在该次调用 `ContextBundle` 实际纳入的证据中逐字命中。
4. **任一项不成立则整个 ModelTurn 作废**，记 `error.code = MODEL_EVIDENCE_VIOLATION`，不做"纠正后采纳"。
5. **接地范围只限实际纳入的证据。** 因预算被 ContextCompiler 裁掉的证据不构成接地——系统知道不等于模型看过。
6. **本约束不随后端变化。** 任何 ModelBackend 都在同一处校验，`CAPABILITY_UNSUPPORTED` 不能豁免它。

校验器与 schema 生成器是同一份定义，不允许各节点自行手写输出 schema。

## 2. 工具注册元数据

| 字段 | 含义 |
|---|---|
| `name` / `schema_version` | 稳定工具名与版本 |
| `input_schema` / `output_schema` | 严格 JSON Schema |
| `effect` | `read` 或 `write` |
| `permission` | 所需权限与 Human Gate 条件 |
| `idempotency` | none、request_key 或 logical_action_id |
| `timeout` / `retry_policy` | 由 Runtime 执行的预算 |
| `adapter` | Python adapter 或 MCP 绑定 |
| `provenance` | real、cache、sample 或 replay |

内部数据库和业务核通过 Python 端口；外部系统可用专用 Adapter 或 MCP。两种绑定共用同一工具契约。

## 3. ToolResult

ToolResult 包含 schema_version、status、data、evidence_refs、retrieved_at、freshness、provenance、error 和 trace_id。

`status` 取 `ok | partial | not_found | error`。`not_found` 表示上游明确无结果；`error` 表示调用失败，两者不得互换。`partial` 不能被描述为全量结果。`provenance` 取 real、cache、sample 或 replay。

错误对象含 code、message、retryable 和可选 retry_after_seconds：

| code | 处理 |
|---|---|
| `TIMEOUT` | 在 Run 预算内有限重试 |
| `RATE_LIMITED` | 遵守 retry_after_seconds |
| `AUTH_EXPIRED` | 按 Adapter 契约最多刷新一次 |
| `FORBIDDEN` | 不重试，等待权限恢复 |
| `INVALID_INPUT` | 拒绝调用 |
| `CAPABILITY_UNSUPPORTED` | 显式失败或已声明降级 |
| `STALE_EVIDENCE` | 重查或等待人工 |
| `NOT_APPROVED` / `PLAN_CHANGED` | 返回 Human Gate |
| `WRITE_RESULT_UNKNOWN` | 进入核对，禁止直接重写 |
| `BUDGET_EXHAUSTED` | 持久化暂停或失败原因 |

## 4. MVP 工具

### `get_material_availability` · read

输入 component_id、need_by_date 和可选 tenant_id。返回库存、隔离、占用、确定和不确定在途明细及 snapshot_at。工具不计算缺口。

### `search_supplier_parts` · read

输入 provider、完整 MPN、可选 manufacturer_hint、region、currency 和分页。返回 SKU、厂商、完整 MPN、包装与 `match_status = exact | ambiguous | suffix_differs`。非 exact 不自动采纳。

### `get_supplier_offer` · read

输入 provider、已核验 distributor_sku、以十进制字符串表示的 quantity、region、currency 和 packaging。返回阶梯价、MOQ、order_multiple、库存、交期、包装与币种。未知数量为 null，不得使用 0。

### `resolve_official_document` · read

输入 mpn、manufacturer、可选 document_kind（`datasheet | product_page | pcn`）。返回 `DocumentResolution`，见 §4.1。本工具**只寻址不取文档**，两步分开是为了让寻址失败与取回失败成为可区分的结果。

### `fetch_official_document` · read

输入 §4.1 返回的 `document_ref`。按内容寻址取回并缓存；命中缓存时保留原始 `retrieved_at` 并标 `provenance=cache`。返回文档字节引用、抽取文本引用、`doc_revision`、`extractor_name` 与 `extractor_version`。无文字层返回 `status=partial` 且 `error.code=CANNOT_EXTRACT`，不做 OCR。

### `extract_document_evidence` · read

输入 document_ref、mpn、attributes。在文档中定位该型号并返回文档型 Evidence，含 page、span 与 quoted_text。型号不在文档中返回 `status=not_found`——与取回失败、寻址失败是三个不同结果，不得合并。

### `create_procurement_draft` · write · deferred

只接受 plan_id、plan_version 和 logical_action_id。服务端读取方案内容并验证 PermissionDecision 与 content_hash；不接受 `approved=true` 或模型传入的数量、价格、供应商。目标系统为 `TBD`。

### `reconcile_procurement_draft` · read · deferred

按 logical_action_id 返回 `found | not_found | unknown`。unknown 进入人工等待，不能当作 not_found 重试。

## 4.1 DocumentResolver 策略链

从型号定位到制造商官方文档。**寻址而非相似度检索**——`docs/product/requirements.md` §8。

### 三态输出

```text
DocumentResolution {
  status: resolved | ambiguous | unresolvable
  document_ref?   -- resolved 时必填
  candidates[]    -- ambiguous 时列出全部命中，不排序、不择一
  reason          -- ambiguous / unresolvable 时必填，说明在哪一步放弃
  strategy_used   -- 命中的策略；unresolvable 时记录已尝试的全部策略
}
```

`ambiguous` **必须转人工，不得自动取第一条**。站内搜索与外部索引的返回是不可信内容（`ARCHITECTURE.md` 不变量 8），"取第一条"是隐式信任，违反 BR-17。

`unresolvable` 是正常结论，不是错误。T3 分层的 18 个元件（分销商自有品牌与占位型号）**本就不存在官方资料**，其正确输出即 `unresolvable`（EV-44）。

### 策略链

按成本升序，任一命中即停；全部未命中则 `unresolvable`。

| 序 | 策略 | 适用 | 成本 | 失败模式 |
|---|---|---|---|---|
| 1 | 本地文档缓存（按型号→文档映射） | 全部 | 极低 | 首次必然未命中 |
| 2 | 厂商 URL 模板 | T1 为主 | 低 | 基础型号推导失败；分类段不可从型号推导 |
| 3 | 分销商接口返回的 datasheet 链接 | T1/T2/T4 | 中 | 依赖 Q-01；链接可能指向第三方镜像，须校验域名 |
| 4 | 厂商站内搜索（skill） | T2/T4 | 高 | 多命中→`ambiguous`；站点改版；需 JS |

**消歧规则（策略 3、4 强制）**：仅当恰好一条结果满足「标题或链接包含完整基础型号」且「域名在该厂商白名单内」时才接受；命中 0 条→继续下一策略，命中 ≥2 条→`ambiguous` 转人工。不按相似度排序择优。

**放弃条件**：策略 4 未命中即 `unresolvable`，不再外扩到通用搜索引擎或第三方镜像站。

分层与实测依据见 `docs/product/requirements.md` §9，此处不复制。

### 按厂商收口的 skill

站内搜索以 skill 形式实现，**每个厂商一个 skill，绑定该厂商域名白名单，流程固定**。

这不是可选的组织方式，而是使 `docs/product/requirements.md` §8「任意网页浏览」这条非范围成立的唯一方式：不收口到白名单域名与固定流程，"能搜索厂商站点"与"能浏览网页"没有区别。

skill 必须声明：`manufacturer`、`allowed_domains`、检索入口、结果解析规则、消歧规则、速率上限。skill 不得跳出 `allowed_domains`，不得跟随跨域重定向（实测 Yageo 出现 301 跨域后 404，正确结果是 `unresolvable` 而非跟随）。

### 触发时机

调研类 skill **按需触发，且不得阻塞告警**：

1. 告警先依据已有证据发出；
2. 再向用户提供深入调研的选项；
3. 用户确认后才执行高成本的站内检索与文档抽取。

调研报告对延迟不敏感，这正是策略 4 可用的前提——用延迟容忍度换取更抗站点改版的寻址方式。

两个触发点：多候选行可提问是否生成对比报告；停产告警的证据中含官方推荐替代时，可提问是否深入调研该替代件。后者在告警之后，因为"存在推荐替代"本身来自已取回的官方文档。

## 4.2 文档取信息流程

### 覆盖模式是探测结果，不是前置条件

无法预先知道某型号是独享文档还是与整个系列共享。**不需要预先知道**——按顺序试，把结果记下来复用。

| 模式 | 例子 | MPN 原文能否搜到 |
|---|---|---|
| `single` | 少数专用器件 | 能 |
| `enumerated` | ST LD1117：订购表逐个列出 **35 个变体（已实测）** | 能 |
| `parametric` | Yageo RC 系列：靠部件号编码规则生成，组合可达数千 | **搜不到** |

### 定位流程

```text
resolve_official_document → document_ref
        ↓
① 枚举命中：抽取文本中存在该 MPN 原文？
     是 → match_mode = enumerated，span = 命中处
     否 ↓
② 解码命中：文档含编码器表，且该 MPN 可解码、各字段合法？
     是 → match_mode = decoded，span = 编码器行 + 适用规格行
     否 → status = not_found
        ↓
③ 把 match_mode 写入 evidence，把 coverage_kind / decoder_page 缓存到 official_document
```

第 ③ 步是扇入收益的真正来源：12 个 `RC0402FR-*` 中第一个确立"这是参数式文档、编码器在第 N 页"，其余 11 个直接复用这一理解，**省下的不只是下载，更是对文档的重新解读**。

### 两种命中的证据强度不同

- **`enumerated`**：证明厂商确实列出了这个可订购型号。可据此断言该型号的生命周期状态。
- **`decoded`**：只证明该编码是合法组合、且该尺寸该精度的器件具有属性 P。**不证明厂商真的生产过这个组合。**

**强制约束**：仅有 `decoded` 命中时，**不得断言该型号的生命周期状态，不得发停产告警**，只能输出 `cannot_confirm`。否则会对一个供应正常的料件谎报停产——这是本产品最坏的失败模式（`docs/product/requirements.md` §30「把未找到解释为停产」必须为零）。

family 级属性（封装、额定值、温度范围）在 `decoded` 下可以陈述，但必须标注适用范围是系列而非该具体型号。

### 并发取证

同一份文档被多个型号同时需要是常态而非例外。两层去重：

| 层 | 键 | 时机 |
|---|---|---|
| 解析级 | Run 内 `mpn → document_ref` 映射 | 取回**之前**，避免发起重复请求 |
| 内容级 | `official_document.content_sha256` UNIQUE | 取回**之后**，兜住不同 URL 指向同一文件 |

下载前无法知道 `sha256`，所以两层缺一不可。

**Single-flight**：按解析目标在 Redis 加锁，第一个调用者取回，其余等待其结果。锁在 Redis、结果落 PostgreSQL，符合「Redis 不保存唯一的恢复事实」。

取文档是**读操作**——重复取只是浪费与不礼貌，不构成正确性问题。真正需要防重复的是 evidence 行，由 `content_hash` 兜住。因此 single-flight 在此是效率与礼节机制，实现失败的后果远低于写操作去重。

**乱序**：到达顺序随机，报告按业务键排序渲染，不按到达序。

### 修订冲突

取证过程中文档发布新修订时，**Run 内锁定先到的修订版**，同时记录"已观察到更新版本"，下次触发时再刷新并回填 `superseded_by`。

理由：一份报告引用 `Rev 38` 第 44 页与 `Rev 39` 第 12 页是自相矛盾的，读者无法把它当作一份文档核对；且文档被频繁更新时，每次重启会活锁。

代价是证据可能旧一轮。可控，因为取代关系有记录、下次触发会追上、界面可提示"存在更新版本"。裁决本身必须留痕（EV-50）。

### 三种失败互不合并

`unresolvable`（寻址不到）、`CANNOT_EXTRACT`（取回了但无文字层）、`not_found`（文档中无此型号）是三个不同结论。合并任意两个都会让"查不到"变成"不存在"，进而变成错误的停产判断。

## 5. 全局约束

1. 不提供任意 URL、任意 SQL 或任意代码执行工具。
2. 外部文本一律是不可信数据，不能改变指令、权限或允许工具。
3. 写工具由服务端权限和幂等约束强制，描述文字不构成权限机制。
4. 工具 request/response 落库前脱敏；密钥永不进入模型上下文。
5. 重试只由 Runtime 一层执行，Adapter、MCP client 和工作流节点不得叠加重试。
6. 真实、缓存、样例和 Replay 结果必须可区分。

## 6. 待定项

| 编号 | 待定内容 |
|---|---|
| I-TBD-04 | MCP 协议版本及是否用于首个外部来源 |

本文还依赖以下跨文档未决项，正文见 `docs/OPEN-QUESTIONS.md`：**Q-01**（首个真实供应 Adapter 及字段校准）、**Q-02**（第二个真实模型后端）、**Q-03**（目标采购系统、草稿字段和外部幂等能力）、**Q-04**（官方资料来源与许可边界）。

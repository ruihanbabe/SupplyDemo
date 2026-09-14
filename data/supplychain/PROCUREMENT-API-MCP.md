# 采购平台 API 与 MCP 接入调研

核对日期：2026-09-09。范围：公开官方资料和现有本地数据；没有新账号申请或新增 API 实测。首版地区已确认 DigiKey US／USD。ERP 选择待定。

## 1. 采购数据来源比较

| 平台 | 已核对能力 | 申请与约束 | 首版建议 |
|---|---|---|---|
| DigiKey | Product Information V4；官方提供 Sandbox 与 Production、OAuth 2-legged 流程 [D1] | 生产调用需组织和应用；具体产品额度须从账户核对。现有项目有历史技术查询快照，不代表当前报价接口已验收 | 第一供应来源；复用已有身份查询，补动态供货及包装字段验证 |
| Mouser | Search API 提供型号、厂商、库存、价格阶梯、MOQ、倍数、包装、交期及资料链接 [M1] | My Mouser 账户申请 Search API Key；官方页列每次最多50结果、每分钟30次、每天1000次。实际账户额度再核对 | 第二供应来源候选；先验证与现有元件的准确覆盖，未接通前不宣称双源比价 |
| element14 / Farnell / Newark | REST 产品搜索支持关键词、厂商型号和渠道商品号；标准/合同价格访问分级 [E1/E2] | API key；合同价格需要额外账户认证。文档较老，站点、配额及申请可用性必须实测 | 第三备选，不同时接入所有平台 |
| LCSC 国际站 | 官方公开 API 文档，含签名、时效、权限及 IP 白名单错误定义 [L1/L2] | API 开通与账户/网络条件待确认；不能认定个人账号自动获得权限 | 条件性备选；国际站文档不能等同于大陆 szlcsc 接口或人民币报价 |

供应搜索 API 与供应商下单 API 是不同权限。首版仅采购信息读取；动作终点是企业侧草稿，不调用供应商真实订单接口。Mouser API 文档入口存在，但动态 Swagger 页面本轮未返回可读 schema，具体 endpoint/version/字段必须在适配前取得 [M2]。

建议接入次序：DigiKey 动态字段 smoke → Q2 企业草稿 smoke → Mouser 少量型号覆盖试验。账号申请、费用、存储展示条款须核对；不以本表推断所有服务免费、可批量再分发或已可用。

## 2. ERP 是什么，接入复杂度在哪里

ERP 是企业管理采购、库存等业务的软件。可以自建开源 ERPNext，获得自己控制的真实业务 API；其中的数据仍可明确采用模拟企业记录。也可接已有企业提供的测试环境，但需要对方授权和接口资料。

ERPNext 候选链路：建测试公司、供应商、物料、仓库和最小必要配置 → 建最小权限服务账户 → 通过 Frappe REST 创建 Purchase Order 草稿 → 读回核对 docstatus 与明细。Frappe 为 DocType 提供 REST CRUD；文档状态区分 draft/submitted/cancelled [F1/F2/F3]。

复杂度主要在安装版本/数据库依赖、主数据、必填字段、业务权限和去重保障，不只是 HTTP POST。ERP 的数据库由其兼容性要求决定，不能为了项目展示 PostgreSQL 擅自替换 ERP 后端；Agent 自有 PostgreSQL 与 ERP 可以是两个独立系统。

最小侦察验收：一个模拟物料、一家供应商、一张草稿，读回一致；缺权限被拒；不触发提交、邮件、付款；重复请求的核对可实现。自定义业务工作流可能触发副作用，必须检查目标实例配置。

原生 REST 不保证本项目逻辑动作的 exactly-once。若没有可靠外部唯一键/原子去重机制，需目标端小扩展或在不确定时停止转人工；本地缓存不能解决响应丢失后的全部问题。

建议先做这一侦察，再决定 ERPNext 或自建草稿服务。自建服务更可控，但只能称为自建业务 API 集成；ERPNext 更能验证现成企业系统约束。尚未估计安装时长或部署成本，也没有创建实例。

## 3. MCP 封装边界（方案建议，不是已实现接口）

建议数据流：用户/定时事件 → Runtime → ToolExecutor/权限与预算 → MCP client → 业务 MCP server → 供应商或 ERP adapter → REST API。

共享业务 adapter 可以被服务端复用，MCP 负责协议暴露；不要把鉴权、报价映射和重试在 MCP 与 REST 入口各写一份。MCP 本身不提供业务审批的权威，也不会自动解决持久恢复与幂等。

候选工具（`search_component_documents` 已随 RAG 出局删除）：

| 工具 | 输入要点 | 输出要点／副作用 |
|---|---|---|
| search_supplier_parts | provider、完整MPN、manufacturer_hint、地区、分页 | 候选身份和匹配状态；只读，不自动批准 |
| get_supplier_offer | 已核验SKU、数量、币种、地区、包装 | 分层报价、库存/交期状态、时间、来源；只读 |
| get_material_availability | 内部物料与需求日期 | 业务版本、实物/占用/在途明细；只读 |
| create_procurement_draft | plan_id、version、logical_action_id | 目标草稿ID与状态；受审批保护的写入 |
| reconcile_procurement_draft | logical_action_id | found/not_found/unknown、草稿内容核验；只读 |

工具名为概念草案，后续与选择的 MCP 版本/SDK 命名规则核对。禁止提供任意URL请求或任意SQL作为模型绕过路径。创建工具只接受受控计划标识，服务端读取批准内容，不能相信模型传来的 approved=true。

建议结构化返回：schema_version、status(ok/partial/not_found/error)、data、evidence_refs、retrieved_at、freshness、identity_status、error(code/retryable/retry_after)、trace_id。API 错误与业务未命中分别建模。具体映射到协议的结果/错误结构由所选协议版本决定。

## 4. 实现前还缺哪些信息

| 项目 | 所需信息／验证 | 提供者 |
|---|---|---|
| 供应账号 | 可用产品、账号价格范围、额度、region/currency支持、token流程 | 用户账号配置与官方文档；凭据只在本地密钥配置 |
| 字段样本 | 成功、无结果、歧义、401/403/429、超时响应；MOQ/倍数/包装/阶梯单位 | 授权少量API调用与离线fixture |
| 数据许可 | 缓存期限、持久快照、展示、再分发与PDF权限 | 平台条款与文档许可 |
| 目标业务系统 | 版本、base URL、公司/供应商/item/warehouse、必填字段、角色、draft语义 | ERP选择与测试实例 |
| 幂等协议 | 外部唯一键、原子创建、按键查询、读后可见性、并发行为 | 目标API或扩展设计 |
| 审批身份 | 登录身份、角色、过期策略、方案哈希绑定、撤销和复核 | 产品规则与Runtime设计 |
| MCP部署 | 哪些客户端、本地stdio或远程Streamable HTTP、连接生命周期和取消语义 | 运行位置及SDK兼容性试验 |
| 配额预算 | 并发、每次超时、重试次数、全任务截止时间、缓存有效期 | 平台限额+实测，不在多层重复放大重试 |
| 可观测性 | 关联ID、脱敏字段、留存、错误分类、恢复事件 | Harness契约 |

MCP 官方 latest 本轮解析到 2026-07-28 [P1]；不能把旧版实现细节直接当作当前协议。后续 ADR 固定协议版本和 SDK 版本，验证工具 schema、结构化结果、错误、取消与重连兼容性。HTTP 的客户端身份与供应商凭据分离；工具只读标注不是权限执行机制。详细授权按选定版本核验。

## 5. 接入完成的证据

每个供应 adapter：一条已知型号、一个无结果、一个歧义样本；身份/包装/单位映射正确；限流和超时离线测试；日志不含 token/key；缓存键隔离地区与账户价格范围。

MCP：目标客户端实际 tools/list/call；合法与非法输入；预算/权限执行；HTTP/进程失败恢复；协议版本兼容。只通过 REST smoke 不算 MCP 已接入。

业务写入：审批前拒绝、批准后草稿读回、重复动作、响应丢失核对、并发副作用计数。先验证本地可控服务，不对供应商线上接口压测或注入故障。

## 官方来源

- [D1 DigiKey Resources](https://developer.digikey.com/resources)
- [M1 Mouser Search API](https://www.mouser.com/en/api-search/)
- [M2 Mouser API documentation](https://api.mouser.com/api/docs/ui/index)
- [E1 element14 API characteristics](https://partner.element14.com/docs/read/Product_Search_API_REST_Characteristics)
- [E2 element14 partner network](https://partner.element14.com/)
- [L1 LCSC API documentation](https://www.lcsc.com/docs/index.html)
- [L2 LCSC API help](https://www.lcsc.com/help-center/api)
- [F1 Frappe REST API](https://docs.frappe.io/framework/user/en/api/rest)
- [F2 ERPNext Purchase Order](https://docs.frappe.io/erpnext/purchase-order)
- [F3 Frappe Docstatus](https://docs.frappe.io/framework/doctypes/docstatus)
- [P1 MCP specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [P2 MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

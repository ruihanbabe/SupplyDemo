# 采购平台与目标业务系统调研

> **文档契约** · 类型：参考层 · 读取：选型、评估配额或准备接入某个平台时按平台名定点读
> 更新：重新核对平台能力、配额、条款，或选型结论变化时由 Claude 写入，须更新核对日期
> 独占：各采购平台的已核对能力与配额、ERP 候选方案与接入复杂度、官方来源链接清单
> 不收录：工具契约（见 `docs/spec/interfaces.md`）、验收标准（见 `docs/product/acceptance-cases.md`）、选型决策与理由（见 `DECISIONS.md`）、当前接入状态（见 `PROGRESS.md`）

**核对日期：2026-09-09。** 范围限于公开官方资料与既有本地数据，**未申请新账号、未做新增 API 实测**。本文是事实快照与候选评估，不是承诺——不得据此推断"所有服务免费、可批量再分发或已可用"。

首版地区已确认 DigiKey US／USD；ERP 选择待定。

## 1. 采购数据来源比较

| 平台 | 已核对能力 | 申请与约束 | 首版定位 |
|---|---|---|---|
| **DigiKey** | Product Information V4；官方提供 Sandbox 与 Production、OAuth 2-legged 流程 [D1] | 生产调用需组织与应用；具体产品额度须从账户核对。此前项目有历史技术查询快照，**不代表当前报价接口已验收** | **第一供应来源**。复用已有身份查询，补动态供货与包装字段验证 |
| **Mouser** | Search API 提供型号、厂商、库存、价格阶梯、MOQ、倍数、包装、交期及资料链接 [M1] | My Mouser 账户申请 Search API Key。官方页列**每次最多 50 结果、每分钟 30 次、每天 1000 次**；实际账户额度须再核对 | 第二供应来源候选。先验证与现有元件的覆盖准确度，未接通前不宣称双源比价 |
| **element14 / Farnell / Newark** | REST 产品搜索支持关键词、厂商型号与渠道商品号；标准价与合同价访问分级 [E1/E2] | API key；合同价格需额外账户认证。文档较老，站点、配额与申请可用性必须实测 | 第三备选。不同时接入所有平台 |
| **LCSC 国际站** | 官方公开 API 文档，含签名、时效、权限及 IP 白名单错误定义 [L1/L2] | API 开通与账户/网络条件待确认；不能认定个人账号自动获得权限 | 条件性备选。国际站文档**不等同于**大陆 szlcsc 接口或人民币报价 |

**权限边界**：供应搜索 API 与供应商下单 API 是两套不同权限。首版仅做采购信息读取，动作终点是企业侧草稿，不调用供应商真实订单接口。

Mouser 的 API 文档入口存在，但动态 Swagger 页面在本轮核对中未返回可读 schema，具体 endpoint、版本与字段必须在写 adapter 前取得 [M2]。

**建议接入次序**：DigiKey 动态字段 smoke → 企业草稿 smoke → Mouser 少量型号覆盖试验。账号申请、费用与"存储/展示"条款须逐条核对。

Mouser 的配额数字是 `DECISIONS.md` D13（供应查询默认单源）的直接依据：121 条用料行 × 4 家 = 484 次调用，一天扫两遍即耗尽当日额度。

## 2. 目标业务系统（ERP）

ERP 是企业管理采购、库存等业务的软件。本项目需要它只为一件事：**提供一个真实的、有业务约束的写入终点**，用来证明审批门与幂等是真的，而不是自说自话。

两条路：自建开源 ERPNext，获得自己控制的真实业务 API（其中数据可以明确采用模拟企业记录）；或接已有企业提供的测试环境，但需对方授权与接口资料。

### ERPNext 候选链路

建测试公司、供应商、物料、仓库与最小必要配置 → 建最小权限服务账户 → 通过 Frappe REST 创建 Purchase Order 草稿 → 读回核对 `docstatus` 与明细。Frappe 为 DocType 提供 REST CRUD；文档状态区分 draft / submitted / cancelled [F1/F2/F3]。

**复杂度不在 HTTP POST**，而在安装版本与数据库依赖、主数据、必填字段、业务权限与去重保障。ERP 的数据库由其自身兼容性要求决定，不能为了项目展示 PostgreSQL 就擅自替换 ERP 后端——Agent 自有的 PostgreSQL 与 ERP 是两个独立系统。

### 最小侦察验收

一个模拟物料、一家供应商、一张草稿，读回一致；缺权限被拒；不触发提交、邮件、付款；重复请求的核对可实现。自定义业务工作流可能触发副作用，必须检查目标实例配置。

### 幂等是硬难点

**原生 REST 不保证本项目逻辑动作的 exactly-once。** 若目标端没有可靠的外部唯一键或原子去重机制，只有两条出路：在目标端做小扩展，或在结果不确定时停止转人工。本地缓存解决不了响应丢失后的全部问题（对应 BR-11 与 `docs/spec/interfaces.md` §3.5）。

### 取舍

自建服务更可控，但只能称为"自建业务 API 集成"；ERPNext 更能验证现成企业系统的真实约束。**建议先做上述侦察，再决定走哪条**。尚未估算安装时长或部署成本，也没有创建实例。

## 3. 实现前仍缺的信息

| 项目 | 所需信息／验证 | 提供者 |
|---|---|---|
| 供应账号 | 可用产品、账号价格范围、额度、region/currency 支持、token 流程 | 用户账号配置与官方文档；凭据只存本地 |
| 字段样本 | 成功、无结果、歧义、401/403/429、超时的真实响应；MOQ/倍数/包装/阶梯单位的实际取值 | 授权少量 API 调用与离线 fixture |
| 数据许可 | 缓存期限、持久快照、展示、再分发与 PDF 权限 | 平台条款与文档许可 |
| 目标业务系统 | 版本、base URL、公司/供应商/item/warehouse、必填字段、角色、draft 语义 | ERP 选型与测试实例 |
| 幂等协议 | 外部唯一键、原子创建、按键查询、读后可见性、并发行为 | 目标 API 或扩展设计 |
| MCP 部署 | 哪些客户端、本地 stdio 还是远程 Streamable HTTP、连接生命周期与取消语义 | 运行位置及 SDK 兼容性试验 |
| 配额预算 | 并发、单次超时、重试次数、全任务截止时间、缓存有效期 | 平台限额 + 实测；不在多层重复放大重试 |

MCP 官方 latest 在本轮核对中解析到 2026-07-28 [P1]；**不能把旧版实现细节当作当前协议**。协议版本与 SDK 版本待后续 ADR 固定，届时须验证工具 schema、结构化结果、错误、取消与重连的兼容性。HTTP 的客户端身份与供应商凭据分离；工具的"只读"标注不是权限执行机制。

## 4. 官方来源

- [D1] DigiKey Resources — https://developer.digikey.com/resources
- [M1] Mouser Search API — https://www.mouser.com/en/api-search/
- [M2] Mouser API documentation — https://api.mouser.com/api/docs/ui/index
- [E1] element14 API characteristics — https://partner.element14.com/docs/read/Product_Search_API_REST_Characteristics
- [E2] element14 partner network — https://partner.element14.com/
- [L1] LCSC API documentation — https://www.lcsc.com/docs/index.html
- [L2] LCSC API help — https://www.lcsc.com/help-center/api
- [F1] Frappe REST API — https://docs.frappe.io/framework/user/en/api/rest
- [F2] ERPNext Purchase Order — https://docs.frappe.io/erpnext/purchase-order
- [F3] Frappe Docstatus — https://docs.frappe.io/framework/doctypes/docstatus
- [P1] MCP specification — https://modelcontextprotocol.io/specification/2026-07-28
- [P2] MCP tools — https://modelcontextprotocol.io/specification/2026-07-28/server/tools

链接与结论均为 2026-09-09 的快照，接入前须重新核对。

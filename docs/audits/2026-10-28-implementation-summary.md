# 当前入口与受控执行实现摘要

日期：2026-10-28。承接 [前置条件与测试盘点](2026-10-28-development-readiness.md)，记录之后按授权顺序实现的切片；完整长期目标仍见各 Contract，验收由用户决定。浏览器和真实模型/渠道端到端测试由用户自行执行。

## 已实现的行为

| 单元 | 现在可以做什么 | 仍保留的边界 |
|---|---|---|
| F38 E0/E1 | 固定契约案例运行、源码/案例指纹、失败/跳过/空执行门禁；原用户私有 Run 结果快照、引用与调用关联、失败代次耗时/用量、绑定案例与结果的人工评分 | fixture 不表示模型质量；未执行真实回答人工评分。完整回放分叉、跨配置统计、Judge/Langfuse/Compose 仍后续 |
| F33 只读可视化 | 私有 SSE 历史补放与持续读取、游标续接、逐批撤权检查；页面时间线和本 Run 用量；终态关闭、重连和组件清理 | 最近500条展示；完整 CRM/审批收件箱/全局配额未完成。生产 build 的既有安全阻断仍需解决 |
| F35 恢复增强 | 永久错误终止、临时错误有限重试、安全原因；固定生命周期 checkpoint；新模型能力绑定 Job/owner/generation，失租后拒绝新请求/结果接受 | 默认整 Job 新 turn 重跑；不能撤回已发出的模型流/效果；自动分支复用、Pi 内存栈恢复、通用人工等待未实现 |
| F41 人工客户更新 | 提案创建/读取/批准或拒绝，同键幂等、禁止自批；审批原子生成 Job/独立 Trace/执行账本；Worker 重验后更新客户，提交后重投不重复改版本 | 仅 displayName 人工提案与本人 manual_action_evidence；自动 Agent R1、采购提交、完整审批 UI 仍后续 |
| F37 本地消息闭环 | 加密草稿/正文/目标、语义幂等；审批后发送意图与独立持久 Adapter；未知结果只 lookup；签名回执、去重、防终态倒退、取消后的实际事实记录 | 唯一 Adapter 为本地 SQLite fixture；没有 WhatsApp/飞书协议、账号、会话接线、公开 HTTP webhook 或真实发送 |

实现入口分别为 `src/product_api/evaluation*.py`、`app.py` 与前端私有 SSE/BFF、`recovery.py`/`runtime.py`/`gateway.py`、`actions.py`、`channels.py`。接口及运行方式见 [评测说明](../../src/product_api/EVALUATION.md)、[动作运行说明](../../src/product_api/ACTIONS_RUNTIME.md) 和 [Gateway/Worker 契约](../design/model-gateway-worker-contract.md)。

## 验证记录

以下数字为执行证据，不叠加计算覆盖率；专项与全量存在重叠。

- F38：eval-check 9；离线案例8；克隆集成2案例/3测试通过。人工语义评审未执行。
- F33：8项离线/Trace、BFF边界、前端 lint/TypeScript，以及3项双协议克隆链通过。
- F35 R1：19项离线、5项克隆恢复/Worker/双协议链通过。
- F41：20项 API/Worker 专项与8项克隆变体通过，含双方撤权、版本/内容/期限/政策/opt-out、重复审批/重投。
- 最新全量离线：`make check` 232 passed、91 deselected；既有 Starlette 提示1条。
- 首次全量集成：86 passed、3 failed；失败均表现为刚入队/重排的工作立即领取返回 idle。窄回归7项通过，尚不能用它覆盖首次失败记录。
- F37最新：`f37-check` lint及2项离线通过；`f37-integration-test` 10 passed/1 failed，lost_response首次领取idle。此前一次专项未审批场景误消费夹具创建的无关供应调查，现已显式取消该种子Job，未审批断言通过；没有放宽“不得调用模型/不得发送”的断言。
- 用户要求跳过时钟排查后，未重复执行全量集成、未增加skip或修改失败断言。F37保持in_progress；F38/F33/F35新增切片与F41保持awaiting_acceptance，不将长期Feature整体标done。

### 本地时钟问题（已记录，按用户指示暂缓处理）

最小复现仅执行 remember→claim，第15轮发现 due_at=05:46:41.857932，领取时服务器时间却为05:46:41.451870，任务仍 pending/generation=0。独立900次只读连接捕获4次157–209ms回跳。Lima同一秒日志记录 guest clock adjusted (was 477ms off)。暂停NTP的700次对照仍捕获回跳，之后已恢复NTP。

复现发生于2026-10-28 13:46:41（Asia/Shanghai，以上数据库时间为UTC）。首次全量失败位置为：

- `test_product_api_channels_integration.py::test_encrypted_send_gates_receipts_and_unknown_reconciliation[unknown]`：第三代Worker预期failed，实际idle；前两代均retry_scheduled。
- `test_product_api_memory_governance_integration.py::test_entity_qualifiers_and_multisource_relations[decision episode]`：remember后立即领取，预期review_pending，实际idle。
- 同文件 `test_legacy_export_revalidate_reconcile_switch_and_safe_rollback[False]`：相同idle症状。

第一项及整个memory_governance文件的窄回归7项均通过；最小压力复现已证明时钟回跳可产生相同idle，但首次三个失败当时没有逐项时间诊断，不能断言每项已独立证明相同原因。

诊断环境为Colima/macOS Virtualization.Framework、x86_64；原Linux时钟源hpet，systemd-timesyncd开启。暂停NTP后700次采样仍有1次约186ms回跳；临时改为可用的acpi_pm后900次仍有3次约85–229ms回跳。两项对照均未解决问题。**NTP已恢复active，时钟源已恢复hpet**；没有留下环境修复或永久配置变更。Lima日志`~/.colima/_lima/colima/ha.stderr.log`在复现同秒记有477ms校时，后续仍每约10秒出现校时记录。实现可对照[官方hostagent校时循环](https://github.com/lima-vm/lima/blob/master/pkg/hostagent/timesync.go)及[Linux Settimeofday实现](https://github.com/lima-vm/lima/blob/master/pkg/guestagent/timesync/timesync_linux.go)；这些链接不是本机固定版本的源码核验，修复方案尚未确定。

回溯证据保存在本机忽略目录：`.runtime/verification/full-integration-2026-10-28.txt`、`db-clock-probe.json`、`db-clock-without-ntp.json`、`db-clock-acpi.json`。这些私有文件不进入Git，不能当成已随代码交付的制品。

结论：立即run_once的idle不能自动判断为任务丢失；任务在数据库时间再次达到due_at/visible_at、且其他条件满足后可被后续轮询领取。到期/租约/代次断言保持，不加盲重试、不修改业务库。用户明确要求本次跳过时钟排查，继续后续开发；全量集成仍保留86通过/3失败记录，不宣称全绿或生产时钟治理完成。

## 用户自行联调前仍需完成

1. 冻结代码、数据、政策、提示与工具版本；建立独立测试库，确认具体迁移组合及受限身份。0042–0044只经过克隆验证，业务库未迁移。
2. 核对独立 Web/BFF、API、Worker、Gateway 指向同一测试库，登录、组织和原用户范围一致。时钟回跳本次暂缓；开展严格时序/租约/立即领取验收前需恢复稳定时钟基线。
3. 页面仍默认 Replay；真实模型测试需接线 gateway 模式和有效 model_policy_id，并另行明确授权/预算。先关闭 Context/Memory，再逐组增加。
4. 解决既有前端生产构建 SecurityFatalError；当前 lint/TS 通过不能替代该门禁或浏览器测试。
5. 预先制定案例与业务 oracle，导出私有评测快照，按人工量表评分；未知 usage/成本保留 unknown，失败重试计入统计。
6. 如单独测试 F41/F37，准备服务政策、两名有权限的人工身份和人工证据；F37另需显式安装可选加密依赖、服务端密钥与 fixture 配置。真实渠道接入仍是独立开发和验收工作。

轻量 model routing 仍按 [todo](../../todo.md) 条件性保留：先形成单模型基线与第二模型的授权/收益证据，不作为当前联调前置。

## 本轮收尾与下一步

约定顺序的本地实现均已写入代码，F37剩余的严格集成通过证据受领取idle问题影响而未闭合。时钟问题按用户指示留待后续；不据此继续调整VM或无限回归。实际渠道、采购与自动Agent提案、完整审批工作台等保留目标没有在本轮扩展实现。用户可按前置清单准备独立服务联调与真实回答人工评测，时序类检查需单独标注本地环境限制。

# SupplyAgent 项目机制索引

更新：2026-10-28。用于从全局进入当前实现；实施状态以features.json/Contract为准，不复制BL/DC正文。F51/F52/F48本地实现待验收；F38入口评测、F33只读可视化、R1恢复、F41/F37当前切片的精确状态见各Contract。

## 一次业务调查的实际路径

```text
Web/BFF → Python API 身份与组织/对象校验 → SQL Mission/Run/Job
                                                    ↓
Worker claim/lease/generation → 固定 Mission/Specialist 编排
                                                    ↓
角色任务与 input_context → Harness / AgentEnginePort → Pi 消息循环
                                                    ↓ 模型请求
                                           私有 Gateway → Provider
                                                    ↓ 工具调用
                         ToolBinding → Runtime.prepare → Python Scheduler
                                                    ↓ 连续兼容组/顺序屏障
                              Runtime.execute → SQL 调用台账/容量
                                                    ↓
                       领域读取/确定性计算 → Artifact / Observation
                                                    ↓
                            引用/来源/租约接受门禁 → 业务结果 / Trace
```

模型和工具循环不管理 SQL Job；模型完成不等于业务成功。F36 固定模板与 F45 情报问答的业务产物/质量门禁不同，不能合成一条假想已实现链路。

## 当前机制、缺口与开发落点

| 机制 | 已有源码和行为 | 尚未实现 / 后续入口 |
|---|---|---|
| 身份/组织/对象权限 | [tenant.py](../../src/product_api/tenant.py)、rbac.py、领域 RLS；原用户读取与 Worker 技术身份分开 | 来源/摘要依赖失效传播按 F48/F52，历史快照不授永久权限 |
| 持久运行 | [runtime.py](../../src/product_api/runtime.py)、worker_main.py；Job 领取/心跳/取消/回收，旧 generation 拒绝 | 通用 Task/计划修订/局部复用仍未交付，不从 Pi 内存恢复 |
| 业务编排 | [mission_runtime.py](../../src/product_api/mission_runtime.py)、[intelligence.py](../../src/product_api/intelligence.py)；固定分支及汇总，角色独立 invocation | 动态 Planner、通用冲突裁决未实现；业务策略 BL03 |
| 模型执行 | [harness.py](../../src/product_api/harness.py)、[sidecar](../../pi_sidecar/f42_protocol_sidecar.mjs)；Pi 0.73.1、生产 JSONL v2、完整 assistant 批次/取消/结构化输出；v1 为显式兼容 | F52 C2 已接通配置控制的 v3，默认 v2；Pi sequential 仅消费 Python 有序结果 |
| 工具声明/绑定（F51 P1） | [tool_registry.py](../../src/product_api/tool_registry.py)：七个显式 ToolSpec、精确别名、冲突检查、严格输入模型、不可变 ToolBinding；Python/Pi 同源 Schema/描述 | [tool_runtime.py](../../src/product_api/tool_runtime.py)/[tool_scheduler.py](../../src/product_api/tool_scheduler.py) 已抽离；harness.ToolRegistry 只作旧执行入口兼容 |
| 工具执行/证据 | Runtime 准备/执行/接受/交付门禁，原用户来源验证，F36 Artifact/F45 Observation 与调用成功同事务 | 0030/0031 未迁移业务库；没有真实供应商资源/QPS 限流接入 |
| 批次/容量 | Python Scheduler 独占 serial/contiguous 调度，双向兼容、屏障、原位归还；SQL 原子多资源准入/续租，超时隔离 | 容量配置见 config/product/tool-runtime-policy.json；16384 字节/16 调用限制，超限终止，不承诺任意大批次成功 |
| Gateway/费用 | [gateway.py](../../src/product_api/gateway.py)、glm.py；模型策略/凭据/预留/结算与私有 exchange，注册别名由 Registry 生成 | v3 额外验证每请求 snapshot/Binding/lease/epoch；新Worker能力经0042绑定Job/owner/generation；旧独立能力和已发出流不宣称即时撤销 |
| Context | F52 C1–C3会话/Working State、每请求Compiler、快照、压缩/渐进读，默认关闭 | 0032–0034仅克隆验证，自动业务续接/生产启用仍后续 |
| Memory | F48 M1–M3 SQL权威版本、Mem0投影/关系/删除/无损Dream；保留旧F36路径 | 0035–0041仅克隆，默认关闭/仅合成；真实服务、旧数据切换/生产备份治理仍后续 |
| 追溯/输出 | [trace.py](../../src/product_api/trace.py)、Artifact/Observation/版本引用；私有正文与公开 Trace 分开 | 引用有效不等于语义结论充分；F45 needs_review，F41/F37当前人工/fixture切片见[ACTIONS_RUNTIME](../../src/product_api/ACTIONS_RUNTIME.md)，真实渠道与完整产品仍后续 |

## 变更从哪里进入

业务判断/确认/记忆策略读 [business-logic](../../business-logic.md)；身份/数据含义/状态/接受时点读 [data-contract](../../data-contract.md)。工具 Interface 读 [TOOL_RUNTIME](../../src/product_api/TOOL_RUNTIME.md)，Context 与 Memory Interface 分别读 [CONTEXT_RUNTIME](../../src/product_api/CONTEXT_RUNTIME.md)、[MEMORY_RUNTIME](../../src/product_api/MEMORY_RUNTIME.md)。

F51 P1 的 Binding 只冻结当前任务允许的工具声明集合和 Schema/版本指纹，不能代替对象权限、用户身份或 lease；业务范围仍由服务器绑定的领域 handler 检查。model_tools.py 只保留兼容导出，不再有第二份可编辑别名目录。

开发依次 F51 P1→P2→P3→P4，再 F52 C1–C3、F48 M1–M3。各单元完成并给出可执行证据后再推进，不能把本文目标列当当前能力。验证命令唯一实现在 Makefile；F51 全部单元使用 f51-check/f51-integration-test，集成仅克隆库，无真实 Provider/渠道调用。F51 P1–P4、F52 C1–C3、F48 M1–M3均为本地实现待验收，不能当作已部署。


## F51 当前交付入口

Worker engine factory 默认 v2；显式启用 Context 配置后固定 v3，整批准备后交 Python Scheduler；F45 input_context 记录实际 toolPolicy 和 Binding，调用台账追加规范名、参数 hash、调度快照及真实结果引用。共享容量只在工具声明资源后启用；当前七工具没有真实外部供应商资源。资源过期隔离与显式 owner receipt 释放分离，不能解释为 TTL 自动恢复。

部署必须先按 F51 确认 0030/0031 清单，再协调 Python/sidecar 版本；本轮业务库未迁移。仅将 strategy 改 serial 即可消融并发，仍保留 v2、授权和持久化。结果超限不交付、不重执行以缩小；大正文/渐进读取与 Context/Memory 均有独立 Contract。

## 当前人工动作与评测

F38入口评测见[EVALUATION](../../src/product_api/EVALUATION.md)；F33原用户私有SSE/续接/每Run用量复用SQL事实，完整工作台仍后续。R1恢复和人工动作见[ACTIONS_RUNTIME](../../src/product_api/ACTIONS_RUNTIME.md)及F35/F41/F37；消息accepted与delivered、Job状态分别表达，不合并为虚假成功。

# F38 入口评测

当前实现 E0 契约回归与 E1 私有业务快照/人工评分，完整范围见 [F38](../../contracts/F38.md)。契约通过不能代替模型语义评分、真实Provider或生产验收。

案例唯一清单：config/product/eval-cases.json；标准库runner：evaluation.py。复用现有pytest断言作为oracle，不另写第二套业务计算；案例分offline/integration，后者仅既有自动删除克隆库与本地固定模型报文。无新增依赖、服务、表或业务配置开关。

命令实现只在 [Makefile](../../Makefile)：eval-check检查报告门禁和清单，eval-run执行离线组，eval-integration执行克隆组。集成依赖与其原测试一致；不执行源库迁移。真实模型标记明确排除，清单不接受live/supabase测试路径。

报告位于.runtime/eval/offline.json与integration.json，含清单版本/hash、源码/测试/配置/迁移/构建命令hash、案例ID/节点/oracle/状态/耗时。源码在运行期间变化使整体失败；空报告、收集错误、失败、跳过不能贡献通过。pytest输出不写入集中报告，避免转存私有正文；失败可用清单中的固定节点在本机诊断。

耗时是整个契约测试耗时，不是单模型响应延迟；costMicros=null、usage=not_aggregated、semanticQuality=not_measured。现有集成断言检查私有Run/Exchange等关联，但runner尚未逐运行导出这些引用。不得据此宣布完整Eval完成。真实回答人工量表、版本化输入快照导入/评分、跨配置质量比较、使用量聚合及全回放分叉仍是后续单元。

## E1 私有业务快照与评分

evaluation_result.snapshot通过原用户的组织/对象权限与当前来源校验读取实际调查结果。API入口为GET /v1/intelligence/runs/{run_id}/evaluation，organization_id与Bearer身份沿用原问答接口；它不提供管理员代读。导出内容含私人问题/答案，应仅保存在私有目录（通常.runtime），不能上传公共Trace。

快照包含Run/Job、各代次角色/状态/输入指纹和耗时、当前可见证据的Observation→invocation/modelCall/toolCall映射、当前结果以及用量汇总。没有原始模型请求/凭证或旧prompt正文。只在reservation非空、全部settled且与usage计数一致时显示已知费用，否则为unknown/NULL；已知免费合成价格0与未知不同。

调用者在运行前准备案例JSON：caseId、version、organizationId、question、expected。expected是答案对象中的点分隔字典路径到确定值的非空映射，例如quality=needs_review、commercialNodesEnabled=true；不存在的路径即使期望null也不能通过。数量/日期等应来自可信固定输入及确定性oracle；目前不自动从中文抽取数字评分。

使用Makefile的eval-score，输入SNAPSHOT、CASE、OUTPUT，人工复核后另提供REVIEW。review包含snapshotHash、caseHash、reviewer、rationale、scores，三个维度为evidence_faithfulness、question_relevance、uncertainty_preservation。0分为关键事实错误/违反维度，1分为部分正确但关键遗漏，2分为该维度关键要求均满足。只有三项均2分才标semantic passed；缺评审为pending；硬约束不通过或语义未通过的CLI均非零。hash是版本/损坏检查，不是签名证明；reviewer为本地人工记录，不冒充服务端认证的审批身份。

确定性检查包括实际完成、非空结论、引用Observation身份/版本/locator/hash、组织和问题、预先给定的expected。合法引用不等于语义正确，人工评分不会覆盖失败的硬约束。E0仍是contract_regression；E1额外支持业务结果评测，二者报告不混为同一个语义分数。成本/耗时为本Run包含失败代次的数据库记录，逐模型时延、比较多个配置的统一统计、全回放分叉与完整部署仍后续。

# 人工提案与受控执行

当前范围：F41人工customer_update与F37本地fixture消息闭环。真实采购、WhatsApp/飞书协议、渠道账号及会话接线未实现，不因本地Adapter通过升级为生产能力。规则见BL10，物理责任见DC22；完整目标保留于对应Contract。

## 身份与命令

Python API验证Bearer身份，并在同一组织事务内检查capability与对象scope。人工证明只接受本人创建的manual_action_evidence，F40 SQL内容hash重算通过；模型或外部派生引用尚不作为可执行授权。Pi仍只读，不获得执行令牌、密钥或任何SQL命令。

- POST /v1/proposals：customer_update，targetId/targetVersion/content.displayName/evidenceArtifactIds/expiresAt；可选idempotencyKey。同组织/请求者/同键同内容返回原提案，冲突409。
- POST /v1/message-proposals：fixture人工消息，targetId/targetVersion/destination/body/evidenceArtifactIds/expiresAt/idempotencyKey。服务端加密后落库，HMAC语义指纹使随机密文下的同键重试保持幂等；实际渠道名拒绝。
- GET /v1/proposals/{id}：organization_id与当前对象权限重验；消息仅请求者或可审批者可读取解密内容。返回目标当前版本/opt-out、草稿hash/到期/证据、执行状态；send.state区分发送接收与实际送达，不返回审批收据标识。
- POST /v1/proposals/{id}/review：organizationId、approved/rejected；禁止自批、重复决定/到期/哈希或目标冲突。批准、短期数据库审批收据、执行账本、独立Trace及F35 Job同事务生成。API不执行动作。

Worker识别action_execution且不调用模型；有效Job owner/generation/lease下重验双方当前权限/scope、审批、内容hash、到期、目标版本、opt-out、服务revision与证据。customer_update的SQL更新与成功账本同事务；提交后Job未完成的重投只读原结果，不重复改版本。

action_service_policy默认无配置不可执行；无配置草稿记录revision=0，后来启用不能自动使旧草稿有效。enabled、revision、max_approval_seconds由受控部署维护。默认24小时、允许60秒–7天，不按模型决定；变更须推进revision，不删除再重建旧版使审批复活。0042增加行锁函数，0043增加F41安全状态和账本，0044增加F37命令与表；都只在自动删除的克隆验证，0023保留历史且只装配一次。

## 发送与未知结果

密文与接收目标只在可信Python解密，调用前再重验。SQL先提交sending意图，Adapter使用logical_action_id作为持久幂等键；超时/未知结果保留reconciliation_required，后续只lookup，不盲目重发。首次意图提交后就算尚未到达远端，若不能确认也不会擅自重发；需要人工处理或后续可证明安全的Adapter策略。

FixtureAdapter使用独立SQLite持久保存加密内容/指纹/回执，支持进程对象重建后的lookup。确认accepted只表示本地接收方已接受，不是delivered。FixtureReceiver只实现自己的HMAC时间窗协议：先校验原始报文/时间/签名与严格字段，再加密写inbox；固定组织Worker上下文，事件ID幂等，同ID变更内容拒绝，terminal状态不被迟到失败覆盖。这不是WhatsApp/飞书签名实现。

取消或失租后旧Worker不能接受迟到结果；已经发出的效果不能撤回。独立可信回执仍可记录实际delivered/failed事实，但不会把cancelled Job改成completed。未知发布的有限重试耗尽后，Job失败与发送结果待对账可以并存，不伪造失败或送达。

## 密钥、依赖与运行

可选依赖requirements.channels.lock.txt（cryptography 46.0.7/cffi/pycparser），安装唯一入口f37-setup。固定wheel在清洁Python3.11环境验证过加解密/错钥拒绝；没有全局重写主环境锁或安装渠道SDK。参照[官方Fernet](https://cryptography.io/en/stable/fernet/)与[46.0.7制品](https://pypi.org/project/cryptography/46.0.7/)。50系列在本机需要源码构建，本轮没有完成/使用该安装。

默认未配置拒绝。仅本地fixture模式读取服务端SUPPLYAGENT_CHANNEL_MODE=fixture、SUPPLYAGENT_CHANNEL_ENCRYPTION_KEY（Fernet密钥），可选SUPPLYAGENT_CHANNEL_FIXTURE_DB；密钥不落库、不进Node/Trace或页面。inbox签名密钥由可信Receiver构造时独立注入，真实webhook HTTP路由未开放。Fixture数据库默认.runtime/channels/fixture.sqlite，不接受客户端路径/URL。

本轮不生成长期密钥、不改真实.env、不应用业务库迁移。正式启用前仍需密钥保管/轮换、保留与备份、账号/协议/回执真实性、跨系统对账及部署验收；当前加密payload没有声称完整渠道数据治理已经完成。

## 验证

命令实现在Makefile：f41-check/f41-integration-test、f37-check/f37-integration-test。当前测试使用真实克隆PostgreSQL/RLS、SQL Job、独立SQLite/Fernet和签名回执；无真实网络发送。浏览器完整审批收件箱/聊天会话、真实渠道联调不在这些证据内。

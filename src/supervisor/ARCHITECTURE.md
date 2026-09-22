# Supervisor 模块

> **文档契约** · 类型：局部架构 · 读取：实现本模块时
> 更新：模块边界变化时
> 独占：局部职责
> 不收录：需求和全局数据流

目标职责是持有会话状态、编排一次请求的 Worker 调用序列，并按复杂度在简单链路与完整分析链路之间条件路由（`DECISIONS.md` D02）。

输出为结构化的路由决策与 Worker 调用结果引用，Run 生命周期与节点推进交给 Run Manager / Workflow Controller。模型路由属于 Model Gateway，不属于本模块。

# Tools 模块

> **文档契约** · 类型：局部架构 · 读取：实现本模块时
> 更新：模块边界变化时
> 独占：局部职责
> 不收录：全局架构

Tool Registry 将业务工具名绑定到严格 schema、权限、副作用、预算和 Adapter。所有结果先标准化为 ToolResult，再由 Evidence Ledger 记录。工具层不计算缺口、不生成采购建议，也不拥有 Run 状态。

# Tools 模块指引

> **文档契约** · 类型：模块入口 · 读取：进入本目录前
> 更新：工具层边界变化时
> 独占：本目录实现约束
> 不收录：具体工具 schema

本模块实现 Tool Registry 与 Adapter：统一 schema、读写属性、权限、幂等、预算、provenance 和 ToolResult。内部能力使用 Python 端口，外部系统按需使用专用 Adapter 或 MCP。供应商字段映射不得进入模型 prompt 或工作流节点。具体契约见 `docs/spec/interfaces.md`。

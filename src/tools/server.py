"""文件摘要（F07 · 工具与 Server 定义）

进程内 Server，接口按 MCP 的工具形状（name / description / inputSchema，list_tools / call_tool）；
将来装上 mcp 包时只加一层标准传输包装。代理层之外的代码不直接调用 handler（G1）。
- ToolDef：参数模型、处理函数、来源（sim / external）、提供方推导函数、返回校验器、真实时间缓存键名、实体键提取。
- Server：一组工具；list_tools 输出 MCP 形状。
- CallContext：代理层传给处理函数的上下文（模拟日、案子、因配额不可用的提供方）。
依赖：pydantic。被各 Server 定义文件与 tools.proxy 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Callable  # 函数类型
from dataclasses import dataclass, field  # 数据类

from pydantic import BaseModel  # 参数模型


class BadFormat(ValueError):
    """上游返回整体无法解析（B3），代理层转成 bad_format 错误。"""


class NotFound(LookupError):
    """查不到，代理层转成 not_found 错误。"""


# 功能：处理函数收到的上下文。依赖：无。完成：模拟日、案子、执行者、配额不可用的提供方（部分成功用）。
@dataclass
class CallContext:
    day: int  # 当前模拟日
    case_id: str | None  # 案子
    executor: str  # 执行者角色
    unavailable: set[str] = field(default_factory=set)  # 本次因配额 / 限流不可用的提供方


# 功能：一个工具的完整声明。依赖：pydantic 参数模型。
# 完成：providers(args) 给出本次调用涉及的提供方（配额按它扣）；validate(raw) 做返回校验（B3），
#       返回 (数据, 警告)；cache_key 为 config/tools.yaml 中的工具名（为空则不缓存）。
@dataclass
class ToolDef:
    name: str  # 工具名
    description: str  # 何时用、何时别用、一个例子
    params: type[BaseModel]  # 参数模型
    handler: Callable[[BaseModel, CallContext], object]  # 处理函数：返回原始数据
    source: str = "sim"  # 来源：sim（模拟环境）或 external（外部）
    providers: Callable[[BaseModel], list[str]] = lambda args: ["sim"]  # 涉及的提供方
    validate: Callable[[object], tuple[object, list[str]]] | None = None  # 返回校验器
    config_key: str | None = None  # 在 config/tools.yaml tools 段里的键（缓存与有效期）
    entity_keys: Callable[[BaseModel], dict[str, list[str]]] = lambda args: {}  # 事件实体键

    def schema(self) -> dict:
        """MCP 形状的工具描述。"""
        return {"name": self.name, "description": self.description,  # 名称与描述
                "inputSchema": self.params.model_json_schema()}  # 参数 Schema


# 功能：一个 Server。依赖：ToolDef。完成：按名取工具、列出工具（MCP 形状）。
@dataclass
class Server:
    name: str  # Server 名
    tools: list[ToolDef]  # 工具

    def list_tools(self) -> list[dict]:
        """MCP list_tools。"""
        return [t.schema() for t in self.tools]  # 逐个描述

    def get(self, name: str) -> ToolDef | None:
        """按名取工具。"""
        return next((t for t in self.tools if t.name == name), None)  # 找不到为 None

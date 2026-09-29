"""文件摘要（F08 · 内部格式）

Agent 循环只面对这里定义的内部格式（spec §5.3 模型适配层）：
- Message / ToolSpec / ToolCall / ModelResponse：消息、工具定义、工具调用、模型响应。
- Mpn：料号字段类型标记；输出模型里用它声明的字段会被"编造料号"检查（spec §5.3）。
- 异常：ModelUnavailable（传输层：超时、限流、上游错误，重试熔断归 F15）、ModelFailure（格式失败且降级也失败）。
依赖：pydantic。被 providers、adapter 以及后续 Runtime（F09）、子 Agent（F23）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from dataclasses import dataclass, field  # 数据类
from typing import Annotated  # 带元数据的类型

from pydantic import BaseModel, Field  # 输出与参数模型基类

#: 料号字段：值必须出现在调用方给的已知集合里，否则按"编造"拒绝（spec §5.3）。
Mpn = Annotated[str, Field(json_schema_extra={"x-mpn": True})]  # 用字段元数据打标


# 功能：一条对话消息（内部格式）。依赖：无。完成：角色、正文、工具调用（助手）、工具调用 ID（工具结果）。
@dataclass
class Message:
    role: str  # system / user / assistant / tool
    content: str = ""  # 正文
    tool_calls: list[ToolCall] = field(default_factory=list)  # 助手发起的工具调用
    tool_call_id: str | None = None  # 工具结果对应的调用 ID


# 功能：一个可供模型调用的工具。依赖：pydantic 参数模型。完成：名称、描述、参数模型（Schema 由它生成）。
@dataclass
class ToolSpec:
    name: str  # 工具名
    description: str  # 何时用、何时别用、一个例子（spec §5.4）
    params: type[BaseModel]  # 参数模型


# 功能：一次工具调用（内部格式）。依赖：无。完成：ID、工具名、参数字典（已从字符串解析）。
@dataclass
class ToolCall:
    id: str  # 调用 ID
    name: str  # 工具名
    arguments: dict  # 参数


# 功能：一次模型响应（内部格式）。依赖：无。完成：正文、工具调用、用量、结束原因、原始响应。
@dataclass
class ModelResponse:
    text: str  # 正文（可为空）
    tool_calls: list[ToolCall]  # 工具调用（可为空）
    input_tokens: int  # 输入 token
    output_tokens: int  # 输出 token
    finish_reason: str | None  # 结束原因
    raw: dict  # 原始响应（记录用）
    parse_errors: list[str] = field(default_factory=list)  # 解析阶段发现的问题（如参数不是合法 JSON）


class ModelUnavailable(RuntimeError):
    """传输层失败：超时、限流、上游错误、凭证缺失。kind 取 timeout / rate_limited / upstream / config。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)  # 基类消息
        self.kind = kind  # 失败类型


class ModelFailure(RuntimeError):
    """格式失败且修复、降级都没救回来（spec §5.8：按普通失败交回上层）。errors 为最后一次的逐条错误。"""

    def __init__(self, message: str, errors: list[str]):
        super().__init__(message)  # 基类消息
        self.errors = errors  # 最后一次错误

"""文件摘要（F07 · 统一返回格式）

所有工具调用都返回同一种结构（spec §5.4）：{ok, data, truncated, next_cursor, source, warnings, error}。
- ERRORS：九种错误类型各自的"能否重试"与给模型的处理提示（处置策略见 spec §5.8，重试本身归 F15）。
- ok_result / error_result：构造成功与失败结果。
依赖：无。被 tools.proxy 与各 Server 使用；F09/F14 把它交给模型或转成反馈卡片。
"""
from __future__ import annotations  # 类型注解延迟求值

#: 错误类型 → (能否重试, 给模型的处理提示)。
ERRORS = {
    "param_error": (False, "参数不合法，按错误说明修正参数后再调用"),  # 参数错误
    "not_found": (False, "查不到，换条件或确认编号，不要编造结果"),  # 查不到
    "rate_limited": (True, "该来源暂时限流，可稍后重试或先用其他来源"),  # 限流
    "timeout": (True, "调用超时，可稍后重试"),  # 超时
    "upstream_error": (True, "上游服务出错，可稍后重试"),  # 上游错误
    "bad_format": (False, "返回格式异常，无法使用该来源这次的结果，不要自行推测"),  # 返回格式异常
    "forbidden": (False, "无权限调用，不可重试；请走合法路径"),  # 无权限
    "gate_rejected": (False, "门控拒绝，见反馈卡片"),  # 门控拒绝
    "over_budget": (False, "超出预算，不可重试"),  # 超预算
}


# 功能：构造成功结果。依赖：无。完成：字段齐全，便于模型与代码按固定结构读。
def ok_result(data, *, source: dict, warnings: list[str] | None = None,
              truncated: bool = False, next_cursor: str | None = None) -> dict:
    return {"ok": True, "data": data, "truncated": truncated, "next_cursor": next_cursor,  # 数据与分页
            "source": source, "warnings": warnings or [], "error": None}  # 来源、警告、无错误


# 功能：构造失败结果。依赖：ERRORS。完成：未知类型直接报错，防止拼错类型名。
def error_result(kind: str, message: str, *, source: dict | None = None,
                 warnings: list[str] | None = None) -> dict:
    retryable, hint = ERRORS[kind]  # 查类型（未知类型抛 KeyError）
    return {"ok": False, "data": None, "truncated": False, "next_cursor": None,  # 无数据
            "source": source, "warnings": warnings or [],  # 来源与警告
            "error": {"type": kind, "message": message, "retryable": retryable, "hint": hint}}  # 错误详情

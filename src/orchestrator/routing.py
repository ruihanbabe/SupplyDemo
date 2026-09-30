"""文件摘要（F22 · 用户请求路由）

spec §2.1：编排者把请求归入 §1.4 的某一类，由此确定目标类型与可用节点集合；意图不明时反问用户，超出范围时拒绝。
Monitor 开的案子由代码定为 S1（F10），不经这里。
RequestRouter.route(text, day, case_id)：LLM 函数（结构化输出 RouteDecision，走 F08 适配层的校验与修复）归类为
S1 / S3 / unclear / out_of_scope：
- S1 / S3：按该模式开案（开案内容里的用户原话以引用结构存放，F13 不可信），写 case_routed；
- unclear：写 request_clarify，返回配置里的反问；
- out_of_scope：写 request_rejected，返回配置里的拒绝语。
依赖：models.adapter、models.types、session.log、orchestrator.planning（配置）。被 F25 Web 入口与测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from typing import Literal  # 取值

from pydantic import BaseModel, Field  # 结构化输出

from models.types import Message  # 消息
from orchestrator.planning import load_orchestrator_config  # 配置
from session.log import Session  # 事件日志

SYSTEM = ("你只做一件事：把用户请求归类。S1 = 处置缺料或供应异常（会涉及比价、采购）；S3 = 问两个料号能否互换；"  # 路由提示
          "unclear = 看不出是哪一类；out_of_scope = 与缺料处置和料号互换都无关。只输出 JSON。")  # 同上


class RouteDecision(BaseModel):
    """路由结果。"""
    category: Literal["S1", "S3", "unclear", "out_of_scope"]  # 类别
    reason: str = Field(min_length=1)  # 一句理由


class RequestRouter:
    # 功能：见文件摘要。依赖：模型适配层、事件日志、配置。
    def __init__(self, adapter, session: Session, *, config: dict | None = None):
        self.adapter, self.session = adapter, session  # 依赖
        self.config = config or load_orchestrator_config()  # 配置

    def route(self, text: str, *, day: int, case_id: str) -> dict:
        """归类并开案 / 反问 / 拒绝。返回 {category, case_id?, reply?}。"""
        res = self.adapter.call("router", [Message("system", SYSTEM), Message("user", text)],  # LLM 函数
                                output=RouteDecision, day=day, actor="router", case_id=None)  # 结构化输出
        d = res.output  # 结果
        if d.category in ("S1", "S3"):  # 可处理
            self.session.open_case(case_id, day=day, actor="router",  # 按模式开案
                                   payload={"goal": f"用户请求（{d.category}）", "mode": d.category,  # 目标与模式
                                            "request": {"quote": text, "source": "user"}})  # 原话以引用结构存放
            self.session.append("case_routed", day=day, actor="router", case_id=case_id,  # 路由记录
                                payload={"category": d.category, "reason": d.reason})  # 类别与理由
            return {"category": d.category, "case_id": case_id}  # 已开案
        kind, reply = (("request_clarify", self.config["router"]["clarify"]) if d.category == "unclear"  # 反问
                       else ("request_rejected", self.config["router"]["reject"]))  # 拒绝
        self.session.append(kind, day=day, actor="router", payload={"category": d.category, "reason": d.reason,  # 记录
                                                                     "reply": reply})  # 回复
        return {"category": d.category, "reply": reply}  # 不开案

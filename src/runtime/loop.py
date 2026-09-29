"""文件摘要（F09 · 执行者循环）

ExecutorLoop.run() 驱动一个执行者（子 Agent 或编排者）：模型调用（F08）→ 工具调用（F07 代理层）→ 结果回给模型 → 直到
给出最终输出、预算触顶、被要求停止或失败（spec §5.3）。
- 预算：轮数上限、工具调用上限；触顶以 unfinished 结束并写明原因。
- 循环检测：相同工具 + 相同参数连续第 2 次不再调用，回"已查过，结果是……"；再犯即强制结束（消融开关 loop_detection）。
- 挂点：after_round（每轮工具调用处理完后调用，返回要追加给模型的消息，编排者用它做批次汇合）；should_stop（编排者 end_segment）。
- 格式失败（ModelFailure）以 failed 结束；传输失败（ModelUnavailable）向上抛（F15 重试熔断）。
依赖：models.adapter、models.types、tools.proxy、session.log.canonical。被 runtime.dispatch（子 Agent）与 runtime.segment（编排者）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 工具结果转文本
from collections.abc import Callable  # 挂点类型
from dataclasses import dataclass  # 结果数据类

from pydantic import BaseModel  # 输出模型

from models.adapter import ModelAdapter  # 模型适配层
from models.types import Message, ModelFailure, ToolSpec  # 内部格式与失败
from session.log import canonical  # 规范化 JSON（循环检测比较参数）
from tools.proxy import ToolProxy  # 工具代理层

ALREADY = "已查过，结果是：{result}\n不要重复同样的调用；换参数或进入下一步。"  # 循环检测反馈（spec §5.3）


# 功能：一次执行者循环的结果。依赖：无。完成：状态、输出、原因、计数、最后的正文。
@dataclass
class LoopResult:
    status: str  # done（给出最终输出）/ unfinished（预算触顶、循环、被停止前未完成）/ failed（格式失败）
    output: BaseModel | None  # 结构化输出（done 且要求输出时）
    reason: str  # 结束原因
    rounds: int  # 用掉的轮数
    tool_calls: int  # 用掉的工具调用数
    text: str = ""  # 最后一次模型正文


class ExecutorLoop:
    # 功能：绑定模型适配层与工具代理层。依赖：F08、F07。完成：循环检测开关。
    def __init__(self, adapter: ModelAdapter, proxy: ToolProxy, *, loop_detection: bool = True):
        self.adapter = adapter  # 模型
        self.proxy = proxy  # 工具
        self.loop_detection = loop_detection  # 循环检测开关

    # -----------------------------------------------------------------------
    # run：跑一个执行者
    # 功能：见文件摘要。
    # 依赖：adapter.call、proxy.call；工具定义从代理层取（参数模型给 F08 做校验）。
    # 完成：executor 用于选模型链（F08）和工具白名单（F07）；actor 是 Runtime 标注的身份（不信模型自报）。
    # -----------------------------------------------------------------------
    def run(self, *, executor: str, actor: str, system: str, user: str, tools: list[str],
            output: type[BaseModel] | None, max_rounds: int, max_tool_calls: int, day: int,
            case_id: str | None, known_mpns: set[str] | None = None,
            after_round: Callable[[], list[Message]] | None = None,
            should_stop: Callable[[], bool] | None = None) -> LoopResult:
        specs = [ToolSpec(n, self.proxy.tools[n].description, self.proxy.tools[n].params)  # 工具定义
                 for n in tools]  # 只给本执行者的工具
        messages = [Message("system", system), Message("user", user)]  # 初始对话（从日志与 Artifact 现组装）
        used_calls, last_key, last_result, strikes = 0, None, None, 0  # 计数与循环检测状态
        text = ""  # 最后正文
        for round_ in range(1, max_rounds + 1):  # 轮数预算
            try:  # 调模型
                res = self.adapter.call(executor, messages, tools=specs, output=output,  # F08
                                        known_mpns=known_mpns, case_id=case_id, day=day, actor=actor)  # 身份
            except ModelFailure as exc:  # 格式失败且降级也失败
                return LoopResult("failed", None, f"模型输出格式失败：{exc}", round_, used_calls)  # 普通失败
            text = res.response.text  # 正文
            if not res.response.tool_calls:  # 没有工具调用：给出最终结果
                return LoopResult("done", res.output, "完成", round_, used_calls, text)  # 完成
            messages.append(Message("assistant", text, tool_calls=res.response.tool_calls))  # 记助手消息
            for call in res.response.tool_calls:  # 逐个工具调用
                key = (call.name, canonical(call.arguments))  # 循环检测键
                if self.loop_detection and key == last_key:  # 与上一次完全相同
                    strikes += 1  # 犯规次数
                    if strikes >= 2:  # 再犯
                        return LoopResult("unfinished", None, f"循环检测：连续重复调用 {call.name}，强制结束",
                                          round_, used_calls, text)  # 强制结束
                    content = ALREADY.format(result=last_result)  # 反馈"已查过"
                else:  # 正常调用
                    if used_calls >= max_tool_calls:  # 工具调用预算
                        return LoopResult("unfinished", None, f"工具调用预算触顶（{max_tool_calls} 次）",
                                          round_, used_calls, text)  # 触顶
                    used_calls += 1  # 计数
                    strikes = 0 if key != last_key else strikes  # 换了调用就清零
                    result = self.proxy.call(call.name, call.arguments, executor=executor, actor=actor,  # F07
                                             day=day, case_id=case_id)  # 模拟日与案子
                    content = json.dumps(result, ensure_ascii=False)  # 统一返回格式转文本
                    last_key, last_result = key, content  # 记住本次
                messages.append(Message("tool", content, tool_call_id=call.id))  # 工具结果
            if after_round is not None:  # 挂点：批次汇合等
                messages += after_round()  # 追加消息
            if should_stop is not None and should_stop():  # 挂点：被要求停止（end_segment）
                return LoopResult("done", None, "执行者结束本段", round_, used_calls, text)  # 结束
        return LoopResult("unfinished", None, f"轮数预算触顶（{max_rounds} 轮）", max_rounds, used_calls, text)  # 触顶

"""文件摘要（F18 · 测试注入计划）

spec §5.12"故障注入挂点"：系统层故障只作为测试注入；注入写在情景 YAML 的 faults 段，每次注入记为事件并标明"测试注入"。
规则字段：hook（tool / model / runtime）、kind（注入类型）、匹配条件（tool、distributor、endpoint、point、day）、nth（第 N 次匹配时才注入，
缺省每次都注入）。每条规则各自计数：其余条件都匹配时计数 +1，计数 == nth 时注入。
- tool_fault(tool, args, day)：接 F07 代理层 faults 挂点（在 F15 重试循环内逐次判断），返回 timeout / rate_limited / upstream_error / bad_format。
- wrap_transport(name, transport)：包模型传输；bad_schema 返回一个不存在的工具调用（校验必不过，触发 F08 修复），timeout 抛 ModelUnavailable。
- kill_point(point, **ctx)：接 F15 采购执行器 on_point 挂点（after_intent：意图之后、调用环境之前），命中即抛 ProcessKilled。
- from_yaml(session, path)：读情景 YAML 的 faults 段。
每次注入写 fault_injected 事件（hook、kind、规则序号、细节、test_injection=True）。
依赖：models.types、session.log、PyYAML。被 Eval 运行器（F27）、测试与演示使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from pathlib import Path  # 情景文件

import yaml  # 读情景

from models.types import ModelUnavailable  # 模型传输失败
from session.log import Session  # 事件日志

TOOL_KINDS = {"timeout", "rate_limited", "upstream_error", "bad_format"}  # 工具代理层可注入
MODEL_KINDS = {"bad_schema", "timeout"}  # 模型适配层可注入
RUNTIME_KINDS = {"kill"}  # Runtime 可注入


class ProcessKilled(BaseException):
    """测试注入的"进程被杀"（BaseException：业务代码的 except Exception 不会吞掉它）。"""


class FaultPlan:
    # 功能：见文件摘要。依赖：session（记事件）、规则列表。完成：规则类型不对立即报错，避免情景写错后静默不注入。
    def __init__(self, session: Session, rules: list[dict]):
        allowed = {"tool": TOOL_KINDS, "model": MODEL_KINDS, "runtime": RUNTIME_KINDS}  # 各挂点可注入类型
        for i, r in enumerate(rules):  # 校验
            if r.get("hook") not in allowed or r.get("kind") not in allowed[r["hook"]]:  # 挂点或类型不对
                raise ValueError(f"第 {i} 条注入规则不合法：{r}")  # 报错
        self.session, self.rules = session, list(rules)  # 事件日志与规则
        self.counts = [0] * len(self.rules)  # 每条规则的匹配计数

    @classmethod
    def from_yaml(cls, session: Session, path: Path) -> FaultPlan:
        """读情景 YAML 的 faults 段（没有即空计划）。"""
        spec = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}  # 情景
        return cls(session, spec.get("faults", []))  # 计划

    def _fire(self, hook: str, day: int | None, detail: dict, **ctx) -> dict | None:
        """找第一条命中的规则：条件都匹配则计数 +1，计数 == nth（或没写 nth）即注入并记事件。"""
        for i, r in enumerate(self.rules):  # 每条规则
            if r["hook"] != hook:  # 挂点不同
                continue  # 下一条
            if any(k in r and r[k] != ctx.get(k) for k in ("tool", "endpoint", "point")):  # 名称条件
                continue  # 不匹配
            if "day" in r and r["day"] != day:  # 模拟日条件
                continue  # 不匹配
            if "distributor" in r and r["distributor"] not in (ctx.get("distributors") or []):  # 分销商条件
                continue  # 不匹配
            self.counts[i] += 1  # 计数
            if r.get("nth") is not None and self.counts[i] != r["nth"]:  # 还不到第 N 次
                continue  # 不注入
            last = self.session.store.last_event(self.session.run_id)  # 取当前模拟日（模型挂点没有日参数）
            self.session.append("fault_injected", day=day if day is not None else (last["day"] if last else 0),  # 事件
                                actor="fault_plan", payload={"hook": hook, "kind": r["kind"], "rule": i,  # 挂点与类型
                                                             "detail": detail, "test_injection": True})  # 标明测试注入
            return r  # 命中
        return None  # 不注入

    def tool_fault(self, tool: str, args: dict, day: int) -> str | None:
        """工具代理层挂点（签名同 F07 faults）。"""
        r = self._fire("tool", day, {"tool": tool}, tool=tool, distributors=args.get("distributors",  # 匹配
                                                                                     ["digikey", "mouser", "element14"]))
        return r["kind"] if r else None  # 注入类型

    def wrap_transport(self, name: str, transport) -> FaultyTransport:
        """模型适配层挂点：包一层传输。"""
        return FaultyTransport(self, name, transport)  # 包装

    def kill_point(self, point: str, **ctx) -> None:
        """Runtime 挂点：命中即杀进程。"""
        if self._fire("runtime", ctx.get("day"), {"point": point, **{k: v for k, v in ctx.items() if k != "day"}},
                      point=point):  # 命中
            raise ProcessKilled(f"测试注入：在 {point} 杀进程")  # 模拟进程被杀


class FaultyTransport:
    """带注入的模型传输：命中 bad_schema 返回校验必不过的应答，命中 timeout 抛超时；否则交给原传输。"""

    def __init__(self, plan: FaultPlan, name: str, inner):
        self.plan, self.name, self.inner = plan, name, inner  # 计划、端点名、原传输

    def send(self, endpoint: dict, body: dict) -> dict:
        r = self.plan._fire("model", None, {"endpoint": self.name}, endpoint=self.name)  # 是否注入
        if r is None:  # 不注入
            return self.inner.send(endpoint, body)  # 原传输
        if r["kind"] == "timeout":  # 超时
            raise ModelUnavailable("timeout", "测试注入：模型端点超时")  # 传输失败
        call = {"id": "c1", "type": "function", "function": {"name": "__injected_invalid__", "arguments": "{}"}}  # 不存在的工具
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]},  # 应答
                             "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

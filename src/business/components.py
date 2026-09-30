"""文件摘要（F21 · 把确定性组件注册为 Runtime 的代码执行者）

code_executors() 返回 {执行者名: ExecutorSpec}，并入 F09 Dispatcher 的注册表后，编排者可用 dispatch_task 派给它们：
- impact_analysis → ImpactReport：params.scope 可选；依赖外部引用 env:<运行>（版本 = 模拟日，有效期 = config/business.yaml impact.valid_days）。
- sourcing → SourcingQuote：params.demands 或从输入 ImpactReport 的缺口生成需求列表；依赖各报价（F06 失效传播与过期）；
  经代理层查报价时用 Dispatcher 签给它的子令牌 ctx["token"]（F11）。
- policy → ProcurementDecision：输入一个 SourcingQuote；规则来自 config/policy.yaml。
依赖：business.impact / decide、runtime.dispatch（ExecutorSpec、CodeResult）。被装配 Runtime 的代码（F22、测试）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Callable  # 取值函数类型

from business.decide import decide, load_yaml, sourcing  # 比价与决策
from business.impact import analyze  # 影响分析
from runtime.dispatch import CodeResult, ExecutorSpec  # 执行者注册与返回
from simenv.api import Environment  # 环境


def _first(inputs: list[dict], kind: str) -> dict | None:
    """取输入中第一个指定类型的 Artifact。"""
    return next((a for a in inputs if a["kind"] == kind), None)  # 第一个


# 功能：见文件摘要。依赖：当前环境取值函数、工具代理层、运行号（环境外部引用用）。
# 完成：policy / business 配置默认从 config/ 读，可注入（测试）。
def code_executors(get_env: Callable[[], Environment], proxy, *, run_id: str,
                   policy: dict | None = None, business: dict | None = None) -> dict[str, ExecutorSpec]:
    policy = policy or load_yaml("policy.yaml")  # Policy 配置
    business = business or load_yaml("business.yaml")  # 业务参数

    def impact(task, inputs, ctx):  # 影响分析
        state = get_env().state  # 当前环境状态
        content = analyze(state, ctx["params"].get("scope"))  # 计算
        dep = {"external": f"env:{run_id}", "version": f"day{state.day}",  # 依赖当日环境状态
               "valid_until_day": state.day + business["impact"]["valid_days"]}  # 有效期
        return CodeResult(content, [dep])  # 内容与依赖

    def quote(task, inputs, ctx):  # 比价
        demands = ctx["params"].get("demands")  # 显式需求列表
        report = _first(inputs, "ImpactReport")  # 或来自影响分析
        if demands is None and report is not None:  # 从缺口生成
            demands = [{"mpn": s["mpn"], "qty": s["gap"]} for s in report["content"]["shortages"]]  # 需求列表
        if not demands:  # 没有需求
            return CodeResult({"day": ctx["day"], "items": []}, status="unfinished", reason="没有需求列表")  # 未完成
        content, deps = sourcing(proxy, demands, distributors=get_env().state.distributors,  # 查报价
                                 day=ctx["day"], token=ctx["token"])  # F11：凭子令牌
        return CodeResult(content, deps)  # 内容与依赖

    def policy_decide(task, inputs, ctx):  # 策略决策
        q = _first(inputs, "SourcingQuote")  # 比价结果
        if q is None:  # 没给
            return CodeResult({"decisions": [], "feasible": False}, status="unfinished", reason="需要 SourcingQuote 输入")
        return CodeResult(decide(q["content"], policy))  # 决策（依赖由 Dispatcher 按输入加上）

    return {"impact_analysis": ExecutorSpec("impact_analysis", "ImpactReport", code=impact),  # 影响分析
            "sourcing": ExecutorSpec("sourcing", "SourcingQuote", code=quote),  # 比价
            "policy": ExecutorSpec("policy", "ProcurementDecision", code=policy_decide)}  # 决策

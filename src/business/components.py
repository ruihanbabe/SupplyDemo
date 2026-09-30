"""文件摘要（F21 · 把确定性组件注册为 Runtime 的代码执行者）

code_executors() 返回 {执行者名: ExecutorSpec}，并入 F09 Dispatcher 的注册表后，编排者可用 dispatch_task 派给它们：
- impact_analysis → ImpactReport：params.scope 可选；依赖外部引用 env:<运行>（版本 = 模拟日，有效期 = config/business.yaml impact.valid_days）。
- sourcing → SourcingQuote：params.demands 或从输入 ImpactReport 的缺口生成需求列表；依赖各报价（F06 失效传播与过期）；
  经代理层查报价时用 Dispatcher 签给它的子令牌 ctx["token"]（F11）。
- policy → ProcurementDecision：输入一个 SourcingQuote；规则来自 config/policy.yaml。
F13 打标（CodeResult.taint）：影响分析全部可信；比价的报价行为外部结构化，需求料号 / 数量继承 ImpactReport 的标签，
来自编排者 params 时料号在环境目录中可查为已校验、否则不可信，数量一律不可信（LLM 给的数值，G4）；
决策先过 Policy 汇点（打分字段不得不可信，否则"未完成"），打分可信、选中报价与料号 / 数量继承比价的标签。
F10：决策产出时由 ClaimBook 为选中报价登记软声明（A9，传入 claims 才登记）。
依赖：business.impact / decide、runtime.dispatch（ExecutorSpec、CodeResult）、taint.labels / sinks、cases.claims。被装配 Runtime 的代码（F22、测试）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Callable  # 取值函数类型

from business.decide import decide, load_yaml, sourcing  # 比价与决策
from business.impact import analyze  # 影响分析
from runtime.dispatch import CodeResult, ExecutorSpec  # 执行者注册与返回
from simenv.api import Environment  # 环境
from taint.labels import (  # F13 标签
    ALL_TRUSTED,
    EXTERNAL,
    TRUSTED,
    UNTRUSTED,
    VERIFIED,
    label_of,
    taint_of,
)
from taint.sinks import TaintSinks  # F13 汇点


def _first(inputs: list[dict], kind: str) -> dict | None:
    """取输入中第一个指定类型的 Artifact。"""
    return next((a for a in inputs if a["kind"] == kind), None)  # 第一个


# 功能：见文件摘要。依赖：当前环境取值函数、工具代理层、运行号（环境外部引用用）、F13 汇点。
# 完成：policy / business 配置默认从 config/ 读，可注入（测试）；sinks 缺省按 config/taint.yaml 开启；
#       claims 为 F10 ClaimBook（缺省不登记）。
def code_executors(get_env: Callable[[], Environment], proxy, *, run_id: str,
                   policy: dict | None = None, business: dict | None = None,
                   sinks: TaintSinks | None = None, claims=None) -> dict[str, ExecutorSpec]:
    sinks = sinks or TaintSinks()  # F13 汇点检查
    policy = policy or load_yaml("policy.yaml")  # Policy 配置
    business = business or load_yaml("business.yaml")  # 业务参数

    def impact(task, inputs, ctx):  # 影响分析
        state = get_env().state  # 当前环境状态
        content = analyze(state, ctx["params"].get("scope"))  # 计算
        dep = {"external": f"env:{run_id}", "version": f"day{state.day}",  # 依赖当日环境状态
               "valid_until_day": state.day + business["impact"]["valid_days"]}  # 有效期
        return CodeResult(content, [dep], taint=ALL_TRUSTED)  # 内容、依赖、全部可信（F13）

    def quote(task, inputs, ctx):  # 比价
        demands = ctx["params"].get("demands")  # 显式需求列表（编排者给的）
        report = _first(inputs, "ImpactReport")  # 或来自影响分析
        labels = []  # F13：每条需求的 (料号标签, 数量标签)
        if demands is None and report is not None:  # 从缺口生成
            demands = [{"mpn": s["mpn"], "qty": s["gap"]} for s in report["content"]["shortages"]]  # 需求列表
            rt = taint_of(report)  # 影响分析的标签
            labels = [(label_of(rt, f"shortages[{k}].mpn"), label_of(rt, f"shortages[{k}].gap"))  # 继承
                      for k in range(len(demands))]  # 逐条
        elif demands:  # 编排者手填：料号查目录，数量不可信
            known = set(get_env().state.on_hand)  # 环境目录（全部料号）
            labels = [(VERIFIED if d.get("mpn") in known else UNTRUSTED, UNTRUSTED) for d in demands]  # 逐条
        if not demands:  # 没有需求
            return CodeResult({"day": ctx["day"], "items": []}, status="unfinished", reason="没有需求列表")  # 未完成
        content, deps = sourcing(proxy, demands, distributors=get_env().state.distributors,  # 查报价
                                 day=ctx["day"], token=ctx["token"])  # F11：凭子令牌
        taint = {"*": TRUSTED, "items[].offers[]": EXTERNAL}  # F13：报价行为外部结构化，其余代码算出
        for i, (lm, lq) in enumerate(labels):  # 每条需求
            taint[f"items[{i}].mpn"], taint[f"items[{i}].qty"] = lm, lq  # 料号与数量的来源标签
        return CodeResult(content, deps, taint=taint)  # 内容、依赖、标签

    def policy_decide(task, inputs, ctx):  # 策略决策
        q = _first(inputs, "SourcingQuote")  # 比价结果
        if q is None:  # 没给
            return CodeResult({"decisions": [], "feasible": False}, status="unfinished", reason="需要 SourcingQuote 输入")
        blocked = sinks.policy_inputs(q)  # F13 Policy 汇点：打分字段不得不可信
        if blocked:  # 有不可信字段
            return CodeResult({"decisions": [], "feasible": False, "blocked": blocked},  # 不出决策
                              status="unfinished", reason="；".join(blocked), taint=ALL_TRUSTED)  # 原因
        content = decide(q["content"], policy)  # 决策（依赖由 Dispatcher 按输入加上）
        if claims is not None:  # F10 软声明（A9）
            claims.register(ctx["case_id"], content, day=ctx["day"])  # 登记选中的供应
        qt = taint_of(q)  # 比价的标签
        taint = {"*": TRUSTED}  # 打分、过滤、理由为代码算出
        for i, d in enumerate(content["decisions"]):  # 每条决策（与比价的需求一一对应）
            taint[f"decisions[{i}].mpn"] = label_of(qt, f"items[{i}].mpn")  # 料号继承
            taint[f"decisions[{i}].qty"] = label_of(qt, f"items[{i}].qty")  # 数量继承
            offers = q["content"]["items"][i]["offers"]  # 该需求的报价
            j = next((k for k, o in enumerate(offers) if d["chosen"] and o["distributor"] == d["chosen"]["distributor"]),
                     None)  # 选中的是第几家
            if j is not None:  # 有选择
                taint[f"decisions[{i}].chosen"] = label_of(qt, f"items[{i}].offers[{j}]")  # 选中报价继承
        return CodeResult(content, taint=taint)  # 决策与标签

    return {"impact_analysis": ExecutorSpec("impact_analysis", "ImpactReport", code=impact),  # 影响分析
            "sourcing": ExecutorSpec("sourcing", "SourcingQuote", code=quote),  # 比价
            "policy": ExecutorSpec("policy", "ProcurementDecision", code=policy_decide)}  # 决策

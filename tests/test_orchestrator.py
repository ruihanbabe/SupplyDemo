"""F22：编排者与 Runtime 装配测试。全部离线：种子 42 环境、固定样例报价与 datasheet、脚本模型（不做任何真实调用）。"""
import json  # 结构化输出
from pathlib import Path  # 路径

from kit import MPN, call, end, tok  # 共用装配

from datasheets.fetch import FixtureSource  # datasheet 样例
from models.providers import ScriptedTransport, reply  # 脚本模型
from orchestrator.assembly import build_runtime  # 装配
from orchestrator.planning import GoalDesk, validate_plan  # 计划与目标
from orchestrator.routing import RequestRouter  # 路由
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from simenv.api import Environment  # 环境
from simenv.bom import load_boms  # BOM
from simenv.params import load_params  # 参数
from tools.proxy import load_tools_config  # 工具配置

FIX = Path(__file__).parent / "fixtures" / "datasheets"  # datasheet 样例


# 功能：装配一套 Runtime（配额不限：令牌桶按真实时间，测试里连续调用会被限流）。完成：返回 (runtime, transport)。
def runtime(script=(), **switches):
    cfg = load_tools_config()  # 工具配置
    cfg["providers"] = {k: {"daily_limit": None, "rate_per_s": None, "burst": None} for k in cfg["providers"]}
    transport = ScriptedTransport(list(script))  # 脚本模型
    s = Session(MemorySessionStore(), "run", clock=lambda: "t")  # 事件日志
    rt = build_runtime(env=Environment.create(load_params(), load_boms(), 42), session=s,  # 装配
                       transports={"glm-flash": transport}, tools_config=cfg, switches={"model_fallback": False,
                                                                                        **switches},
                       datasheet_source=FixtureSource(FIX), sleep=lambda x: None, vault_key=b"k" * 32,
                       seeds={"env": 42})  # 样例、不真睡、固定密钥
    return rt, transport  # 返回


def use(rt, name, mode="S1", **args):
    """以编排者身份直接调工具（给定模式）。"""
    return rt.proxy.call(name, args, token=tok(rt.proxy, "orchestrator", mode=mode), day=0)  # 调用


# 功能：合并工具——env_lookup 与原查询结果一致；po_action 缺参数给原因、按动作走同一门控；S1 编排者 8 个工具、S3 看不到 po_action。
def test_merged_tools_and_toolsets():
    rt, _ = runtime()  # 装配
    a = use(rt, "env_lookup", kind="inventory", mpns=[MPN])["data"]  # 合并查询
    assert a == use(rt, "env_inventory", mpns=[MPN])["data"]  # 同一实现
    wo = use(rt, "env_lookup", kind="work_orders", board="hackrf-one")["data"]  # 按板查工单
    assert wo and all(w["board"] == "hackrf-one" for w in wo)  # 结果
    r = use(rt, "po_action", action="draft")  # 缺参数
    assert r["data"]["ok"] is False and "decision" in r["data"]["reasons"][0]  # 原因
    r = use(rt, "po_action", action="submit", draft="PODraft:X@v1")  # 不存在的草稿
    assert r["data"]["ok"] is False and rt.session.events(type="gate_rejected")  # 门控照旧
    tools = rt.segments.tools_by_mode  # 编排者工具集
    assert len(tools["S1"]) == 8 and "po_action" in tools["S1"] and len(tools["S3"]) == 6  # ≤ 8
    assert not {"po_action", "request_human"} & set(tools["S3"])  # S3 只有 R0
    vis = set(rt.proxy.guard.visible(tok(rt.proxy, "orchestrator", mode="S3"), rt.proxy.tools))  # S3 可见
    assert "po_action" not in vis and set(tools["S3"]) <= vis  # S3 看不到写工具


# 功能：模式节点集合——S3 案子派比价节点被拒（硬边界）；编排者收到的工具表就是该模式的工具集。
def test_mode_node_sets():
    script = [reply(tool_calls=[call("dispatch_task", executor="sourcing", task="比价", reason="r")]), end()]
    rt, t = runtime(script)  # 装配
    rt.session.open_case("Q1", day=0, actor="router", payload={"goal": "A 能替代 B 吗", "mode": "S3"})  # S3 案子
    rt.segments.run("Q1", reason="开案", day=0)  # 跑一段
    result = json.loads(t.requests[1]["messages"][-1]["content"])  # 派发结果（回给模型的工具结果）
    assert result["data"]["accepted"] is False and "S3 模式不能使用节点" in result["data"]["error"]  # 拒绝
    names = [x["function"]["name"] for x in t.requests[0]["tools"]]  # 模型看到的工具
    assert sorted(names) == sorted(rt.segments.tools_by_mode["S3"])  # 就是 S3 工具集


# 功能：用户请求路由——S1 / S3 按模式开案（原话以引用结构存放）；意图不明反问、超出范围拒绝，都不开案。
def test_request_routing():
    def decision(cat):  # 路由结果
        return reply(json.dumps({"category": cat, "reason": "r"}))  # 结构化输出

    rt, _ = runtime([decision("S3"), decision("unclear"), decision("out_of_scope")])  # 装配
    router = RequestRouter(rt.adapter, rt.session)  # 路由
    out = router.route("SKY13453 能用 CAND-RF-01 替代吗？", day=0, case_id="Q1")  # S3
    assert out == {"category": "S3", "case_id": "Q1"}  # 开案
    opened = rt.session.events(type="case_opened")[0]["payload"]  # 开案内容
    assert opened["mode"] == "S3" and opened["request"]["source"] == "user"  # 模式与原话
    assert "还是问两个料号" in router.route("帮我看看", day=0, case_id="Q2")["reply"]  # 反问
    assert "超出本系统范围" in router.route("写首诗", day=0, case_id="Q3")["reply"]  # 拒绝
    assert [c["case_id"] for c in rt.session.cases()] == ["Q1"]  # 只开了一个案子


# 功能：计划校验——未知节点、依赖不存在、成环、超预算逐条给原因；连续被拒两次退回固定模板；关 plan_validation 不校验。
def test_plan_validation_and_fallback():
    cfg = {"modes": {"S1": {"nodes": ["impact_analysis", "sourcing", "verifier"]}},
           "plan": {"node_cost": {"verifier": 8}}}  # 小配置
    bad = [{"id": "T1", "executor": "magic", "task": "x", "depends_on": ["T9"], "reason": "r"},  # 未知节点、依赖不存在
           {"id": "T2", "executor": "sourcing", "task": "x", "depends_on": ["T3"], "reason": "r"},  # 成环
           {"id": "T3", "executor": "verifier", "task": "x", "depends_on": ["T2"], "reason": "r"}]  # 同上
    reasons = validate_plan(bad, mode="S1", registry={"impact_analysis", "sourcing"}, config=cfg, remaining_llm=5)
    text = "；".join(reasons)  # 合并
    for want in ("magic", "T9", "verifier 尚未接入", "成环", "超过案子剩余预算"):  # 各条原因
        assert want in text, want  # 写明
    rt, _ = runtime()  # 装配
    rt.session.open_case("K9", day=0, actor="runtime", payload={"goal": "g", "mode": "S1"})  # 案子
    token = tok(rt.proxy, "orchestrator", case_id="K9")  # 编排者身份
    wrong = [{"id": "T1", "executor": "reschedule", "task": "x", "depends_on": [], "inputs": [], "reason": "r"}]  # 不在节点集合
    first = rt.segments.planner.write(token, wrong, day=0)  # 第 1 次
    assert first["accepted"] is False and "fallback" not in first  # 拒绝
    second = rt.segments.planner.write(token, wrong, day=0)  # 第 2 次：退回模板
    assert second["fallback"] == "Plan:K9@v1" and rt.session.events(type="plan_fallback")  # 模板计划
    plan = rt.session.artifact_view("Plan:K9", 1)["content"]  # 计划内容
    assert plan["source"] == "template" and [t["executor"] for t in plan["tasks"]] == ["impact_analysis", "sourcing",
                                                                                        "policy"]
    off, _ = runtime(plan_validation=False)  # 关校验
    off.session.open_case("K9", day=0, actor="runtime", payload={"goal": "g", "mode": "S1"})  # 案子
    assert off.segments.planner.write(tok(off.proxy, "orchestrator", case_id="K9"), wrong, day=0)["accepted"]  # 不校验


# 功能：两级目标修订——提出后目标不变；采购员确认后卡片显示新目标，且下一次路由按人工应答唤醒案子。
def test_goal_revision_needs_buyer():
    rt, _ = runtime()  # 装配
    rt.session.open_case("K9", day=0, actor="runtime", payload={"goal": "按原料号补货", "mode": "S1"})  # 案子
    goals = GoalDesk(rt.session)  # 目标修订
    p = goals.propose(case_id="K9", day=0, actor="orchestrator@K9", new_goal="改为替代料采购", reason="原料三家无货")
    assert goals.current_goal("K9") == "按原料号补货"  # 未确认前不变
    assert rt.session.events(type="human_request")[-1]["payload"]["node"] == "H4"  # 通知采购员
    goals.confirm(p["proposal_seq"], by="buyer", day=1)  # 确认
    assert goals.current_goal("K9") == "改为替代料采购"  # 生效
    _, user = rt.segments.context_builder(rt.session, "K9", "目标修订生效")  # 编排者上下文
    assert "目标：改为替代料采购" in user  # 卡片更新
    env_day = rt.session.append("env_day", day=1, actor="env", payload={})  # 当天的环境事件
    assert ("K9", "人工应答：human_decision") in rt.router(env_day)  # 人工应答唤醒


# 功能：固定模板驱动器（对照组）——A1 缺料：影响分析 → 比价 → 决策 → 逐料草稿 → 送审，不用 LLM。
def test_template_driver_runs_a1():
    rt, _ = runtime()  # 装配
    rt.env.advance()  # 第 1 天
    rt.session.open_case("K1", day=1, actor="monitor", payload={"goal": "处置缺料", "mode": "S1"})  # 案子（不唤醒编排者）
    out = rt.template_driver().run("K1", day=1)  # 模板
    assert out["skipped"] == [] and set(out["artifacts"]) == {"T1", "T2", "T3"}  # 三个代码节点都跑了
    assert out["drafts"] and out["approvals"] == out["drafts"]  # 可行料都建了草稿并送审
    assert rt.session.artifact_view("Plan:K1", 1)["content"]["source"] == "template"  # 模板计划


# 功能：装配出的 Runtime 端到端（脚本模型扮演编排者）：Monitor 开案 → 写计划 → 依次派三个代码节点 → po_action 建草稿、送审 →
#       结束本段等审批；计划任务状态投影进上下文；启动时恢复与运行清单都在。
def test_end_to_end_with_scripted_orchestrator():
    side, _ = runtime()  # 旁路：算出第 1 天第一颗可行料（环境与报价都确定）
    side.env.advance()  # 第 1 天（与下面的 Monitor 开案同一局面）
    side.session.open_case("K1", day=1, actor="monitor", payload={"goal": "处置缺料", "mode": "S1"})  # 案子
    mpn = side.template_driver().run("K1", day=1)["drafts"][0].split(":")[2].rsplit("@v", 1)[0]  # 料号
    plan = [{"id": "T1", "executor": "impact_analysis", "task": "影响分析", "reason": "算缺口"},
            {"id": "T2", "executor": "sourcing", "task": "比价", "depends_on": ["T1"], "reason": "查报价"},
            {"id": "T3", "executor": "policy", "task": "决策", "depends_on": ["T2"], "reason": "按规则选"}]
    script = [reply(tool_calls=[call("write_plan", tasks=plan)]),  # 写计划
              reply(tool_calls=[call("dispatch_task", executor="impact_analysis", task="影响分析", reason="算缺口",
                                     plan_task="T1")]),  # 派 T1
              reply(tool_calls=[call("dispatch_task", executor="sourcing", task="比价", reason="查报价",
                                     inputs=["ImpactReport:K1-T1@v1"], plan_task="T2")]),  # 派 T2
              reply(tool_calls=[call("dispatch_task", executor="policy", task="决策", reason="按规则选",
                                     inputs=["SourcingQuote:K1-T2@v1"], plan_task="T3")]),  # 派 T3
              reply(tool_calls=[call("po_action", action="draft", decision="ProcurementDecision:K1-T3@v1",
                                     mpn=mpn)]),  # 建草稿
              reply(tool_calls=[call("po_action", action="request_approval", draft=f"PODraft:K1:{mpn}@v1")]),  # 送审
              end("human")]  # 等审批
    rt, t = runtime(script)  # 装配
    started = rt.start()  # 启动：两步写恢复 + 运行清单
    assert started["recovered"] == [] and rt.session.events(type="run_manifest")  # 清单
    rt.run_day()  # 第 1 天
    assert rt.session.case_state("K1")["wait_for"] == "human"  # 等审批
    assert rt.session.events(type="approval_requested")[0]["payload"]["draft"] == f"PODraft:K1:{mpn}@v1"  # 送审
    assert [e["payload"]["plan_task"] for e in rt.session.events(type="task_dispatched")] == ["T1", "T2", "T3"]
    _, user = rt.segments.context_builder(rt.session, "K1", "复查")  # 下次唤醒的上下文
    assert "T1 impact_analysis：影响分析" in user and "状态：done" in user and "（依赖 T1）" in user  # 计划与状态
    assert not t.script  # 脚本恰好用完

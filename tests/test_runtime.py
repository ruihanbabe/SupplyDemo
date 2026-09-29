"""F09：Runtime 离线测试。装配见 tests/kit.py（模型全部用 ScriptedTransport，不产生任何真实调用）。"""
import pytest  # 测试框架
from kit import MPN, build, call, count, dispatch, end, inv, open_case  # 共用装配

from models.providers import reply  # 应答构造
from models.types import ModelUnavailable  # 传输失败


# 功能：一轮派 3 个任务只唤醒编排者一次；产出 3 个 Artifact；end_segment 后进入等待人工。
def test_batch_join_wakes_orchestrator_once():
    good = reply('{"note": "ok"}')  # 子 Agent 直接给结论
    s, seg, _, _, _ = build([dispatch(3), good, good, good, end()])  # 脚本
    open_case(s)  # 开案
    out = seg.run("K7", reason="L3 物流延误", day=0)  # 跑一段
    orch_calls = [e for e in s.events(type="llm_call") if e["actor"] == "orchestrator@K7"]  # 编排者调用
    assert len(orch_calls) == 2 and count(s, "batch_joined") == 1  # 只唤醒一次
    assert [a["kind"] for a in s.store.artifacts("run")] == ["SourcingNote"] * 3  # 3 个 Artifact
    assert out["status"] == "waiting" and s.case_state("K7")["wait_for"] == "human"  # 等人工
    assert [e["payload"]["to"] for e in s.events(type="case_transition")] == ["planning", "executing", "waiting"]


# 功能：关掉批次汇合：每完成一个任务唤醒编排者一次（3 个任务 → 编排者多被调 2 次）。
def test_batch_join_ablation():
    good = reply('{"note": "ok"}')  # 子 Agent
    s, seg, _, _, _ = build([dispatch(3), good, inv(1), good, inv(2), good, end()], batch_join=False)  # 脚本
    open_case(s)  # 开案
    seg.run("K7", reason="x", day=0)  # 跑一段
    orch_calls = [e for e in s.events(type="llm_call") if e["actor"] == "orchestrator@K7"]  # 编排者调用
    assert len(orch_calls) == 4 and count(s, "batch_joined") == 3  # 每个任务都唤醒一次


# 功能：子 Agent 轮数与工具调用预算触顶交回"未完成"Artifact 并附原因。
def test_subagent_budgets_return_unfinished():
    q = lambda n: reply(tool_calls=[call("distributor_quotes", mpns=[MPN], qty=n)])
    s, seg, _, _, _ = build([dispatch(1), q(1), q(2), end()],  # 子 Agent 两轮都在查
                            budgets={"subagent": {"max_rounds": 2, "max_tool_calls": 12}})  # 轮数 2
    open_case(s)  # 开案
    seg.run("K7", reason="x", day=0)  # 跑一段
    art = s.store.artifacts("run")[0]  # 产出
    assert art["content"] == {"status": "unfinished", "reason": "轮数预算触顶（2 轮）"} and art["meta"]["status"] == "unfinished"
    s2, seg2, _, _, _ = build([dispatch(1), reply(tool_calls=[call("distributor_quotes", mpns=[MPN], qty=1),  # 一轮两次调用
                                                               call("distributor_quotes", mpns=[MPN], qty=2)]), end()],
                              budgets={"subagent": {"max_rounds": 10, "max_tool_calls": 1}})  # 工具调用 1 次
    open_case(s2)  # 开案
    seg2.run("K7", reason="x", day=0)  # 跑一段
    assert "工具调用预算触顶" in s2.store.artifacts("run")[0]["content"]["reason"]  # 触顶


# 功能：循环检测：第 2 次相同调用回"已查过"不调上游，第 3 次强制结束；关掉后照常调用。
def test_loop_detection():
    same = reply(tool_calls=[call("distributor_quotes", mpns=[MPN], qty=5)])  # 同一调用
    s, seg, _, _, t = build([dispatch(1), same, same, same, end()])  # 连续三次
    open_case(s)  # 开案
    seg.run("K7", reason="x", day=0)  # 跑一段
    assert "循环检测" in s.store.artifacts("run")[0]["content"]["reason"]  # 强制结束
    assert sum(1 for e in s.events(type="tool_call") if e["payload"]["tool"] == "distributor_quotes") == 1  # 只调一次
    assert "已查过" in t.requests[3]["messages"][-1]["content"]  # 第 3 次模型请求里带"已查过"反馈
    s2, seg2, _, _, _ = build([dispatch(1), same, same, same, reply('{"note": "ok"}'), end()], loop_detection=False)
    open_case(s2)  # 开案
    seg2.run("K7", reason="x", day=0)  # 跑一段
    assert sum(1 for e in s2.events(type="tool_call") if e["payload"]["tool"] == "distributor_quotes") == 3  # 照常调用


# 功能：编排者段轮数触顶：结束本段、记录原因、进入等待外部。
def test_orchestrator_segment_budget():
    s, seg, _, _, _ = build([inv(1), inv(2)], budgets={"orchestrator": {"max_rounds_per_segment": 2}})  # 2 轮
    open_case(s)  # 开案
    out = seg.run("K7", reason="x", day=0)  # 跑一段
    assert out["loop_status"] == "unfinished" and "轮数预算触顶" in out["reason"]  # 触顶
    assert s.case_state("K7")["wait_for"] == "external"  # 等外部
    assert s.events(type="segment_ended")[0]["payload"]["reason"] == out["reason"]  # 记录原因


# 功能：案子级预算：LLM 调用达到上限或重规划超 3 次 → 升级给人并关闭。
def test_case_budgets_escalate():
    s, seg, _, _, _ = build([end()], budgets={"case": {"max_replans": 3, "max_llm_calls": 1}})  # LLM 上限 1
    open_case(s)  # 开案
    seg.run("K7", reason="第一次", day=0)  # 用掉 1 次
    assert seg.run("K7", reason="第二次", day=1)["status"] == "escalated"  # 触顶升级
    assert s.case_state("K7")["outcome"] == "escalated"  # 以已升级关闭
    s2, seg2, _, _, _ = build([])  # 不需要模型
    open_case(s2)  # 开案
    s2.transition("K7", "planning", day=0, actor="t")  # 首次规划
    for _ in range(4):  # 4 次重规划
        s2.transition("K7", "executing", day=0, actor="t")  # 执行
        s2.transition("K7", "planning", day=0, actor="t")  # 重规划
    assert "重规划 4 次" in seg2.run("K7", reason="x", day=1)["reason"]  # 升级


# 功能：调度循环：并发上限 1 时第二个新案子排队；第一个关闭后次日补上；定时器到期唤醒；每日推进环境。
def test_scheduler_queue_and_timer():
    s, _, sched, env, _ = build([end("arrival")] * 3, max_active=1)  # 三段：K1、K2、定时器唤醒的 K2
    open_case(s, "K1")  # 案子 1
    open_case(s, "K2")  # 案子 2
    d1 = sched.run_day()  # 第 1 天
    assert d1 == {"day": 1, "ran": ["K1"], "queued": ["K2"]} and env.state.day == 1  # K2 排队
    s.close_case("K1", "resolved", day=1, actor="t")  # K1 关闭腾出空位
    sched.set_timer("K2", 3, "查到货", day=1)  # K2 第 3 天的定时器（K2 开始后才有意义）
    assert sched.run_day()["ran"] == ["K2"]  # 第 2 天 K2 补上
    assert sched.run_day()["ran"] == ["K2"]  # 第 3 天定时器唤醒 K2
    assert s.events(type="timer_fired")[0]["day"] == 3  # 定时器按模拟日触发
    assert len(s.events(type="env_day")) == 3  # 每天推进环境一次


# 功能：段中"崩溃"（模型端点中途不可用，异常抛出进程外）后整段重跑，最终状态与未崩溃一致。
def test_segment_rerun_after_crash_matches_clean_run():
    good = reply('{"note": "ok"}')  # 子 Agent
    clean, seg_c, _, _, _ = build([dispatch(1), good, end()])  # 未崩溃
    open_case(clean)  # 开案
    seg_c.run("K7", reason="x", day=0)  # 跑一段
    crashed, seg_x, _, _, t = build([dispatch(1), ModelUnavailable("upstream", "进程被杀")])  # 子 Agent 调用时崩
    open_case(crashed)  # 开案
    with pytest.raises(ModelUnavailable):  # 崩溃抛出
        seg_x.run("K7", reason="x", day=0)  # 跑一段
    assert crashed.case_state("K7")["state"] == "executing"  # 停在执行中
    t.script += [dispatch(1), good, end()]  # 重启后模型照常应答
    seg_x.run("K7", reason="x", day=0)  # 整段重跑
    for s in (clean, crashed):  # 两次运行
        assert s.case_state("K7")["state"] == "waiting" and s.verify_chain() is None  # 同样进入等待
    def valid(s):  # 有效 Artifact 内容
        st = s.artifact_status()  # 状态
        return [a["content"] for a in s.store.artifacts("run") if st[(a["artifact_id"], a["version"])] == "valid"]
    assert valid(crashed) == valid(clean)  # 有效结论一致

"""F05：模拟人工测试。全部离线：种子 42 环境、固定样例报价、代码节点（固定模板驱动），不做任何真实调用；
另有一条集成测试在 Postgres 上验证推迟写入的崩溃恢复。"""
from pathlib import Path  # 路径

import pytest  # 测试框架

from business.impact import analyze  # 影响分析（算推迟前后的预计停线）
from datasheets.fetch import FixtureSource  # datasheet 样例
from humans.desks import load_humans_config  # 默认规则
from models.providers import ScriptedTransport  # 脚本模型（本文件不调用）
from orchestrator.assembly import build_runtime  # 装配
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from simenv.api import Environment  # 环境
from simenv.bom import load_boms  # BOM
from simenv.engine import WriteRejected  # 写入拒绝
from simenv.model import state_hash  # 状态哈希
from simenv.params import load_params  # 参数
from simenv.store import MemoryEnvStore, PgEnvStore  # 环境存储
from tools.proxy import load_tools_config  # 工具配置

FIX = Path(__file__).parent / "fixtures" / "datasheets"  # datasheet 样例
SUB, ORIG = "SUBX-1", "SKY13453-385LF"  # 替代料（不在 BOM）与原料


# 功能：装配一套 Runtime，humans 为模拟人工规则（None = 不启用）。完成：返回 runtime。
def runtime(humans=None, seed=42):
    cfg = load_tools_config()  # 工具配置
    cfg["providers"] = {k: {"daily_limit": None, "rate_per_s": None, "burst": None} for k in cfg["providers"]}  # 不限流
    s = Session(MemorySessionStore(), "run", clock=lambda: "t")  # 事件日志
    return build_runtime(env=Environment.create(load_params(), load_boms(), 42), session=s,  # 装配
                         transports={"glm-flash": ScriptedTransport([])}, tools_config=cfg,  # 不调模型
                         switches={"model_fallback": False}, datasheet_source=FixtureSource(FIX),  # 样例
                         sleep=lambda x: None, vault_key=b"k" * 32, seeds={"env": 42, "humans": seed},  # 固定密钥与种子
                         humans=humans)  # 模拟人工


def rules(**over):
    """默认规则上按段覆盖。"""
    cfg = load_humans_config()  # 默认
    for section, values in over.items():  # 每段
        cfg[section] = {**cfg[section], **values}  # 覆盖
    return cfg  # 返回


def a1(rt):
    """第 1 天开案并用固定模板跑到送审（5 张草稿）。"""
    rt.env.advance()  # 第 1 天
    rt.session.open_case("K1", day=1, actor="monitor", payload={"goal": "处置缺料", "mode": "S1"})  # 案子
    return rt.template_driver().run("K1", day=1)  # 模板


def view(rt, ref):
    """按引用取 Artifact。"""
    aid, ver = ref.rsplit("@v", 1)  # 拆引用
    return rt.session.artifact_view(aid, int(ver))  # 取


# 功能：H2——主管在 1–2 天内批准、延迟按种子确定（同种子相同）；批准后路由唤醒案子、提交成功；重复 respond 不重复应答。
def test_supervisor_approves_within_delay():
    rt = runtime(True)  # 默认规则
    out = a1(rt)  # 送审
    reqs = rt.session.events(type="human_request")  # 5 个 H2 请求
    delays = {e["seq"]: rt.humans.delay("H2", e["seq"]) for e in reqs}  # 延迟
    assert set(delays.values()) <= {1, 2}  # 在区间内
    assert delays == {e["seq"]: runtime(True).humans.delay("H2", e["seq"]) for e in reqs}  # 同种子相同
    assert rt.humans.respond(1) == []  # 当天不应答
    day2 = rt.humans.respond(2)  # 第 2 天：延迟 1 的
    assert {d["request"] for d in day2} == {s for s, d in delays.items() if d == 1}  # 恰好这些
    rt.humans.respond(3)  # 第 3 天：其余
    assert len(rt.session.events(type="approval_granted")) == len(out["drafts"])  # 全部批准
    assert rt.humans.respond(3) == [] and rt.humans.respond(4) == []  # 不重复应答
    env_day = rt.session.append("env_day", day=3, actor="env", payload={})  # 当天的环境事件
    assert ("K1", "人工应答：human_decision") in rt.router(env_day)  # 人工应答唤醒
    r = rt.executor.submit(case_id="K1", day=3, draft_ref=out["drafts"][0])  # 提交
    assert r["ok"], r  # 令牌有效


# 功能：H2 驳回规则与跳过——金额上限为 0 时全部驳回；草稿失效后批准核对不过写跳过事件，不重试；不同种子延迟可不同。
def test_supervisor_reject_and_skip():
    rt = runtime(rules(supervisor={"reject_amount_over_usd": 0}))  # 一律超上限
    a1(rt)  # 送审
    rt.humans.respond(3)  # 到期
    decisions = [e["payload"]["decision"] for e in rt.session.events(type="human_decision")]  # 决定
    assert decisions and set(decisions) == {"reject"} and not rt.session.events(type="approval_granted")  # 全驳回
    rt = runtime(True)  # 默认规则
    out = a1(rt)  # 送审
    d = view(rt, out["drafts"][0])  # 第一张草稿
    rt.session.produce(d["artifact_id"], "PODraft", {**d["content"], "qty": d["content"]["qty"] + 1}, day=1,  # 改版：v1 被取代
                       actor="procurement@K1", case_id="K1")  # 同案
    rt.humans.respond(3)  # 到期
    skipped = rt.session.events(type="sim_human_skipped")  # 跳过
    assert len(skipped) == 1 and skipped[0]["payload"]["node"] == "H2"  # 只有失效那张
    assert rt.humans.respond(4) == []  # 不重试
    delays = {runtime(True, seed=s).humans.delay("H2", 27) for s in range(10)}  # 不同种子
    assert delays == {1, 2}  # 两种延迟都会出现


# 功能：替代料 B 级报告（门控测试同款：代码汇总等级、可信标签）与决策。
def substitute_case(rt):
    rt.session.open_case("K7", day=0, actor="runtime", payload={"goal": "替代料", "mode": "S1"})  # 案子
    chosen = {"distributor": "digikey", "stock": 900, "unit_price_usd": 0.5, "eta_day": 4, "amount_usd": 50.0}
    rt.session.produce("Decision:SUB", "ProcurementDecision", {"feasible": True, "decisions": [  # 替代料决策
        {"mpn": SUB, "qty": 100, "chosen": chosen}]}, day=0, actor="policy", case_id="K7",
        meta={"taint": {"*": "trusted", "decisions[].chosen": "external"}})  # 代码决策
    art = rt.session.produce(f"VerificationReport:{SUB}", "VerificationReport",  # B 级报告
                             {"candidate": SUB, "original": ORIG, "grade": {"grade": "B"}},  # 内容
                             day=0, actor="verifier@K7", case_id="K7", meta={"taint": {"*": "trusted"}})  # 本案
    return f"{art['artifact_id']}@v{art['version']}"  # 引用


# 功能：H1——门控因缺 H1 拒绝时只通知工程师一次；表内规则确认后草稿通过；表外默认否决。
def test_engineer_by_mock_rules():
    approve = rules(engineer={"substitutes": [{"original": ORIG, "substitute": SUB, "decision": "approve"}]})  # 表内批准
    for cfg, ok in ((approve, True), (rules(), False)):  # 表内 / 表外
        rt = runtime(cfg)  # 装配
        ref = substitute_case(rt)  # 报告

        def draft(rt=rt):  # 为替代料建草稿（绑定本轮 Runtime）
            return rt.executor.create_draft(case_id="K7", day=0, decision_ref="Decision:SUB@v1", mpn=SUB)  # 调用

        assert "H1" in draft()["reasons"][0] and "H1" in draft()["reasons"][0]  # 两次都拒
        reqs = [e for e in rt.session.events(type="human_request") if e["payload"]["node"] == "H1"]  # 通知
        assert len(reqs) == 1 and reqs[0]["payload"]["report"] == ref  # 只通知一次
        assert rt.humans.respond(0) == []  # 延迟 1 天
        assert rt.humans.respond(1)[0]["action"] == ("approve" if ok else "reject")  # 按规则
        reasons = draft().get("reasons", [])  # 再建草稿（装配里有软声明检查，替代料声明不在本测试范围）
        assert any("H1" in r for r in reasons) is not ok  # 表内不再因 H1 被拒、表外仍因 H1 被拒
        assert rt.humans.respond(2) == []  # 否决后不再通知、不重复


# 功能：记忆提议——替代关系按工程师规则确认落库 / 否决不落库；别名命中别名表才确认，未命中不应答。
def test_memory_confirmations():
    approve = rules(engineer={"substitutes": [{"original": ORIG, "substitute": SUB, "decision": "approve"}]},
                    buyer={"aliases": {"那颗射频开关": ORIG}})  # 规则
    rt = runtime(approve)  # 装配
    ref = substitute_case(rt)  # 报告
    kb = rt.knowledge  # 知识库
    kb.propose(rt.session, case_id="K7", day=0, actor="orchestrator@K7", kind="substitution", report=ref,
               board="hackrf-one", designators=["U1"])  # 替代关系提议
    kb.propose(rt.session, case_id="K7", day=0, actor="orchestrator@K7", kind="alias", alias="那颗射频开关", mpn=ORIG)
    kb.propose(rt.session, case_id="K7", day=0, actor="orchestrator@K7", kind="alias", alias="那个电阻", mpn="R1")
    done = rt.humans.respond(1)  # 应答
    assert sorted(d["node"] for d in done) == ["H1", "H3"]  # 表外别名不应答
    assert len(rt.session.events(type="memory_confirmed")) == 2  # 两条落库
    rt = runtime(True)  # 表外替代：否决
    ref = substitute_case(rt)  # 报告
    rt.knowledge.propose(rt.session, case_id="K7", day=0, actor="orchestrator@K7", kind="substitution", report=ref,
                         board="hackrf-one", designators=["U1"])  # 提议
    assert rt.humans.respond(1)[0]["action"] == "reject" and not rt.session.events(type="memory_confirmed")  # 不落库


# 功能：H3 消歧——别名表给出的料号在候选里才作答；BuyerDesk 拒绝候选外料号；H4 按配置确认或不应答。
def test_buyer_resolve_and_goal_revision():
    rt = runtime(rules(buyer={"aliases": {"那颗射频开关": ORIG}}))  # 规则
    rt.session.open_case("K2", day=0, actor="runtime", payload={"goal": "查料", "mode": "S1"})  # 案子
    hit = rt.session.append("human_request", day=0, actor="resolver@K2", case_id="K2",  # Resolver 交人（同款载荷）
                            payload={"role": "buyer", "node": "H3", "query": "那颗射频开关", "candidates": [ORIG, "X"]})
    rt.session.append("human_request", day=0, actor="resolver@K2", case_id="K2",  # 表外说法
                      payload={"role": "buyer", "node": "H3", "query": "某颗料", "candidates": ["X", "Y"]})
    with pytest.raises(ValueError, match="不在候选"):  # 候选外
        rt.buyer_desk.resolve(hit["seq"], mpn="Z", by="buyer", day=0)  # 拒绝
    done = rt.humans.respond(1)  # 应答
    assert [d["request"] for d in done] == [hit["seq"]]  # 只答命中的
    assert rt.session.events(type="human_decision")[-1]["payload"]["mpn"] == ORIG  # 所答料号
    p = rt.goals.propose(case_id="K2", day=1, actor="orchestrator@K2", new_goal="改用替代料", reason="原料无货")  # H4
    rt.humans.respond(2)  # 默认确认
    assert rt.goals.current_goal("K2") == "改用替代料"  # 生效
    rt = runtime(rules(buyer={"goal_revision": "ignore"}))  # 不应答
    rt.session.open_case("K2", day=0, actor="runtime", payload={"goal": "查料", "mode": "S1"})  # 案子
    rt.goals.propose(case_id="K2", day=0, actor="orchestrator@K2", new_goal="改", reason="因")  # 提议
    assert rt.humans.respond(5) == [] and rt.goals.current_goal("K2") == "查料"  # 按原目标
    assert p["accepted"]  # 提议本身被接受


# 功能：H5——计划员按 ImpactReport 推迟预计停线且未开工的工单（不超上限），开工日后移、预计停线下降；Agent 工具里没有推迟。
def test_planner_postpones_work_orders():
    rt = runtime(True)  # 默认规则（上限 5 天）
    out = a1(rt)  # 第 1 天影响分析
    impact = view(rt, out["artifacts"]["T1"])["content"]  # ImpactReport
    before = {w: rt.env.state.work_orders[w].start_day for w in impact["work_orders"]}  # 原开工日
    rt.session.append("human_request", day=1, actor="orchestrator@K1", case_id="K1",  # 上报（与 segment 同款载荷）
                      payload={"role": "planner", "node": "H5", "tool": "report_to_planner",
                               "args": {"impact_artifact": out["artifacts"]["T1"], "reason": "预计停线"}})
    assert rt.humans.respond(2)[-1]["action"] == "reply"  # 回复
    dec = next(e for e in rt.session.events(type="human_decision") if e["payload"]["node"] == "H5")["payload"]  # 决定
    stopping = {w for w, v in impact["work_orders"].items() if v["stop_days"] > 0}  # 预计停线的
    assert set(dec["postpone"]) == stopping and all(0 < d <= 5 for d in dec["postpone"].values())  # 只推停线的、不超上限
    for w, d in dec["postpone"].items():  # 逐张
        assert rt.env.state.work_orders[w].start_day == max(before[w], 2) + d  # 开工日后移
    assert analyze(rt.env.state)["projected_stop_days"] < impact["projected_stop_days"]  # 预计停线下降
    tools = {t for ts in rt.segments.tools_by_mode.values() for t in ts} | set(rt.proxy.tools)  # 全部工具名
    assert not any("postpone" in t for t in tools)  # G8：没有推迟工具
    with pytest.raises(ValueError, match="上限"):  # 入口上限
        rt.planner_desk.reply(rt.session.events(type="human_request")[-1]["seq"], postpone={"W-006": 6},
                              by="planner", day=2)  # 超上限


# 功能：不传 humans 时不启用：调度没有挂点、多日推进也没有任何人工决定。
def test_disabled_by_default():
    rt = runtime()  # 不启用
    assert rt.humans is None and rt.scheduler.before_routing is None  # 没有挂点
    a1(rt)  # 送审
    assert not rt.session.events(type="human_decision")  # 没有自动应答


# 功能：环境推迟写入——已开工拒绝、幂等不重复推迟、推迟后停线工单回到未开工；崩溃恢复重放推迟与对照一致。
def check_postpone_recover(store, run):
    params, boms = load_params(), load_boms()  # 输入
    control = Environment.create(params, boms, 42)  # 对照
    crashed = Environment.create(params, boms, 42, store=store, run_id=run)  # 持久化运行
    for env in (control, crashed):  # 同一剧本
        for _ in range(3):  # 推进到第 3 天
            env.advance()  # 推进
        r = env.postpone_work_order(wo_id="W-004", days=3, idempotency_key="H5:1:W-004")  # 推迟
        again = env.postpone_work_order(wo_id="W-004", days=3, idempotency_key="H5:1:W-004")  # 重复
        assert again["duplicate"] and again["start_day"] == r["start_day"] == 40  # 幂等（37 + 3）
    revived = Environment.recover(store, run)  # 恢复
    assert state_hash(revived.state) == state_hash(control.state)  # 一致
    return control  # 对照


def test_env_postpone_write():
    env = check_postpone_recover(MemoryEnvStore(), "run-h5")  # 内存存储
    with pytest.raises(WriteRejected, match="正整数"):  # 天数非法
        env.postpone_work_order(wo_id="W-005", days=0, idempotency_key="x")  # 拒绝
    with pytest.raises(WriteRejected, match="未知工单"):  # 工单不存在
        env.postpone_work_order(wo_id="W-999", days=1, idempotency_key="y")  # 拒绝
    while env.state.work_orders["W-001"].status == "planned":  # 推进到 W-001 开工或停线
        env.advance()  # 推进
    wo = env.state.work_orders["W-001"]  # 工单
    if wo.status == "stopped":  # 缺料停线：推迟后回到未开工
        env.postpone_work_order(wo_id="W-001", days=2, idempotency_key="z")  # 推迟
        assert wo.status == "planned" and wo.start_day == env.state.day + 2  # 从今天起算
    else:  # 已开工：拒绝
        with pytest.raises(WriteRejected, match="已开工"):  # 拒绝
            env.postpone_work_order(wo_id="W-001", days=2, idempotency_key="z")  # 调用


@pytest.mark.integration
def test_postgres_postpone_recover(migrated):
    conn, _, _ = migrated  # 迁移好的连接（测试后回滚）
    check_postpone_recover(PgEnvStore(conn), "run-h5-pg")  # 同一套断言

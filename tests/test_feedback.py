"""F14：反馈与升级测试。全部离线：种子 42 环境、固定样例报价 + 注入挂点、脚本模型，不产生任何真实调用。"""
from kit import ORCH_TOOLS, build, call, end, open_case  # 共用装配
from test_gate import decide_first, setup  # F12 装配（A2 买空场景）

from cases.router import CaseRouter  # F10 路由
from context.assemble import OrchestratorContext  # F16 上下文
from feedback.escalation import Escalation  # 升级
from feedback.loop import FeedbackLoop  # 反馈循环
from infrastructure.config import load_harness_config  # 配置
from models.providers import reply  # 脚本应答
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储

CFG = load_harness_config()["context"]  # context 配置段


# 功能：A2 门控拒绝 → 固定格式卡片：对象、发生了什么、影响（已失效与仍有效）、可选下一步、需要重规划；
#       F16 新反馈分区出现卡片且不再重复原始事件；必须级唤醒、完成的子任务不发卡片、未完成的只汇总不唤醒。
def test_gate_rejection_card_and_levels():
    def buyout(tool, args, data, day):  # 第 2 天起买空
        return [{**r, "stock": 0} for r in data] if day >= 2 else data  # 注入

    p, _, desk = setup(overlays=[buyout])  # 装配
    dec_ref, d = decide_first(p)  # 决策
    draft = p.ex.create_draft(case_id="K7", day=0, decision_ref=dec_ref, mpn=d["mpn"])["draft"]  # 草稿
    p.ex.request_approval(case_id="K7", day=0, draft_ref=draft)  # 送审
    desk.approve_po(draft, by="supervisor", day=2)  # 批准
    p.ex.submit(case_id="K7", day=2, draft_ref=draft)  # 提交被拒
    rejected = p.s.events(type="gate_rejected")[-1]  # 拒绝事件
    assert rejected["payload"]["mpn"] == d["mpn"]  # 带料号
    loop = FeedbackLoop(p.s, lambda: p.env)  # 反馈循环
    done = p.s.events(type="task_finished")[0]  # 正常完成的子任务
    wakes = loop.on_events([rejected, done], day=2)  # 生成卡片
    assert wakes == [("K7", f"必须重规划：gate_rejected #{rejected['seq']}")]  # 只有门控拒绝唤醒
    card = p.s.events(type="feedback")[-1]["payload"]["text"].splitlines()  # 卡片
    assert card[0] == f"【门控拒绝】提交采购单 {draft}"  # 标题
    assert card[1].startswith("发生了什么：提交前重校验") and "→0" in card[1]  # 事实
    assert "Decision:K7-T3@v1" in card[2] and "已失效" in card[2] and "ImpactReport:K7-T1" in card[2]  # 影响
    assert card[3] == "可选下一步：重新比价 / 提出目标修订 / 升级给人" and card[4] == "需要重规划：是"  # 下一步与分级
    assert len(p.s.events(type="feedback")) == 1  # 完成的子任务不发卡片
    unfinished = p.s.append("task_finished", day=2, actor="runtime", case_id="K7",  # 未完成的子任务
                            payload={"task_id": "K7-T9", "status": "unfinished", "reason": "预算触顶"})
    assert loop.on_events([unfinished], day=2) == []  # 汇总级不唤醒
    assert "需要重规划：否" in p.s.events(type="feedback")[-1]["payload"]["text"]  # 但有卡片
    _, user = OrchestratorContext(CFG)(p.s, "K7", "门控拒绝")  # 编排者上下文
    feedback = user[user.index("[新反馈]"):]  # 新反馈分区
    assert "【门控拒绝】提交采购单" in feedback and "【gate_rejected】" not in feedback  # 卡片替代原始事件


# 功能：装配带路由与反馈循环的 Runtime（编排者每段直接结束）。
def runtime(**switches):
    s, _, sched, env, _ = build([end("external")] * 200)  # 足够多的"结束本段"
    loop = FeedbackLoop(s, lambda: env, **switches)  # 反馈循环
    sched.router = CaseRouter(s, lambda: env, feedback=loop)  # 路由
    return s, sched, env  # 返回


# 功能：A1 偏差——按草稿预期第 2 天到货的采购单，运单预计更晚 → 迟到偏差（只报一次）、卡片、唤醒；
#       采购单到货后缺口仍在（只买了 10 颗）→ 缺口未归零偏差；关 deviation_feedback 不报。
def test_deviation_late_and_gap():
    for enabled in (True, False):  # 开 / 关
        s, sched, env = runtime(deviation=enabled)  # 装配
        sched.run_day()  # 第 1 天开案
        k1 = s.events(type="case_opened")[0]["case_id"]  # 案子号
        mpn = s.events(type="case_opened")[0]["payload"]["targets"][0]["mpn"]  # 一个目标料
        d = s.produce(f"PODraft:{k1}:{mpn}", "PODraft", {"mpn": mpn, "qty": 10, "eta_day": 2}, day=1,  # 预期第 2 天到
                      actor=f"procurement@{k1}", case_id=k1)  # 草稿
        po = env.submit_purchase_order(mpn=mpn, qty=10, distributor="digikey", unit_price="1", idempotency_key="t")
        s.append("po_submitted", day=1, actor=f"procurement@{k1}", case_id=k1,  # 提交事件
                 payload={"po_id": po["po_id"], "draft": f"{d['artifact_id']}@v1"}, entity_keys={"po": [po["po_id"]]})
        for _ in range(20):  # 推进到到货之后
            sched.run_day()  # 一天
        kinds = [e["payload"]["kind"] for e in s.events(case_id=k1, type="deviation")]  # 偏差
        if not enabled:  # 关掉
            assert kinds == []  # 不报
            continue  # 下一轮
        assert kinds == ["late", "gap"]  # 各报一次
        woken_days = {e["day"] for e in s.events(case_id=k1, type="case_woken")}  # 被唤醒的日子
        assert all(e["day"] in woken_days for e in s.events(case_id=k1, type="deviation"))  # 偏差当天必唤醒
        cards = [e["payload"]["text"] for e in s.events(case_id=k1, type="feedback")]  # 卡片
        assert any(c.startswith(f"【偏差】{po['po_id']}") for c in cards)  # 偏差卡片


def _case(s, cid="K1"):
    """开一个进入执行中的案子。"""
    s.open_case(cid, day=0, actor="runtime", payload={"goal": "处置缺口", "mode": "S1"})  # 开案
    s.transition(cid, "planning", day=0, actor="runtime")  # 规划
    s.transition(cid, "executing", day=0, actor="runtime")  # 执行


# 功能：代码强制升级的各条规则——拒绝后重规划 2 次仍被拒、三家都无货、候选全 C 级、关键参数冲突、疑似注入（另标记维护者）；
#       升级 = escalated 事件 + 通知采购员 + 以 escalated 关闭；关 forced_escalation 不升级。
def test_forced_escalation_rules():
    def rejected(s):  # 同一料号：拒绝 → 重规划两次 → 再拒
        s.append("gate_rejected", day=1, actor="gate", case_id="K1", payload={"action": "draft", "mpn": "X", "reasons": ["r"]})
        for _ in range(2):  # 两次重规划
            s.transition("K1", "planning", day=1, actor="runtime")  # 重规划
            s.transition("K1", "executing", day=1, actor="runtime")  # 执行
        s.append("gate_rejected", day=2, actor="gate", case_id="K1", payload={"action": "draft", "mpn": "X", "reasons": ["r"]})

    def no_stock(s):  # 三家都无货
        s.produce("Decision:K1", "ProcurementDecision", {"feasible": False, "decisions": [
            {"mpn": "X", "qty": 5, "chosen": None, "filtered": [{"distributor": d, "reason": "现货不足"}
                                                                for d in ("digikey", "mouser", "element14")]}]},
            day=1, actor="policy", case_id="K1")  # 决策

    def all_c(s):  # 候选全 C
        for c in ("Y1", "Y2"):  # 两个候选
            s.produce(f"VerificationReport:{c}", "VerificationReport",
                      {"candidate": c, "original": "X", "grade": {"grade": "C"}}, day=1, actor="verifier", case_id="K1")

    def conflict(s):  # 关键参数冲突
        s.append("source_conflict", day=1, actor="verifier", case_id="K1",
                 payload={"param": "isolation_db", "key_param": True, "resolved": False})

    def injection(s):  # 疑似注入
        s.append("tool_call", day=1, actor="verifier@K1", case_id="K1",
                 payload={"tool": "distributor_quotes", "injection_suspected": [{"path": "[0].note", "match": "ignore"}]})

    for rule, make in (("rejected_after_replans", rejected), ("no_stock", no_stock), ("all_fail_grade", all_c),
                       ("source_conflict", conflict), ("injection", injection)):  # 每条规则
        s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
        _case(s)  # 开案
        make(s)  # 构造局面
        assert Escalation(s, enabled=False).check("K1", day=3) is False  # 关掉不升级
        assert Escalation(s).check("K1", day=3) is True, rule  # 升级
        assert s.events(type="escalated")[0]["payload"]["rule"] == rule  # 规则名
        assert s.case_state("K1")["outcome"] == "escalated"  # 关闭
        assert s.events(type="human_request")[-1]["payload"]["role"] == "buyer"  # 通知采购员
        assert bool(s.events(type="maintainer_flag")) == (rule == "injection")  # 只有注入标记维护者
        assert Escalation(s).check("K1", day=4) is False  # 不重复升级


# 功能：升级工具——编排者调 escalate_to_human：案子关闭、通知采购员、本段结束且不再迁入等待；关卡片时仍按分级唤醒。
def test_escalate_tool_and_cards_switch():
    esc = reply(tool_calls=[call("escalate_to_human", reason="三家都没货且无替代")])  # 编排者升级
    s, seg, _, _, _ = build([esc])  # 装配
    seg.tools = [*ORCH_TOOLS, "escalate_to_human"]  # 编排者可用升级工具
    open_case(s)  # 开案
    out = seg.run("K7", reason="开案", day=0)  # 跑一段
    assert out["status"] == "closed" and s.case_state("K7")["outcome"] == "escalated"  # 关闭
    assert s.events(type="escalated")[0]["payload"] == {"rule": "orchestrator", "reason": "三家都没货且无替代",
                                                         "evidence": []}  # 升级事件
    assert s.events(type="segment_ended") and s.events(type="human_request")[-1]["payload"]["role"] == "buyer"
    s2, _, _ = runtime(cards=False)  # 关卡片
    loop = FeedbackLoop(s2, lambda: None, cards=False)  # 反馈循环
    _case(s2)  # 开案
    ev = s2.append("gate_rejected", day=1, actor="gate", case_id="K1", payload={"action": "submit", "reasons": ["x"]})
    assert loop.on_events([ev], day=1) == [("K1", f"必须重规划：gate_rejected #{ev['seq']}")]  # 仍唤醒
    assert not s2.events(type="feedback")  # 不生成卡片

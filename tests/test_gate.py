"""F12：证据门控与审批令牌测试。全部离线：固定样例报价 + 注入挂点模拟 A2 买空，不产生任何真实调用。"""
import json  # 在事件里找令牌值

import pytest  # 测试框架
from kit import build, code_pipeline, open_case, tok  # 共用装配

from gate.approval import ApprovalVault, HumanDesk  # 令牌保管库与人工入口
from gate.evidence import EvidenceGate, planner_report_gate  # 证据门控
from simenv.model import state_hash  # 环境状态哈希
from tools.server import CallContext  # 控制工具上下文
from tools.servers import PlannerReportArgs  # 上报参数

SUB = "SUBX-1"  # 不在任何 BOM 里的替代料


# 功能：装配带证据门控的代码执行链；revalidate 对应消融开关，overlays 为分销商注入。
# 依赖：kit.code_pipeline。完成：返回 (p, vault, desk)。
def setup(*, overlays=(), revalidate=True):
    vault = ApprovalVault(key=b"k" * 32)  # 固定密钥
    p = code_pipeline(overlays=overlays, unlimited=True,  # 装配（不受真实时间限流影响）
                      gate=lambda proxy: EvidenceGate(vault, proxy=proxy, authority=proxy.guard.authority,
                                                      revalidate=revalidate))  # F12 门控
    return p, vault, HumanDesk(p.s, vault)  # 返回


def decide_first(p, day=0):
    """代码路径：影响分析 → 比价 → 决策，返回 (决策引用, 第一颗可行料)。"""
    impact_ref, _ = p.run("impact_analysis", day=day)  # 影响分析
    quote_ref, _ = p.run("sourcing", [impact_ref], day=day)  # 比价
    dec_ref, dec = p.run("policy", [quote_ref], day=day)  # 决策
    return dec_ref, next(d for d in dec["content"]["decisions"] if d["chosen"])  # 第一颗可行料


def view(p, ref):
    """按引用取 Artifact。"""
    aid, ver = ref.rsplit("@v", 1)  # 拆引用
    return p.s.artifact_view(aid, int(ver))  # 取


# 功能：原料号换供应商的完整路径——建草稿（无需验证报告）→ 送审带预演、真实环境不变 → 无令牌提交被拒 →
#       主管批准签发令牌 → 提交成功 → 重复提交按幂等重放；令牌值不出现在任何事件里。
def test_original_part_flow_with_token_and_preview():
    p, vault, desk = setup()  # 装配
    dec_ref, d = decide_first(p)  # 决策
    draft = p.ex.create_draft(case_id="K7", day=0, decision_ref=dec_ref, mpn=d["mpn"])  # 建草稿
    assert draft["ok"], draft  # 原料号不需要验证报告
    before = state_hash(p.env.state)  # 送审前环境
    assert p.ex.request_approval(case_id="K7", day=0, draft_ref=draft["draft"])["ok"]  # 送审
    assert state_hash(p.env.state) == before  # 预演不改真实环境
    preview = p.s.events(type="approval_requested")[-1]["payload"]["preview"]  # 预演
    assert preview["after"]["projected_stop_days"] <= preview["before"]["projected_stop_days"]  # 不会更糟
    assert preview["part"]["mpn"] == d["mpn"] and preview["part"]["po_qty"] == d["qty"]  # 该料与本单
    assert preview["part"]["gap"] == d["qty"]  # 缺口数字来自影响分析
    assert p.s.events(type="human_request")[-1]["payload"]["preview"] == preview  # 审批页附预演
    r = p.ex.submit(case_id="K7", day=0, draft_ref=draft["draft"])  # 无令牌提交
    assert r["ok"] is False and "审批令牌" in r["reasons"][0]  # 拒绝
    desk.approve_po(draft["draft"], by="supervisor", day=1)  # 主管批准
    first = p.ex.submit(case_id="K7", day=1, draft_ref=draft["draft"])  # 提交
    again = p.ex.submit(case_id="K7", day=1, draft_ref=draft["draft"])  # 重复提交
    assert first["ok"] and again == {**first, "duplicate": True}  # 幂等
    dump = json.dumps(p.s.events(), ensure_ascii=False)  # 全部事件
    assert vault.values() and not any(t in dump for t in vault.values())  # 令牌值不进事件


# 功能：令牌失效的各种情况——没送审不能批准；过期；主管驳回；别的草稿的令牌；草稿改版后旧令牌（旧版被替代、新版无令牌）；
#       进程重启后用同一密钥从事件恢复保管库。
def test_token_rejections_and_rebuild():
    p, _, desk = setup()  # 装配
    dec_ref, d = decide_first(p)  # 决策
    v1 = p.ex.create_draft(case_id="K7", day=0, decision_ref=dec_ref, mpn=d["mpn"])["draft"]  # 草稿 v1
    with pytest.raises(ValueError, match="送审"):  # 没送审
        desk.approve_po(v1, by="supervisor", day=0)  # 不能批准
    p.ex.request_approval(case_id="K7", day=0, draft_ref=v1)  # 送审
    desk.approve_po(v1, by="supervisor", day=0)  # 批准（有效到第 3 天）
    assert "过期" in p.ex.submit(case_id="K7", day=4, draft_ref=v1)["reasons"][0]  # 过期
    restarted = ApprovalVault(key=b"k" * 32)  # 重启后的保管库（同一密钥）
    restarted.rebuild(p.s)  # 从事件恢复
    assert restarted.check(v1, view(p, v1)["content_hash"], 1) is None  # 恢复后可查验
    desk.reject_po(v1, by="supervisor", day=1)  # 驳回
    assert "审批令牌" in p.ex.submit(case_id="K7", day=1, draft_ref=v1)["reasons"][0]  # 令牌作废
    restarted.rebuild(p.s)  # 按事件恢复：驳回后同样作废
    assert restarted.check(v1, view(p, v1)["content_hash"], 1) is not None  # 作废
    p.ex.request_approval(case_id="K7", day=1, draft_ref=v1)  # 重新送审
    desk.approve_po(v1, by="supervisor", day=1)  # 重新批准 v1
    v2 = p.ex.create_draft(case_id="K7", day=1, decision_ref=dec_ref, mpn=d["mpn"])["draft"]  # 同料再建 → v2
    assert v2.endswith("@v2")  # 新版本
    assert "审批令牌" in p.ex.submit(case_id="K7", day=1, draft_ref=v2)["reasons"][0]  # v1 的令牌不给 v2 用
    assert "状态为 superseded" in p.ex.submit(case_id="K7", day=1, draft_ref=v1)["reasons"][0]  # v1 已被替代


# 功能：A2——审批期间被买空：提交前重校验以跳过缓存的实时报价发现库存 → 0，拒绝提交、写明变化，决策与草稿失效；
#       关掉 presubmit_revalidation 后同一局面照常提交（Eval 对照）。
@pytest.mark.parametrize("revalidate", [True, False])
def test_a2_buyout_during_approval(revalidate):
    def buyout(tool, args, data, day):  # 第 2 天起所有分销商该料被买空
        return [{**r, "stock": 0} for r in data] if day >= 2 else data  # 注入

    p, _, desk = setup(overlays=[buyout], revalidate=revalidate)  # 装配
    dec_ref, d = decide_first(p)  # 第 0 天决策
    draft = p.ex.create_draft(case_id="K7", day=0, decision_ref=dec_ref, mpn=d["mpn"])["draft"]  # 草稿
    p.ex.request_approval(case_id="K7", day=0, draft_ref=draft)  # 送审
    desk.approve_po(draft, by="supervisor", day=2)  # 第 2 天才批
    r = p.ex.submit(case_id="K7", day=2, draft_ref=draft)  # 提交
    if not revalidate:  # 消融：不重校验
        assert r["ok"]  # 照常提交（拿过期结论下单）
        return  # 结束
    assert r["ok"] is False and "库存" in r["reasons"][0] and "→0" in r["reasons"][0]  # 写明变化
    assert view(p, draft)["status"] == "invalid" and view(p, dec_ref)["status"] == "invalid"  # 决策与草稿失效
    fresh = [e for e in p.s.events(type="tool_call") if e["actor"] == "procurement@K7"]  # 重校验的调用
    assert fresh and fresh[-1]["payload"]["cache_hit"] is False  # 跳过缓存
    assert p.s.events(type="gate_rejected")[-1]["payload"]["action"] == "submit"  # 反馈事件


# 功能：替代料证据——没有报告拒绝；C 级永远拒绝；B 级须 H1 确认；A 级通过；等级字段不可信（没打标）不采信。
def test_substitute_evidence_by_grade():
    p, _, desk = setup()  # 装配
    chosen = {"distributor": "digikey", "stock": 900, "unit_price_usd": 0.5, "eta_day": 4, "amount_usd": 50.0}
    p.s.produce("Decision:SUB", "ProcurementDecision", {"feasible": True, "decisions": [  # 替代料决策
        {"mpn": SUB, "qty": 100, "chosen": chosen}]}, day=0, actor="policy", case_id="K7",
        meta={"taint": {"*": "trusted", "decisions[].chosen": "external"}})  # 代码决策

    def draft():  # 为替代料建草稿
        return p.ex.create_draft(case_id="K7", day=0, decision_ref="Decision:SUB@v1", mpn=SUB)  # 调用

    def report(grade, labelled=True):  # 产出一版验证报告
        meta = {"taint": {"*": "trusted"}} if labelled else {}  # 等级由代码汇总才打可信标签
        art = p.s.produce(f"VerificationReport:{SUB}", "VerificationReport",  # 报告
                          {"candidate": SUB, "original": "SKY13453-385LF", "grade": {"grade": grade}},  # 内容
                          day=0, actor="verifier@K7", case_id="K7", meta=meta)  # 本案
        return f"{art['artifact_id']}@v{art['version']}"  # 引用

    assert "替代料" in draft()["reasons"][0]  # 没有报告
    report("C")  # C 级
    assert "不能采购" in draft()["reasons"][0]  # 永远拒绝
    b = report("B")  # B 级
    assert "H1" in draft()["reasons"][0]  # 须工程师确认
    desk.confirm_substitute(b, by="engineer", day=0)  # H1 确认
    assert draft()["ok"]  # 通过
    report("A", labelled=False)  # A 级但等级来源不明
    assert "不可信" in draft()["reasons"][0]  # 不采信
    report("A")  # 代码汇总的 A 级
    assert draft()["ok"]  # 通过


# 功能：上报计划员门控——必须附本案有效 ImpactReport；理由里的数字须出自它；经编排控制工具走一遍拒绝与放行。
def test_planner_report_gate():
    p = code_pipeline()  # 装配
    impact_ref, impact = p.run("impact_analysis")  # 影响分析
    stop = impact["content"]["projected_stop_days"]  # 报告里的数字
    assert planner_report_gate(p.s, "K7", impact_ref, f"预计停线 {stop} 天") == []  # 数字有出处
    assert "找不到出处" in planner_report_gate(p.s, "K7", impact_ref, f"预计停线 {stop + 9999} 天")[0]  # 编造
    assert "ImpactReport" in planner_report_gate(p.s, "K7", "Nope@v1", "x")[0]  # 没附报告
    s, seg, _, _, _ = build([])  # Runtime 装配（控制工具处理函数）
    open_case(s)  # 开案
    ctx = CallContext(day=0, case_id="K7", executor="orchestrator", token=tok(seg.authority, "orchestrator"))  # 上下文
    out = seg.handle_control("report_to_planner", PlannerReportArgs(impact_artifact="Nope@v1", reason="停线 3 天"), ctx)
    assert out["accepted"] is False and s.events(type="gate_rejected")  # 拒绝并写反馈
    assert not s.events(type="human_request")  # 没打扰计划员

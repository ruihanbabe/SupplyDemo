"""文件摘要（F14 · 代码强制升级与升级工具）

spec §5.7"升级给人"：满足条件由代码强制升级；编排者也可用 escalate_to_human 主动升级（须写理由）。升级后案子关闭，人在系统外处理。
Escalation.check(case_id, day) 依次检查（消融开关 forced_escalation 关闭时不查；预算触顶由 F09 处理）：
- rejected_after_replans：同一料号门控拒绝后又重规划 ≥ 配置次数，仍被拒；
- no_stock：该案最新有效决策里某料无选择，且各来源都因"现货不足"类原因被过滤（三家都无货）；
- all_fail_grade：某原料的全部有效验证报告都是 C 级（候选全不可用）；
- source_conflict：出现未化解的关键参数来源冲突事件（B2）；
- injection：本案工具返回里检测到疑似指令（B1），另写 maintainer_flag 标记给维护者。
escalate(case_id, rule, reason, day, evidence)：写 escalated（规则、原因、证据引用）→ 给采购员 human_request（代码摘要；
Explainer 摘要归 F23）→ 以 escalated 关闭案子。
依赖：session.log、feedback.cards（配置）。被 feedback.loop（每日）与 runtime.segment（escalate_to_human 工具）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from feedback.cards import load_feedback_config  # 配置
from session.log import (  # 事件日志
    ART_VALID,
    CASE_CLOSED,
    CASE_PLANNING,
    EV_CASE_TRANSITION,
    Session,
)


class Escalation:
    # 功能：见文件摘要。依赖：session、配置、开关。完成：已关闭的案子不重复升级。
    def __init__(self, session: Session, *, config: dict | None = None, enabled: bool = True):
        self.session = session  # 事件日志
        self.config = (config or load_feedback_config())["escalation"]  # 升级配置
        self.enabled = enabled  # 消融开关 forced_escalation

    def escalate(self, case_id: str, rule: str, reason: str, *, day: int, evidence: list[str] = (),
                 actor: str = "escalation") -> bool:
        """升级给人并关闭案子；案子已关闭返回 False。"""
        state = self.session.case_state(case_id)  # 当前状态
        if state is None or state["state"] == CASE_CLOSED:  # 不存在或已关闭
            return False  # 不重复
        ev = self.session.append("escalated", day=day, actor=actor, case_id=case_id,  # 升级事件
                                 payload={"rule": rule, "reason": reason, "evidence": list(evidence)})  # 规则、原因、证据
        goal = self.session.events(case_id=case_id, type="case_opened")[0]["payload"].get("goal", "")  # 案子目标
        self.session.append("human_request", day=day, actor=actor, case_id=case_id,  # 通知采购员
                            payload={"role": "buyer", "node": "escalation",  # 对象
                                     "summary": f"案子 {case_id}（{goal}）已升级给人：{reason}",  # 代码摘要
                                     "evidence": list(evidence)}, caused_by=ev["seq"])  # 证据
        self.session.close_case(case_id, "escalated", day=day, actor=actor, reason=reason, caused_by=ev["seq"])  # 关闭
        return True  # 已升级

    def check(self, case_id: str, day: int) -> bool:
        """按规则检查，命中即升级；返回是否升级。"""
        if not self.enabled:  # 消融
            return False  # 不查
        for rule in (self._rejected_after_replans, self._no_stock, self._all_fail_grade,  # 规则顺序固定
                     self._source_conflict, self._injection):  # 同上
            hit = rule(case_id)  # (规则名, 原因, 证据) 或 None
            if hit is not None:  # 命中
                if hit[0] == "injection":  # B1：同时标记给维护者
                    self.session.append("maintainer_flag", day=day, actor="escalation", case_id=case_id,
                                        payload={"reason": hit[1], "evidence": hit[2]})  # 维护者标记
                return self.escalate(case_id, hit[0], hit[1], day=day, evidence=hit[2])  # 升级
        return False  # 未命中

    def _rejected_after_replans(self, case_id: str):
        """同一料号：第一次拒绝与最近一次拒绝之间重规划次数达到阈值。"""
        rejects: dict[str, list[int]] = {}  # 料号 → 拒绝事件号
        for e in self.session.events(case_id=case_id, type="gate_rejected"):  # 门控拒绝
            if e["payload"].get("mpn"):  # 有料号
                rejects.setdefault(e["payload"]["mpn"], []).append(e["seq"])  # 记下
        plans = [e["seq"] for e in self.session.events(case_id=case_id, type=EV_CASE_TRANSITION)  # 重规划
                 if e["payload"]["to"] == CASE_PLANNING]  # 进入规划
        for mpn, seqs in sorted(rejects.items()):  # 每颗料
            n = sum(1 for p in plans if seqs[0] < p < seqs[-1])  # 两次拒绝之间的重规划
            if n >= self.config["replans_after_rejection"]:  # 达到阈值
                return ("rejected_after_replans", f"{mpn} 门控拒绝后重规划 {n} 次仍被拒",  # 原因
                        [f"event#{s}" for s in seqs])  # 证据
        return None  # 未命中

    def _latest_valid(self, case_id: str, kind: str) -> list[dict]:
        """本案某类型的有效 Artifact（按产出顺序）。"""
        status = self.session.artifact_status()  # 状态表
        return [a for a in self.session.store.artifacts(self.session.run_id)  # 全部版本
                if a["case_id"] == case_id and a["kind"] == kind  # 本案该类型
                and status[(a["artifact_id"], a["version"])] == ART_VALID]  # 有效

    def _no_stock(self, case_id: str):
        """最新有效决策里某料无选择，且所有来源都因无货被过滤。"""
        decisions = self._latest_valid(case_id, "ProcurementDecision")  # 有效决策
        if not decisions:  # 没有
            return None  # 未命中
        dec = decisions[-1]  # 最新
        for d in dec["content"].get("decisions", []):  # 每颗料
            reasons = [f["reason"] for f in d.get("filtered", [])]  # 过滤原因
            if d.get("chosen") is None and reasons and all(r in self.config["no_stock_reasons"] for r in reasons):  # 全无货
                return ("no_stock", f"{d['mpn']} 各来源都无货，无可行对策",  # 原因
                        [f"{dec['artifact_id']}@v{dec['version']}"])  # 证据
        return None  # 未命中

    def _all_fail_grade(self, case_id: str):
        """某原料的全部有效验证报告都是失败等级。"""
        by_orig: dict[str, list[dict]] = {}  # 原料 → 报告
        for r in self._latest_valid(case_id, "VerificationReport"):  # 有效报告
            by_orig.setdefault(r["content"].get("original", "?"), []).append(r)  # 归组
        for orig, reports in sorted(by_orig.items()):  # 每个原料
            grades = [(r["content"].get("grade") or {}) for r in reports]  # 等级
            grades = [g.get("grade") if isinstance(g, dict) else g for g in grades]  # 取等级字母
            if grades and all(g in self.config["fail_grades"] for g in grades):  # 全失败
                return ("all_fail_grade", f"{orig} 的 {len(reports)} 个候选全部为 {'/'.join(self.config['fail_grades'])} 级",
                        [f"{r['artifact_id']}@v{r['version']}" for r in reports])  # 证据
        return None  # 未命中

    def _source_conflict(self, case_id: str):
        """未化解的关键参数来源冲突（B2）。"""
        for e in self.session.events(case_id=case_id, type="source_conflict"):  # 冲突事件
            if e["payload"].get("key_param") and not e["payload"].get("resolved"):  # 关键参数且未化解
                return ("source_conflict", f"关键参数 {e['payload'].get('param')} 来源冲突无法化解",  # 原因
                        [f"event#{e['seq']}"])  # 证据
        return None  # 未命中

    def _injection(self, case_id: str):
        """本案工具返回里检测到疑似指令（B1）。"""
        for e in self.session.events(case_id=case_id, type="tool_call"):  # 工具调用
            if e["payload"].get("injection_suspected"):  # 命中
                return ("injection", f"工具 {e['payload']['tool']} 的返回含疑似指令文本",  # 原因
                        [f"event#{e['seq']}", f"record:{e['payload'].get('record_id')}"])  # 证据
        return None  # 未命中

"""文件摘要（F10 · 软声明与多案争用，A9）

spec §5.2"多案争用"：案子决策要用某批供应（分销商 × 料号）时由代码登记软声明；其他未关闭案子的有效声明加上本次超过
可用量时，本次声明判为冲突——写 claim_conflict 给后来的案子并 human_request 通知采购员；先到先得，不做联合优化（§1.7）。
- ClaimBook.register(case_id, decision_content, day)：Policy 决策产出时调用（F21 policy 代码执行者），逐个选中报价登记 supply_claimed。
- ClaimBook.check(case_id, mpn, distributor)：F12 草稿门控的附加检查——所用供应须有本案最新且未冲突的声明。
- 有效声明：每个 (案子, 分销商, 料号) 取最新一条；已关闭案子与冲突声明不占量。
消融开关 supply_claims 关闭时不登记、不判冲突、check 放行。
依赖：session.log。被 business.components（登记）、gate.evidence（检查）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from session.log import CASE_CLOSED, Session  # 事件日志


class ClaimBook:
    # 功能：见文件摘要。依赖：session。完成：全部状态从事件投影，重启后不丢。
    def __init__(self, session: Session, *, enabled: bool = True):
        self.session = session  # 事件日志
        self.enabled = enabled  # 消融开关 supply_claims

    def _latest(self) -> dict[tuple[str, str, str], dict]:
        """每个 (案子, 分销商, 料号) 的最新声明。"""
        out = {}  # 结果
        for e in self.session.events(type="supply_claimed"):  # 按序
            p = e["payload"]  # 内容
            out[(e["case_id"], p["distributor"], p["mpn"])] = {**p, "case_id": e["case_id"], "seq": e["seq"]}  # 覆盖
        return out  # 最新声明

    def held_by_others(self, case_id: str, distributor: str, mpn: str) -> int:
        """其他未关闭案子对同一批供应的有效（未冲突）声明总量。"""
        closed = {c["case_id"] for c in self.session.cases() if c["state"] == CASE_CLOSED}  # 已关闭案子
        return sum(c["qty"] for (cid, d, m), c in self._latest().items()  # 累计
                   if d == distributor and m == mpn and cid != case_id and cid not in closed and not c["conflict"])

    def register(self, case_id: str, decision_content: dict, *, day: int) -> list[dict]:
        """为决策里每个选中报价登记声明；冲突时通知后来的案子与采购员。返回写下的声明事件。"""
        if not self.enabled:  # 消融
            return []  # 不登记
        out = []  # 声明事件
        for d in decision_content.get("decisions", []):  # 每条决策
            c = d.get("chosen")  # 选中报价
            if not c:  # 没有选择
                continue  # 跳过
            available = c["stock"] if isinstance(c["stock"], int) else 0  # 当时可用量（未知按 0）
            others = self.held_by_others(case_id, c["distributor"], d["mpn"])  # 别人已占
            conflict = others + d["qty"] > available  # 超过可用量
            ev = self.session.append("supply_claimed", day=day, actor="claims", case_id=case_id,  # 声明
                                     payload={"mpn": d["mpn"], "distributor": c["distributor"], "qty": d["qty"],  # 供应
                                              "available": available, "held_by_others": others,  # 可用与已占
                                              "conflict": conflict},  # 是否冲突
                                     entity_keys={"mpn": [d["mpn"]], "distributor": [c["distributor"]]})  # 实体键
            out.append(ev)  # 收下
            if conflict:  # 冲突：后到的收到反馈
                msg = (f"{c['distributor']} 的 {d['mpn']} 可用 {available} 颗，其他案子已声明 {others} 颗，"  # 说明
                       f"本案再要 {d['qty']} 颗超出；请重规划（换来源或提出目标修订）")  # 引导
                self.session.append("claim_conflict", day=day, actor="claims", case_id=case_id,  # 冲突反馈
                                    payload={"mpn": d["mpn"], "distributor": c["distributor"], "message": msg},
                                    caused_by=ev["seq"])  # 因果
                self.session.append("human_request", day=day, actor="claims", case_id=case_id,  # 通知采购员
                                    payload={"role": "buyer", "node": "notice", "message": msg}, caused_by=ev["seq"])
        return out  # 声明事件

    def check(self, case_id: str, mpn: str, distributor: str) -> list[str]:
        """草稿门控附加检查：所用供应须有本案最新且未冲突的声明。"""
        if not self.enabled:  # 消融
            return []  # 放行
        claim = self._latest().get((case_id, distributor, mpn))  # 本案声明
        if claim is None:  # 没登记
            return [f"{distributor} 的 {mpn} 没有本案的供应声明（须由 Policy 决策登记）"]  # 原因
        if claim["conflict"]:  # 冲突
            return [(f"{distributor} 的 {mpn} 与其他案子的声明冲突（可用 {claim['available']}，他案已占 "
                     f"{claim['held_by_others']}），先到先得，请重规划")]  # 原因
        return []  # 通过

"""文件摘要（F12 · 证据门控、提交前重校验、上报计划员门控）

EvidenceGate 接入 F21 ProcurementExecutor 的门控挂点（签名同 minimal_gate，返回拒绝原因列表，空 = 通过），spec §2.4：
- 先做 F21 最小门控（引用存在、类型对、有效、属于本案、决策可行）。
- 建草稿 / 送审 / 提交：替代料证据。是否替代料由代码判定——料号不在任何板的 BOM 里即替代料（不看模型给的字段）；
  替代料须有本案有效的 VerificationReport（content.candidate = 料号），其等级字段不得为不可信标签（F13）；
  A 级通过，B 级须有 H1 工程师确认，C 级永远拒绝（等级要求在 config/gate.yaml）。原料号换供应商不需要报告。
- 提交另要：同一草稿已提交过 → 幂等重放放行；否则审批令牌查验（ApprovalVault）+ 提交前重校验（消融开关 presubmit_revalidation）：
  以 procurement 身份的 F11 令牌经代理层跳过缓存实时查该分销商报价，库存须覆盖数量、单价偏差不超容差；
  不通过即拒绝，原因写明变化，并对该报价写外部数据失效（F06 传播使决策与草稿失效，须重规划、重新送审）。
planner_report_gate()：上报计划员须引用本案有效 ImpactReport，理由里的数字都须出现在该 ImpactReport 中。
依赖：business.procurement.minimal_gate、gate.approval、taint.labels、session.log。被装配执行器的代码（F22、测试）与 runtime.segment 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import re  # 数字抽取、引用解析

from business.procurement import minimal_gate  # F21 最小门控
from gate.approval import ApprovalVault, load_gate_config  # 令牌保管库与配置
from session.log import ART_VALID, Session  # 事件日志
from taint.labels import UNTRUSTED, label_of, taint_of  # F13 标签

REF = re.compile(r"^(?P<id>.+)@v(?P<ver>\d+)$")  # Artifact 引用
NUM = re.compile(r"\d+(?:\.\d+)?")  # 数字


def _grade(report: dict) -> str | None:
    """验证报告的等级：content.grade 为字符串或等级汇总器输出 {grade, ...}。"""
    g = report["content"].get("grade")  # 等级字段
    return g.get("grade") if isinstance(g, dict) else g  # 取等级


class EvidenceGate:
    # -----------------------------------------------------------------------
    # 构造
    # 功能：绑定令牌保管库、代理层与 F11 签发者（重校验用）、门控配置与消融开关。
    # 依赖：ApprovalVault、ToolProxy、Authority。
    # 完成：proxy / authority 为空时不能做重校验（提交直接拒绝，写明"重校验不可用"），避免静默放行。
    # -----------------------------------------------------------------------
    def __init__(self, vault: ApprovalVault, *, proxy=None, authority=None, config: dict | None = None,
                 revalidate: bool = True):
        self.vault, self.proxy, self.authority = vault, proxy, authority  # 依赖
        self.config = config or load_gate_config()  # 门控配置
        self.revalidate = revalidate  # 消融开关 presubmit_revalidation

    # -----------------------------------------------------------------------
    # __call__：门控挂点
    # 功能：见文件摘要。
    # 依赖：minimal_gate、_substitute_evidence、_submit_checks。
    # 完成：动作名与 F21 执行器一致：draft / request_approval / submit；mpn 由建草稿时传入。
    # -----------------------------------------------------------------------
    def __call__(self, action: str, *, session: Session, case_id: str, artifact: dict | None,
                 expected_kind: str, day: int, env, mpn: str | None = None, **_) -> list[str]:
        reasons = minimal_gate(action, session=session, case_id=case_id, artifact=artifact,  # F21 最小门控
                               expected_kind=expected_kind)  # 类型
        if reasons:  # 不通过
            return reasons  # 返回
        if action != "draft":  # 送审 / 提交：对象是草稿
            mpn = artifact["content"]["mpn"]  # 草稿的料号
        reasons = self._substitute_evidence(session, case_id, mpn, env)  # 替代料证据
        if reasons or action != "submit":  # 不通过或无需更多检查
            return reasons  # 返回
        return self._submit_checks(session, case_id, artifact, day)  # 令牌与重校验

    def _substitute_evidence(self, session: Session, case_id: str, mpn: str, env) -> list[str]:
        """替代料须有足够等级的有效验证报告；原料号不需要。"""
        if mpn in {m for bom in env.state.boards.values() for m in bom}:  # 在 BOM 里：原料号换供应商
            return []  # 不需要验证报告
        status = session.artifact_status()  # 状态表
        reports = [a for a in session.store.artifacts(session.run_id)  # 本案有效的验证报告
                   if a["kind"] == "VerificationReport" and a["case_id"] == case_id
                   and a["content"].get("candidate") == mpn and status[(a["artifact_id"], a["version"])] == ART_VALID]
        if not reports:  # 没有
            return [f"{mpn} 是替代料，须有本案有效的 VerificationReport"]  # 原因
        report = reports[-1]  # 最新一份
        ref = f"{report['artifact_id']}@v{report['version']}"  # 引用
        if label_of(taint_of(report), "grade") == UNTRUSTED:  # 等级不是代码汇总的（F13）
            return [f"{ref} 的等级字段来源不可信，门控不采信"]  # 原因
        grade, sub = _grade(report), self.config["substitute"]  # 等级与规则
        if grade in sub["pass_grades"]:  # A 级
            return []  # 通过
        if grade in sub["confirm_grades"]:  # B 级：看 H1
            decisions = [e["payload"]["decision"] for e in session.events(case_id=case_id, type="human_decision")  # H1 决定
                         if e["payload"]["node"] == "H1" and e["payload"]["artifact"] == ref]  # 针对这份报告
            if decisions and decisions[-1] == "approve":  # 最近一次是确认
                return []  # 通过
            return [f"{ref} 为 {grade} 级，须工程师 H1 确认后才能采购"]  # 原因
        return [f"{ref} 为 {grade} 级，不能采购该替代料"]  # C 级等

    def _submit_checks(self, session: Session, case_id: str, draft: dict, day: int) -> list[str]:
        """提交：幂等重放放行；否则查审批令牌、做提交前重校验。"""
        ref = f"{draft['artifact_id']}@v{draft['version']}"  # 草稿引用
        if any(e["payload"]["draft"] == ref for e in session.events(case_id=case_id, type="po_submitted")):  # 已提交
            return []  # 幂等重放（环境按幂等键返回原单）
        reason = self.vault.check(ref, draft["content_hash"], day)  # 令牌
        if reason:  # 不通过
            return [reason]  # 原因
        return self._recheck(session, case_id, draft, day) if self.revalidate else []  # 重校验

    def _recheck(self, session: Session, case_id: str, draft: dict, day: int) -> list[str]:
        """提交前重校验：实时报价（跳过缓存）对比草稿快照；不通过即写外部数据失效。"""
        if self.proxy is None or self.authority is None:  # 缺依赖
            return ["提交前重校验不可用（未接入代理层），拒绝提交"]  # 不静默放行
        c = draft["content"]  # 草稿内容
        token = self.authority.issue_root(executor="procurement", case_id=case_id, mode="S1",  # procurement 身份
                                          initiated_by=f"gate:{draft['artifact_id']}")  # 由门控发起
        r = self.proxy.call("distributor_quotes", {"mpns": [c["mpn"]], "qty": c["qty"],  # 实时查该家
                                                   "distributors": [c["distributor"]]},  # 只查这一家
                            token=token, day=day, fresh=True)  # 跳过缓存
        if not r["ok"] or not r["data"]:  # 查不到
            err = (r["error"] or {}).get("type", "无数据")  # 原因
            return [f"提交前重校验：无法取得 {c['distributor']} 的实时报价（{err}），拒绝提交"]  # 原因
        row, snap = r["data"][0], c["evidence"]  # 实时行与快照
        reasons = []  # 原因
        stock = row["stock"]  # 实时库存
        if not isinstance(stock, int) or stock < c["qty"]:  # 不再能覆盖数量（A2 买空）
            reasons.append(f"提交前重校验：{c['distributor']} 库存 {snap['stock']}→{stock}（需 {c['qty']}）")  # 原因
        price, old = row["unit_price_usd"], snap["unit_price_usd"]  # 实时与快照单价
        tol = self.config["revalidation"]["price_tolerance_pct"]  # 容差
        if not isinstance(price, int | float) or abs(price - old) > abs(old) * tol / 100:  # 单价变化超容差
            reasons.append(f"提交前重校验：{c['distributor']} 单价 {old}→{price}")  # 原因
        if reasons:  # 变化了：决策依据失效
            session.invalidate_external(f"quote:{c['distributor']}:{c['mpn']}", f"day{day}:recheck",  # 外部数据变化
                                        day=day, actor="gate")  # 传播到决策与草稿
        return reasons  # 结果


# ---------------------------------------------------------------------------
# planner_report_gate：上报计划员门控
# 功能：spec §2.4"附带 ImpactReport；上报中的数字必须来自 ImpactReport"。
# 依赖：session；被 runtime.segment 的 report_to_planner 处理使用。
# 完成：引用须为本案有效的 ImpactReport；理由里每个数字都须出现在该报告的任一值（含字符串中的数字）里。
# ---------------------------------------------------------------------------
def planner_report_gate(session: Session, case_id: str, impact_ref: str, reason: str) -> list[str]:
    m = REF.match(impact_ref or "")  # 解析引用
    art = session.artifact_view(m["id"], int(m["ver"])) if m else None  # 取
    if art is None or art["kind"] != "ImpactReport" or art["case_id"] != case_id or art["status"] != ART_VALID:  # 不合格
        return [f"上报计划员须附本案有效的 ImpactReport（给的是 {impact_ref}）"]  # 原因
    known: set[str] = set()  # 报告里出现过的数字

    def walk(v):  # 递归收集
        if isinstance(v, dict):  # 字典：键与值
            for k, x in v.items():  # 逐项
                walk(k)  # 键（如工单号）
                walk(x)  # 值
        elif isinstance(v, list):  # 列表
            for x in v:  # 逐项
                walk(x)  # 递归
        elif isinstance(v, bool) or v is None:  # 布尔与空
            return  # 跳过
        else:  # 数字或文本
            known.update(NUM.findall(str(v)))  # 抽数字

    walk(art["content"])  # 收集
    made_up = [n for n in NUM.findall(reason) if n not in known]  # 报告里没有的数字
    if made_up:  # 有编造数字
        return [f"上报中的数字须来自 ImpactReport，以下数字找不到出处：{', '.join(made_up)}"]  # 原因
    return []  # 通过

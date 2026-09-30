"""文件摘要（F21 · Procurement 执行器与"采购"Server）

ProcurementExecutor 是唯一能写采购数据的组件（spec §3.3）；写操作只接受 Artifact 引用，内容由代码从被引用字段生成（§5.5）：
- create_draft(Decision 引用, 料号) → PODraft：料号、数量、分销商、单价、预计到货日来自 Decision，附所依赖报价的数值快照。
- request_approval(PODraft 引用)：写 approval_requested（绑定草稿内容哈希）与 human_request（主管 H2）。
- submit(PODraft 引用)：幂等键 = 案子 + 草稿版本 + 内容哈希前缀，写入环境（F03），写 po_submitted。
每个动作先过门控挂点 gate(action, ...) → 拒绝原因列表；本文件自带最小门控（被引用 Artifact 有效、Decision 可行、类型正确），
F12 用证据门控 + 审批令牌 + 提交前重校验替换同一挂点。拒绝时写 gate_rejected（F16 新反馈、F14 反馈卡片的来源）。
procurement_server()：三个写工具（参数只收 Artifact 引用），风险级 R1 建草稿、R1 送审、R2 提交；
仅编排者在 S1 模式下可见（F11 按风险级与 config/permissions.yaml 强制）。
依赖：session.log、simenv.api、tools.server。被编排者（经代理层）、F12、F15 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import re  # 解析 Artifact 引用
from collections.abc import Callable  # 挂点类型

from pydantic import BaseModel, Field  # 工具参数模型

from session.log import ART_VALID, Session  # 事件日志
from simenv.api import Environment  # 环境
from tools.server import Server, ToolDef  # 工具定义

REF = re.compile(r"^(?P<id>.+)@v(?P<ver>\d+)$")  # Artifact 引用：ID@v版本
Gate = Callable[..., list[str]]  # 门控挂点：返回拒绝原因（空 = 通过）


def minimal_gate(action: str, *, session: Session, case_id: str, artifact: dict | None,
                 expected_kind: str, **_) -> list[str]:
    """最小门控：引用存在、类型正确、有效、属于本案；Decision 至少一颗料可行。F12 替换为证据门控。"""
    if artifact is None:  # 引用不存在
        return ["引用的 Artifact 不存在"]  # 拒绝
    reasons = []  # 原因
    if artifact["kind"] != expected_kind:  # 类型不对
        reasons.append(f"需要 {expected_kind}，给的是 {artifact['kind']}")  # 原因
    if artifact["status"] != ART_VALID:  # 已失效或被替代
        reasons.append(f"{artifact['artifact_id']}@v{artifact['version']} 状态为 {artifact['status']}，不能使用")  # 原因
    if artifact["case_id"] != case_id:  # 不属于本案
        reasons.append("引用的 Artifact 不属于本案子")  # 原因
    if expected_kind == "ProcurementDecision" and not any(  # 一颗可行的料都没有（逐料检查在 create_draft）
            d.get("chosen") for d in artifact["content"].get("decisions", [])):  # 部分可行时允许为可行的料建草稿
        reasons.append("决策为无可行报价")  # 原因
    return reasons  # 结果


class ProcurementExecutor:
    # 功能：见文件摘要。依赖：事件日志、当前环境（崩溃恢复后环境对象会换，所以用取值函数）、门控挂点。
    def __init__(self, session: Session, get_env: Callable[[], Environment], gate: Gate = minimal_gate):
        self.session, self.get_env, self.gate = session, get_env, gate  # 依赖

    def _view(self, ref: str) -> dict | None:
        """按引用取 Artifact（含状态）；格式不对返回 None。"""
        m = REF.match(ref or "")  # 解析
        return self.session.artifact_view(m["id"], int(m["ver"])) if m else None  # 取

    def _check(self, action, case_id, day, artifact, kind, **extra) -> list[str]:
        """过门控；拒绝则写 gate_rejected。"""
        reasons = self.gate(action, session=self.session, case_id=case_id, artifact=artifact,  # 门控
                            expected_kind=kind, day=day, env=self.get_env(), **extra)  # 上下文
        if reasons:  # 拒绝
            self._reject(action, case_id, day, artifact, reasons)  # 记反馈事件
        return reasons  # 结果

    def _reject(self, action, case_id, day, artifact, reasons) -> None:
        """写 gate_rejected 事件（F16 新反馈、F14 反馈卡片的来源）。"""
        self.session.append("gate_rejected", day=day, actor="gate", case_id=case_id,  # 反馈事件
                            payload={"action": action, "reasons": reasons,  # 动作与原因
                                     "artifact": artifact and f"{artifact['artifact_id']}@v{artifact['version']}"})

    # 功能：建草稿。完成：只从 Decision 里取字段（模型无法填自由文本进写操作）；同一 Decision + 料号重复建草稿产生新版本。
    def create_draft(self, *, case_id: str, day: int, decision_ref: str, mpn: str) -> dict:
        dec = self._view(decision_ref)  # 决策
        reasons = self._check("draft", case_id, day, dec, "ProcurementDecision")  # 门控
        pick = next((d for d in dec["content"]["decisions"] if d["mpn"] == mpn), None) if not reasons else None  # 该料
        if not reasons and (pick is None or pick["chosen"] is None):  # 决策里没有这颗料的选择
            reasons = [f"决策中没有料号 {mpn} 的可行选择"]  # 原因
            self._reject("draft", case_id, day, dec, reasons)  # 记拒绝
        if reasons:  # 拒绝
            return {"ok": False, "reasons": reasons}  # 返回
        c = pick["chosen"]  # 选中的报价
        content = {"mpn": mpn, "qty": pick["qty"], "distributor": c["distributor"],  # 草稿内容：全部来自 Decision
                   "unit_price": str(c["unit_price_usd"]), "eta_day": c["eta_day"],  # 单价与到货日
                   "amount_usd": c["amount_usd"], "decision": decision_ref,  # 金额与来源
                   "evidence": {"stock": c["stock"], "unit_price_usd": c["unit_price_usd"]}}  # 数值快照（提交前重校验用）
        art = self.session.produce(f"PODraft:{case_id}:{mpn}", "PODraft", content, day=day,  # 产出
                                   actor=f"procurement@{case_id}", case_id=case_id,  # 身份
                                   deps=[{"artifact": dec["artifact_id"], "version": dec["version"]}])  # 依赖决策
        return {"ok": True, "draft": f"{art['artifact_id']}@v{art['version']}", "content": content}  # 返回

    # 功能：送审（R1）。完成：记下草稿内容哈希，审批令牌（F12）将绑定它；通知主管。
    def request_approval(self, *, case_id: str, day: int, draft_ref: str) -> dict:
        draft = self._view(draft_ref)  # 草稿
        reasons = self._check("request_approval", case_id, day, draft, "PODraft")  # 门控
        if reasons:  # 拒绝
            return {"ok": False, "reasons": reasons}  # 返回
        ev = self.session.append("approval_requested", day=day, actor=f"procurement@{case_id}",  # 送审事件
                                 case_id=case_id, payload={"draft": draft_ref,  # 草稿
                                                           "content_hash": draft["content_hash"]})  # 内容哈希
        self.session.append("human_request", day=day, actor=f"procurement@{case_id}", case_id=case_id,  # 通知主管
                            payload={"role": "supervisor", "node": "H2", "draft": draft_ref}, caused_by=ev["seq"])
        return {"ok": True, "approval_seq": ev["seq"]}  # 返回

    # 功能：提交（R2）。完成：幂等键防重复下单；环境拒绝（WriteRejected）按门控拒绝反馈。
    def submit(self, *, case_id: str, day: int, draft_ref: str) -> dict:
        draft = self._view(draft_ref)  # 草稿
        reasons = self._check("submit", case_id, day, draft, "PODraft")  # 门控（F12：令牌 + 重校验）
        if reasons:  # 拒绝
            return {"ok": False, "reasons": reasons}  # 返回
        c = draft["content"]  # 草稿内容
        key = f"{case_id}:{draft['artifact_id']}@v{draft['version']}:{draft['content_hash'][:16]}"  # 幂等键
        result = self.get_env().submit_purchase_order(mpn=c["mpn"], qty=c["qty"], distributor=c["distributor"],
                                                      unit_price=c["unit_price"], idempotency_key=key)  # 写环境
        self.session.append("po_submitted", day=day, actor=f"procurement@{case_id}", case_id=case_id,  # 提交事件
                            payload={"draft": draft_ref, "idempotency_key": key, **result},  # 采购单号与是否重复
                            entity_keys={"mpn": [c["mpn"]], "po": [result["po_id"]], "distributor": [c["distributor"]]})
        return {"ok": True, **result}  # 返回


class DraftArgs(BaseModel):
    """建草稿：只收 Decision 引用与料号。"""
    decision: str = Field(pattern=r"^.+@v\d+$")  # Decision 引用
    mpn: str  # 料号（须在 Decision 中）


class DraftRefArgs(BaseModel):
    """送审 / 提交：只收草稿引用。"""
    draft: str = Field(pattern=r"^.+@v\d+$")  # 草稿引用


def procurement_server(executor: Callable[[], ProcurementExecutor]) -> Server:
    """"采购"Server：三个写工具（R1 建草稿、R1 送审、R2 提交），参数只收 Artifact 引用。"""
    return Server("procurement", [
        ToolDef("create_po_draft", "按决策为一颗料生成采购单草稿（写操作，受门控）。例：create_po_draft(decision='Decision:K7@v1', mpn='X')",
                DraftArgs, lambda a, ctx: executor().create_draft(case_id=ctx.case_id, day=ctx.day,
                                                                  decision_ref=a.decision, mpn=a.mpn),
                risk="R1"),  # F11：可撤销写
        ToolDef("request_approval", "把草稿送主管审批（H2），之后案子等待人工。例：request_approval(draft='PODraft:K7:X@v1')",
                DraftRefArgs, lambda a, ctx: executor().request_approval(case_id=ctx.case_id, day=ctx.day,
                                                                         draft_ref=a.draft),
                risk="R1"),  # F11：可撤销写
        ToolDef("submit_po", "审批通过后提交采购单（不可撤销，受门控与审批令牌）。例：submit_po(draft='PODraft:K7:X@v1')",
                DraftRefArgs, lambda a, ctx: executor().submit(case_id=ctx.case_id, day=ctx.day, draft_ref=a.draft),
                risk="R2"),  # F11：不可撤销写
    ])

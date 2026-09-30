"""文件摘要（F14 · 预期与偏差比对，A1）

spec §5.7"预期与偏差"：决策类 Artifact 写明预期，Monitor 用代码比对预期与实际，出现偏差即生成偏差反馈并唤醒案子。
预期来自本案提交采购单所依据的草稿（PODraft.eta_day：预计到货日；到货后该料缺口应归零）。
check_deviations(session, env, short_now, day) 对每个未关闭案子的每张采购单：
- 迟到：运单预计到货日晚于预期，或已过预期日仍未到货 → deviation(kind=late)；
- 缺口未归零：采购单已到货但该料仍在缺料集合里 → deviation(kind=gap)。
同一采购单同类偏差只报一次（去重键）。返回新写的偏差事件。
依赖：session.log、simenv.model（状态常量）。被 feedback.loop 每日调用。
"""
from __future__ import annotations  # 类型注解延迟求值

import re  # 解析 Artifact 引用

from session.log import CASE_CLOSED, Session  # 事件日志
from simenv.model import PO_ARRIVED, SH_IN_TRANSIT  # 采购单与运单状态

REF = re.compile(r"^(?P<id>.+)@v(?P<ver>\d+)$")  # Artifact 引用


def _emit(session: Session, case_id: str, day: int, kind: str, po_id: str, mpn: str, text: str,
          expected: int | None) -> dict | None:
    """写一条偏差（去重）；已报过返回 None。"""
    key = f"deviation:{kind}:{po_id}"  # 去重键
    if session.store.event_by_dedup(session.run_id, key) is not None:  # 报过
        return None  # 不重复
    return session.append("deviation", day=day, actor="monitor", case_id=case_id,  # 偏差事件
                          payload={"kind": kind, "po_id": po_id, "mpn": mpn, "expected_day": expected, "text": text},
                          entity_keys={"po": [po_id], "mpn": [mpn]}, dedup_key=key)  # 实体键与去重


# 功能：见文件摘要。依赖：env 的采购单与运单、当前缺料集合。完成：没有预期（草稿没写到货日）的采购单只查缺口。
def check_deviations(session: Session, env, short_now: set[str], day: int) -> list[dict]:
    out = []  # 新偏差
    state = env.state  # 环境状态
    for c in session.cases():  # 每个案子
        if c["state"] == CASE_CLOSED:  # 已关闭
            continue  # 跳过
        for e in session.events(case_id=c["case_id"], type="po_submitted"):  # 本案采购单
            po_id = e["payload"]["po_id"]  # 采购单号
            po = state.purchase_orders.get(po_id)  # 采购单
            if po is None:  # 不在环境里（测试或数据不全）
                continue  # 跳过
            m = REF.match(e["payload"].get("draft") or "")  # 依据的草稿
            draft = session.artifact_view(m["id"], int(m["ver"])) if m else None  # 草稿
            expected = draft["content"].get("eta_day") if draft else None  # 预期到货日
            sh = next((s for s in state.shipments.values() if s.po_id == po_id), None)  # 对应运单
            ev = None  # 本张采购单的偏差
            if expected is not None and po.status != PO_ARRIVED:  # 有预期、未到货
                if sh is not None and sh.status == SH_IN_TRANSIT and sh.eta_day > expected:  # 运单被推迟
                    ev = _emit(session, c["case_id"], day, "late", po_id, po.mpn,  # 迟到
                               f"{po_id} 预计第 {expected} 天到货，运单 {sh.id} 现预计第 {sh.eta_day} 天", expected)
                elif day > expected:  # 过了预期日还没到
                    ev = _emit(session, c["case_id"], day, "late", po_id, po.mpn,  # 迟到
                               f"{po_id} 预计第 {expected} 天到货，第 {day} 天仍未到（状态 {po.status}）", expected)
            elif po.status == PO_ARRIVED and po.mpn in short_now:  # 到货了但缺口还在
                ev = _emit(session, c["case_id"], day, "gap", po_id, po.mpn,  # 缺口未归零
                           f"{po_id} 已到货，但 {po.mpn} 仍有缺口", expected)
            if ev is not None:  # 新偏差
                out.append(ev)  # 收下
    return out  # 结果

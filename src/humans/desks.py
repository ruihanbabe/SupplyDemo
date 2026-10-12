"""文件摘要（F05 · 新人工入口：计划员 H5、采购员 H3 消歧）

与 gate.approval.HumanDesk（H1、H2）、orchestrator.planning.GoalDesk（H4）、KnowledgeBase.confirm / reject（H1、H3 记忆）并列，
模拟人工（humans.sim）与 F25 Web 共用这些入口，不走捷径：
- PlannerDesk.reply：计划员回复上报（H5），可给出各工单推迟天数；先经环境写入 postpone_work_order 推迟（幂等键，崩溃重做无害），
  再写 H5 人工决定。天数上限取 config/humans.yaml（G7）。Agent 没有任何调用它的工具（G8）。
- BuyerDesk.resolve：采购员回答 Resolver 的料号消歧（H3），所答料号必须在该请求的候选里。
核对不过一律抛 ValueError（页面据此报错，模拟人工据此记跳过）。
依赖：session.log、simenv.api / engine（写入校验）、config/humans.yaml。被 humans.sim、orchestrator.assembly、F25 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Callable  # 取当前环境的函数
from pathlib import Path  # 配置路径

import yaml  # 读配置

from session.log import Session  # 事件日志
from simenv import engine  # 写入校验
from simenv.api import Environment  # 环境

HUMANS_CONFIG = Path(__file__).resolve().parents[2] / "config" / "humans.yaml"  # 配置文件


def load_humans_config(path: Path = HUMANS_CONFIG) -> dict:
    """读模拟人工配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


def _request(session: Session, request_seq: int, node: str) -> dict:
    """取人工请求事件并核对节点；不对抛 ValueError。"""
    ev = session.store.event(session.run_id, request_seq)  # 请求事件
    if ev is None or ev["type"] != "human_request" or ev["payload"].get("node") != node:  # 不是该节点的请求
        raise ValueError(f"事件 #{request_seq} 不是 {node} 人工请求")  # 报错
    return ev  # 返回


def _decision(session: Session, ev: dict, node: str, decision: str, *, by: str, day: int, **extra) -> dict:
    """写一条针对请求的人工决定（artifact 记为 request#事件号）。"""
    return session.append("human_decision", day=day, actor=f"human:{by}", case_id=ev["case_id"],  # 人工事件
                          payload={"node": node, "artifact": f"request#{ev['seq']}", "decision": decision,  # 节点、对象、决定
                                   "by": by, "note": "", **extra}, caused_by=ev["seq"])  # 内容与因果


# ---------------------------------------------------------------------------
# PlannerDesk：计划员回复（H5）
# 功能：见文件摘要。
# 依赖：session、get_env（当前环境）、配置 planner.max_postpone_days。
# 完成：全部推迟先逐条校验（任何一条不过则整体不做）；环境写入在前、人工决定在后，决定里记下各工单的新开工日。
# ---------------------------------------------------------------------------
class PlannerDesk:
    def __init__(self, session: Session, get_env: Callable[[], Environment], config: dict | None = None):
        self.session, self.get_env = session, get_env  # 依赖
        self.max_days = (config or load_humans_config())["planner"]["max_postpone_days"]  # 推迟上限

    def reply(self, request_seq: int, *, postpone: dict[str, int], by: str, day: int, note: str = "") -> dict:
        """回复上报：postpone 为 {工单号: 推迟天数}，可为空（不推迟）。"""
        ev = _request(self.session, request_seq, "H5")  # 请求
        env = self.get_env()  # 当前环境
        writes = []  # 待执行的写入
        for wo_id, days in sorted(postpone.items()):  # 按工单号固定顺序
            if isinstance(days, int) and days > self.max_days:  # 超上限
                raise ValueError(f"{wo_id} 推迟 {days} 天超过上限 {self.max_days} 天")  # 报错
            w = {"kind": "postpone_work_order", "idempotency_key": f"H5:{request_seq}:{wo_id}",  # 幂等键绑定请求与工单
                 "payload": {"wo_id": wo_id, "days": days}}  # 内容
            if not engine.is_duplicate(env.state, w):  # 未执行过的才校验
                try:  # 校验
                    engine.validate_write(env.state, w)  # 不合法抛错
                except engine.WriteRejected as e:  # 转成入口错误
                    raise ValueError(str(e)) from None  # 报错
            writes.append(w)  # 记下
        moved = {}  # 工单 → 新开工日
        for w in writes:  # 逐条执行
            p = w["payload"]  # 内容
            r = env.postpone_work_order(wo_id=p["wo_id"], days=p["days"], idempotency_key=w["idempotency_key"])  # 写入
            moved[p["wo_id"]] = r["start_day"]  # 新开工日
        return _decision(self.session, ev, "H5", "reply", by=by, day=day,  # 人工决定
                         postpone=dict(sorted(postpone.items())), new_start_days=moved, reply_note=note)  # 推迟与结果


# ---------------------------------------------------------------------------
# BuyerDesk：采购员回答料号消歧（H3）
# 功能：见文件摘要。依赖：session。
# 完成：料号不在候选里抛 ValueError；决定里带所答料号。
# ---------------------------------------------------------------------------
class BuyerDesk:
    def __init__(self, session: Session):
        self.session = session  # 事件日志

    def resolve(self, request_seq: int, *, mpn: str, by: str, day: int) -> dict:
        """回答消歧：mpn 须为请求候选之一。"""
        ev = _request(self.session, request_seq, "H3")  # 请求
        cands = ev["payload"].get("candidates")  # 候选
        if cands is None:  # 这是别名记忆请求，不是消歧
            raise ValueError(f"事件 #{request_seq} 是别名记忆确认，请用 KnowledgeBase.confirm")  # 报错
        if mpn not in cands:  # 候选外
            raise ValueError(f"{mpn} 不在候选 {cands} 里")  # 报错
        return _decision(self.session, ev, "H3", "approve", by=by, day=day, mpn=mpn)  # 人工决定

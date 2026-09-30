"""文件摘要（F10 · 环境事件实体键与 Monitor）

- env_day_keys(state, log)：把当日环境事实转成实体键——运单、采购单、工单、订单，另补料号（到货运单、发货采购单、
  开工 / 停线 / 完工工单所需的料）与板（工单所属板），便于案子订阅按料号命中。
- monitor_shortages(session, state, env_event)：Monitor（代码，spec §3.3）。对当日状态跑影响分析（F21 analyze），
  每颗缺料写一条 monitor_shortage 事件（料号、缺口、首次缺料日、涉及工单），caused_by = 当日 env_day（F04 的异常事件接入后
  可改指向异常事件，根因沿 caused_by 追）。去重键 = 料号 + 缺口 + 首次缺料日：同样的缺口不重复报。
  返回 (本次新写的事件, 当前全部缺料的料号集合)。
依赖：business.impact、simenv.engine.need_of、session.log。被 runtime.segment.Scheduler（实体键）与 cases.router 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from business.impact import analyze  # 影响分析
from session.log import Session  # 事件日志
from simenv.engine import need_of  # 工单用料
from simenv.model import State  # 环境状态


# 功能：当日环境事实 → 实体键。依赖：state 的运单、采购单、工单表。完成：每类去重排序由 Session.append 统一做。
def env_day_keys(state: State, log: dict) -> dict[str, list[str]]:
    wos = log["started"] + log["stopped"] + log["done"]  # 当日涉及的工单
    mpns = [state.shipments[s].mpn for s in log["arrived"] if s in state.shipments]  # 到货运单的料
    mpns += [state.purchase_orders[p].mpn for p in log["shipped"] if p in state.purchase_orders]  # 发货采购单的料
    for w in wos:  # 工单所需的料
        if w in state.work_orders:  # 存在
            mpns += list(need_of(state, state.work_orders[w]))  # BOM 料号
    boards = [state.work_orders[w].board for w in wos if w in state.work_orders]  # 工单所属板
    return {"shipment": log["arrived"], "po": log["shipped"], "work_order": wos,  # 运单、采购单、工单
            "order": log["orders_late"], "mpn": mpns, "board": boards}  # 订单、料号、板


# 功能：Monitor 每日缺料检测。依赖：analyze、session.append（去重键）。完成：只返回本次新写的事件（已报过的同样缺口被去重）。
def monitor_shortages(session: Session, state: State, env_event: dict) -> tuple[list[dict], set[str]]:
    report = analyze(state)  # 当日影响分析（副本上前推，真实环境不变）
    new = []  # 新事件
    for s in report["shortages"]:  # 每颗缺料
        key = f"shortage:{s['mpn']}:{s['gap']}:{s['first_short_day']}"  # 去重键
        existed = session.store.event_by_dedup(session.run_id, key) is not None  # 是否报过
        ev = session.append("monitor_shortage", day=state.day, actor="monitor",  # Monitor 事件
                            payload={"mpn": s["mpn"], "gap": s["gap"], "first_short_day": s["first_short_day"],  # 缺口
                                     "work_orders": s["work_orders"]},  # 涉及工单
                            entity_keys={"mpn": [s["mpn"]], "work_order": s["work_orders"]},  # 实体键
                            caused_by=env_event["seq"], dedup_key=key)  # 因果与去重
        if not existed:  # 新的
            new.append(ev)  # 收下
    return new, {s["mpn"] for s in report["shortages"]}  # 新事件与当前缺料集合

"""文件摘要（F21 · 影响分析）

analyze(state) 在环境状态的内存副本上计算影响（spec §2.3 影响分析、§3.3），不修改真实环境：
- 供需时间线（每颗料）：可用 = 在库 + 按预计到货日累加的在途；需求 = 未开工工单按"可开工日"（计划开工日与明天取晚）累加，
  按与引擎相同的优先级（开工日、工单号）排序；得出缺口数量（累计需求 − 累计可用的最大值）与首次缺料日、涉及的工单。
- 不干预前推：在副本上按引擎推进到模拟终点，记录各工单预计开工日与停线天数、各订单预计延期天数。
- scope 可按料号、运单、工单、订单过滤报告（计算始终是全量的）。
数字全部由代码算出，LLM 只能引用（spec §2.3）。依赖：simenv.model / engine。被 business.components（代码执行者）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from simenv.engine import advance, need_of, priority_order  # 引擎：推进、需求、优先级
from simenv.model import SH_IN_TRANSIT, WO_DONE, WO_PLANNED, WO_STOPPED, State  # 状态与常量


# ---------------------------------------------------------------------------
# gap_timeline：每颗料的缺口
# 功能：见文件摘要第一条。
# 依赖：priority_order、need_of（与引擎一致的优先级与需求口径）。
# 完成：返回 {料号: {gap, first_short_day, work_orders}}，只含有缺口的料。
# ---------------------------------------------------------------------------
def gap_timeline(state: State) -> dict[str, dict]:
    tomorrow = state.day + 1  # 最早可开工日
    supply: dict[str, list[tuple[int, int]]] = {}  # 料号 → [(日, 数量)]
    for mpn, qty in state.on_hand.items():  # 在库
        supply.setdefault(mpn, []).append((state.day, qty))  # 今天已有
    for sh in state.shipments.values():  # 在途
        if sh.status == SH_IN_TRANSIT:  # 只算在途
            supply.setdefault(sh.mpn, []).append((max(sh.eta_day, tomorrow), sh.qty))  # 到货日
    demand: dict[str, list[tuple[int, int, str]]] = {}  # 料号 → [(日, 数量, 工单)]
    for wo in priority_order(state):  # 未开工工单，按优先级
        when = max(wo.start_day, tomorrow)  # 可开工日
        for mpn, qty in need_of(state, wo).items():  # 每颗料
            demand.setdefault(mpn, []).append((when, qty, wo.id))  # 需求
    report = {}  # 结果
    for mpn in sorted(demand):  # 有需求的料
        points = sorted({d for d, _ in supply.get(mpn, [])} | {d for d, _, _ in demand[mpn]})  # 时间点
        gap, first, wos = 0, None, []  # 缺口、首次缺料日、涉及工单
        for day in points:  # 按时间推进
            have = sum(q for d, q in supply.get(mpn, []) if d <= day)  # 累计可用
            need = sum(q for d, q, _ in demand[mpn] if d <= day)  # 累计需求
            if need > have:  # 缺
                first = day if first is None else first  # 首次缺料日
                gap = max(gap, need - have)  # 最大缺口
        if gap:  # 有缺口
            wos = [w for d, _, w in demand[mpn] if d >= first]  # 首次缺料日及之后开工的工单
            report[mpn] = {"gap": gap, "first_short_day": first, "work_orders": wos}  # 记录
    return report  # 结果


# ---------------------------------------------------------------------------
# project：不干预前推
# 功能：在副本上推进到终点，记录工单与订单的预计结果。
# 依赖：State.from_dict（深拷贝）、engine.advance。
# 完成：只报告受影响的（停线 > 0 或延期 > 0 或开工推迟）工单与订单。
# ---------------------------------------------------------------------------
def project(state: State) -> tuple[dict, dict]:
    copy = State.from_dict(state.to_dict())  # 副本，真实环境不受影响
    while copy.day < copy.horizon_days:  # 推进到终点
        advance(copy)  # 不加注入：只看已知信息下的后果
    wos = {w.id: {"planned_start": w.start_day, "projected_start": w.started_day,  # 预计开工
                  "stop_days": w.stop_days - state.work_orders[w.id].stop_days,  # 新增停线天数
                  "done": w.status == WO_DONE}  # 是否能在终点前完工
           for w in copy.work_orders.values()  # 每张工单
           if state.work_orders[w.id].status in (WO_PLANNED, WO_STOPPED)  # 只看尚未开工的
           and (w.stop_days > state.work_orders[w.id].stop_days or w.started_day != w.start_day)}  # 受影响
    orders = {o.id: {"due_day": o.due_day, "projected_completed_day": o.completed_day,  # 预计交付
                     "delay_days": o.delay_days}  # 预计延期（到终点为止）
              for o in copy.orders.values() if o.delay_days > 0}  # 只看延期的
    return wos, orders  # 结果


# ---------------------------------------------------------------------------
# analyze：影响分析主入口
# 功能：合成 ImpactReport 内容；scope 过滤报告范围。
# 依赖：gap_timeline、project。
# 完成：scope 支持 mpns / shipments / work_orders / orders；运单映射到其料号；工单与订单按受影响料号关联。
# ---------------------------------------------------------------------------
def analyze(state: State, scope: dict | None = None) -> dict:
    gaps = gap_timeline(state)  # 缺口
    wos, orders = project(state)  # 前推
    scope = scope or {}  # 范围
    mpns = set(scope.get("mpns", []))  # 料号范围
    for sid in scope.get("shipments", []):  # 运单 → 料号
        if sid in state.shipments:  # 存在
            mpns.add(state.shipments[sid].mpn)  # 加入
    if mpns or scope.get("work_orders") or scope.get("orders"):  # 有范围
        gaps = {m: g for m, g in gaps.items() if m in mpns  # 料号命中
                or set(g["work_orders"]) & set(scope.get("work_orders", []))}  # 或工单命中
        related_wos = {w for g in gaps.values() for w in g["work_orders"]} | set(scope.get("work_orders", []))  # 相关工单
        wos = {w: v for w, v in wos.items() if w in related_wos}  # 过滤工单
        related_orders = {state.work_orders[w].order_id for w in related_wos if w in state.work_orders}  # 相关订单
        related_orders |= set(scope.get("orders", []))  # 加上指定订单
        orders = {o: v for o, v in orders.items() if o in related_orders}  # 过滤订单
    return {"day": state.day, "scope": scope,  # 当日与范围
            "shortages": [{"mpn": m, **g} for m, g in sorted(gaps.items())],  # 缺口（按料号）
            "work_orders": wos, "orders": orders,  # 工单与订单
            "total_gap_parts": len(gaps),  # 缺料颗数
            "projected_stop_days": sum(v["stop_days"] for v in wos.values()),  # 预计停线天数
            "projected_delay_days": sum(v["delay_days"] for v in orders.values())}  # 预计延期天数

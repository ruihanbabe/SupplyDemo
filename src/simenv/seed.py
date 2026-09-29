"""文件摘要（F03 环境内核 · 按种子生成初始状态）

本文件把"参数 + BOM + 种子"变成第 0 天的完整 State（spec §1.3 模拟环境、§0.2 可复现）。
生成顺序固定：订单 → 工单排产（拆批、分配产线、串行不重叠）→ 初始在库 → 初始在途采购单与运单。
- 随机只出现在这里（random.Random(seed)）；日推进本身不含随机数。
- 少数短缺料号的初始在库只覆盖一段时间的需求，初始在途补一部分：不加干预时，环境会在中后期自然缺料停线，给 Harness 施压。
依赖：model（对象与状态）、engine.need_of（工单需求）。被 api.Environment.create、cli 与测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import random  # 种子化伪随机数

from simenv.engine import need_of  # 计算一张工单的物料需求
from simenv.model import (  # 领域对象与常量
    PO_SHIPPED,
    Order,
    PurchaseOrder,
    Shipment,
    State,
    WorkOrder,
)


# ---------------------------------------------------------------------------
# generate：生成第 0 天的状态
# 功能：见文件摘要；同一 (params, boms, seed) 必然得到同一状态（哈希相同）。
# 依赖：_orders、_schedule、_stock、_in_transit；params 来自 params.load_params，boms 来自 bom.load_boms。
# 完成：只为 params.boards 里配置了的板生成订单；BOM 里有但未配置的板不生产。
# ---------------------------------------------------------------------------
def generate(params: dict, boms: dict[str, dict[str, int]], seed: int) -> State:
    rng = random.Random(seed)  # 本次生成唯一的随机源
    boards = {b: boms[b] for b in params["boards"]}  # 只取配置了订单规则的板的 BOM
    state = State(day=0, seed=seed, horizon_days=params["horizon_days"],  # 第 0 天、种子、总天数
                  boards=boards, lines=list(params["lines"]),  # BOM 与产线
                  distributors={k: dict(v) for k, v in params["distributors"].items()})  # 分销商参数拷贝
    _orders(state, params, rng)  # 生成订单
    _schedule(state, params, rng)  # 拆工单并排到产线
    short = _stock(state, params, rng)  # 初始在库，得到短缺料号
    _in_transit(state, params, rng, short)  # 短缺料号的初始在途
    return state  # 返回第 0 天状态


# ---------------------------------------------------------------------------
# _orders：生成客户订单
# 功能：每块板随机若干张订单，台数按步长取整，到期日在配置区间内；订单号按到期日先后编号。
# 依赖：被 generate 调用。
# 完成：先收集再按 (到期日, 板) 排序编号，编号与生成顺序无关、便于阅读。
# ---------------------------------------------------------------------------
def _orders(state: State, params: dict, rng: random.Random) -> None:
    drafts = []  # 待编号的订单草稿 (到期日, 板, 台数)
    for board, rule in params["boards"].items():  # 每块板
        for _ in range(rng.randint(*rule["orders"])):  # 随机张数
            step = rule["qty_step"]  # 台数步长
            qty = rng.randint(rule["qty"][0] // step, rule["qty"][1] // step) * step  # 按步长取整的台数
            due = rng.randint(*params["orders"]["due_day"])  # 到期日
            drafts.append((due, board, max(qty, step)))  # 台数至少一个步长
    for due, board, qty in sorted(drafts):  # 按到期日先后
        oid = state.next_id("O")  # 分配订单号
        state.orders[oid] = Order(id=oid, board=board, qty=qty, due_day=due)  # 登记订单


# ---------------------------------------------------------------------------
# _schedule：把订单拆成工单并排到产线（给定计划，G8：之后谁也不改）
# 功能：每张订单按单批上限拆批；每批的期望开工日 = 到期日 − 缓冲 − 工期；按期望开工日先后，
#       分配给最早空闲的产线，实际计划开工日 = max(期望开工日, 产线空闲日, 第 1 天)，同产线不重叠。
# 依赖：被 generate 调用。
# 完成：工单号按计划开工先后编号；到期太紧的订单会被排到期后（计划本身就会延期，是真实工厂里常见的情况）。
# ---------------------------------------------------------------------------
def _schedule(state: State, params: dict, rng: random.Random) -> None:
    wo_rule = params["work_orders"]  # 工单参数
    batches = []  # 待排的批 (期望开工日, 订单号, 批序号, 台数, 工期)
    for order in state.orders.values():  # 按订单号顺序
        left, n = order.qty, 0  # 剩余台数、批序号
        while left > 0:  # 还有没拆完的台数
            qty = min(left, wo_rule["batch_max"])  # 本批台数
            duration = rng.randint(*wo_rule["duration_days"])  # 本批工期
            want = order.due_day - wo_rule["lead_days"] - duration  # 期望开工日
            batches.append((want, order.id, n, qty, duration))  # 记下这一批
            left, n = left - qty, n + 1  # 扣减并递增批序号
    free = {line: 1 for line in state.lines}  # 每条产线最早可开工的日子
    planned = []  # 已排定的 (开工日, 订单号, 批序号, 产线, 台数, 工期)
    for want, oid, n, qty, duration in sorted(batches):  # 按期望开工日先后排
        line = min(state.lines, key=lambda ln: (free[ln], ln))  # 最早空闲的产线，同日取名字小的
        start = max(want, free[line], 1)  # 计划开工日
        free[line] = start + duration  # 该产线下一次可开工的日子
        planned.append((start, oid, n, line, qty, duration))  # 记下排定结果
    for start, oid, _, line, qty, duration in sorted(planned):  # 按开工日编号
        wid = state.next_id("W")  # 分配工单号
        state.work_orders[wid] = WorkOrder(id=wid, board=state.orders[oid].board, order_id=oid,  # 工单
                                           line=line, qty=qty, start_day=start,  # 产线、台数、开工日
                                           duration_days=duration)  # 工期


# ---------------------------------------------------------------------------
# demand_until：某颗料在某天之前（含）开工的工单总需求
# 功能：给在库与在途数量定量用。
# 依赖：need_of；被 _stock、_in_transit 使用。
# 完成：按计划开工日筛工单并累加。
# ---------------------------------------------------------------------------
def demand_until(state: State, mpn: str, day: int) -> int:
    return sum(need_of(state, w).get(mpn, 0)  # 该工单对这颗料的需求
               for w in state.work_orders.values() if w.start_day <= day)  # 只算该日前开工的


# ---------------------------------------------------------------------------
# all_parts：两块板涉及的全部料号（排序去重）
# 功能：给库存生成一个稳定的遍历顺序（保证随机数消耗顺序一致）。
# 依赖：被 _stock、_in_transit 使用。
# 完成：跨板去重（库存按料号全局共享，spec §1.7 ④）。
# ---------------------------------------------------------------------------
def all_parts(state: State) -> list[str]:
    return sorted({m for bom in state.boards.values() for m in bom})  # 去重后排序


# ---------------------------------------------------------------------------
# _stock：初始在库
# 功能：按比例抽出少数"短缺料号"，它们的在库只覆盖到随机覆盖日之前开工的工单需求；其余料号在库覆盖全程需求。
#       这样不加干预时只有少数料号在中后期缺料，缺料有具体原因可查，而不是一开局全部停线。
# 依赖：demand_until、all_parts；被 generate 调用。
# 完成：结果写入 state.on_hand（含 0，便于查询时每颗料都有记录）；返回短缺料号列表给 _in_transit。
# ---------------------------------------------------------------------------
def _stock(state: State, params: dict, rng: random.Random) -> list[str]:
    rule = params["initial_stock"]  # 在库参数
    short = []  # 短缺料号
    for mpn in all_parts(state):  # 稳定顺序遍历料号
        if rng.random() < rule["short_fraction"]:  # 抽中为短缺料号
            cover = rng.randint(*rule["cover_days"])  # 在库能撑到第几天
            short.append(mpn)  # 记下
        else:  # 其余料号
            cover = state.horizon_days  # 在库覆盖全程
        state.on_hand[mpn] = demand_until(state, mpn, cover)  # 在库数量
    return short  # 返回短缺料号


# ---------------------------------------------------------------------------
# _in_transit：初始在途采购单与运单
# 功能：短缺料号中按比例抽一部分已有在途货，数量 = 在库覆盖之后再往后若干天的需求增量，随机分销商、随机到货日。
# 依赖：demand_until；被 generate 调用，输入是 _stock 返回的短缺料号。
# 完成：在途采购单标记 from_run=False（不计入采购成本）、单价记 "0"（开局前已付，不属于本次运行的决策）。
#       需求增量为 0 的料号不建在途单。
# ---------------------------------------------------------------------------
def _in_transit(state: State, params: dict, rng: random.Random, short: list[str]) -> None:
    rule = params["in_transit"]  # 在途参数
    names = sorted(state.distributors)  # 分销商按名字排序，保证随机选择可复现
    for mpn in short:  # 只看短缺料号（已排序）
        if rng.random() >= rule["fraction"]:  # 按比例抽中才有在途
            continue  # 没抽中
        eta = rng.randint(*rule["eta_day"])  # 预计到货日
        distributor = rng.choice(names)  # 哪个分销商
        covered = state.on_hand[mpn]  # 在库已覆盖的需求
        horizon = eta + rule["cover_extra_days"]  # 在途要覆盖到的日子
        qty = demand_until(state, mpn, horizon) - covered  # 需求增量
        if qty <= 0:  # 没有增量需求
            continue  # 不建在途单
        po = PurchaseOrder(id=state.next_id("PO"), mpn=mpn, qty=qty, distributor=distributor,  # 在途采购单
                           unit_price="0", submitted_day=0, ship_day=0,  # 开局前已下
                           status=PO_SHIPPED, from_run=False)  # 已发货、不计成本
        sh = Shipment(id=state.next_id("SH"), po_id=po.id, mpn=mpn, qty=qty,  # 对应运单
                      eta_day=eta, original_eta_day=eta)  # 到货日
        po.shipment_id = sh.id  # 回填运单号
        state.purchase_orders[po.id] = po  # 登记采购单
        state.shipments[sh.id] = sh  # 登记运单

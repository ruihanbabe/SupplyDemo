"""文件摘要（F03 环境内核 · 日推进与写入）

本文件是模拟工厂的"物理定律"：把 State 推进一天（spec §4.2），以及把一条外部写入作用到 State。
- advance(state, injectors)：注入挂点 → 到货 → 发货 → 重算预留 → 领料开工/缺料停线 → 完工 → 订单到期。
- apply_write(state, write)：submit_po（下采购单）与 postpone_work_order（F05 计划员推迟工单开工日，只有人工入口调用），
  都带幂等键，重复写入返回同一结果。
- 推进过程不含任何随机数：同一初始状态 + 同一写入序列 ⇒ 同一结果，这是快照恢复与分叉的前提。
依赖：model（领域对象与状态）。被 api.Environment（对外门面）、测试、F04（注入函数通过 injectors 挂入）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Callable, Iterable  # 注入函数与可迭代类型注解
from decimal import Decimal, InvalidOperation  # 校验单价是合法十进制数

from simenv.model import (  # 引入领域对象与状态常量
    PO_ARRIVED,  # 采购单：已到货
    PO_SHIPPED,  # 采购单：已发货
    PO_SUBMITTED,  # 采购单：已提交
    SH_ARRIVED,  # 运单：已到货
    SH_IN_TRANSIT,  # 运单：在途
    WO_DONE,  # 工单：已完工
    WO_PLANNED,  # 工单：未开工
    WO_STARTED,  # 工单：在制
    WO_STOPPED,  # 工单：缺料停线
    PurchaseOrder,  # 采购单对象
    Shipment,  # 运单对象
    State,  # 整体状态
    WorkOrder,  # 工单对象
)

#: 注入函数签名：接收状态与当天日序号，就地修改状态（F04 的异常注入按此实现）。
Injector = Callable[[State, int], None]  # 类型别名，便于阅读


class WriteRejected(ValueError):
    """外部写入不合法（料号不存在、数量非正、分销商未知等），整条写入不生效。"""


# ---------------------------------------------------------------------------
# advance：把状态推进一天
# 功能：执行 spec §4.2 的一日机制，顺序固定（完工先于开工：当天完工腾出的产线当天可接下一单）；返回当天发生了什么（给 sim-run 打印与日后事件接入 F10 用）。
# 依赖：下方各 _step 函数；被 api.Environment.advance 调用；injectors 由 F04 提供（F03 默认空）。
# 完成：七个步骤按固定顺序执行，超出模拟总天数时拒绝推进。
# ---------------------------------------------------------------------------
def advance(state: State, injectors: Iterable[Injector] = ()) -> dict[str, list]:
    if state.day >= state.horizon_days:  # 已到模拟终点
        raise RuntimeError(f"已到模拟终点第 {state.horizon_days} 天，不能再推进")  # 明确报错而不是静默
    state.day += 1  # 进入新的一天
    day = state.day  # 取出当天日序号，后面反复用
    log: dict[str, list] = {"arrived": [], "shipped": [], "started": [],  # 当天事件收集器
                            "stopped": [], "done": [], "orders_late": []}  # 六类当日事实
    for inject in injectors:  # ① 注入挂点：先注入当天的异常（spec §4.2）
        inject(state, day)  # 注入函数就地修改状态
    _arrive(state, day, log)  # ② 运单到期到货入库
    _ship(state, day, log)  # ③ 已提交采购单到发货日则发货、生成运单
    _finish_work_orders(state, day, log)  # ④ 到完工日的工单完工、成品计入订单，并腾出产线
    _reserve(state)  # ⑤ 按开工优先级重算预留
    _start_work_orders(state, day, log)  # ⑥ 轮到的工单领料开工，缺料则停线
    _check_orders(state, day, log)  # ⑦ 订单到期检查，累计延期
    return log  # 返回当天事实摘要


# ---------------------------------------------------------------------------
# _arrive：到货入库
# 功能：当前预计到货日 ≤ 今天的在途运单到厂，数量加进在库，对应采购单转"已到货"。
# 依赖：被 advance 调用；运单由 _ship 或 seed 创建，eta 可被 F04 注入推迟。
# 完成：按运单号排序处理保证顺序确定；用 ≤ 而非 == 防止注入把 eta 改到过去时货物永远不到。
# ---------------------------------------------------------------------------
def _arrive(state: State, day: int, log: dict) -> None:
    for sh in sorted(state.shipments.values(), key=lambda s: s.id):  # 按编号确定顺序遍历运单
        if sh.status != SH_IN_TRANSIT or sh.eta_day > day:  # 已到或未到期的跳过
            continue  # 看下一张
        sh.status = SH_ARRIVED  # 运单标记到货
        sh.arrived_day = day  # 记录实际到货日
        state.on_hand[sh.mpn] = state.on_hand.get(sh.mpn, 0) + sh.qty  # 入库
        state.purchase_orders[sh.po_id].status = PO_ARRIVED  # 对应采购单转已到货
        log["arrived"].append(sh.id)  # 记入当天事实


# ---------------------------------------------------------------------------
# _ship：分销商发货
# 功能：已提交的采购单到计划发货日即发货，按该分销商运输天数生成运单。
# 依赖：被 advance 调用；采购单由 apply_write 创建；分销商参数来自 state.distributors（源自配置，G7）。
# 完成：生成运单、回填运单号、转"已发货"；按单号排序保证运单编号可复现。
# ---------------------------------------------------------------------------
def _ship(state: State, day: int, log: dict) -> None:
    for po in sorted(state.purchase_orders.values(), key=lambda p: p.id):  # 按单号顺序遍历
        if po.status != PO_SUBMITTED or po.ship_day > day:  # 非已提交或未到发货日的跳过
            continue  # 看下一张
        eta = day + state.distributors[po.distributor]["transit_days"]  # 预计到货日 = 今天 + 运输天数
        sh = Shipment(id=state.next_id("SH"), po_id=po.id, mpn=po.mpn,  # 新建运单
                      qty=po.qty, eta_day=eta, original_eta_day=eta)  # 数量与到货日
        state.shipments[sh.id] = sh  # 登记运单
        po.status = PO_SHIPPED  # 采购单转已发货
        po.shipment_id = sh.id  # 回填运单号
        log["shipped"].append(po.id)  # 记入当天事实


# ---------------------------------------------------------------------------
# priority_order：未开工工单的优先级顺序
# 功能：定义"谁先用库存"：计划开工日早的先，同日按工单号。预留与领料都用这个顺序，保证一致。
# 依赖：被 _reserve、_start_work_orders 使用。
# 完成：只含未开工（计划中或停线中）的工单。
# ---------------------------------------------------------------------------
def priority_order(state: State) -> list[WorkOrder]:
    pending = [w for w in state.work_orders.values()  # 取所有工单
               if w.status in (WO_PLANNED, WO_STOPPED)]  # 只要还没开工的
    return sorted(pending, key=lambda w: (w.start_day, w.id))  # 开工日优先，其次工单号


# ---------------------------------------------------------------------------
# need_of：一张工单需要的全部物料
# 功能：BOM 单板用量 × 工单台数，得到领料清单。
# 依赖：state.boards（seed 从原理图 BOM 生成）；被 _reserve、_start_work_orders、api 使用。
# 完成：返回料号 → 需求数量。
# ---------------------------------------------------------------------------
def need_of(state: State, wo: WorkOrder) -> dict[str, int]:
    return {mpn: per_board * wo.qty  # 单板用量乘台数
            for mpn, per_board in state.boards[wo.board].items()}  # 遍历该板 BOM


# ---------------------------------------------------------------------------
# _reserve：重算预留
# 功能：把在库数量按优先级依次分给未开工工单，得到每颗料被预留了多少（spec §4.1 例：100 颗被 W12 预留）。
# 依赖：priority_order、need_of；被 advance 在领料前调用，写入 state.reserved 供查询。
# 完成：每日全量重算（不做手工锁定，见 Contract 默认值）；返回每张工单拿到的份额给领料判断用。
# ---------------------------------------------------------------------------
def _reserve(state: State) -> dict[str, dict[str, int]]:
    remaining = dict(state.on_hand)  # 可分配的剩余在库，复制一份逐步扣减
    reserved: dict[str, int] = {}  # 料号 → 预留总量
    shares: dict[str, dict[str, int]] = {}  # 工单号 → {料号: 分到的数量}
    for wo in priority_order(state):  # 按优先级逐张工单分配
        share = {}  # 这张工单分到的物料
        for mpn, need in need_of(state, wo).items():  # 遍历它的每颗料
            got = min(need, remaining.get(mpn, 0))  # 能分到的 = 需求与剩余取小
            share[mpn] = got  # 记下份额
            remaining[mpn] = remaining.get(mpn, 0) - got  # 扣减剩余
            if got:  # 分到了才计入预留
                reserved[mpn] = reserved.get(mpn, 0) + got  # 累加到该料的预留
        shares[wo.id] = share  # 保存这张工单的份额
    state.reserved = reserved  # 覆盖写回状态
    return shares  # 返回份额明细


# ---------------------------------------------------------------------------
# _start_work_orders：领料开工或缺料停线
# 功能：对"已到开工日、且所在产线空闲、且它是该产线排在最前的"工单，料齐就一次性领料开工，缺任一料就整单停线。
# 依赖：_reserve（份额）、need_of；被 advance 调用。
# 完成：产线按计划顺序串行（前一单没完工，后一单等产线，不计停线天数）；停线天数只在"轮到它上线却缺料"时 +1；
#       领料按优先级逐张进行，开工后立即重算预留，保证后面工单看到的是扣减后的库存。
# ---------------------------------------------------------------------------
def _start_work_orders(state: State, day: int, log: dict) -> None:
    busy = {w.line for w in state.work_orders.values() if w.status == WO_STARTED}  # 在制工单占用的产线
    shares = _reserve(state)  # 当前份额（到货后刚重算过，这里再取一次明细）
    for wo in priority_order(state):  # 按优先级看每张未开工工单
        if wo.start_day > day or wo.line in busy:  # 未到开工日，或产线被占
            continue  # 这张今天不动
        if _earlier_pending_on_line(state, wo):  # 同产线还有排在它前面的未开工工单
            continue  # 按计划顺序串行，轮不到它
        need = need_of(state, wo)  # 领料清单
        if all(shares[wo.id].get(m, 0) >= q for m, q in need.items()):  # 每颗料的份额都够
            for mpn, qty in need.items():  # 一次性领料
                state.on_hand[mpn] -= qty  # 从在库扣减
            wo.status = WO_STARTED  # 转在制
            wo.started_day = day  # 记录实际开工日
            busy.add(wo.line)  # 该产线被占用
            log["started"].append(wo.id)  # 记入当天事实
            shares = _reserve(state)  # 库存变了，重算后续工单份额
        else:  # 有料不够
            wo.status = WO_STOPPED  # 缺料停线
            wo.stop_days += 1  # 停线天数 +1
            busy.add(wo.line)  # 该产线今天因它而停，不让后面的工单插队
            log["stopped"].append(wo.id)  # 记入当天事实


# ---------------------------------------------------------------------------
# _earlier_pending_on_line：同产线前面是否还有未开工的工单
# 功能：保证产线按计划顺序串行生产（Contract 默认值）。
# 依赖：被 _start_work_orders 调用。
# 完成：比较 (开工日, 工单号) 的先后。
# ---------------------------------------------------------------------------
def _earlier_pending_on_line(state: State, wo: WorkOrder) -> bool:
    return any(o.line == wo.line and o.status in (WO_PLANNED, WO_STOPPED)  # 同产线且未开工
               and (o.start_day, o.id) < (wo.start_day, wo.id)  # 且排在它前面
               for o in state.work_orders.values())  # 遍历全部工单


# ---------------------------------------------------------------------------
# _finish_work_orders：完工
# 功能：在制工单到"实际开工日 + 工期"那天完工，成品台数计入订单交付量。
# 依赖：被 advance 调用；工单开工由 _start_work_orders 完成。
# 完成：更新工单与订单；订单交齐时记录完成日。开工当天工期为 0 的情况由参数校验排除（工期 ≥ 1）。
# ---------------------------------------------------------------------------
def _finish_work_orders(state: State, day: int, log: dict) -> None:
    for wo in sorted(state.work_orders.values(), key=lambda w: w.id):  # 按工单号顺序
        if wo.status != WO_STARTED or wo.started_day + wo.duration_days > day:  # 非在制或未到完工日
            continue  # 看下一张
        wo.status = WO_DONE  # 转完工
        wo.done_day = day  # 记录完工日
        order = state.orders[wo.order_id]  # 找到它服务的订单
        order.delivered_qty += wo.qty  # 交付量累加
        if order.delivered_qty >= order.qty and order.completed_day is None:  # 刚好交齐
            order.completed_day = day  # 记录交齐日
        log["done"].append(wo.id)  # 记入当天事实


# ---------------------------------------------------------------------------
# _check_orders：订单到期检查
# 功能：过了到期日仍未交齐的订单，每天延期天数 +1（spec §4.6 订单延期天数）。
# 依赖：被 advance 在完工之后调用。
# 完成：按天累计，交齐当天也算一天（到期第 6 天、第 9 天交齐 ⇒ 延期 3 天）；运行结束仍未交齐的算到终点为止。
# ---------------------------------------------------------------------------
def _check_orders(state: State, day: int, log: dict) -> None:
    for order in sorted(state.orders.values(), key=lambda o: o.id):  # 按订单号顺序
        late_today = order.completed_day is None or order.completed_day == day  # 今天仍欠交，或今天才交齐
        if late_today and day > order.due_day:  # 且已过到期日
            order.delay_days += 1  # 延期 +1 天
            log["orders_late"].append(order.id)  # 记入当天事实


# ---------------------------------------------------------------------------
# apply_write：把一条外部写入作用到状态
# 功能：外部对环境的唯一写入口：submit_po（经 F21 Procurement 执行器）、postpone_work_order（经 F05 PlannerDesk）。
# 依赖：被 api.Environment 的两个写方法与崩溃恢复重放调用；幂等索引存在 state.idempotency 里。
# 完成：幂等（同键返回同一结果）、校验（validate_write，拒绝时状态不变）、按分销商备货天数算计划发货日。
#       不校验审批令牌：G2 由 F12/F21 在执行器一侧强制（见 Contract）；推迟上限由 PlannerDesk 按配置检查（G7）。
# ---------------------------------------------------------------------------
def apply_write(state: State, write: dict) -> dict:
    if is_duplicate(state, write):  # 这个幂等键已经写过
        done = state.idempotency[write["idempotency_key"]]  # 原结果的编号
        if write.get("kind") == "postpone_work_order":  # 推迟工单
            return {"wo_id": done, "start_day": state.work_orders[done].start_day, "duplicate": True}  # 原结果
        return {"po_id": done, "duplicate": True}  # 返回原单
    validate_write(state, write)  # 先校验，不合法则抛错、状态不变
    if write["kind"] == "postpone_work_order":  # F05 计划员推迟
        return _postpone(state, write)  # 推迟
    key, p = write["idempotency_key"], write["payload"]  # 幂等键与采购单内容
    po = PurchaseOrder(  # 新建采购单
        id=state.next_id("PO"),  # 分配单号
        mpn=p["mpn"], qty=p["qty"], distributor=p["distributor"],  # 料号、数量、分销商
        unit_price=str(Decimal(str(p["unit_price"]))),  # 单价规范化为十进制字符串
        submitted_day=state.day,  # 今天提交
        ship_day=state.day + state.distributors[p["distributor"]]["ship_days"],  # 计划发货日
        idempotency_key=key)  # 记住幂等键
    state.purchase_orders[po.id] = po  # 登记采购单
    state.idempotency[key] = po.id  # 登记幂等索引
    return {"po_id": po.id, "duplicate": False}  # 返回新单号


# ---------------------------------------------------------------------------
# _postpone：计划员推迟工单开工日（F05，spec §4.5）
# 功能：新开工日 = max(原开工日, 今天) + 天数；缺料停线的工单改回"未开工"（重新排期，不再算停线）。
# 依赖：被 apply_write 调用（已校验）；Agent 没有调用它的工具（G8），只有 humans.desks.PlannerDesk。
# 完成：登记幂等键 → 工单号；返回新开工日。
# ---------------------------------------------------------------------------
def _postpone(state: State, write: dict) -> dict:
    p = write["payload"]  # 内容
    wo = state.work_orders[p["wo_id"]]  # 工单
    wo.start_day = max(wo.start_day, state.day) + p["days"]  # 往后挪
    wo.status = WO_PLANNED  # 停线的也改回未开工
    state.idempotency[write["idempotency_key"]] = wo.id  # 幂等索引
    return {"wo_id": wo.id, "start_day": wo.start_day, "duplicate": False}  # 结果


# ---------------------------------------------------------------------------
# is_duplicate：写入是否命中已有幂等键
# 功能：让门面（api）在落写入日志之前就能识别重复写入，重复的不再记日志。
# 依赖：state.idempotency；被 apply_write 与 api.Environment 调用。
# 完成：键缺失或非字符串时返回 False，交给 validate_write 报错。
# ---------------------------------------------------------------------------
def is_duplicate(state: State, write: dict) -> bool:
    key = write.get("idempotency_key")  # 取幂等键
    return isinstance(key, str) and key in state.idempotency  # 已登记过即为重复


# ---------------------------------------------------------------------------
# validate_write：写入校验
# 功能：挡住不合法的写入：未知种类、缺幂等键、未知料号、非正数量、未知分销商、非法或非正单价。
# 依赖：被 apply_write 调用，也被 api.Environment 在落写入日志前调用（不合法的写入不进日志）。
# 完成：料号必须出现在某块板的 BOM 里（本期环境只管这两块板的料）。
# ---------------------------------------------------------------------------
def validate_write(state: State, write: dict) -> None:
    if write.get("kind") not in ("submit_po", "postpone_work_order"):  # 只支持这两种
        raise WriteRejected(f"未知写入种类：{write.get('kind')!r}")  # 未知种类直接拒绝
    key = write.get("idempotency_key")  # 幂等键
    if not isinstance(key, str) or not key:  # 没有幂等键就无法防重复
        raise WriteRejected("写入必须带非空的幂等键")  # 拒绝
    p = write.get("payload")  # 写入内容
    if not isinstance(p, dict):  # 内容必须是字典
        raise WriteRejected("写入内容 payload 必须是对象")  # 拒绝
    if write["kind"] == "postpone_work_order":  # F05 推迟工单
        wo = state.work_orders.get(p.get("wo_id"))  # 目标工单
        if wo is None:  # 不存在
            raise WriteRejected(f"未知工单：{p.get('wo_id')!r}")  # 拒绝
        if wo.status not in (WO_PLANNED, WO_STOPPED):  # 已开工或已完工
            raise WriteRejected(f"工单 {wo.id} 已开工或完工，不能推迟")  # 拒绝
        days = p.get("days")  # 推迟天数
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:  # 须为正整数
            raise WriteRejected(f"推迟天数必须是正整数：{days!r}")  # 拒绝
        return  # 推迟写入校验完毕
    known = {m for bom in state.boards.values() for m in bom}  # 所有板 BOM 里的料号
    if p.get("mpn") not in known:  # 料号不在任何 BOM 里
        raise WriteRejected(f"未知料号：{p.get('mpn')!r}")  # 拒绝
    if not isinstance(p.get("qty"), int) or isinstance(p.get("qty"), bool) or p["qty"] <= 0:  # 数量须为正整数
        raise WriteRejected(f"数量必须是正整数：{p.get('qty')!r}")  # 拒绝
    if p.get("distributor") not in state.distributors:  # 分销商未配置
        raise WriteRejected(f"未知分销商：{p.get('distributor')!r}")  # 拒绝
    try:  # 单价必须是合法十进制数
        price = Decimal(str(p.get("unit_price")))  # 尝试解析
    except InvalidOperation:  # 解析失败
        raise WriteRejected(f"单价不合法：{p.get('unit_price')!r}") from None  # 拒绝
    if not price.is_finite() or price <= 0:  # 非有限数或非正
        raise WriteRejected(f"单价必须为正：{p.get('unit_price')!r}")  # 拒绝

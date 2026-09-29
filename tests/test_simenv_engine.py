"""F03 step ①：领域模型与日推进的离线测试（纯内存，不连服务）。

用一个手搭的小工厂（1 块板、2 颗料、2 条产线）验证 spec §4.2 的机制链：
到货 → 领料开工 / 缺料停线 → 完工 → 订单到期延期；以及写入幂等与状态序列化往返。
"""
import pytest  # 测试框架

from simenv.engine import WriteRejected, advance, apply_write  # 被测：日推进与写入
from simenv.model import (  # 领域对象与常量
    WO_DONE,  # 工单已完工
    WO_STARTED,  # 工单在制
    WO_STOPPED,  # 工单缺料停线
    Order,  # 订单
    PurchaseOrder,  # 采购单
    Shipment,  # 运单
    State,  # 整体状态
    WorkOrder,  # 工单
    state_hash,  # 状态哈希
)


# 功能：搭一个最小工厂：板 B 每台用 A×2、B×1；订单 O-001 要 10 台第 6 天到期；工单 W-001 第 2 天开工、工期 2 天。
# 依赖：model 的各对象；被本文件所有测试使用。
# 完成：A 在库 20（够）、B 在库 5（缺 5），另有一张 B×5 的初始在途运单第 4 天到。
def tiny_state() -> State:
    s = State(day=0, seed=0, horizon_days=10,  # 第 0 天、10 天模拟期
              boards={"B": {"A": 2, "Bx": 1}},  # 一块板的 BOM
              lines=["L1", "L2"],  # 两条产线
              distributors={"digikey": {"ship_days": 1, "transit_days": 2}})  # 一个分销商
    s.orders["O-001"] = Order(id="O-001", board="B", qty=10, due_day=6)  # 订单
    s.work_orders["W-001"] = WorkOrder(id="W-001", board="B", order_id="O-001",  # 工单
                                       line="L1", qty=10, start_day=2, duration_days=2)  # 第 2 天开工、工期 2 天
    s.on_hand = {"A": 20, "Bx": 5}  # 初始在库：A 够，Bx 缺 5
    s.purchase_orders["PO-001"] = PurchaseOrder(  # 初始在途采购单
        id="PO-001", mpn="Bx", qty=5, distributor="digikey", unit_price="0.10",  # 买 5 颗 Bx
        submitted_day=0, ship_day=0, status="shipped", shipment_id="SH-001", from_run=False)  # 已发货
    s.shipments["SH-001"] = Shipment(id="SH-001", po_id="PO-001", mpn="Bx", qty=5,  # 对应运单
                                     eta_day=4, original_eta_day=4)  # 第 4 天到
    s.counters = {"PO": 1, "SH": 1}  # 编号计数器与已有单号一致
    return s  # 返回小工厂


# 功能：验证完整机制链：第 2、3 天缺料停线，第 4 天到货当日开工，第 6 天完工交齐，不延期。
def test_shortage_stops_line_until_arrival_then_starts_same_day():
    s = tiny_state()  # 搭小工厂
    assert advance(s)["stopped"] == []  # 第 1 天：未到开工日，不停线
    assert s.reserved == {"A": 20, "Bx": 5}  # 未开工工单已预留全部在库
    assert advance(s)["stopped"] == ["W-001"]  # 第 2 天：缺 Bx，停线
    advance(s)  # 第 3 天：仍缺
    assert s.work_orders["W-001"].status == WO_STOPPED and s.work_orders["W-001"].stop_days == 2  # 停 2 天
    log = advance(s)  # 第 4 天：运单到货
    assert log["arrived"] == ["SH-001"] and log["started"] == ["W-001"]  # 到货当日即领料开工
    assert s.on_hand == {"A": 0, "Bx": 0} and s.purchase_orders["PO-001"].status == "arrived"  # 物料扣光、单到货
    advance(s)  # 第 5 天：在制
    assert s.work_orders["W-001"].status == WO_STARTED  # 仍在制
    assert advance(s)["done"] == ["W-001"]  # 第 6 天：完工（开工日 4 + 工期 2）
    assert s.orders["O-001"].completed_day == 6 and s.orders["O-001"].delay_days == 0  # 到期当天交齐，不延期


# 功能：验证延期按天累计：把运单推迟到第 7 天（模拟 A1 注入），订单第 9 天才交齐，延期 3 天。
def test_delay_days_accumulate_after_due_day_via_injector():
    s = tiny_state()  # 搭小工厂

    def delay_shipment(state: State, day: int) -> None:  # 一个注入函数：第 3 天把运单推迟 3 天
        if day == 3:  # 只在第 3 天注入
            state.shipments["SH-001"].eta_day += 3  # 第 4 天 → 第 7 天

    for _ in range(9):  # 推进到第 9 天
        advance(s, [delay_shipment])  # 每天都挂上注入函数
    wo = s.work_orders["W-001"]  # 取工单
    assert (wo.started_day, wo.done_day, wo.stop_days) == (7, 9, 5)  # 第 2–6 天停线 5 天，第 7 天开工、第 9 天完工
    assert s.shipments["SH-001"].original_eta_day == 4  # 原预计到货日保留，便于对比延误
    assert s.orders["O-001"].delay_days == 3  # 第 7、8、9 天各 +1


# 功能：验证产线串行：同产线后一张工单要等前一张完工，等待期间不计停线天数。
def test_line_runs_work_orders_in_plan_order():
    s = tiny_state()  # 搭小工厂
    s.on_hand = {"A": 40, "Bx": 20}  # 料充足
    s.orders["O-002"] = Order(id="O-002", board="B", qty=10, due_day=9)  # 第二张订单
    s.work_orders["W-002"] = WorkOrder(id="W-002", board="B", order_id="O-002",  # 同产线第二张工单
                                       line="L1", qty=10, start_day=3, duration_days=2)  # 计划第 3 天开工
    for _ in range(6):  # 推进 6 天
        advance(s)  # 无注入
    w1, w2 = s.work_orders["W-001"], s.work_orders["W-002"]  # 取两张工单
    assert (w1.started_day, w1.done_day) == (2, 4)  # W-001 第 2 天开工、第 4 天完工
    assert (w2.started_day, w2.done_day, w2.stop_days) == (4, 6, 0)  # W-002 等到第 4 天产线空出才开工，不算停线
    assert w2.status == WO_DONE  # 已完工


# 功能：验证写入：幂等键重复返回同一张单；第 N 天提交的单按备货 1 天、运输 2 天到货。
def test_submit_po_is_idempotent_and_flows_to_arrival():
    s = tiny_state()  # 搭小工厂
    advance(s)  # 第 1 天
    w = {"kind": "submit_po", "idempotency_key": "k1",  # 一条下单写入
         "payload": {"mpn": "Bx", "qty": 50, "distributor": "digikey", "unit_price": "0.12"}}  # 50 颗 Bx
    assert apply_write(s, w) == {"po_id": "PO-002", "duplicate": False}  # 新单
    assert apply_write(s, w) == {"po_id": "PO-002", "duplicate": True}  # 同键重复：返回原单
    assert len(s.purchase_orders) == 2  # 没有重复下单
    assert advance(s)["shipped"] == ["PO-002"]  # 第 2 天发货
    assert s.shipments["SH-002"].eta_day == 4  # 预计第 4 天到
    assert s.procurement_cost() == "6.00"  # 成本只算运行中下的单：50 × 0.12


# 功能：验证非法写入被拒且状态不变。
@pytest.mark.parametrize("payload", [  # 各种不合法的采购单
    {"mpn": "NOPE", "qty": 1, "distributor": "digikey", "unit_price": "1"},  # 未知料号
    {"mpn": "A", "qty": 0, "distributor": "digikey", "unit_price": "1"},  # 数量非正
    {"mpn": "A", "qty": 1, "distributor": "acme", "unit_price": "1"},  # 未知分销商
    {"mpn": "A", "qty": 1, "distributor": "digikey", "unit_price": "abc"},  # 单价非法
    {"mpn": "A", "qty": 1, "distributor": "digikey", "unit_price": "-1"},  # 单价非正
])
def test_invalid_writes_are_rejected_without_side_effects(payload):
    s = tiny_state()  # 搭小工厂
    before = state_hash(s)  # 记下写入前的哈希
    with pytest.raises(WriteRejected):  # 应被拒绝
        apply_write(s, {"kind": "submit_po", "idempotency_key": "k", "payload": payload})  # 尝试写入
    assert state_hash(s) == before  # 状态完全没变


# 功能：验证序列化往返无损、推进确定：同一状态推进同样天数哈希相同；终点之后拒绝推进。
def test_roundtrip_and_determinism():
    a, b = tiny_state(), State.from_dict(tiny_state().to_dict())  # 一份原样、一份经过序列化往返
    assert state_hash(a) == state_hash(b)  # 往返无损
    for _ in range(10):  # 推进到终点
        advance(a), advance(b)  # 两份同步推进
    assert state_hash(a) == state_hash(b)  # 结果一致
    with pytest.raises(RuntimeError):  # 终点之后
        advance(a)  # 不能再推进

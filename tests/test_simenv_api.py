"""F03 step ③：查询与写入接口的离线测试（在生成的真实两板环境上跑）。"""
import pytest  # 测试框架

from simenv.api import Environment, NotFound  # 被测：对外门面
from simenv.bom import load_boms  # 原理图 BOM
from simenv.engine import WriteRejected  # 写入拒绝异常
from simenv.model import state_hash  # 状态哈希
from simenv.params import load_params  # 参数
from simenv.seed import generate  # 生成初始状态


# 功能：每个测试一个新的种子 42 环境（生成很快，不共享可变状态）。
@pytest.fixture
def env():
    return Environment(generate(load_params(), load_boms(), 42))  # 第 0 天的环境


# 功能：库存查询：可用 = 在库 − 预留；在途合计等于在途明细之和；查询结果改了也不影响环境。
def test_inventory_view_is_consistent_and_read_only(env):
    env.advance()  # 推进到第 1 天，预留已计算
    for inv in env.list_inventory():  # 每颗料
        assert inv["available"] == inv["on_hand"] - inv["reserved"]  # 可用口径
        assert inv["in_transit"] == sum(i["qty"] for i in inv["inbound"])  # 在途口径
    before = state_hash(env.state)  # 记下哈希
    env.list_work_orders()[0]["status"] = "done"  # 改查询结果
    env.get_bom("hackrf-one").clear()  # 清空返回的 BOM
    assert state_hash(env.state) == before  # 环境不受影响


# 功能：各类查询与筛选能用，未知编号报 NotFound。
def test_queries_and_filters(env):
    assert env.today()["day"] == 0 and env.today()["simulated"]  # 当前日与模拟标记
    assert {o["board"] for o in env.list_orders(board="bms-c1")} == {"bms-c1"}  # 按板筛选
    wo = env.list_work_orders()[0]  # 第一张工单
    assert env.get_work_order(wo["id"]) == wo  # 单条查询一致
    assert env.work_order_needs(wo["id"])  # 领料清单非空
    assert len(env.get_bom("hackrf-one")) == 62  # BOM 料号数
    for getter in (env.get_order, env.get_work_order, env.get_purchase_order,  # 各单条查询
                   env.get_shipment, env.get_inventory, env.get_bom):  # 含库存与 BOM
        with pytest.raises(NotFound):  # 未知编号
            getter("NOPE")  # 报不存在


# 功能：写入接口：新单可查、幂等键重复返回同单、非法写入拒绝；到期发货后出现在库存在途里。
def test_submit_purchase_order_through_facade(env):
    env.advance()  # 第 1 天
    mpn = min(env.get_bom("hackrf-one"))  # 取料号最小的一颗 hackrf 料
    args = {"mpn": mpn, "qty": 100, "distributor": "mouser",  # 采购单参数
            "unit_price": "0.25", "idempotency_key": "case-K1:po-1"}  # 含幂等键
    first = env.submit_purchase_order(**args)  # 第一次提交
    assert env.submit_purchase_order(**args) == {**first, "duplicate": True}  # 重复提交返回同单
    assert env.get_purchase_order(first["po_id"])["status"] == "submitted"  # 新单已提交
    assert len(env.list_purchase_orders(mpn=mpn, status="submitted")) == 1  # 只有一张
    with pytest.raises(WriteRejected):  # 非法写入
        env.submit_purchase_order(**{**args, "qty": -1, "idempotency_key": "k2"})  # 负数量
    env.advance()  # 第 2 天：mouser 备货 1 天后发货
    assert any(i["po_id"] == first["po_id"] for i in env.get_inventory(mpn)["inbound"])  # 已在途

"""F03 step ⑤：结果指标的离线测试。"""
from simenv.api import Environment  # 环境门面
from simenv.bom import load_boms  # 原理图 BOM
from simenv.metrics import outcome_metrics  # 被测：结果指标
from simenv.params import load_params  # 参数


# 功能：跑满全程并返回指标；buy_shortfalls=True 时第 0 天按"全程需求 − 在库 − 在途"一次性补足缺口（最朴素的规则）。
# 依赖：Environment 的查询与写入；只用于本测试，正式的"简单规则"基线归 F04。
# 完成：单价用固定值 1，成本即补货总颗数。
def run(seed: int, buy_shortfalls: bool) -> dict:
    env = Environment.create(load_params(), load_boms(), seed)  # 生成环境
    if buy_shortfalls:  # 朴素补货
        total: dict[str, int] = {}  # 料号 → 全程需求
        for wo in env.list_work_orders():  # 每张工单
            for mpn, qty in env.work_order_needs(wo["id"]).items():  # 每颗料
                total[mpn] = total.get(mpn, 0) + qty  # 累加
        for mpn, need in sorted(total.items()):  # 每颗料
            inv = env.get_inventory(mpn)  # 库存
            gap = need - inv["on_hand"] - inv["in_transit"]  # 缺口
            if gap > 0:  # 有缺口才下单
                env.submit_purchase_order(mpn=mpn, qty=gap, distributor="digikey",  # 补足缺口
                                          unit_price="1", idempotency_key=f"fill:{mpn}")  # 单价 1
    while env.state.day < env.state.horizon_days:  # 跑到终点
        env.advance()  # 推进
    return outcome_metrics(env.state)  # 指标


# 功能：不干预时有停线与延期、成本为 0；朴素补货后停线减少、成本为正；总计等于按板之和；同种子指标完全相同。
def test_metrics_respond_to_purchases_and_are_reproducible():
    idle, fill = run(42, False), run(42, True)  # 不干预 vs 朴素补货
    assert idle["stop_line_days"] > 0 and idle["order_delay_days"] > 0  # 不干预必有缺料后果
    assert idle["procurement_cost"] == "0"  # 没下单就没成本
    assert fill["stop_line_days"] < idle["stop_line_days"]  # 补货后停线减少
    assert fill["procurement_cost"] != "0"  # 补货有成本
    for m in (idle, fill):  # 两次运行
        assert m["stop_line_days"] == sum(b["stop_line_days"] for b in m["by_board"].values())  # 总计 = 分板和
        assert m["order_delay_days"] == sum(b["order_delay_days"] for b in m["by_board"].values())  # 同上
    assert run(42, False) == idle  # 同种子指标（含状态哈希）完全相同

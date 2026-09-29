"""F03 step ②：按种子生成初始状态的离线测试（读仓库内原始层原理图，不连服务、不联网）。"""
from itertools import pairwise  # 相邻两两配对
from pathlib import Path  # 临时配置文件路径

import pytest  # 测试框架
import yaml  # 写坏配置做校验测试

from simenv.bom import load_boms  # 被测：从原理图读 BOM
from simenv.engine import need_of  # 工单需求
from simenv.model import state_hash  # 状态哈希
from simenv.params import DEFAULT_PARAMS, ParamsError, load_params  # 被测：参数读取与校验
from simenv.seed import generate  # 被测：生成初始状态


# 功能：模块级只读一次参数与 BOM，所有测试共用（解析原理图约 0.1 秒）。
@pytest.fixture(scope="module")
def inputs():
    return load_params(), load_boms()  # (参数, BOM)


# 功能：BOM 与 spec §1.3 一致：hackrf 62 料号 / 297 贴装位，bms-c1 55 / 154。
def test_boms_match_the_part_table(inputs):
    _, boms = inputs  # 取 BOM
    assert {k: (len(v), sum(v.values())) for k, v in boms.items()} == {  # 料号数与贴装位总数
        "hackrf-one": (62, 297), "bms-c1": (55, 154)}  # spec §1.3 的数字


# 功能：同种子两次生成哈希相同；不同种子不同（spec §0.2 可复现）。
def test_same_seed_same_state_different_seed_different(inputs):
    params, boms = inputs  # 取输入
    assert state_hash(generate(params, boms, 42)) == state_hash(generate(params, boms, 42))  # 同种子一致
    assert state_hash(generate(params, boms, 42)) != state_hash(generate(params, boms, 43))  # 异种子不同


# 功能：生成结果满足结构约束：两块板都有订单；工单台数加总等于订单台数；同产线工单不重叠；
#       每条记录标记为模拟数据；每颗料都有在库记录；在途运单与采购单一一对应。
@pytest.mark.parametrize("seed", [1, 42, 2026])
def test_generated_plan_is_consistent(inputs, seed):
    params, boms = inputs  # 取输入
    s = generate(params, boms, seed)  # 生成
    assert {o.board for o in s.orders.values()} == {"hackrf-one", "bms-c1"}  # 两块板都有订单
    for o in s.orders.values():  # 每张订单
        assert sum(w.qty for w in s.work_orders.values() if w.order_id == o.id) == o.qty  # 拆批加总不变
    for line in s.lines:  # 每条产线
        wos = sorted((w for w in s.work_orders.values() if w.line == line), key=lambda w: w.start_day)  # 按开工排
        for a, b in pairwise(wos):  # 相邻两张
            assert a.start_day + a.duration_days <= b.start_day  # 前一张完工前后一张不开工
    records = [*s.orders.values(), *s.work_orders.values(), *s.purchase_orders.values(),  # 全部记录
               *s.shipments.values()]  # 含运单
    assert all(r.simulated for r in records)  # 都标记为模拟数据（spec §1.3）
    assert set(s.on_hand) == {m for bom in s.boards.values() for m in bom}  # 每颗料都有在库记录
    assert {p.shipment_id for p in s.purchase_orders.values()} == set(s.shipments)  # 在途单与运单一一对应
    assert all(not p.from_run for p in s.purchase_orders.values())  # 初始在途都不计运行成本


# 功能：非短缺料号的在库覆盖全程需求，所以缺料只来自少数短缺料号。
def test_only_a_few_parts_are_short(inputs):
    params, boms = inputs  # 取输入
    s = generate(params, boms, 42)  # 生成
    total = {}  # 料号 → 全程需求
    for w in s.work_orders.values():  # 每张工单
        for m, q in need_of(s, w).items():  # 每颗料
            total[m] = total.get(m, 0) + q  # 累加
    short = [m for m, q in total.items() if s.on_hand[m] + s.in_transit(m) < q]  # 在库+在途不够的料
    assert 0 < len(short) < len(total) * 0.25  # 有缺料，但只是少数


# 功能：参数校验：天数 < 1 或区间颠倒都拒绝。
@pytest.mark.parametrize("mutate", [
    lambda p: p["distributors"]["digikey"].update(ship_days=0),  # 备货 0 天
    lambda p: p["work_orders"].update(duration_days=[4, 2]),  # 区间颠倒
    lambda p: p.pop("lines"),  # 缺键
    lambda p: p["in_transit"].update(fraction=1.5),  # 比例越界
])
def test_invalid_params_are_rejected(tmp_path: Path, mutate):
    p = yaml.safe_load(DEFAULT_PARAMS.read_text(encoding="utf-8"))  # 读默认配置
    mutate(p)  # 改坏
    bad = tmp_path / "bad.yaml"  # 临时文件
    bad.write_text(yaml.safe_dump(p), encoding="utf-8")  # 写出
    with pytest.raises(ParamsError):  # 应被拒绝
        load_params(bad)  # 读取

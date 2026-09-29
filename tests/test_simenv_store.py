"""F03 step ④：快照、分叉、写入日志与崩溃恢复。

同一组场景分别在内存存储（离线）与 Postgres 存储（集成，需 make services-up）上跑，两种实现行为一致。
"""
import pytest  # 测试框架
from sqlalchemy import text  # 集成测试里直接查表

from simenv.api import Environment  # 被测：对外门面
from simenv.bom import load_boms  # 原理图 BOM
from simenv.model import state_hash  # 状态哈希
from simenv.params import load_params  # 参数
from simenv.store import MemoryEnvStore, PgEnvStore, SnapshotCorrupted  # 被测：两种存储


# 功能：模块级只读一次参数与 BOM。
@pytest.fixture(scope="module")
def inputs():
    return load_params(), load_boms()  # (参数, BOM)


# 功能：按固定剧本跑一段：推进到第 5 天，下两张单（其中一张重复提交），再推进到第 8 天，第 8 天再下一张。
# 依赖：Environment 的推进与写入；被下方各测试共用，保证"对照运行"和"崩溃运行"走同一剧本。
# 完成：stop_at 指定在哪一步之后停下（模拟进程被杀），返回停下时的环境。
def script(env: Environment, stop_at: str | None = None) -> Environment:
    mpn = min(env.get_bom("hackrf-one"))  # 取一颗 hackrf 料
    for _ in range(5):  # 推进到第 5 天
        env.advance()  # 每天存开盘快照
    env.submit_purchase_order(mpn=mpn, qty=100, distributor="digikey",  # 第 5 天下单
                              unit_price="0.10", idempotency_key="k1")  # 幂等键 k1
    env.submit_purchase_order(mpn=mpn, qty=100, distributor="digikey",  # 重复提交
                              unit_price="0.10", idempotency_key="k1")  # 同键不重复下单
    if stop_at == "day5-after-write":  # 在第 5 天写入之后崩溃
        return env  # 停下
    env.submit_purchase_order(mpn=mpn, qty=50, distributor="element14",  # 第 5 天第二张
                              unit_price="0.20", idempotency_key="k2")  # 幂等键 k2
    for _ in range(3):  # 推进到第 8 天
        env.advance()  # 每天存开盘快照
    env.submit_purchase_order(mpn=mpn, qty=10, distributor="mouser",  # 第 8 天下单
                              unit_price="0.30", idempotency_key="k3")  # 幂等键 k3
    return env  # 跑完剧本


# 功能：两种存储各跑一遍的通用断言：崩溃恢复一致、恢复后继续跑与对照一致、分叉互不影响、预演不落库。
# 依赖：script；被离线与集成测试调用。
# 完成：见下方逐条注释。
def check_store(store, inputs, run: str):
    params, boms = inputs  # 取输入
    control = script(Environment.create(params, boms, 42))  # 对照：纯内存、不崩溃，跑完整剧本
    crashed = script(Environment.create(params, boms, 42, store=store, run_id=run),  # 持久化运行
                     stop_at="day5-after-write")  # 第 5 天写入后"被杀"
    before = state_hash(crashed.state)  # 被杀时的状态哈希
    del crashed  # 丢掉内存对象，只剩存储
    revived = Environment.recover(store, run)  # 从存储恢复
    assert state_hash(revived.state) == before  # 恢复出的状态与被杀前一致
    assert len(revived.list_purchase_orders(status="submitted")) == 1  # 没有重复下单
    mpn = min(revived.get_bom("hackrf-one"))  # 同一颗料
    again = revived.submit_purchase_order(mpn=mpn, qty=100, distributor="digikey",  # 恢复后重试 k1
                                          unit_price="0.10", idempotency_key="k1")  # 调用方不知道是否成功过
    assert again["duplicate"] is True  # 幂等：返回原单
    revived.submit_purchase_order(mpn=mpn, qty=50, distributor="element14",  # 继续剧本余下部分
                                  unit_price="0.20", idempotency_key="k2")  # k2
    for _ in range(3):  # 推进到第 8 天
        revived.advance()  # 存快照
    revived.submit_purchase_order(mpn=mpn, qty=10, distributor="mouser",  # 第 8 天下单
                                  unit_price="0.30", idempotency_key="k3")  # k3
    assert state_hash(revived.state) == state_hash(control.state)  # 与未崩溃的对照完全一致
    assert state_hash(Environment.recover(store, run).state) == state_hash(control.state)  # 再恢复一次也一致

    fork = Environment.load(store, run, 5, into=(store, run + "-fork"))  # 从第 5 天开盘分叉
    assert fork.state.day == 5 and fork.list_purchase_orders(status="submitted") == []  # 分叉点在当天写入之前
    fork.submit_purchase_order(mpn=mpn, qty=999, distributor="mouser",  # 分叉里下一张不同的单
                               unit_price="0.50", idempotency_key="k1")  # 同键名在另一运行里互不冲突
    fork.advance()  # 分叉推进一天
    assert state_hash(Environment.recover(store, run).state) == state_hash(control.state)  # 原运行不受影响
    assert Environment.recover(store, run + "-fork").state.day == 6  # 分叉可独立恢复

    preview = Environment.load(store, run, 8)  # 预演：纯内存载入第 8 天
    preview.submit_purchase_order(mpn=mpn, qty=1, distributor="mouser",  # 预演里随便写
                                  unit_price="1", idempotency_key="preview")  # 不落日志
    preview.advance()  # 预演推进
    assert preview.store is None  # 预演不挂存储
    assert store.load_snapshot(run)[0] == 8  # 原运行最近快照仍是第 8 天
    return control  # 返回对照运行给调用方做额外检查


# 功能：离线版：内存存储；另外验证快照被改坏时拒绝恢复。
def test_memory_store_recover_fork_preview(inputs):
    store = MemoryEnvStore()  # 内存存储
    check_store(store, inputs, "run-mem")  # 通用断言
    store.snapshots[("run-mem", 8)]["state"]["on_hand"]["X"] = 1  # 篡改最近快照
    with pytest.raises(SnapshotCorrupted):  # 哈希对不上
        Environment.recover(store, "run-mem")  # 拒绝恢复


# 功能：离线版：进程死在"推进之后、存快照之前"——日志里有比最近快照更晚一天的写入时，恢复会补推进。
def test_recover_replays_past_a_missing_snapshot(inputs):
    params, boms = inputs  # 取输入
    store = MemoryEnvStore()  # 内存存储
    env = Environment.create(params, boms, 7, store=store, run_id="r")  # 持久化运行
    env.advance()  # 第 1 天（有快照）
    env.store = None  # 模拟第 2 天快照没存上
    env.advance()  # 第 2 天
    env.store = store  # 恢复存储挂接
    env.submit_purchase_order(mpn=min(env.get_bom("bms-c1")), qty=5, distributor="digikey",  # 第 2 天写入
                              unit_price="1", idempotency_key="k")  # 已进日志
    revived = Environment.recover(store, "r")  # 从第 1 天快照 + 日志恢复
    assert state_hash(revived.state) == state_hash(env.state)  # 与崩溃前一致
    assert store.load_snapshot("r")[0] == 2  # 补存了第 2 天快照


# 功能：集成版：Postgres 存储走同一套断言，并确认两张表只追加、写入日志确实落库。
@pytest.mark.integration
def test_postgres_store_recover_fork_preview(migrated, inputs):
    conn, _, _ = migrated  # 迁移好的连接（测试后回滚）
    check_store(PgEnvStore(conn), inputs, "run-pg")  # 通用断言
    assert conn.scalar(text("SELECT count(*) FROM env_write WHERE run_id = 'run-pg'")) == 3  # k1 k2 k3 各一次
    assert conn.scalar(text("SELECT max(day) FROM env_snapshot WHERE run_id = 'run-pg'")) == 8  # 快照到第 8 天
    with pytest.raises(Exception, match="append-only"), conn.begin_nested():  # 改快照被拒；保存点让外层事务可继续
        conn.execute(text("UPDATE env_snapshot SET state_hash = 'x'"))  # 尝试修改
    with pytest.raises(Exception, match="append-only"), conn.begin_nested():  # 删写入日志被拒绝
        conn.execute(text("DELETE FROM env_write"))  # 尝试删除

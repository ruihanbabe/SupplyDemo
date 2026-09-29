"""F06：Session 事件日志与 Artifact 的测试。

核心场景取自 spec §5.1 的 A2 / A5 例子：PartProfile → VerificationReport → Decision ← SourcingQuote，Decision → PODraft。
同一场景在内存存储（离线）与 Postgres 存储（集成）上各跑一遍，比对结果完全一致。
"""
import pytest  # 测试框架
from sqlalchemy import text  # 集成测试里直接操作表

from infrastructure.config import ablation, load_harness_config  # 消融开关
from session.log import (  # 被测
    ART_INVALID,  # 失效
    ART_SUPERSEDED,  # 被替代
    ART_VALID,  # 有效
    IllegalTransition,  # 非法迁移
    Session,  # 门面
)
from session.store import MemorySessionStore, PgSessionStore  # 两种存储

QUOTE = "quote:element14:SKY13453"  # 报价外部引用
SHEET = "datasheet:X"  # datasheet 外部引用


def fixed_clock():
    """测试用固定真实时钟，便于比对。"""
    return "2026-09-29T00:00:00+00:00"  # 固定时间


# 功能：按 spec §5.1 例子搭一个案子 K7 与五个 Artifact，再依次发生 A2（报价变化）与 A5（datasheet 改版）。
# 依赖：Session 全部公开方法；被离线与集成测试共用。
# 完成：返回每一步之后的状态快照，供断言与跨存储比对。
def a2_a5_scenario(s: Session) -> dict:
    opened = s.open_case("K7", day=16, actor="runtime", payload={"source": "L3 物流延误"},  # 开案
                         entity_keys={"shipment": ["SH-017"], "mpn": ["SKY13453"]})  # 实体键
    s.transition("K7", "planning", day=16, actor="runtime")  # 规划中
    s.transition("K7", "executing", day=16, actor="orchestrator@K7")  # 执行中
    pp = s.produce("PartProfile:SKY13453", "PartProfile", {"isolation_db_min": 25}, day=16,  # 料画像
                   actor="profiler@K7", case_id="K7",  # 执行者与案子
                   deps=[{"external": "hw_part:hackrf-one:SKY13453", "version": "7f96cc8"}])  # 依赖原理图
    vr = s.produce("VerificationReport:X", "VerificationReport", {"grade": "A"}, day=16,  # 验证报告
                   actor="verifier@K7", case_id="K7",  # 执行者与案子
                   deps=[{"artifact": pp["artifact_id"], "version": 1},  # 依赖料画像
                         {"external": SHEET, "version": "v3"}])  # 依赖 datasheet v3
    sq = s.produce("SourcingQuote:SKY13453", "SourcingQuote", {"element14": {"qty": 300}}, day=16,  # 比价
                   actor="sourcing@K7", case_id="K7",  # 代码组件
                   deps=[{"external": QUOTE, "version": "day16", "valid_until_day": 17}])  # 报价有效到第 17 天
    dec = s.produce("Decision:SKY13453", "ProcurementDecision", {"choose": "X"}, day=16,  # 决策
                    actor="policy@K7", case_id="K7",  # 代码组件
                    deps=[{"artifact": vr["artifact_id"], "version": 1},  # 依赖验证报告
                          {"artifact": sq["artifact_id"], "version": 1}])  # 依赖比价
    s.produce("PODraft:PO-044", "PODraft", {"qty": 300}, day=16, actor="procurement@K7",  # 草稿
              case_id="K7", deps=[{"artifact": dec["artifact_id"], "version": 1}])  # 依赖决策
    s.transition("K7", "waiting", day=17, actor="runtime", wait_for="human", reason="等 H2 审批")  # 挂起
    before_a2 = dict(s.artifact_status())  # A2 前状态
    change = s.append("tool_result", day=18, actor="proxy", case_id="K7",  # 提交前重查：库存 300→0
                      payload={"ref": QUOTE, "qty": 0}, entity_keys={"distributor": ["element14"]},  # 内容
                      caused_by=opened["seq"])  # 因果链
    a2 = s.invalidate_external(QUOTE, "day18", day=18, caused_by=change["seq"])  # A2 失效传播
    s.produce("SourcingQuote:SKY13453", "SourcingQuote", {"element14": {"qty": 0}}, day=18,  # 重新比价 v2
              actor="sourcing@K7", case_id="K7",  # 代码组件
              deps=[{"external": QUOTE, "version": "day18", "valid_until_day": 19}])  # 新报价
    a5 = s.invalidate_external(SHEET, "v4", day=20)  # A5：datasheet 出 v4
    return {"before_a2": before_a2, "a2": a2, "after": dict(s.artifact_status()), "a5": a5,  # 各步结果
            "case": s.case_state("K7"), "chain": s.causal_chain(a2 and s.events(  # 案子投影、因果链
                type="artifact_invalidated")[0]["seq"])}  # 第一条失效事件的因果链


def check_scenario(s: Session) -> dict:
    """通用断言：A2 只让报价、决策、草稿失效；A5 只让验证报告失效；哈希链完好。"""
    r = a2_a5_scenario(s)  # 跑场景
    assert set(r["before_a2"].values()) == {ART_VALID}  # A2 前全部有效
    assert r["a2"] == [("Decision:SKY13453", 1), ("PODraft:PO-044", 1),  # A2：决策、草稿
                       ("SourcingQuote:SKY13453", 1)]  # 与报价 v1 失效
    after = r["after"]  # 最终状态
    assert after[("PartProfile:SKY13453", 1)] == ART_VALID  # 料画像不受影响
    assert after[("SourcingQuote:SKY13453", 1)] == ART_INVALID  # 旧报价：失效优先于被替代
    assert after[("SourcingQuote:SKY13453", 2)] == ART_VALID  # 新报价有效
    assert r["a5"] == [("VerificationReport:X", 1)]  # A5：只有验证报告新失效（下游已失效不重复）
    assert after[("VerificationReport:X", 1)] == ART_INVALID  # 验证报告失效
    notices = s.events(type="invalidation_notice")  # 失效通知
    assert [n["payload"]["artifacts"] for n in notices] == [  # 每次传播给 K7 一条
        ["Decision:SKY13453@v1", "PODraft:PO-044@v1", "SourcingQuote:SKY13453@v1"],  # A2
        ["VerificationReport:X@v1"]]  # A5
    assert all(n["case_id"] == "K7" for n in notices)  # 都发给 K7
    assert r["case"]["state"] == "waiting" and r["case"]["wait_for"] == "human"  # 案子仍在等审批
    assert [e["type"] for e in r["chain"]] == ["case_opened", "tool_result", "artifact_invalidated"]  # 因果链
    assert s.verify_chain() is None  # 哈希链完好
    return r  # 返回结果供跨存储比对


# 功能：离线版场景 + 篡改检测。
def test_a2_a5_invalidation_on_memory_store():
    store = MemorySessionStore()  # 内存存储
    s = Session(store, "run-1", clock=fixed_clock)  # 门面
    check_scenario(s)  # 通用断言
    store._events["run-1"][3]["payload"]["tampered"] = True  # 篡改第 4 个事件
    assert s.verify_chain() == 4  # 校验定位到第 4 个事件


# 功能：去重键：重复投递返回原事件、不增加日志；caused_by 必须指向已存在事件。
def test_dedup_and_caused_by():
    s = Session(MemorySessionStore(), "r", clock=fixed_clock)  # 门面
    first = s.append("env_event", day=3, actor="env", dedup_key="env:day3:SH-017", payload={"a": 1})  # 首次
    again = s.append("env_event", day=3, actor="env", dedup_key="env:day3:SH-017", payload={"a": 2})  # 重复
    assert again == first and len(s.events()) == 1  # 返回原事件，不重复记录
    with pytest.raises(ValueError):  # 因果指向不存在
        s.append("x", day=3, actor="env", caused_by=99)  # 拒绝


# 功能：过期失效：报价有效到第 17 天，第 17 天仍有效，第 18 天失效。
def test_expiry_uses_simulated_day():
    s = Session(MemorySessionStore(), "r", clock=fixed_clock)  # 门面
    s.produce("Q", "SourcingQuote", {}, day=16, actor="sourcing",  # 报价
              deps=[{"external": QUOTE, "version": "day16", "valid_until_day": 17}])  # 有效到第 17 天
    assert s.expire(day=17) == []  # 第 17 天不失效
    assert s.expire(day=18) == [("Q", 1)]  # 第 18 天失效
    assert s.expire(day=19) == []  # 已失效不重复


# 功能：案子状态机：非法迁移、等待无原因、重复关闭都被拒绝；新版本使旧版本被替代。
def test_case_state_machine_and_superseded():
    s = Session(MemorySessionStore(), "r", clock=fixed_clock)  # 门面
    s.open_case("K1", day=1, actor="runtime")  # 开案
    with pytest.raises(IllegalTransition):  # 开启不能直接执行
        s.transition("K1", "executing", day=1, actor="runtime")  # 拒绝
    s.transition("K1", "planning", day=1, actor="runtime")  # 规划中
    s.transition("K1", "executing", day=1, actor="runtime")  # 执行中
    with pytest.raises(IllegalTransition):  # 等待必须写原因
        s.transition("K1", "waiting", day=1, actor="runtime")  # 拒绝
    s.transition("K1", "waiting", day=1, actor="runtime", wait_for="arrival")  # 等到货
    s.transition("K1", "executing", day=5, actor="runtime")  # 到货唤醒
    s.close_case("K1", "resolved", day=5, actor="runtime")  # 关闭
    with pytest.raises(IllegalTransition):  # 重复关闭
        s.close_case("K1", "resolved", day=6, actor="runtime")  # 拒绝
    with pytest.raises(IllegalTransition):  # 重复开案
        s.open_case("K1", day=6, actor="runtime")  # 拒绝
    assert s.case_state("K1")["outcome"] == "resolved"  # 投影结果
    s.produce("A", "Plan", {"v": 1}, day=1, actor="orchestrator")  # 计划 v1
    s.produce("A", "Plan", {"v": 2}, day=2, actor="orchestrator")  # 计划 v2
    assert s.artifact_status() == {("A", 1): ART_SUPERSEDED, ("A", 2): ART_VALID}  # 旧版本被替代
    assert s.artifact_view("A")["content"] == {"v": 2}  # 默认取最新


# 功能：消融开关：配置里登记了；关掉时外部数据变化与过期都不产生失效，直接作废不受影响。
def test_ablation_switch_disables_propagation():
    assert ablation(load_harness_config(), "invalidation_propagation") is True  # 默认开启
    s = Session(MemorySessionStore(), "r", propagate=False, clock=fixed_clock)  # 关掉传播
    s.produce("Q", "SourcingQuote", {}, day=16, actor="sourcing",  # 报价
              deps=[{"external": QUOTE, "version": "day16", "valid_until_day": 17}])  # 依赖
    assert s.invalidate_external(QUOTE, "day18", day=18) == [] and s.expire(day=30) == []  # 不失效
    assert s.invalidate([("Q", 1)], "目标修订", day=18, actor="orchestrator") == [("Q", 1)]  # 直接作废仍生效


# 功能：集成版：Postgres 上同一场景结果与内存版逐项相同（含每个事件的哈希），且三张表只追加。
@pytest.mark.integration
def test_postgres_store_matches_memory(migrated):
    conn, _, _ = migrated  # 迁移好的连接
    pg = Session(PgSessionStore(conn), "run-1", clock=fixed_clock)  # Pg 版
    mem = Session(MemorySessionStore(), "run-1", clock=fixed_clock)  # 内存版
    assert check_scenario(pg) == check_scenario(mem)  # 场景结果一致
    assert [e["hash"] for e in pg.events()] == [e["hash"] for e in mem.events()]  # 哈希链逐条相同
    for table in ("session_event", "artifact", "artifact_dep"):  # 三张表
        with pytest.raises(Exception, match="append-only"), conn.begin_nested():  # 改删被拒
            conn.execute(text(f"DELETE FROM {table}"))  # 尝试删除

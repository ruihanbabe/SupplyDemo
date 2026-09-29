"""F17：可观测计量测试（离线全部 mock；集成测 Postgres 计量表）。"""
import pytest  # 测试框架

from models.adapter import ModelAdapter  # 模型适配层
from models.providers import ScriptedTransport, load_models_config, reply  # 脚本传输
from models.types import Message  # 消息
from observability.meter import (  # 被测
    MemoryMeterStore,  # 内存存储
    Meter,  # 计量入口
    PgMeterStore,  # Pg 存储
    case_summary,  # 案子汇总
    context_snapshot,  # 上下文快照
    quota_panel,  # 配额面板
    rebuild_quota,  # 配额重建
    run_summary,  # 运行汇总
)
from session.log import Session  # 事件日志
from session.store import MemorySessionStore, PgSessionStore  # 事件存储
from simenv.api import Environment  # 环境
from simenv.bom import load_boms  # BOM
from simenv.params import load_params  # 参数
from tools.archive import MemoryArchive  # 留档
from tools.kv import MemoryKV  # KV
from tools.proxy import ToolProxy, load_tools_config  # 代理层
from tools.quota import QuotaGuard  # 配额
from tools.servers import distributor_server, environment_server  # Server

DAY = "2026-09-30"  # 固定真实日期


# 功能：在给定事件存储与计量存储上跑一个小案子：开案、规划、执行、一次修复 + 降级的模型调用、
#       三次工具调用（首次、缓存命中、配额用完的单家查询）、一次重规划、打扰人工一次、以升级关闭。
# 依赖：F06/F07/F08 全部组件；被离线与集成测试共用。
# 完成：返回 (session, meter_store, kv)。
def scenario(session_store, meter_store, use_meter: bool = True):
    s = Session(session_store, "run", clock=lambda: "t")  # 事件日志
    meter = Meter(meter_store, today=lambda: DAY) if use_meter else None  # 计量（可关）
    config = load_tools_config()  # 工具配置
    config["providers"]["mouser"] = {"daily_limit": 1, "rate_per_s": None, "burst": None}  # mouser 每天 1 次
    kv = MemoryKV()  # KV
    env = Environment.create(load_params(), load_boms(), 42)  # 环境
    proxy = ToolProxy([environment_server(lambda: env), distributor_server()], s, kv=kv,  # 代理层
                      quota=QuotaGuard(kv, config["providers"], today=lambda: DAY),  # 配额
                      archive=MemoryArchive(), config=config, whitelist={"v": {"*"}}, meter=meter)  # 计量
    adapter = ModelAdapter(load_models_config(), {  # 适配层
        "glm-flash": ScriptedTransport([reply("x", tokens=(100, 5)), reply("y", tokens=(120, 5))]),  # 两次失败
        "qwen-free": ScriptedTransport([reply('{"a": 1}', tokens=(90, 7))])}, s, meter=meter)  # 降级成功
    s.open_case("K7", day=16, actor="runtime")  # 开案
    s.transition("K7", "planning", day=16, actor="runtime")  # 规划
    s.transition("K7", "executing", day=16, actor="orch")  # 执行
    from pydantic import BaseModel  # 输出模型

    class Out(BaseModel):  # 简单输出
        a: int  # 字段

    adapter.call("default", [Message("user", "q")], output=Out, day=16, actor="orch", case_id="K7")  # 模型调用
    args = {"mpns": ["SKY13453-385LF"], "qty": 10}  # 报价参数
    proxy.call("distributor_quotes", args, executor="v", actor="v", day=16, case_id="K7")  # 首次：三家扣配额
    proxy.call("distributor_quotes", args, executor="v", actor="v", day=16, case_id="K7")  # 缓存命中
    proxy.call("distributor_quotes", {**args, "qty": 11, "distributors": ["mouser"]},  # mouser 配额已用完
               executor="v", actor="v", day=16, case_id="K7")  # 限流
    s.transition("K7", "planning", day=17, actor="runtime")  # 重规划
    s.append("human_request", day=17, actor="runtime", case_id="K7", payload={"role": "buyer"})  # 打扰人工
    s.close_case("K7", "escalated", day=17, actor="runtime")  # 升级关闭
    return s, meter_store, kv  # 返回


# 功能：计量行、案子汇总、上下文快照、配额面板与重建都正确。
def test_metering_summary_panel_and_rebuild():
    s, store, kv = scenario(MemorySessionStore(), MemoryMeterStore())  # 跑场景
    rows = store.rows("run")  # 计量行
    assert [r["kind"] for r in rows] == ["llm"] * 3 + ["tool"] * 3  # 3 次 LLM、3 次工具
    summary = case_summary(s, store, "K7")  # 案子汇总
    assert summary | {"latency_ms": 0} == {  # 除耗时外逐项
        "case_id": "K7", "llm_calls": 3, "input_tokens": 310, "output_tokens": 17, "repairs": 1,
        "fallbacks": 1, "tool_calls": 3, "cache_hits": 1, "errors": {"rate_limited": 1},
        "replans": 1, "escalations": 1, "human_interruptions": 1, "violations": 0, "latency_ms": 0}
    first_llm = s.events(type="llm_call")[0]["seq"]  # 第一次模型调用
    assert context_snapshot(s, first_llm)["messages"][-1]["content"] == "q"  # 当时的完整请求
    assert context_snapshot(s, 1) is None  # 非 llm_call 事件
    panel = quota_panel(store, load_tools_config()["providers"] | {"mouser": {"daily_limit": 1}}, DAY)  # 面板
    assert panel["mouser"] == {"used": 1, "limit": 1, "remaining": 0}  # mouser 用完
    assert panel["glm-flash"]["used"] == 2 and panel["qwen-free"]["used"] == 1  # 模型端点
    before = dict(kv.counters)  # 原计数
    fresh = MemoryKV()  # Redis 丢失
    rebuild_quota(fresh, store, load_tools_config()["providers"] | {"mouser": {"daily_limit": 1}}, DAY)  # 重建
    for key, (n, _) in before.items():  # 每个日配额键
        if key.startswith("quota:"):  # 日配额
            assert fresh.counters[key][0] == n  # 与原计数一致
    assert run_summary(s, store, {"stop_line_days": 0})["environment"] == {"stop_line_days": 0}  # 附环境指标


# 功能：不传 meter 时行为不变：事件哈希链与有计量时逐条相同，耗时不进事件。
def test_no_meter_no_change():
    with_meter, _, _ = scenario(MemorySessionStore(), MemoryMeterStore())  # 有计量
    without, store, _ = scenario(MemorySessionStore(), MemoryMeterStore(), use_meter=False)  # 无计量
    assert [e["hash"] for e in with_meter.events()] == [e["hash"] for e in without.events()]  # 链相同
    assert store.rows() == []  # 没写计量
    assert all("latency" not in str(e["payload"]) for e in with_meter.events())  # 耗时不进哈希链


# 功能：集成：Postgres 计量表往返，汇总与内存实现一致。
@pytest.mark.integration
def test_pg_meter_matches_memory(migrated):
    conn, _, _ = migrated  # 连接
    s_pg, pg, _ = scenario(PgSessionStore(conn), PgMeterStore(conn))  # Pg 版
    s_mem, mem, _ = scenario(MemorySessionStore(), MemoryMeterStore())  # 内存版

    def strip(summary):  # 去掉耗时（真实时间不同）
        return {k: v for k, v in summary.items() if k != "latency_ms"}  # 其余逐项比较

    assert strip(case_summary(s_pg, pg, "K7")) == strip(case_summary(s_mem, mem, "K7"))  # 汇总一致
    assert [r["quota_used"] for r in pg.rows("run")] == [r["quota_used"] for r in mem.rows("run")]  # 行一致

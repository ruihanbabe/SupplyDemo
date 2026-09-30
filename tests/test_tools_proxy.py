"""F07：工具代理层测试。离线部分用内存 KV / 留档；集成部分用真实 Redis 与 Postgres。全部不联网。"""
import threading  # 并发扣令牌
from uuid import uuid4  # Redis 键前缀隔离

import pytest  # 测试框架
from kit import guard, tok  # F11 权限装配

from infrastructure.config import load_harness_config  # 消融开关
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from simenv.api import Environment  # 模拟环境
from simenv.bom import load_boms  # BOM
from simenv.params import load_params  # 参数
from tools.archive import MemoryArchive, PgArchive  # 留档
from tools.conflicts import conflict_warnings  # B2
from tools.kv import MemoryKV, RedisKV  # 缓存与计数
from tools.proxy import ToolProxy, load_tools_config  # 被测
from tools.quota import QuotaGuard  # 配额
from tools.servers import (  # 各 Server
    archive_server,  # 留档
    control_server,  # 编排控制
    distributor_server,  # 分销商
    environment_server,  # 环境
    validate_quotes,  # B3 校验
)

MPN = "SKY13453-385LF"  # hackrf 上的射频开关
ALL = ["env_inventory", "env_purchase_orders", "env_shipments", "env_work_orders", "env_orders",  # 四个 Server 的全部工具
       "distributor_quotes", "read_record", "dispatch_task", "propose_goal_revision", "report_to_planner",
       "ask_user", "end_segment"]


class Clock:
    """可拨动的真实时钟（秒）。"""

    def __init__(self):
        self.t = 1000.0  # 起点

    def __call__(self):
        return self.t  # 当前时刻


@pytest.fixture(scope="module")
def env0():
    return Environment.create(load_params(), load_boms(), 42)  # 种子 42 环境（只读用）


# 功能：装配一个代理层；可替换 KV、留档、挂点、白名单、配置。
def make(env, *, kv=None, archive=None, overlays=(), faults=(), executors=None, config=None, **sw):
    config = config or load_tools_config()  # 配置
    kv = kv or MemoryKV(Clock())  # KV
    session = Session(MemorySessionStore(), "run", clock=lambda: "t")  # 事件日志
    backend_calls = []  # 记录上游调用次数

    class CountingBackend:  # 数上游调用的固定样例后端
        def raw_quote(self, mpn, d):  # 取原文报价
            backend_calls.append((mpn, d))  # 计数
            from tools.servers import FixtureDistributorBackend  # 延迟导入
            return FixtureDistributorBackend().raw_quote(mpn, d)  # 委托

    servers = [environment_server(lambda: env), distributor_server(CountingBackend()),  # 四个 Server
               archive_server(), control_server()]  # 留档与控制
    proxy = ToolProxy(servers, session, kv=kv, quota=QuotaGuard(kv, config["providers"]),  # 代理层
                      archive=archive or MemoryArchive(), config=config,  # 留档与配置
                      guard=guard(executors or {"verifier": ["distributor_quotes", "read_record",  # F11 角色工具
                                                             "env_inventory"]}),  # 三个工具
                      overlays=overlays, faults=faults,  # 挂点
                      switches={**load_harness_config()["ablation"], **sw})  # 开关
    return proxy, session, backend_calls  # 返回


def q(proxy, day=16, **args):
    """以 verifier 身份查报价。"""
    return proxy.call("distributor_quotes", {"mpns": [MPN], "qty": 300, **args},  # 调用
                      token=tok(proxy, "verifier"), day=day)  # 身份与日


# 功能：白名单拒绝与不存在的工具都不调用上游，错误附可用工具；参数错误不扣配额。环境 Server 没有写 / 改计划工具。
def test_whitelist_params_and_no_write_tools(env0):
    proxy, session, calls = make(env0)  # 装配
    r = proxy.call("env_work_orders", {}, token=tok(proxy, "verifier"), day=1)  # 不在白名单
    assert r["error"]["type"] == "forbidden" and "distributor_quotes" in r["error"]["message"]  # 附可用工具
    r = proxy.call("reschedule_work_order", {}, token=tok(proxy, "verifier"), day=1)  # 编造的工具
    assert r["error"]["type"] == "not_found"  # 不存在
    r = q(proxy, qty=-1)  # 参数错误
    assert r["error"]["type"] == "param_error" and calls == []  # 不调用上游
    assert proxy.quota.kv.counters == {}  # 没扣配额
    full = make(env0, executors={"x": ALL})[0]  # 角色拥有全部工具的代理层
    names = {t["name"] for t in full.visible_tools(tok(full, "x"))}  # 全部可见工具（S1）
    assert "distributor_quotes" in names  # 确实拿到了工具清单
    assert not any(w in n for n in names for w in ("submit", "reschedule", "update", "write"))  # 无写 / 改计划工具
    assert len(session.events(type="tool_call")) == 3  # 每次调用一条事件


# 功能：缓存按真实时间：命中不调上游、但沿用原取数日（有效期按模拟日照样过期）；TTL 到期后重新调用。
def test_cache_uses_real_time_validity_uses_sim_day(env0):
    clock = Clock()  # 真实时钟
    proxy, _, calls = make(env0, kv=MemoryKV(clock))  # 装配
    first = q(proxy, day=16)  # 第 16 天首次
    assert first["ok"] and len(calls) == 3  # 三家各一次
    assert first["source"]["fetched_at"] == 16 and first["source"]["valid_until"] == 17  # 有效到第 17 天
    clock.t += 60  # 真实时间过 1 分钟
    second = q(proxy, day=18)  # 第 18 天（模拟时间已过期）
    assert len(calls) == 3 and second["source"]["fetched_at"] == 16  # 命中缓存，取数日仍是第 16 天
    assert second["source"]["valid_until"] < 18  # 门控据此判过期（A3）
    assert second["data"] == first["data"] and second["source"]["record_id"] != first["source"]["record_id"]  # 新交付新记录
    clock.t += 1800  # 真实时间超过 TTL
    q(proxy, day=18)  # 再查
    assert len(calls) == 6  # 重新调用上游


# 功能：日配额与令牌桶：某家限流时部分成功并警告，全部不可用时返回限流错误；令牌按时间补充。
def test_quota_partial_success_and_bucket_refill(env0):
    clock = Clock()  # 真实时钟
    config = load_tools_config()  # 配置
    config["tools"]["distributor_quotes"]["cache_ttl_s"] = 0  # 关缓存，专测配额
    config["providers"]["mouser"] = {"daily_limit": 1, "rate_per_s": None, "burst": None}  # mouser 每天 1 次
    proxy, _, _ = make(env0, kv=MemoryKV(clock), config=config)  # 装配
    assert {r["distributor"] for r in q(proxy)["data"]} == {"digikey", "mouser", "element14"}  # 第一次三家齐
    r = q(proxy)  # 第二次：mouser 日配额用完
    assert {x["distributor"] for x in r["data"]} == {"digikey", "element14"}  # 部分成功
    assert any("mouser" in w for w in r["warnings"])  # 写明缺了哪家
    only = q(proxy, distributors=["mouser"])  # 只查 mouser
    assert only["error"]["type"] == "rate_limited" and only["error"]["retryable"]  # 全部不可用
    guard = QuotaGuard(MemoryKV(clock), {"p": {"daily_limit": None, "rate_per_s": 1, "burst": 2}})  # 令牌桶
    assert [guard.try_acquire("p") for _ in range(3)] == [None, None, "rate_limited"]  # 突发 2 次
    clock.t += 1  # 过 1 秒补 1 个令牌
    assert guard.try_acquire("p") is None  # 恢复
    assert guard.try_acquire("unknown") == "unconfigured"  # 未配置的提供方拒绝


# 功能：返回校验（B3）：缺字段置未知并警告；整体格式异常返回错误；消融关闭时原样交付。
def test_return_validation_b3(env0):
    rows = [{"mpn": "A", "distributor": "mouser", "stock": 5, "unit_price_usd": None, "lead_days": 3}]  # 缺单价
    data, warnings = validate_quotes(rows)  # 校验
    assert data[0]["unit_price_usd"] == "unknown" and "不要自行推测" in warnings[0]  # 置未知并警告

    proxy, _, _ = make(env0, faults=[lambda tool, args, day: "bad_format" if day == 20 else None])  # 第 20 天注入
    assert q(proxy, day=20)["error"]["type"] == "bad_format"  # 测试注入的格式异常
    assert q(proxy, day=21)["ok"]  # 其他天正常
    event = [e for e in proxy.session.events(type="tool_call") if e["payload"].get("injected_fault")]  # 注入事件
    assert event[0]["payload"]["injected_fault"] == "bad_format"  # 标明测试注入


# 功能：注入叠加：留档同时记原始与交付数据；缓存里是原始数据，命中时按当天重新叠加。
def test_overlay_records_raw_and_delivered(env0):
    def buyout(tool, args, data, day):  # A2：第 18 天起 element14 被买空
        if tool != "distributor_quotes" or day < 18:  # 其他情况不改
            return data  # 原样
        return [{**r, "stock": 0} if r["distributor"] == "element14" else r for r in data]  # 库存改 0

    from tools.servers import (  # 选一颗 element14 有货的料
        FixtureDistributorBackend,
        normalize_quote,
    )
    mpn = next(m for m in sorted(env0.get_bom("hackrf-one"))  # 遍历 hackrf 料号
               if normalize_quote("element14", FixtureDistributorBackend().raw_quote(m, "element14"), 1)["stock"])
    archive = MemoryArchive()  # 留档
    proxy, _, calls = make(env0, archive=archive, overlays=[buyout])  # 装配
    before = q(proxy, day=16, mpns=[mpn])  # 第 16 天
    after = q(proxy, day=18, mpns=[mpn])  # 第 18 天（缓存命中）
    assert len(calls) == 3  # 只调用了一次上游
    def e14(rows):  # 取 element14 行
        return next(r for r in rows if r["distributor"] == "element14")  # 第一条
    assert e14(after["data"])["stock"] == 0  # 当天叠加生效
    rec = archive.get("run", after["source"]["record_id"])  # 留档
    assert e14(rec["raw"])["stock"] == e14(before["data"])["stock"]  # 原始值保留为第 16 天的真实值
    assert rec["from_cache_of"] == before["source"]["record_id"]  # 指向原记录
    assert rec["delivered"] == after["data"]  # 交付数据是叠加后的
    ev = proxy.session.events(type="tool_call")[-1]["payload"]  # 事件
    assert ev["overlaid"] is True and ev["cache_hit"] is True  # 标明被注入、命中缓存


# 功能：分页：列表超过 10 条截断，游标读留档翻页，不调上游、不扣配额。
def test_pagination_reads_archive(env0):
    proxy, _, calls = make(env0)  # 装配
    mpns = sorted(env0.get_bom("hackrf-one"))[:5]  # 5 颗料 × 3 家 = 15 行
    r = proxy.call("distributor_quotes", {"mpns": mpns, "qty": 10}, token=tok(proxy, "verifier"), day=1)  # 查
    assert len(r["data"]) == 10 and r["truncated"] and r["next_cursor"].endswith(":10")  # 截断
    n = len(calls)  # 上游调用数
    page2 = proxy.call("read_record", {"cursor": r["next_cursor"]}, token=tok(proxy, "verifier"), day=1)  # 翻页
    assert len(page2["data"]) == 5 and not page2["truncated"] and len(calls) == n  # 读留档，不调上游
    bad = proxy.call("read_record", {"cursor": "rec-999"}, token=tok(proxy, "verifier"), day=1)  # 不存在
    assert bad["error"]["type"] == "not_found"  # 查不到


# 功能：环境 Server 返回 F03 的真实状态、查不到转 not_found；编排控制未注册时明确报"尚未具备"。
def test_environment_and_control_servers(env0):
    proxy, _, _ = make(env0, executors={"orch": ALL})  # 全部工具
    inv = proxy.call("env_inventory", {"mpns": [MPN]}, token=tok(proxy, "orch"), day=0)  # 查库存
    assert inv["data"][0] == env0.get_inventory(MPN) and inv["source"]["provider"] == "sim"  # 与环境一致
    miss = proxy.call("env_orders", {"order_id": "O-999"}, token=tok(proxy, "orch"), day=0)  # 查不到
    assert miss["error"]["type"] == "not_found"  # 转换
    ctl = proxy.call("end_segment", {"wait_for": "human", "reason": "等审批"}, token=tok(proxy, "orch"), day=0)  # 控制工具
    assert ctl["error"]["type"] == "upstream_error" and "尚未具备" in ctl["error"]["message"]  # 明确报尚未具备


# 功能：B2 多源矛盾：超过容差警告、容差内不警告、非数值不同警告、缺值来源不参与。
def test_conflict_warnings_b2():
    assert conflict_warnings("isolation_db", {"digikey": 25, "datasheet": 20}, 5)  # 差 20%
    assert conflict_warnings("isolation_db", {"digikey": 25, "datasheet": 24.5}, 5) == []  # 差 2%
    assert conflict_warnings("package", {"digikey": "QFN-16", "datasheet": "QFN16"}, 5)  # 文本不同
    assert conflict_warnings("x", {"digikey": 1, "mouser": None}, 5) == []  # 只有一个有效来源


# 功能：集成：真实 Redis 上缓存 TTL、日配额、令牌桶在并发线程下不超发；Postgres 留档往返。
@pytest.mark.integration
def test_redis_and_pg_archive(migrated, env0):
    import redis  # Redis 客户端

    from infrastructure.redis_store import redis_url  # 地址

    client = redis.Redis.from_url(redis_url())  # 连接
    prefix = f"test:{uuid4().hex}:"  # 隔离前缀
    kv = RedisKV(client, prefix)  # Redis 实现
    try:  # 测试后清理
        kv.set("k", "v", 1)  # TTL 1 秒
        assert kv.get("k") == "v"  # 读到
        assert [kv.take_quota("q", 3, 60) for _ in range(4)] == [True, True, True, False]  # 日配额
        results = []  # 并发结果

        def worker():  # 每个线程取 5 次令牌
            for _ in range(5):  # 5 次
                results.append(kv.take_token("b", 0.001, 7))  # 几乎不补充，突发 7

        threads = [threading.Thread(target=worker) for _ in range(8)]  # 8 个线程共 40 次
        [t.start() for t in threads]  # 启动
        [t.join() for t in threads]  # 等待
        assert sum(results) == 7  # 原子扣减：恰好放行 7 次
        conn, _, _ = migrated  # 迁移好的连接
        proxy, _, _ = make(env0, kv=kv, archive=PgArchive(conn))  # Pg 留档 + Redis
        r = q(proxy, day=16)  # 查报价
        rec = PgArchive(conn).get("run", r["source"]["record_id"])  # 读留档
        assert rec["delivered"] == r["data"] and rec["tool"] == "distributor_quotes"  # 往返一致
    finally:  # 清理测试键
        for key in client.scan_iter(prefix + "*"):  # 所有测试键
            client.delete(key)  # 删除

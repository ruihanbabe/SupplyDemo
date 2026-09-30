"""F15：异常处理与恢复测试。离线部分用内存 KV 与可拨动时钟（不真睡）、固定样例报价、脚本模型；集成部分用真实 Redis。"""
from uuid import uuid4  # Redis 键前缀隔离

import pytest  # 测试框架
from kit import MPN, build, code_pipeline, dispatch, end, guard, open_case, tok  # 共用装配

from business.procurement import ProcurementExecutor  # 采购执行器
from feedback.escalation import Escalation  # 升级
from models.providers import reply  # 脚本应答
from models.types import ModelUnavailable  # 传输失败
from resilience.policy import Resilience  # 重试与熔断
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from tools.archive import MemoryArchive  # 留档
from tools.kv import MemoryKV, RedisKV  # KV
from tools.proxy import ToolProxy, load_tools_config  # 代理层
from tools.quota import QuotaGuard  # 配额
from tools.servers import FixtureDistributorBackend, distributor_server  # 分销商


class Clock:
    """可拨动的真实时钟（秒）。"""

    def __init__(self):
        self.t = 1000.0  # 起点

    def __call__(self):
        return self.t  # 当前时刻


class Flaky(FixtureDistributorBackend):
    """可控失败的后端：fail 为要失败的分销商集合（"*" = 全部），calls 记录真实请求。"""

    def __init__(self):
        self.fail, self.calls = set(), []  # 失败集合与请求记录

    def raw_quote(self, mpn, d):
        self.calls.append(d)  # 记请求
        if "*" in self.fail or d in self.fail:  # 该家失败
            raise ConnectionError(f"{d} 503")  # 上游错误
        return super().raw_quote(mpn, d)  # 正常


# 功能：装配带重试与熔断的代理层（配额不限，避免真实时间令牌桶干扰）。依赖：kit.guard。
# 完成：返回 (proxy, session, backend, clock, sleeps)。
def make(faults=(), **switches):
    clock, sleeps = Clock(), []  # 时钟与睡眠记录
    kv = MemoryKV(clock)  # 缓存、配额、熔断共用
    cfg = load_tools_config()  # 配置
    cfg["providers"] = {k: {"daily_limit": None, "rate_per_s": None, "burst": None} for k in cfg["providers"]}
    backend = Flaky()  # 后端
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    res = Resilience(kv, sleep=sleeps.append, **switches)  # 重试与熔断（睡眠只记录）
    proxy = ToolProxy([distributor_server(backend)], s, kv=kv, quota=QuotaGuard(kv, cfg["providers"]),  # 代理层
                      archive=MemoryArchive(), config=cfg, guard=guard(), faults=faults, resilience=res)
    return proxy, s, backend, clock, sleeps  # 返回


def q(proxy, qty=1, **args):
    """以 sourcing 身份查报价（数量不同即不命中缓存）。"""
    return proxy.call("distributor_quotes", {"mpns": [MPN], "qty": qty, **args}, token=tok(proxy, "sourcing"), day=0)


# 功能：重试——首次超时（测试注入）第二次成功，退避 0.5 秒；参数错误不重试；关 auto_retry 后首次失败即交回。
def test_retry_with_backoff():
    first = iter([True])  # 只在第一次注入

    def once(tool, args, day):  # 第一次超时
        return "timeout" if next(first, False) else None  # 注入

    proxy, s, _, _, sleeps = make(faults=[once])  # 装配
    r = q(proxy)  # 调用
    assert r["ok"] and sleeps == [0.5] and s.events(type="tool_call")[-1]["payload"]["attempts"] == 2  # 重试一次
    bad = proxy.call("distributor_quotes", {"mpns": [MPN], "qty": -1}, token=tok(proxy, "sourcing"), day=0)  # 参数错误
    assert bad["error"]["type"] == "param_error" and len(sleeps) == 1  # 不重试
    proxy, s, _, _, sleeps = make(faults=[lambda *a: "timeout"], retry=False)  # 关自动重试
    r = q(proxy)  # 调用
    assert r["error"]["type"] == "timeout" and r["error"]["retryable"] and sleeps == []  # 直接交回


# 功能：熔断——全部分销商连续失败：每次调用重试 2 次（退避 0.5、1.0）仍失败计 1 次，3 次后熔断；
#       熔断期间不发请求、返回可重试的上游错误；5 分钟后恢复；关 circuit_breaker 后一直发请求。
def test_circuit_breaker_opens_and_recovers():
    proxy, s, backend, clock, sleeps = make()  # 装配
    backend.fail = {"*"}  # 全部失败
    for i in range(3):  # 三次调用
        r = q(proxy, qty=i + 1)  # 调用
        assert r["error"]["type"] == "upstream_error"  # 失败
    assert sleeps == [0.5, 1.0] * 3 and len(backend.calls) == 3 * 3 * 3  # 每次 3 次尝试 × 3 家
    n = len(backend.calls)  # 已发请求
    r = q(proxy, qty=9)  # 熔断中
    assert r["error"]["retryable"] and "熔断" in r["error"]["message"] and len(backend.calls) == n  # 不发请求
    assert s.events(type="tool_call")[-1]["payload"]["circuit_open"] == ["digikey", "element14", "mouser"]  # 记录
    backend.fail = set()  # 上游恢复
    clock.t += 301  # 过 5 分钟
    assert q(proxy, qty=10)["ok"]  # 熔断自动恢复
    proxy, _, backend, _, _ = make(breaker=False)  # 关熔断
    backend.fail = {"*"}  # 全部失败
    for i in range(4):  # 四次
        q(proxy, qty=i + 1)  # 调用
    assert len(backend.calls) == 4 * 3 * 3  # 一直发请求


# 功能：单家失败——mouser 出错时部分成功（写明缺失、不整体重试），计入 mouser 熔断；3 次后 mouser 熔断、不再请求它。
def test_single_distributor_failure_partial_and_breaker():
    proxy, _, backend, _, sleeps = make()  # 装配
    backend.fail = {"mouser"}  # 只有 mouser 失败
    for i in range(3):  # 三次
        r = q(proxy, qty=i + 1)  # 调用
        assert r["ok"] and {x["distributor"] for x in r["data"]} == {"digikey", "element14"}  # 部分成功
        assert any("mouser 本次调用失败" in w for w in r["warnings"])  # 写明
    assert sleeps == []  # 部分成功不重试
    backend.calls.clear()  # 清记录
    r = q(proxy, qty=9)  # mouser 已熔断
    assert "mouser" not in backend.calls and any("mouser 熔断中" in w for w in r["warnings"])  # 不请求 mouser


# 功能：模型端点——超时一次后重试成功；三次都超时 → 子 Agent 交回"未完成"（写明端点不可用），编排者照常结束本段。
def test_model_endpoint_retry_and_unfinished():
    good = reply('{"note": "ok"}')  # 子 Agent 结论
    timeout = ModelUnavailable("timeout", "超时")  # 传输失败
    s, seg, _, _, _ = build([dispatch(1), timeout, good, end()])  # 子 Agent 首次超时
    sleeps = []  # 睡眠记录
    seg.loop.adapter.resilience = Resilience(MemoryKV(), sleep=sleeps.append)  # 接入重试
    open_case(s)  # 开案
    seg.run("K7", reason="开案", day=0)  # 跑一段
    assert s.events(type="task_finished")[0]["payload"]["status"] == "done" and sleeps == [0.5]  # 重试后成功
    s2, seg2, _, _, _ = build([dispatch(1), timeout, timeout, timeout, end()])  # 三次都超时
    seg2.loop.adapter.resilience = Resilience(MemoryKV(), sleep=lambda x: None)  # 接入重试
    open_case(s2)  # 开案
    seg2.run("K7", reason="开案", day=0)  # 跑一段
    fin = s2.events(type="task_finished")[0]["payload"]  # 子任务结果
    assert fin["status"] == "unfinished" and "模型端点不可用" in fin["reason"]  # 交回未完成
    assert s2.case_state("K7")["state"] == "waiting"  # 编排者正常结束本段


class ProcessKilled(BaseException):
    """模拟进程被杀（不是 Exception，任何业务代码都不会吞掉它）。"""


# 功能：装配到"草稿已建好"的局面（最小门控，不需审批）。完成：返回 (p, 草稿引用)。
def drafted():
    p = code_pipeline(unlimited=True)  # 装配
    impact, _ = p.run("impact_analysis")  # 影响分析
    quote, _ = p.run("sourcing", [impact])  # 比价
    dec_ref, dec = p.run("policy", [quote])  # 决策
    mpn = next(d["mpn"] for d in dec["content"]["decisions"] if d["chosen"])  # 可行料
    return p, p.ex.create_draft(case_id="K7", day=0, decision_ref=dec_ref, mpn=mpn)["draft"]  # 草稿


# 功能：两步写与崩溃恢复——提交途中进程被杀（写环境前 / 写环境后两种）：重启后按意图补做，环境里只有一张采购单，
#       日志补齐完成（标 recovered）；再恢复一次什么都不做。
@pytest.mark.parametrize("after_write", [False, True])
def test_crash_recovery_never_double_orders(after_write):
    p, draft = drafted()  # 局面
    real = p.env.submit_purchase_order  # 真实写接口

    def killed(**kw):  # 被杀的写接口
        if after_write:  # 写入后才死
            real(**kw)  # 环境已写入
        raise ProcessKilled()  # 进程被杀

    p.env.submit_purchase_order = killed  # 换上
    with pytest.raises(ProcessKilled):  # 提交途中崩溃
        p.ex.submit(case_id="K7", day=0, draft_ref=draft)  # 提交
    assert p.s.events(type="po_submit_intent") and not p.s.events(type="po_submitted")  # 有意图无完成
    p.env.submit_purchase_order = real  # "重启"
    restarted = ProcurementExecutor(p.s, lambda: p.env)  # 新进程的执行器
    out = restarted.recover_pending_writes(day=0)  # 恢复
    assert len(out) == 1 and out[0]["ok"] and out[0]["duplicate"] is after_write  # 写过的补记、没写的重做
    done = p.s.events(type="po_submitted")  # 完成事件
    assert len(done) == 1 and done[0]["payload"]["recovered"] is True  # 补齐
    assert len(p.env.list_purchase_orders()) == len({po["id"] for po in p.env.list_purchase_orders()})  # 无重复
    mpn = p.s.events(type="po_submit_intent")[0]["payload"]["order"]["mpn"]  # 料号
    assert len([po for po in p.env.list_purchase_orders(mpn=mpn) if po["status"] == "submitted"]) == 1  # 只有一张
    assert restarted.recover_pending_writes(day=0) == []  # 再恢复一次：没有待补的


# 功能：环境拒绝写入（替代料不在 BOM）→ 记 po_submit_failed 并按门控拒绝反馈，恢复时不再重做。
def test_environment_rejection_is_recorded():
    p = code_pipeline()  # 装配（最小门控）
    chosen = {"distributor": "digikey", "stock": 900, "unit_price_usd": 0.5, "eta_day": 4, "amount_usd": 50.0}
    p.s.produce("Decision:S", "ProcurementDecision", {"feasible": True, "decisions": [
        {"mpn": "SUBX-1", "qty": 100, "chosen": chosen}]}, day=0, actor="policy", case_id="K7",
        meta={"taint": {"*": "trusted", "decisions[].chosen": "external"}})  # 替代料决策
    draft = p.ex.create_draft(case_id="K7", day=0, decision_ref="Decision:S@v1", mpn="SUBX-1")["draft"]  # 草稿
    r = p.ex.submit(case_id="K7", day=0, draft_ref=draft)  # 提交
    assert r["ok"] is False and "环境拒绝写入" in r["reasons"][0]  # 反馈
    assert p.s.events(type="po_submit_failed") and p.s.events(type="gate_rejected")[-1]["payload"]["action"] == "submit"
    assert p.ex.recover_pending_writes(day=0) == []  # 失败也算完成，不重做


# 功能：补偿——采购单提交后，所依据的草稿因报价变化失效 → 不撤单，升级给人。
def test_post_submit_invalidation_escalates():
    p, draft = drafted()  # 局面
    assert p.ex.submit(case_id="K7", day=0, draft_ref=draft)["ok"]  # 提交
    order = p.s.events(type="po_submit_intent")[0]["payload"]["order"]  # 采购单内容
    mpn, dist = order["mpn"], order["distributor"]  # 料号与分销商
    p.s.invalidate_external(f"quote:{dist}:{mpn}", "day5", day=5)  # 报价变化 → 决策、草稿失效
    assert Escalation(p.s).check("K7", day=5) is True  # 升级
    assert p.s.events(type="escalated")[0]["payload"]["rule"] == "post_submit_invalidation"  # 规则
    assert len(p.env.list_purchase_orders(mpn=mpn)) >= 1  # 没有撤单


# 功能：部分成功——mouser 当日配额为 0：比价只含可用来源并写明缺失，决策记录缺失来源，草稿照常建立。
def test_partial_success_still_drafts():
    p = code_pipeline(unlimited=True, providers={"mouser": {"daily_limit": 0, "rate_per_s": None, "burst": None}})
    impact, _ = p.run("impact_analysis")  # 影响分析
    quote_ref, quote = p.run("sourcing", [impact])  # 比价
    assert all(i["missing"] == ["mouser"] for i in quote["content"]["items"])  # 写明缺失
    dec_ref, dec = p.run("policy", [quote_ref])  # 决策
    d = next(x for x in dec["content"]["decisions"] if x["chosen"])  # 可行料
    assert d["missing"] == ["mouser"]  # 决策记录缺失来源
    assert p.ex.create_draft(case_id="K7", day=0, decision_ref=dec_ref, mpn=d["mpn"])["ok"]  # 照常建草稿


# 功能：重派——子任务未完成后同一执行者同一任务再派，标 redispatch_of，并计入案子的重规划预算。
def test_redispatch_counts_as_replan():
    s, seg, _, _, _ = build([], budgets={"case": {"max_replans": 0, "max_llm_calls": 80}})  # 重规划上限 0
    open_case(s)  # 开案
    s.transition("K7", "planning", day=0, actor="runtime")  # 首次规划（不算重规划）
    parent = tok(seg.authority, "orchestrator")  # 编排者令牌
    seg.dispatcher.dispatch(parent=parent, day=0, executor="quoter", task="查报价", inputs=[], reason="r")  # 派
    s.append("task_finished", day=0, actor="runtime", case_id="K7",  # 未完成
             payload={"task_id": "K7-T1", "status": "unfinished", "reason": "预算触顶"})
    seg.dispatcher.dispatch(parent=parent, day=0, executor="quoter", task="查报价", inputs=[], reason="r")  # 重派
    assert s.events(type="task_dispatched")[-1]["payload"]["redispatch_of"] == "K7-T1"  # 标记
    assert "含重派 1 次" in seg._over_budget("K7")  # 计入重规划预算


# 功能：集成——真实 Redis 上熔断：三次失败后熔断键带 TTL（≤ 5 分钟），成功清零计数。
@pytest.mark.integration
def test_breaker_on_redis():
    import redis  # Redis 客户端

    from infrastructure.redis_store import redis_url  # 地址

    client = redis.Redis.from_url(redis_url())  # 连接
    prefix = f"test:{uuid4().hex}:"  # 隔离前缀
    res = Resilience(RedisKV(client, prefix))  # Redis 上的熔断
    try:  # 测试后清理
        res.record_failure("mouser")  # 1
        res.record_success("mouser")  # 清零
        assert [res.record_failure("mouser") for _ in range(3)] == [False, False, True]  # 连续 3 次熔断
        assert res.is_open("mouser") and not res.is_open("digikey")  # 只熔断这一家
        assert 0 < client.ttl(prefix + "cb:open:mouser") <= 300  # TTL 到期自动恢复
    finally:  # 清理测试键
        for key in client.scan_iter(prefix + "*"):  # 所有测试键
            client.delete(key)  # 删除

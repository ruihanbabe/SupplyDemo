"""F18：测试注入、回放与分叉测试。全部离线（固定样例报价、脚本模型）；集成部分用 Postgres 环境存储做分叉往返。"""
import shutil  # 复制配置目录

import pytest  # 测试框架
import yaml  # 改配置
from kit import MPN, build, call, code_pipeline, dispatch, end, open_case, tok  # 共用装配

from models.providers import load_models_config, reply  # 脚本应答与模型配置
from replay.faults import FaultPlan, ProcessKilled  # 注入
from replay.fork import fork_at_wake  # 分叉
from replay.manifest import ROOT, build_manifest, diff_manifests, record_manifest  # 运行清单
from replay.modes import ArchiveReplay, ReplayDiverged, ReplayMissing, ReplayTransport  # 回放
from resilience.policy import Resilience  # 重试
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from simenv.model import state_hash  # 环境状态哈希
from simenv.store import MemoryEnvStore, PgEnvStore  # 环境存储
from tools.kv import MemoryKV  # KV


# 功能：工具代理层注入——"element14 第 2 次报价超时"：第 1 次正常，第 2 次注入超时后 F15 重试成功；注入记为测试注入事件。
def test_tool_fault_nth_call_with_retry():
    p = code_pipeline(unlimited=True)  # 装配
    plan = FaultPlan(p.s, [{"hook": "tool", "tool": "distributor_quotes", "distributor": "element14",
                            "nth": 2, "kind": "timeout"}])  # 规则
    p.proxy.faults = [plan.tool_fault]  # 接挂点
    p.proxy.resilience = Resilience(MemoryKV(), sleep=lambda s: None)  # 接重试（不真睡）
    token = tok(p.proxy, "sourcing")  # 身份
    first = p.proxy.call("distributor_quotes", {"mpns": [MPN], "qty": 1}, token=token, day=0)  # 第 1 次
    second = p.proxy.call("distributor_quotes", {"mpns": [MPN], "qty": 2}, token=token, day=0)  # 第 2 次
    assert first["ok"] and second["ok"]  # 都成功
    ev = p.s.events(type="tool_call")[-1]["payload"]  # 第 2 次的事件
    assert ev["attempts"] == 2 and ev["injected_fault"] == "timeout"  # 注入后重试一次
    inj = p.s.events(type="fault_injected")  # 注入事件
    assert len(inj) == 1 and inj[0]["payload"]["test_injection"] is True and inj[0]["payload"]["kind"] == "timeout"
    with pytest.raises(ValueError):  # 写错的规则立即报错
        FaultPlan(p.s, [{"hook": "tool", "kind": "explode"}])  # 未知类型


# 功能：模型适配层注入——第 1 次调用返回不合 Schema 的输出，F08 修复一轮后通过；注入事件记录端点。
def test_model_fault_bad_schema_triggers_repair():
    s, seg, _, _, _ = build([end()])  # 编排者只有一个真实应答
    plan = FaultPlan(s, [{"hook": "model", "endpoint": "glm-flash", "nth": 1, "kind": "bad_schema"}])  # 规则
    ad = seg.loop.adapter  # 适配层
    ad.transports["glm-flash"] = plan.wrap_transport("glm-flash", ad.transports["glm-flash"])  # 包传输
    open_case(s)  # 开案
    out = seg.run("K7", reason="开案", day=0)  # 跑一段
    assert out["status"] == "waiting"  # 修复后正常结束本段
    errors = [e["payload"]["errors"] for e in s.events(type="llm_validation")]  # 校验结论
    assert "不存在" in errors[0][0] and errors[1] == []  # 注入的输出被拒，修复轮通过
    assert s.events(type="fault_injected")[0]["payload"]["detail"] == {"endpoint": "glm-flash"}  # 注入事件


# 功能：Runtime 注入——在"意图之后、调用环境之前"杀进程；重启后恢复只有一张采购单。
def test_runtime_kill_after_intent():
    p = code_pipeline(unlimited=True)  # 装配
    plan = FaultPlan(p.s, [{"hook": "runtime", "point": "after_intent", "kind": "kill"}])  # 规则
    p.ex.on_point = plan.kill_point  # 接挂点
    impact, _ = p.run("impact_analysis")  # 影响分析
    quote, _ = p.run("sourcing", [impact])  # 比价
    dec_ref, dec = p.run("policy", [quote])  # 决策
    mpn = next(d["mpn"] for d in dec["content"]["decisions"] if d["chosen"])  # 可行料
    draft = p.ex.create_draft(case_id="K7", day=0, decision_ref=dec_ref, mpn=mpn)["draft"]  # 草稿
    before = len(p.env.list_purchase_orders(mpn=mpn))  # 提交前的单数
    with pytest.raises(ProcessKilled):  # 被杀
        p.ex.submit(case_id="K7", day=0, draft_ref=draft)  # 提交
    assert len(p.env.list_purchase_orders(mpn=mpn)) == before  # 还没写环境
    p.ex.on_point = None  # "重启"后不再注入
    assert p.ex.recover_pending_writes(day=0)[0]["ok"]  # 恢复
    assert len(p.env.list_purchase_orders(mpn=mpn)) == before + 1  # 恰好一张
    assert p.s.events(type="fault_injected")[0]["payload"]["detail"]["point"] == "after_intent"  # 注入事件


QUOTE = reply(tool_calls=[call("distributor_quotes", mpns=[MPN], qty=5)])  # 子 Agent 查报价
NOTE = reply('{"note": "digikey 最便宜"}')  # 子 Agent 结论


def scenario():
    """一段完整运行：编排者派任务 → 子 Agent 查报价、给结论 → 编排者结束本段。"""
    return [dispatch(1), QUOTE, NOTE, end()]  # 脚本


def live_run():
    """实时运行（固定样例后端 = 这里的"真实"外部数据），返回 (session, 留档)。"""
    s, seg, _, _, _ = build(scenario())  # 装配
    open_case(s)  # 开案
    seg.run("K7", reason="开案", day=0)  # 跑一段
    return s, seg.loop.proxy.archive  # 事件与留档


def hashes(s):
    """事件哈希链。"""
    return [e["hash"] for e in s.store.events(s.run_id)]  # 逐条


# 功能：数据回放——外部数据取留档：不调后端、不扣配额；模型照常（同一脚本）→ 同一运行号下哈希链一致；缺留档直接报错。
def test_data_replay_uses_archive_only():
    src, archive = live_run()  # 原运行
    s, seg, _, _, _ = build(scenario())  # 回放运行（同一运行号 run）
    proxy = seg.loop.proxy  # 代理层
    proxy.replay = ArchiveReplay(src, archive, {"distributor_quotes"})  # 数据回放源
    open_case(s)  # 开案
    seg.run("K7", reason="开案", day=0)  # 跑一段
    assert proxy.quota.kv.buckets == {} and proxy.quota.kv.counters == {}  # 没扣配额（没发请求）
    assert hashes(s) == hashes(src)  # 哈希链逐条一致
    empty = Session(MemorySessionStore(), "x", clock=lambda: "t")  # 空的源运行（没有任何调用）
    proxy.replay = ArchiveReplay(empty, archive, {"distributor_quotes"})  # 没有留档
    with pytest.raises(ReplayMissing):  # 缺留档直接报错，不回退真实调用
        proxy.call("distributor_quotes", {"mpns": [MPN], "qty": 9}, token=tok(proxy, "quoter"), day=0)


# 功能：全回放——外部数据与模型都回放：同一运行号哈希链一致（模型脚本为空也能跑完）；上下文变了抛 ReplayDiverged。
def test_full_replay_hash_chain_and_divergence():
    src, archive = live_run()  # 原运行
    s, seg, _, _, _ = build([])  # 回放运行：没有任何脚本应答
    seg.loop.proxy.replay = ArchiveReplay(src, archive, {"distributor_quotes"})  # 数据回放
    seg.loop.adapter.transports["glm-flash"] = ReplayTransport(src, "glm-flash")  # 模型回放
    open_case(s)  # 同样开案
    seg.run("K7", reason="开案", day=0)  # 跑一段
    assert hashes(s) == hashes(src)  # 哈希链逐条一致
    s2, seg2, _, _, _ = build([])  # 再回放，但案子目标变了
    seg2.loop.proxy.replay = ArchiveReplay(src, archive, {"distributor_quotes"})  # 数据回放
    seg2.loop.adapter.transports["glm-flash"] = ReplayTransport(src, "glm-flash")  # 模型回放
    s2.open_case("K7", day=0, actor="runtime", payload={"goal": "换了目标"})  # 上下文不同
    with pytest.raises(ReplayDiverged):  # 请求与原记录不一致
        seg2.run("K7", reason="开案", day=0)  # 跑一段


# 功能：分叉——在唤醒边界分叉：分支复制之前的日志与 Artifact、环境取当日快照并补上分叉点前已完成的写入；
#       分叉点之后用新脚本跑出不同结果，原运行不受影响；非唤醒事件处分叉被拒。
def test_fork_at_wake():
    store = MemoryEnvStore()  # 环境存储
    src, seg, _, env, _ = build([end("arrival")], env_store=store)  # 原运行
    open_case(src)  # 开案
    seg.run("K7", reason="开案", day=0)  # 第一段
    po = env.submit_purchase_order(mpn=MPN, qty=10, distributor="digikey", unit_price="1", idempotency_key="k1")
    src.append("po_submitted", day=0, actor="procurement@K7", case_id="K7",  # 分叉点前已完成的写入
               payload={"po_id": po["po_id"], "idempotency_key": "k1"})  # 采购单
    wake = src.append("case_woken", day=0, actor="runtime", case_id="K7", payload={"reason": "到货"})  # 分叉点
    src.append("segment_started", day=0, actor="runtime", case_id="K7", payload={"reason": "到货"})  # 原运行继续
    with pytest.raises(ValueError, match="唤醒边界"):  # 非唤醒事件
        fork_at_wake(src, wake["seq"] - 1, Session(MemorySessionStore(), "f0", clock=lambda: "t"))  # 拒绝
    dst = Session(MemorySessionStore(), "run~f1", clock=lambda: "t")  # 分支
    fenv = fork_at_wake(src, wake["seq"], dst, env_store=store)  # 分叉
    copied = dst.store.events(dst.run_id)[:-1]  # 复制的事件（最后一条是 forked_from）
    assert [(e["seq"], e["type"], e["payload"]) for e in copied] == \
        [(e["seq"], e["type"], e["payload"]) for e in src.store.events(src.run_id) if e["seq"] < wake["seq"]]  # 前段一致
    assert dst.store.events(dst.run_id)[-1]["type"] == "forked_from"  # 分叉记录
    assert len(dst.store.artifacts(dst.run_id)) == len(src.store.artifacts(src.run_id))  # Artifact 复制
    assert state_hash(fenv.state) == state_hash(env.state)  # 环境一致（含补上的写入）
    _, seg2, _, _, _ = build([end("human")], session=dst, env=fenv)  # 分叉点之后换脚本
    seg2.run("K7", reason="分叉重跑", day=0)  # 跑一段
    assert dst.events(type="segment_ended")[-1]["payload"]["reason"] == "等审批"  # 新结果
    assert dst.case_state("K7")["wait_for"] == "human" and src.case_state("K7")["wait_for"] != "human"  # 原运行不变


# 功能：运行清单——记录 commit、模型、配置哈希、Policy 版本、种子、留档源；改了配置与种子后差异能列出来。
def test_manifest_diff(tmp_path):
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    a = build_manifest(seeds={"env": 42, "faults": 1}, models_config=load_models_config())  # 清单 A
    record_manifest(s, a)  # 写事件
    assert s.events(type="run_manifest")[0]["payload"]["models"]["glm-flash"]["model"]  # 模型 ID
    assert a["policy_version"] == 1 and "harness.yaml" in a["config_hashes"]  # Policy 与配置哈希
    cfg = tmp_path / "config"  # 复制一份配置
    shutil.copytree(ROOT / "config", cfg)  # 复制
    h = yaml.safe_load((cfg / "harness.yaml").read_text(encoding="utf-8"))  # 读
    h["ablation"]["loop_detection"] = False  # 改一个开关
    (cfg / "harness.yaml").write_text(yaml.safe_dump(h, allow_unicode=True), encoding="utf-8")  # 写回
    b = build_manifest(seeds={"env": 43, "faults": 1}, models_config=load_models_config(), config_dir=cfg,
                       archive_source="run")  # 清单 B
    diff = diff_manifests(a, b)  # 差异
    assert any(d.startswith("config.ablation.loop_detection: True → False") for d in diff)  # 开关
    assert any(d.startswith("config_hashes.harness.yaml") for d in diff)  # 文件哈希
    assert any(d.startswith("seeds.env: 42 → 43") for d in diff) and any("archive_source" in d for d in diff)  # 种子与留档源
    assert not any(d.startswith("seeds.faults") for d in diff)  # 没变的不列


# 功能：集成——分叉用 Postgres 环境存储：快照与写入往返，分支环境与原环境一致。
@pytest.mark.integration
def test_fork_with_pg_env_store(migrated):
    conn, _, _ = migrated  # 迁移好的连接（测试后回滚）
    store = PgEnvStore(conn)  # Postgres 环境存储
    src, seg, _, env, _ = build([end("arrival")], env_store=store)  # 原运行
    open_case(src)  # 开案
    seg.run("K7", reason="开案", day=0)  # 第一段
    po = env.submit_purchase_order(mpn=MPN, qty=10, distributor="digikey", unit_price="1", idempotency_key="k1")
    src.append("po_submitted", day=0, actor="procurement@K7", case_id="K7",
               payload={"po_id": po["po_id"], "idempotency_key": "k1"})  # 已完成的写入
    wake = src.append("case_woken", day=0, actor="runtime", case_id="K7", payload={"reason": "到货"})  # 分叉点
    dst = Session(MemorySessionStore(), "run~pg", clock=lambda: "t")  # 分支
    fenv = fork_at_wake(src, wake["seq"], dst, env_store=store)  # 分叉
    assert state_hash(fenv.state) == state_hash(env.state)  # 一致
    assert store.load_snapshot("run~pg", 0) is not None and len(store.list_writes("run~pg", 0)) == 1  # 分支落库

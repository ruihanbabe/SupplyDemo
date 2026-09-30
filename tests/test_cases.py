"""F10：事件接入与案子管理测试。全部离线：种子 42 环境 + 脚本模型（编排者每段直接结束），不产生任何真实调用。"""
from kit import MPN, build, code_pipeline, end  # 共用装配

from cases.claims import ClaimBook  # 软声明
from cases.router import CaseRouter  # 路由
from gate.approval import ApprovalVault  # 令牌保管库
from gate.evidence import EvidenceGate  # 证据门控


# 功能：装配带路由的 Runtime；编排者每次被唤醒都直接结束本段（等外部）。
# 依赖：kit.build。完成：返回 (session, scheduler, env, router)。
def runtime(merge=True, max_active=4):
    s, _, sched, env, _ = build([end("external")] * 200, max_active=max_active)  # 足够多的"结束本段"
    router = CaseRouter(s, lambda: env, merge=merge)  # F10 路由
    sched.router = router  # 接到调度循环
    return s, sched, env, router  # 返回


# 功能：Monitor 开案与同源合并（A8）——第 1 天同一根因的 5 颗缺料合成一个 S1 案子（每料一个目标），由代码开案；
#       第 2 天同样的缺口不重复报、不重复开案；关掉 same_source_merge 后每颗料单独开案。
def test_monitor_opens_one_case_per_root():
    s, sched, _, router = runtime()  # 装配
    sched.run_day()  # 第 1 天
    opened = s.events(type="case_opened")  # 开案事件
    assert len(opened) == 1 and opened[0]["actor"] == "monitor"  # 一个案子，代码开的
    k1 = opened[0]["case_id"]  # 案子号
    assert opened[0]["payload"]["mode"] == "S1" and len(router.targets(k1)) == 5  # 处置模式、五个目标
    assert s.store.event(s.run_id, opened[0]["caused_by"])["type"] == "monitor_shortage"  # 因果：Monitor 事件
    n = len(s.events(type="monitor_shortage"))  # 已报缺料数
    sched.run_day()  # 第 2 天
    assert len(s.events(type="monitor_shortage")) == n and len(s.events(type="case_opened")) == 1  # 不重复
    s2, sched2, _, _ = runtime(merge=False, max_active=10)  # 关掉同源合并
    sched2.run_day()  # 第 1 天
    assert len(s2.events(type="case_opened")) == 5  # 每料一案


# 功能：订阅唤醒——案子提交的采购单发货时，env_day 的实体键命中订阅，投递并唤醒该案子；人工应答直接唤醒所属案子；
#       用户中途插话只记录并提示"本期不支持"。
def test_subscription_human_wake_and_user_message():
    s, sched, env, router = runtime()  # 装配
    sched.run_day()  # 第 1 天开案
    k1 = s.events(type="case_opened")[0]["case_id"]  # 案子号
    mpn = min(router.targets(k1))  # 一个目标料
    po = env.submit_purchase_order(mpn=mpn, qty=10, distributor="digikey", unit_price="1", idempotency_key="t1")  # 下单
    s.append("po_submitted", day=1, actor=f"procurement@{k1}", case_id=k1, payload={"po_id": po["po_id"]},  # 提交事件
             entity_keys={"po": [po["po_id"]], "mpn": [mpn]})  # 实体键
    assert po["po_id"] in router.subscriptions(k1)["po"]  # 订阅自动包含本案采购单
    for _ in range(10):  # 推进到发货
        sched.run_day()  # 一天
        if any(e["payload"]["type"] == "env_day" for e in s.events(case_id=k1, type="event_delivered")):  # 已投递
            break  # 停
    woken = [e["payload"]["reason"] for e in s.events(case_id=k1, type="case_woken")]  # 唤醒原因
    assert any("订阅事件：env_day" in r for r in woken)  # 被发货事件唤醒
    s.append("human_decision", day=env.state.day, actor="human:buyer", case_id=k1,  # 人工应答
             payload={"node": "H4", "artifact": "x@v1", "decision": "approve", "by": "buyer"})  # 内容
    assert (k1, "人工应答：human_decision") in router(s.events(type="env_day")[-1])  # 直接唤醒
    assert "不支持" in router.user_message(k1, "改成买 500 颗", day=env.state.day)  # 插话提示
    assert s.events(type="user_message")[0]["payload"]["handled"] is False  # 未处理


# 功能：关闭条件——按缺口下单后，缺口在发货（在途）时就消除、目标先 resolved，但要等本案采购单全部到货才关闭。
def test_close_only_after_arrival():
    s, sched, env, router = runtime()  # 装配
    sched.run_day()  # 第 1 天开案
    k1 = s.events(type="case_opened")[0]["case_id"]  # 案子号
    for i, t in enumerate(router.targets(k1).values()):  # 每个目标按缺口下单
        po = env.submit_purchase_order(mpn=t["mpn"], qty=t["gap"], distributor="digikey", unit_price="1",
                                       idempotency_key=f"gap{i}")  # 下单
        s.append("po_submitted", day=1, actor=f"procurement@{k1}", case_id=k1, payload={"po_id": po["po_id"]},
                 entity_keys={"po": [po["po_id"]], "mpn": [t["mpn"]]})  # 提交事件
    for _ in range(30):  # 最多推进 30 天
        sched.run_day()  # 一天
        if s.case_state(k1)["state"] == "closed":  # 已关闭
            break  # 停
    closed = s.events(case_id=k1, type="case_closed")  # 关闭事件
    assert closed and closed[0]["payload"]["outcome"] == "resolved"  # 已解决
    resolved_days = [e["day"] for e in s.events(case_id=k1, type="target_resolved")]  # 目标消除日
    assert len(resolved_days) == 5 and max(resolved_days) < closed[0]["day"]  # 先消除、后关闭
    pos = [e["payload"]["po_id"] for e in s.events(case_id=k1, type="po_submitted")]  # 本案采购单
    arrived = [sh.arrived_day for sh in env.state.shipments.values() if sh.po_id in pos]  # 到货日
    assert len(arrived) == 5 and all(d is not None and d <= closed[0]["day"] for d in arrived)  # 到货后才关闭


# 功能：软声明（A9）——两案争用同一批供应：先到的正常，后到的收到冲突与采购员通知、草稿门控拒绝；
#       先到的案子关闭后声明释放；关掉 supply_claims 后不判冲突、门控不拦。
def test_supply_claims_conflict_and_gate():
    offer = {"distributor": "element14", "stock": 300, "unit_price_usd": 0.5, "eta_day": 4, "amount_usd": 100.0}
    content = {"feasible": True, "decisions": [{"mpn": MPN, "qty": 200, "chosen": offer}]}  # 决策：要 200 颗
    for enabled in (True, False):  # 开 / 关
        p = code_pipeline(claims=lambda s, on=enabled: ClaimBook(s, enabled=on),  # 软声明
                          gate=lambda proxy, claims: EvidenceGate(ApprovalVault(key=b"k" * 32), claims=claims))  # 门控
        p.s.open_case("K8", day=0, actor="runtime", payload={"mode": "S1"})  # 第二个案子
        for case in ("K7", "K8"):  # 先 K7 后 K8
            p.claims.register(case, content, day=0)  # 登记声明
            p.s.produce(f"Decision:{case}", "ProcurementDecision", content, day=0, actor="policy", case_id=case,
                        meta={"taint": {"*": "trusted", "decisions[].chosen": "external"}})  # 同样的决策
        k7 = p.ex.create_draft(case_id="K7", day=0, decision_ref="Decision:K7@v1", mpn=MPN)  # 先到
        k8 = p.ex.create_draft(case_id="K8", day=0, decision_ref="Decision:K8@v1", mpn=MPN)  # 后到
        assert k7["ok"], k7  # 先到先得
        if not enabled:  # 消融
            assert k8["ok"] and not p.s.events(type="claim_conflict")  # 不判冲突、不拦
            continue  # 下一轮
        assert k8["ok"] is False and "冲突" in k8["reasons"][0]  # 后到被草稿门控拒绝
        conflict = p.s.events(case_id="K8", type="claim_conflict")  # 冲突反馈
        assert conflict and "300" in conflict[0]["payload"]["message"]  # 写明可用量
        assert p.s.events(case_id="K8", type="human_request")[-1]["payload"]["role"] == "buyer"  # 通知采购员
        p.s.close_case("K7", "abandoned", day=1, actor="runtime")  # 先到的案子放弃
        p.claims.register("K8", content, day=1)  # K8 重规划后重新登记
        assert p.claims.check("K8", MPN, "element14") == []  # 声明释放后不再冲突

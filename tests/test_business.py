"""F21：确定性业务组件离线测试（固定样例报价，不联网、不调用 LLM）。"""
import pytest  # 测试框架

from business.components import code_executors  # 被测：代码执行者
from business.decide import decide, grade, load_yaml, sourcing  # 被测：比价、决策、等级
from business.impact import analyze  # 被测：影响分析
from business.procurement import ProcurementExecutor  # 被测：执行器
from infrastructure.config import load_harness_config  # 配置
from runtime.dispatch import Dispatcher  # 派发（代码执行者不需要模型）
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from simenv.api import Environment  # 环境
from simenv.bom import load_boms  # BOM
from simenv.metrics import outcome_metrics  # 结果指标
from simenv.model import state_hash  # 状态哈希
from simenv.params import load_params  # 参数
from tools.archive import MemoryArchive  # 留档
from tools.kv import MemoryKV  # KV
from tools.proxy import ToolProxy, load_tools_config  # 代理层
from tools.quota import QuotaGuard  # 配额
from tools.servers import distributor_server  # 分销商 Server

POLICY = load_yaml("policy.yaml")  # Policy 配置


def run_to_end(env):
    """推进到模拟终点。"""
    while env.state.day < env.state.horizon_days:  # 直到终点
        env.advance()  # 推进


def make_proxy(session, providers=None):
    """只带分销商 Server 的代理层；sourcing 身份可查报价。"""
    cfg = load_tools_config()  # 配置
    cfg["providers"].update(providers or {})  # 覆盖配额
    kv = MemoryKV()  # KV
    return ToolProxy([distributor_server()], session, kv=kv, quota=QuotaGuard(kv, cfg["providers"]),  # 代理层
                     archive=MemoryArchive(), config=cfg, whitelist={"sourcing": {"distributor_quotes"}})


# 功能：影响分析不改真实环境；前推的停线天数与真实不干预运行一致；按缺口在第 0 天补货后不再停线。
def test_impact_matches_engine_and_is_read_only():
    env = Environment.create(load_params(), load_boms(), 42)  # 种子 42
    before = state_hash(env.state)  # 分析前哈希
    report = analyze(env.state)  # 分析
    assert state_hash(env.state) == before  # 真实环境不变
    assert report["total_gap_parts"] > 0 and all(s["first_short_day"] >= 5 for s in report["shortages"])  # 有缺口
    idle = Environment.create(load_params(), load_boms(), 42)  # 不干预的对照
    run_to_end(idle)  # 跑完
    assert report["projected_stop_days"] == outcome_metrics(idle.state)["stop_line_days"]  # 前推 = 实际
    for s in report["shortages"]:  # 按缺口补货
        env.submit_purchase_order(mpn=s["mpn"], qty=s["gap"], distributor="digikey",  # 数量 = 缺口
                                  unit_price="1", idempotency_key=f"gap:{s['mpn']}")  # 幂等键
    run_to_end(env)  # 跑完
    assert outcome_metrics(env.state)["stop_line_days"] == 0  # 缺口数字足以消除停线
    scoped = analyze(Environment.create(load_params(), load_boms(), 42).state,  # 按料号过滤
                     {"mpns": [report["shortages"][0]["mpn"]]})  # 只看第一颗
    assert [s["mpn"] for s in scoped["shortages"]] == [report["shortages"][0]["mpn"]]  # 范围生效


# 功能：Sourcing 部分成功写明缺失；依赖带报价版本与有效期。
def test_sourcing_partial_success():
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    proxy = make_proxy(s, {"mouser": {"daily_limit": 0, "rate_per_s": None, "burst": None}})  # mouser 不可用
    env = Environment.create(load_params(), load_boms(), 42)  # 环境
    content, deps = sourcing(proxy, [{"mpn": "SKY13453-385LF", "qty": 10}], distributors=env.state.distributors,
                             day=3, case_id="K1", actor="sourcing@K1")  # 比价
    item = content["items"][0]  # 唯一需求
    assert item["missing"] == ["mouser"] and {o["distributor"] for o in item["offers"]} == {"digikey", "element14"}
    assert all(d["version"] == "day3" and d["valid_until_day"] == 4 for d in deps)  # 版本与有效期
    fill = next((o for o in item["offers"] if o["fills"]), None)  # 有货的报价
    if fill:  # 有货时到货日 = 今天 + 备货 + 运输
        logi = env.state.distributors[fill["distributor"]]  # 物流参数
        assert fill["eta_day"] == 3 + logi["ship_days"] + logi["transit_days"]  # 推算


# 功能：Policy 过滤原因逐项、打分可复核、同输入同输出、同分按分销商名。
def test_policy_filters_scores_and_is_deterministic():
    offer = lambda d, stock, price, eta: {"distributor": d, "stock": stock, "unit_price_usd": price,
                                         "lead_days": 5, "fills": isinstance(stock, int) and stock >= 100,
                                         "eta_day": eta}  # 构造报价
    quote = {"items": [{"mpn": "X", "qty": 100, "offers": [  # 一条需求
        offer("digikey", 500, 1.0, 10), offer("mouser", 50, 0.5, 8),  # mouser 现货不足
        offer("element14", 500, "unknown", 9), offer("acme", 500, 0.1, 5)], "missing": []}]}  # 单价未知、不在 AVL
    d = decide(quote, POLICY)  # 决策
    reasons = {f["distributor"]: f["reason"] for f in d["decisions"][0]["filtered"]}  # 过滤原因
    assert reasons == {"mouser": "现货不足", "element14": "单价未知", "acme": "不在 AVL"}  # 逐项原因
    assert d["decisions"][0]["chosen"]["distributor"] == "digikey" and d["feasible"]  # 唯一可行
    assert decide(quote, POLICY) == d  # 同输入同输出
    tie = {"items": [{"mpn": "X", "qty": 100, "missing": [],  # 两家完全相同
                      "offers": [offer("mouser", 500, 1.0, 10), offer("digikey", 500, 1.0, 10)]}]}  # 同分
    assert decide(tie, POLICY)["decisions"][0]["chosen"]["distributor"] == "digikey"  # 按名取舍
    big = {"items": [{"mpn": "X", "qty": 100000, "missing": [],  # 金额超限
                      "offers": [{**offer("digikey", 10**6, 1.0, 10), "fills": True}]}]}  # 10 万美元
    out = decide(big, POLICY)  # 决策
    assert out["feasible"] is False and out["decisions"][0]["filtered"][0]["reason"] == "超过单张金额上限"


# 功能：等级汇总：关键项不满足 → C；关键项证据不足 → B；重要项不满足 → B；全部满足 → A；缺结论按证据不足。
@pytest.mark.parametrize("results,expected", [
    ({"package": "not_satisfied"}, "C"),  # 关键项不满足（其余缺结论也不影响先命中 C）
    ({k: "satisfied" for k in ("package", "band", "supply", "control_logic", "insertion_loss")}, "B"),  # 隔离度缺结论
    ({k: "satisfied" for k in ("package", "band", "isolation", "supply", "control_logic")}
     | {"insertion_loss": "not_satisfied"}, "B"),  # 重要项不满足
    ({k: "satisfied" for k in ("package", "band", "isolation", "insertion_loss", "control_logic", "supply")}, "A"),
])
def test_grade_rules(results, expected):
    assert grade(load_yaml("checklists/rf_switch.yaml"), results)["grade"] == expected  # 等级


# 功能：执行器只收正确类型的有效 Artifact；同一草稿重复提交不重复下单；提交后环境里有采购单。
def test_executor_gates_and_idempotency():
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    env = Environment.create(load_params(), load_boms(), 42)  # 环境
    s.open_case("K1", day=0, actor="runtime")  # 开案
    chosen = {"distributor": "digikey", "stock": 900, "unit_price_usd": 0.5, "eta_day": 4, "amount_usd": 50.0}  # 选择
    s.produce("Decision:K1", "ProcurementDecision", {"feasible": True, "decisions": [  # 决策
        {"mpn": "SKY13453-385LF", "qty": 100, "chosen": chosen}]}, day=0, actor="policy", case_id="K1",
        deps=[{"external": "quote:digikey:SKY13453-385LF", "version": "day0", "valid_until_day": 1}])  # 依赖报价
    ex = ProcurementExecutor(s, lambda: env)  # 执行器（最小门控）
    bad = ex.create_draft(case_id="K1", day=0, decision_ref="Decision:K1@v9", mpn="SKY13453-385LF")  # 不存在
    assert bad["ok"] is False and s.events(type="gate_rejected")  # 被拒并写反馈事件
    draft = ex.create_draft(case_id="K1", day=0, decision_ref="Decision:K1@v1", mpn="SKY13453-385LF")  # 建草稿
    assert draft["ok"] and draft["content"]["distributor"] == "digikey" and draft["content"]["unit_price"] == "0.5"
    assert ex.create_draft(case_id="K1", day=0, decision_ref=draft["draft"], mpn="X")["ok"] is False  # 类型不对
    assert ex.request_approval(case_id="K1", day=0, draft_ref=draft["draft"])["ok"]  # 送审
    assert s.events(type="human_request")[0]["payload"]["node"] == "H2"  # 通知主管
    first = ex.submit(case_id="K1", day=0, draft_ref=draft["draft"])  # 提交
    again = ex.submit(case_id="K1", day=0, draft_ref=draft["draft"])  # 重复提交
    assert first["duplicate"] is False and again == {**first, "duplicate": True}  # 幂等
    assert len(env.list_purchase_orders(mpn="SKY13453-385LF", status="submitted")) == 1  # 只有一张
    s.expire(day=2)  # 报价过期 → 决策与草稿失效
    assert ex.submit(case_id="K1", day=2, draft_ref=draft["draft"])["ok"] is False  # 失效草稿不能提交


# 功能：A1 情景端到端（纯代码）：影响分析 → 比价 → 决策 → 逐料草稿 → 提交，跑完后停线天数下降为 0。
def test_a1_code_path_clears_shortages():
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    env = Environment.create(load_params(), load_boms(), 42)  # 环境
    proxy = make_proxy(s)  # 代理层
    registry = code_executors(lambda: env, proxy, run_id="r")  # 三个代码执行者
    rt = load_harness_config()["runtime"]  # 预算
    disp = Dispatcher(s, loop=None, registry=registry, budgets=rt)  # 代码执行者不需要模型循环
    s.open_case("K7", day=0, actor="runtime", payload={"goal": "消除全部缺口"})  # 开案

    def run(executor, inputs=(), params=None):  # 派一个任务并立即汇合
        disp.dispatch(case_id="K7", day=0, actor="orch", executor=executor, task=executor,  # 派发
                      inputs=list(inputs), reason="测试", params=params)  # 输入与参数
        disp.run_pending(case_id="K7", day=0)  # 运行
        ref = s.events(type="task_finished")[-1]["payload"]["artifact"]  # 刚完成任务的产出引用
        art_id, _, ver = ref.rpartition("@v")  # 拆引用
        return ref, s.artifact_view(art_id, int(ver))  # 引用与内容

    impact_ref, impact = run("impact_analysis")  # 影响分析
    quote_ref, quote = run("sourcing", [impact_ref])  # 比价（需求来自缺口）
    dec_ref, dec = run("policy", [quote_ref])  # 决策
    assert [i["mpn"] for i in quote["content"]["items"]] == [x["mpn"] for x in impact["content"]["shortages"]]
    ex = ProcurementExecutor(s, lambda: env)  # 执行器
    for d in dec["content"]["decisions"]:  # 每颗缺料
        if d["chosen"]:  # 有可行选择
            draft = ex.create_draft(case_id="K7", day=0, decision_ref=dec_ref, mpn=d["mpn"])  # 草稿
            assert draft["ok"], draft  # 草稿须通过门控
            assert ex.submit(case_id="K7", day=0, draft_ref=draft["draft"])["ok"]  # 提交
    feasible = {d["mpn"] for d in dec["content"]["decisions"] if d["chosen"]}  # 能买到的料
    run_to_end(env)  # 跑完
    left = analyze(env.state)["shortages"]  # 终点时剩余缺口
    assert not ({x["mpn"] for x in left} & feasible)  # 买到的料不再缺
    assert s.verify_chain() is None  # 哈希链完好

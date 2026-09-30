"""F23：子 Agent 与 LLM 函数测试。全部离线：合成 datasheet 样例、脚本模型（不做任何真实调用）。"""
import json  # 结构化输出

from kit import MPN, call, tok  # 共用装配
from test_orchestrator import runtime  # F22 装配（配额不限、样例 datasheet）

from business.decide import grade  # 等级汇总器
from knowledge_base.assets import checklist, seed_rows  # 比对清单与种子
from models.providers import reply  # 脚本应答

CAND = "CAND-RF-01"  # 虚构候选
CAP = "CL05C470JB5NNNC"  # 场景缺料的电容（有原理图种子）


def out(obj) -> dict:
    """子 Agent 的最终结构化输出。"""
    return reply(json.dumps(obj, ensure_ascii=False))  # JSON 正文


def msgs(t, i=0) -> tuple[str, str]:
    """第 i 次模型请求的（全部系统消息拼接, 第一条用户消息）——适配层会在最前面插一条输出 Schema 说明。"""
    m = t.requests[i]["messages"]  # 消息
    return ("\n".join(x["content"] for x in m if x["role"] == "system"),  # 系统
            next(x["content"] for x in m if x["role"] == "user"))  # 用户


def setup(script, **switches):
    """装配 + 开 S1 案子 K7 + 两份 datasheet 入库。返回 (rt, transport, 原料版本, 候选版本, 编排者令牌)。"""
    rt, t = runtime(script, **switches)  # 装配
    rt.session.open_case("K7", day=0, actor="runtime", payload={"goal": "g", "mode": "S1"})  # 案子
    v1 = rt.ingestor.ensure(MPN, day=0)["version"]  # 原料 datasheet
    vc = rt.ingestor.ensure(CAND, day=0)["version"]  # 候选 datasheet
    return rt, t, v1, vc, tok(rt.proxy, "orchestrator", case_id="K7")  # 返回


def run(rt, parent, executor, inputs=(), **params):
    """派一个任务并立即汇合，返回产出 Artifact。"""
    r = rt.dispatcher.dispatch(parent=parent, day=0, executor=executor, task=executor, inputs=list(inputs),  # 派发
                               reason="测试", params=params)  # 参数
    assert r["accepted"], r  # 接受
    rt.dispatcher.run_pending(case_id="K7", day=0)  # 运行
    ref = rt.session.events(type="task_finished")[-1]["payload"]["artifact"]  # 产出引用
    aid, _, ver = ref.rpartition("@v")  # 拆
    return rt.session.artifact_view(aid, int(ver))  # Artifact


# 功能：原料画像——读 datasheet 后给出带引用的约束；真引用的约束去污、编造的标"无法确认"；作用域 = 原料 + 板级文档；Skill 与清单必定加载。
def test_part_profile_citations():
    rt, t, v1, _, parent = setup([])  # 装配
    t.script += [reply(tool_calls=[call("datasheet_search", mpn=MPN, query="isolation")]),  # 检索
                 out({"mpn": MPN, "board": "hackrf-one", "constraints": [  # 结论
                     {"param": "isolation", "requirement": "≥ 25 dB @ 2.4 GHz", "hard": True,
                      "citation": {"mpn": MPN, "version": v1, "page": 2, "value": 25}},  # 真引用
                     {"param": "supply", "requirement": "5 V", "hard": True,
                      "citation": {"mpn": MPN, "version": v1, "page": 2, "value": 5}}]})]  # 编造的数值
    art = run(rt, parent, "part_profile", mpn=MPN, board="hackrf-one")  # 派发
    c = art["content"]["constraints"]  # 约束
    assert c[0]["confirmed"] and not c[1]["confirmed"] and "不在" in c[1]["unconfirmed_reason"]  # 校验结果
    assert art["meta"]["taint"]["constraints[0]"] == "verified" and "constraints[1]" not in art["meta"]["taint"]  # 去污
    dispatched = rt.session.events(type="task_dispatched")[-1]["payload"]  # 派发记录
    assert dispatched["scope"] == sorted([MPN, "board:hackrf-one"])  # 作用域由代码给
    system, user = msgs(t)  # 上下文
    assert "PartAnalyst" in system and "品类比对清单" in user and "术语：" in user  # Skill、清单、术语


# 功能：替代检索——只列候选；输出里多写等级字段校验不过、修复后重写；候选来自知识库 B 种子。
def test_substitute_search_no_grades():
    rt, t, _, _, parent = setup([])  # 装配
    rt.knowledge.import_seeds(seed_rows())  # 原理图种子
    t.script += [reply(tool_calls=[call("kb_substitutes", mpn=CAP)]),  # 查知识库 B
                 out({"original": CAP, "source": "kb", "candidates": [{"mpn": "GRM1555C1H470JA01D"}],
                      "grade": "A"}),  # 多写等级
                 out({"original": CAP, "source": "kb", "candidates": [{"mpn": "GRM1555C1H470JA01D", "note": "种子"}]})]
    art = run(rt, parent, "substitute_search", original=CAP)  # 派发
    assert art["kind"] == "CandidateSet" and "grade" not in art["content"]  # 不评等级
    errors = [e["payload"]["errors"] for e in rt.session.events(type="llm_validation")]  # 校验记录
    assert any("grade" in x for es in errors for x in es)  # 等级字段被拒


def profile(rt):
    """手工产出一份 PartProfile 与一份带价格的 SourcingQuote（测 Verifier 输入隔离）。"""
    p = rt.session.produce("PartProfile:X", "PartProfile", {"mpn": MPN, "constraints": [
        {"param": "isolation", "requirement": "≥ 25 dB", "hard": True}]}, day=0, actor="t", case_id="K7")
    q = rt.session.produce("SourcingQuote:X", "SourcingQuote", {"items": [{"mpn": CAND, "offers": [
        {"distributor": "digikey", "unit_price_usd": 0.42, "stock": 900}]}]}, day=0, actor="t", case_id="K7")
    return [f"{p['artifact_id']}@v1", f"{q['artifact_id']}@v1"]  # 两个引用


def verifier_items(v1, vc):
    """Verifier 的逐项结论：一项真引用、一项编造、一项清单外。"""
    return {"candidate": CAND, "original": MPN, "items": [
        {"id": "package", "result": "satisfied", "citations": [
            {"mpn": MPN, "version": v1, "page": 3, "quote": "QFN-12"}, {"mpn": CAND, "version": vc, "page": 2,
                                                                        "quote": "QFN-12"}]},
        {"id": "isolation", "result": "satisfied", "citations": [
            {"mpn": MPN, "version": v1, "page": 2, "value": 25}, {"mpn": CAND, "version": vc, "page": 2, "value": 27}]},
        {"id": "band", "result": "satisfied", "citations": [{"mpn": CAND, "version": vc, "page": 1, "value": 9.9}]},
        {"id": "price_ok", "result": "satisfied", "citations": []}]}  # 清单外的项


# 功能：Verifier——上下文里没有价格与检索过程；读第三颗料被拒；编造引用的项降为证据不足；清单外的项忽略；等级由代码按清单汇总。
def test_verifier_isolation_scope_downgrade_and_grade():
    rt, t, v1, vc, parent = setup([])  # 装配
    t.script += [reply(tool_calls=[call("datasheet_read_page", mpn=CAP, page=1)]),  # 读第三颗料
                 out(verifier_items(v1, vc))]  # 结论
    art = run(rt, parent, "verifier", profile(rt), candidate=CAND)  # 派发（输入含报价）
    _, user = msgs(t)  # Verifier 收到的输入
    assert "unit_price" not in user and "0.42" not in user and "SourcingQuote" not in user  # 隔离
    assert "≥ 25 dB" in user and CAND in user and "比对清单" in user  # 只有约束、候选、清单
    denied = [e for e in rt.session.events(type="tool_call") if e["payload"].get("denied") == "scope"]  # 作用域拒绝
    assert denied and denied[0]["payload"]["args"]["mpn"] == CAP  # 第三颗料被拒
    items = {i["id"]: i for i in art["content"]["items"]}  # 逐项
    assert items["package"]["result"] == "satisfied" and items["isolation"]["result"] == "satisfied"  # 真引用
    assert items["band"]["result"] == "insufficient" and "不在" in items["band"]["downgraded"]  # 编造降级
    assert items["control_logic"]["result"] == "insufficient"  # 缺项按证据不足
    assert art["content"]["ignored_items"] == ["price_ok"]  # 清单外忽略
    cl = checklist("rf_switch")  # 清单
    assert art["content"]["grade"] == grade(cl, {k: v["result"] for k, v in items.items()})  # 代码汇总
    assert art["content"]["grade"]["grade"] == "B" and art["meta"]["taint"]["grade"] == "trusted"  # 等级可信
    deps = {(d["target"], d["target_version"]) for d in rt.session.store.deps(rt.session.run_id)
            if d["artifact_id"] == art["artifact_id"]}  # 依赖
    assert (f"datasheet:{MPN}", v1) in deps and (f"datasheet:{CAND}", vc) in deps  # 两颗料的 datasheet 版本


# 功能：消融——关 verifier_isolation 时 Verifier 看得到价格；关 citation_check 时编造引用不降级。
def test_verifier_ablations():
    rt, t, v1, vc, parent = setup([], verifier_isolation=False, citation_check=False)  # 两个开关都关
    t.script += [out(verifier_items(v1, vc))]  # 结论
    art = run(rt, parent, "verifier", profile(rt), candidate=CAND)  # 派发
    assert "0.42" in msgs(t)[1]  # 看得到价格
    band = next(i for i in art["content"]["items"] if i["id"] == "band")  # 编造的那项
    assert band["result"] == "satisfied" and "downgraded" not in band  # 不降级


# 功能：Resolver 三层——规则层（去卷带后缀）直接命中不调 LLM；已确认别名直接命中；描述性说法由 LLM 从候选里选；
#       低置信度或列表外交采购员确认（H3）。
def test_resolver_layers():
    choose = [out({"mpn": MPN, "confidence": 0.9, "reason": "隔离度匹配"}),  # 可采信
              out({"mpn": MPN, "confidence": 0.4, "reason": "拿不准"}),  # 低置信度
              out({"mpn": "SKY99999", "confidence": 0.95, "reason": "编的"})]  # 列表外
    rt, t, _, _, parent = setup(choose)  # 装配
    r = run(rt, parent, "resolver", query="sky13453-385lf-tr")["content"]  # 规则层
    assert r["mpn"] == MPN and r["layer"] == "rules" and not t.requests  # 不调 LLM
    p = rt.knowledge.propose(rt.session, case_id="K7", day=0, actor="o", kind="alias", alias="那颗老开关", mpn=MPN)
    rt.knowledge.confirm(rt.session, p["proposal_seq"], by="buyer", node="H3", day=0)  # 已确认别名
    assert run(rt, parent, "resolver", query="那颗老开关")["content"]["layer"] == "alias"  # 别名命中
    good = run(rt, parent, "resolver", query="板上那颗射频开关")["content"]  # 描述性说法
    assert good["mpn"] == MPN and good["layer"] == "llm" and MPN in good["candidates"]  # LLM 从候选里选
    assert "SKY13317-373LF" in good["candidates"]  # 同品类的另一颗也在候选里
    low = run(rt, parent, "resolver", query="板上那颗射频开关")["content"]  # 低置信度
    bad = run(rt, parent, "resolver", query="板上那颗射频开关")["content"]  # 列表外
    assert low["needs_confirmation"] and low["mpn"] is None  # 交人
    assert bad["needs_confirmation"] and bad["rejected_choice"] == "SKY99999"  # 列表外不采信
    h3 = [e for e in rt.session.events(type="human_request") if e["payload"].get("node") == "H3"]  # 通知采购员
    assert len(h3) >= 2  # 两次交人（外加别名提议那次）


# 功能：Explainer——必定加载 Skill；数字有出处时标已校验，编造数字被列出、整段不可信。
def test_explainer_numbers_must_come_from_inputs():
    rt, t, _, _, parent = setup([out({"text": "SKY13453 缺 510 颗，第 37 天起停线。"}),  # 数字有出处
                                 out({"text": "SKY13453 缺 999 颗。"})])  # 编造
    imp = rt.session.produce("ImpactReport:X", "ImpactReport", {"shortages": [{"mpn": MPN, "gap": 510,
                                                                             "first_short_day": 37}]},
                             day=0, actor="t", case_id="K7")  # 事实
    ref = f"{imp['artifact_id']}@v1"  # 引用
    ok = run(rt, parent, "explainer", [ref], audience="buyer")  # 采购员
    assert ok["content"]["unconfirmed"] == [] and ok["meta"]["taint"]["text"] == "verified"  # 都有出处
    assert "Explainer" in msgs(t)[0]  # Skill 必定加载
    bad = run(rt, parent, "explainer", [ref], audience="buyer")  # 第二次
    assert bad["content"]["unconfirmed"] == ["999"] and bad["meta"]["taint"]["text"] == "untrusted"  # 编造被标出


# 功能：装配——Runtime 注册了五个节点；S3 模式可派 Verifier、不能派比价。
def test_runtime_registers_nodes():
    rt, _ = runtime()  # 装配
    assert {"part_profile", "substitute_search", "verifier", "resolver", "explainer"} <= set(rt.dispatcher.registry)
    rt.session.open_case("Q1", day=0, actor="router", payload={"goal": "g", "mode": "S3"})  # S3 案子
    s3 = tok(rt.proxy, "orchestrator", case_id="Q1", mode="S3")  # S3 身份
    ok = rt.dispatcher.dispatch(parent=s3, day=0, executor="verifier", task="v", inputs=[], reason="r",
                                params={"original": MPN, "candidate": CAND})  # Verifier
    no = rt.dispatcher.dispatch(parent=s3, day=0, executor="sourcing", task="s", inputs=[], reason="r")  # 比价
    assert ok["accepted"] and not no["accepted"]  # 模式节点集合

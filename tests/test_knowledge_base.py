"""F20：知识资产与知识库 B 测试。全部离线（原始层原理图与 README 已在仓库、合成 datasheet 样例、内存存储）；集成部分用 Postgres。"""
from pathlib import Path  # 路径

import pytest  # 测试框架
import yaml  # 读术语表
from kit import MPN, ORCH_TOOLS, build, call, end, guard, tok  # 共用装配

from context.assemble import OrchestratorContext  # F16 上下文
from datasheets.fetch import FixtureSource, Ingestor  # 获取与入库
from datasheets.parse import FixtureParser  # 样例解析
from datasheets.server import datasheet_server  # 文档工具
from datasheets.store import MemoryDatasheetStore, search  # 存储与检索
from infrastructure.config import load_harness_config  # 配置
from knowledge_base.assets import (  # 知识资产
    KNOWLEDGE,  # 知识配置目录
    CompositeParser,  # 按类型选解析器
    CompositeSource,  # 按前缀选来源
    Glossary,  # 术语表
    category_of,  # 品类
    checklist,  # 比对清单
    list_skills,  # Skill 列表
    load_skill,  # 读 Skill
    seed_rows,  # 种子行
)
from knowledge_base.kb import (  # 知识库 B
    KnowledgeBase,
    MemoryKBStore,
    PgKBStore,
    case_memory,
    kb_server,
)
from models.providers import reply  # 脚本应答
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from tools.archive import MemoryArchive  # 留档
from tools.kv import MemoryKV  # KV
from tools.proxy import ToolProxy, load_tools_config  # 代理层
from tools.quota import QuotaGuard  # 配额

FIX = Path(__file__).parent / "fixtures" / "datasheets"  # datasheet 样例
CAND = "CAND-RF-01"  # 虚构候选
DOCS = ["find_datasheet", "datasheet_toc", "datasheet_search", "datasheet_read_page", "kb_substitutes"]  # 工具


# 功能：术语表——5 个品类各约 20 条、品类内无重复；按关键词注入；同义词接入 F19 检索；比对清单与品类映射齐全。
def test_glossary_categories_and_checklists():
    g = Glossary()  # 术语表
    assert g.categories() == ["capacitor", "led", "resistor", "rf_switch", "rf_synthesizer"]  # 5 个品类
    for path in sorted((KNOWLEDGE / "glossary").glob("*.yaml")):  # 每个品类
        assert 18 <= len(yaml.safe_load(path.read_text(encoding="utf-8"))["terms"]) <= 22, path.name  # 约 20 条
    lines = g.inject("候选 X 的隔离度和 insertion loss 都要比", categories=["rf_switch"])  # 注入
    assert lines[0].startswith("术语：isolation") and lines[1].startswith("术语：insertion loss")  # 按出现先后
    assert Glossary(enabled=False).inject("隔离度") == []  # 关掉不注入
    store = MemoryDatasheetStore()  # datasheet 存储
    Ingestor(FixtureSource(FIX), FixtureParser(), store, Session(MemorySessionStore(), "r")).ensure(MPN, day=0)
    v = store.versions(MPN)[0]["version"]  # 版本
    assert search(store, MPN, v, "端口隔离", synonyms=g.synonyms()) == []  # 术语表里没有"端口隔离"这种写法
    assert search(store, MPN, v, "隔离度", synonyms=g.synonyms())[0]["page"] in (1, 2)  # 术语表同义词命中
    assert {category_of(m) for m in (MPN, "RFFC5072TR7", "CL05C470JB5NNNC", "RC0402JR-0718RL", "WSLP5931L3000FEA",
                                     "LTST-S220KGKT")} == {"rf_switch", "rf_synthesizer", "capacitor", "resistor", "led"}
    assert category_of("BQ76952PFBR") is None  # 未分类
    assert all(checklist(c) is not None for c in g.categories())  # 每个品类都有比对清单


# 功能：Skills——四个都能加载、带描述、哈希稳定且互不相同。
def test_skills_load_with_hash():
    assert list_skills() == ["explainer", "goal_revision", "part_analyst", "substitute_evaluation"]  # 四个
    a, b = load_skill("substitute_evaluation"), load_skill("substitute_evaluation")  # 读两次
    assert a["sha256"] == b["sha256"] and a["description"] and "证据不足" in a["text"]  # 稳定、有描述
    assert len({load_skill(n)["sha256"] for n in list_skills()}) == 4  # 互不相同
    with pytest.raises(FileNotFoundError):  # 不存在
        load_skill("nope")  # 报错


# 功能：板级文档——README 原文按标题切页，以 board:<板> 为键进页表，文档工具可检索与读页。
def test_board_docs_share_document_tools():
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    store = MemoryDatasheetStore()  # 页表
    ing = Ingestor(CompositeSource(FixtureSource(FIX)), CompositeParser(FixtureParser()), store, s)  # 组合来源
    cfg, kv = load_tools_config(), MemoryKV()  # 配置
    proxy = ToolProxy([datasheet_server(store, ing)], s, kv=kv, quota=QuotaGuard(kv, cfg["providers"]),
                      archive=MemoryArchive(), config=cfg, guard=guard({"verifier": DOCS}))  # 代理层
    t = tok(proxy, "verifier")  # 身份

    def use(name, **a):  # 调工具
        return proxy.call(name, a, token=t, day=0)  # 调用

    found = use("find_datasheet", mpn="board:hackrf-one")["data"]  # 板级文档
    assert found["pages"] >= 2 and found["toc_entries"] >= 1  # 按标题切页
    hits = use("datasheet_search", mpn="board:hackrf-one", query="HackRF")["data"]  # 检索
    assert hits and "hackrf" in use("datasheet_read_page", mpn="board:hackrf-one", page=hits[0]["page"])["data"]["text"].lower()
    assert use("find_datasheet", mpn=MPN)["ok"]  # datasheet 照常


# 功能：种子导入——"any equivalent" 丢弃，其余为未验证种子并带板与位号；重复导入不重复写；门控不认。
def test_seed_import():
    kb = KnowledgeBase(MemoryKBStore())  # 知识库
    rows = seed_rows()  # 原理图 Substitution 字段
    assert any((r["alternates"] or "").lower() == "any equivalent" for r in rows)  # 原始数据里有 any equivalent
    n = kb.import_seeds(rows)  # 导入
    entries = kb.store.entries()  # 条目
    assert n == len(entries) > 0 and all(e["status"] == "unverified_seed" for e in entries)  # 全是未验证种子
    assert not any("equivalent" in e["body"]["substitute"].lower() for e in entries)  # 丢弃了
    cap = kb.substitutes("CL05C470JB5NNNC")[0]  # 场景缺料之一
    assert cap["substitute"] == "GRM1555C1H470JA01D" and cap["gate_accepts"] is False  # 门控不认
    assert cap["scope"]["board"] == "hackrf-one" and "C26" in cap["scope"]["designators"]  # 适用范围
    assert kb.import_seeds(rows) == 0  # 重复导入不重复写


# 功能：装配一个有 VerificationReport 的案子（报告依赖候选 datasheet v1）。完成：返回 (session, kb, 报告引用, 入库器, 来源)。
def with_report(tmp_path):
    import shutil  # 复制样例

    fixtures = tmp_path / "ds"  # 可改的样例目录
    shutil.copytree(FIX, fixtures)  # 复制
    (fixtures / f"{CAND}.v2.json").write_text(  # 候选改版
        (FIX / f"{CAND}.v1.json").read_text(encoding="utf-8").replace("| 27 |", "| 20 |"), encoding="utf-8")
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    source = FixtureSource(fixtures)  # 来源
    source.use(CAND, "v1")  # 当前 v1
    ing = Ingestor(source, FixtureParser(), MemoryDatasheetStore(), s)  # 入库器
    v1 = ing.ensure(CAND, day=0)["version"]  # 入库
    s.open_case("K7", day=0, actor="runtime", payload={"mode": "S1"}, entity_keys={"mpn": [MPN]})  # 案子
    rep = s.produce(f"VerificationReport:{CAND}", "VerificationReport",  # 报告
                    {"candidate": CAND, "original": MPN, "grade": {"grade": "B"}}, day=0, actor="verifier@K7",
                    case_id="K7", deps=[{"external": f"datasheet:{CAND}", "version": v1}])  # 依赖 v1
    return s, KnowledgeBase(MemoryKBStore()), f"{rep['artifact_id']}@v1", ing, source  # 返回


# 功能：提议与确认——替代关系的内容与依赖由代码从报告取；错误节点确认被拒；H1 确认落库并能注入；别名说法以引用块存放；
#       datasheet 改版使条目待重验；按案子一键撤销后不再注入；注入最多 5 条、关掉不注入。
def test_propose_confirm_revalidate_revoke_inject(tmp_path):
    s, kb, rep, ing, source = with_report(tmp_path)  # 装配
    bad = kb.propose(s, case_id="K7", day=1, actor="orchestrator@K7", kind="substitution", report="Nope@v1")
    assert bad["accepted"] is False  # 没有报告不收
    p = kb.propose(s, case_id="K7", day=1, actor="orchestrator@K7", kind="substitution", report=rep,
                   board="hackrf-one", designators=["U7"])  # 提议替代
    body = s.store.event(s.run_id, p["proposal_seq"])["payload"]["body"]  # 提议内容
    assert body["grade"] == "B" and body["substitute"] == CAND  # 等级来自报告
    assert s.events(type="human_request")[-1]["payload"]["node"] == "H1"  # 通知工程师
    with pytest.raises(ValueError, match="H1"):  # 采购员不能确认替代关系
        kb.confirm(s, p["proposal_seq"], by="buyer", node="H3", day=2)  # 拒绝
    eid = kb.confirm(s, p["proposal_seq"], by="engineer", node="H1", day=2)  # 工程师确认
    line = kb.inject({"mpn": [MPN]})[0]  # 注入
    assert f"{MPN} → {CAND}" in line and "来源 K7" in line and "H1 engineer 确认" in line and "已确认" in line
    a = kb.propose(s, case_id="K7", day=2, actor="orchestrator@K7", kind="alias", alias="那颗射频开关", mpn=MPN)
    aid = kb.confirm(s, a["proposal_seq"], by="buyer", node="H3", day=2)  # 采购员确认别名
    alias = next(e for e in kb.store.entries() if e["entry_id"] == aid)  # 条目
    assert alias["body"]["alias"] == {"quote": "那颗射频开关", "source": "case:K7"}  # 不可信文本以引用块存放
    source.use(CAND, "v2")  # 候选 datasheet 改版
    ing.refresh(CAND, day=3)  # 刷新
    assert kb.sync(s.events(type="datasheet_ingested")) == [eid]  # 依赖旧版的条目待重验
    assert "待重验" in kb.inject({"mpn": [CAND]})[0]  # 注入时标明
    assert sorted(kb.revoke_case("K7", reason="判定被投毒", actor="admin", day=4)) == sorted([eid, aid])  # 一键撤销
    assert kb.inject({"mpn": [MPN, CAND]}) == []  # 撤销的不注入
    many = KnowledgeBase(MemoryKBStore())  # 另一个知识库
    many.import_seeds(seed_rows())  # 种子
    assert len(many.inject({"board": ["hackrf-one"]})) == 5  # 每次最多 5 条
    assert KnowledgeBase(many.store, inject_enabled=False).inject({"board": ["hackrf-one"]}) == []  # 关掉不注入


# 功能：F13 汇点——绕过 confirm 直接给裸露不可信文本的条目，长期记忆汇点拒绝。
def test_memory_sink_rejects_bare_untrusted_text():
    kb = KnowledgeBase(MemoryKBStore())  # 知识库
    entry = {"alias": "那颗射频开关", "mpn": MPN, "confirmation": {"node": "H3", "by": "buyer", "event_seq": 1}}
    assert kb.sinks.memory_entry(entry, {"*": "trusted", "alias": "untrusted"})  # 拒绝裸文本
    assert kb.sinks.memory_entry({"alias": "x", "mpn": MPN}, {"*": "trusted"})  # 没有确认记录也拒绝


# 功能：编排者经控制工具 propose_memory 提议（R1，S1 下可见）；编排者上下文出现 [记忆] 分区；kb_substitutes 受作用域约束。
def test_propose_tool_context_partition_and_scope(tmp_path):
    s0, kb, _, _, _ = with_report(tmp_path)  # 有报告的局面（另一个 Session）
    kb.import_seeds(seed_rows())  # 种子
    prop = reply(tool_calls=[call("propose_memory", kind="alias", alias="射频开关", mpn=MPN)])  # 编排者提议
    s, seg, _, _, _ = build([prop, end()],  # 装配
                            context=OrchestratorContext(load_harness_config()["context"], memory=case_memory(kb)))
    seg.knowledge = kb  # 接入知识库
    seg.tools = [*ORCH_TOOLS, "propose_memory"]  # 编排者工具（端点上限 8 个）
    s.open_case("K7", day=0, actor="runtime", payload={"goal": "处置缺口", "mode": "S1"},  # S1 案子
                entity_keys={"mpn": ["CL05C470JB5NNNC"]})  # 目标料号
    seg.run("K7", reason="开案", day=0)  # 跑一段
    assert s.events(type="memory_proposed")[0]["payload"]["body"] == {"alias": "射频开关", "mpn": MPN}  # 提议
    assert s.events(type="human_request")[-1]["payload"]["node"] == "H3"  # 通知采购员
    rec = s.events(type="context_assembled")[0]["payload"]  # 上下文记录
    assert "memory" in rec["partitions"]  # 有记忆分区
    user = seg.loop.adapter.transports["glm-flash"].requests[0]["messages"][1]["content"]  # 编排者收到的
    assert "[记忆]" in user and "GRM1555C1H470JA01D" in user and "未验证种子" in user  # 按目标料号注入种子
    cfg, kv = load_tools_config(), MemoryKV()  # 配置
    proxy = ToolProxy([kb_server(kb)], s0, kv=kv, quota=QuotaGuard(kv, cfg["providers"]), archive=MemoryArchive(),
                      config=cfg, guard=guard({"retriever": ["kb_substitutes"]}))  # 替代检索的工具
    child = proxy.guard.authority.attenuate(tok(proxy, "orchestrator"), executor="retriever", scope=[MPN])  # 作用域
    assert proxy.call("kb_substitutes", {"mpn": MPN}, token=child, day=0)["ok"]  # 作用域内
    assert proxy.call("kb_substitutes", {"mpn": "CL05C470JB5NNNC"}, token=child, day=0)["error"]["type"] == "forbidden"


# 功能：集成——Postgres 知识库往返：种子、确认、状态变更、撤销。
@pytest.mark.integration
def test_pg_kb_roundtrip(migrated, tmp_path):
    conn, _, _ = migrated  # 迁移好的连接（测试后回滚）
    s, _, rep, _, _ = with_report(tmp_path)  # 局面
    kb = KnowledgeBase(PgKBStore(conn))  # Postgres 知识库
    n = kb.import_seeds(seed_rows())  # 种子
    assert n > 0 and kb.import_seeds(seed_rows()) == 0  # 幂等
    p = kb.propose(s, case_id="K7", day=1, actor="orchestrator@K7", kind="substitution", report=rep,
                   board="hackrf-one", designators=["U7"])  # 提议
    eid = kb.confirm(s, p["proposal_seq"], by="engineer", node="H1", day=2)  # 确认
    e = next(x for x in kb.store.entries() if x["entry_id"] == eid)  # 读回
    assert e["status"] == "confirmed" and e["confirmation"]["node"] == "H1" and e["deps"][0]["ref"] == f"datasheet:{CAND}"
    assert kb.revoke_case("K7", reason="投毒", actor="admin", day=3) == [eid]  # 撤销
    assert next(x for x in kb.store.entries() if x["entry_id"] == eid)["status"] == "revoked"  # 状态
    assert all(x["confirmation"] is None for x in kb.store.entries() if x["status"] == "unverified_seed")  # 种子无确认

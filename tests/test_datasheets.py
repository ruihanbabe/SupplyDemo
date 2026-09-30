"""F19：datasheet 知识库 A 测试。只用合成样例（tests/fixtures/datasheets），HTTP 来源用 httpx 模拟传输，不发任何真实请求。"""
import shutil  # 复制样例目录
from pathlib import Path  # 路径

import httpx  # 模拟传输
import pytest  # 测试框架
from kit import MPN, code_pipeline, guard, tok  # 共用装配

from datasheets.cite import check_citation  # 引用校验
from datasheets.fetch import FetchRejected, FixtureSource, HttpSource, Ingestor  # 获取与入库
from datasheets.parse import DoclingParser, FixtureParser, ParserUnavailable  # 解析
from datasheets.server import datasheet_server, load_datasheets_config  # Server
from datasheets.store import MemoryDatasheetStore, PgDatasheetStore, latest, search  # 存储与检索
from gate.approval import ApprovalVault, HumanDesk  # 审批
from gate.evidence import EvidenceGate  # 证据门控
from permissions.egress import EgressPolicy  # 出站策略
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from tools.archive import MemoryArchive  # 留档
from tools.kv import MemoryKV  # KV
from tools.proxy import ToolProxy, load_tools_config  # 代理层
from tools.quota import QuotaGuard  # 配额

FIX = Path(__file__).parent / "fixtures" / "datasheets"  # 样例目录
CAND = "CAND-RF-01"  # 虚构的替代候选
TOOLS = ["find_datasheet", "datasheet_toc", "datasheet_search", "datasheet_read_page"]  # 四个工具
CFG = load_datasheets_config()  # 配置


# 功能：装配只带 datasheet Server 的代理层。依赖：样例来源、内存存储。完成：返回 (proxy, session, store, source)。
def make(fixtures=FIX, use_synonyms=True):
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    store, source = MemoryDatasheetStore(), FixtureSource(fixtures)  # 存储与样例来源
    ing = Ingestor(source, FixtureParser(), store, s)  # 入库器
    cfg, kv = load_tools_config(), MemoryKV()  # 配置
    proxy = ToolProxy([datasheet_server(store, ing, use_synonyms=use_synonyms)], s, kv=kv,  # 代理层
                      quota=QuotaGuard(kv, cfg["providers"]), archive=MemoryArchive(), config=cfg,
                      guard=guard({"verifier": TOOLS}))  # 测试执行者
    return proxy, s, store, source  # 返回


def use(proxy, name, **args):
    """以 verifier 身份调工具。"""
    return proxy.call(name, args, token=tok(proxy, "verifier"), day=3)  # 调用


# 功能：按需入库——没取得前读目录 / 检索提示先 find；find 首次入库并记事件，再 find 不重复入库；目录与读页。
def test_ingest_on_demand_toc_and_read():
    proxy, s, _, _ = make()  # 装配
    r = use(proxy, "datasheet_search", mpn=MPN, query="isolation")  # 还没取得
    assert r["error"]["type"] == "not_found" and "find_datasheet" in r["error"]["message"]  # 引导
    found = use(proxy, "find_datasheet", mpn=MPN)["data"]  # 首次：入库
    assert found["pages"] == 3 and found["toc_entries"] == 3 and len(found["version"]) == 64  # 版本 = 内容哈希
    use(proxy, "find_datasheet", mpn=MPN)  # 再找一次
    assert len(s.events(type="datasheet_ingested")) == 1  # 不重复入库
    toc = use(proxy, "datasheet_toc", mpn=MPN)["data"]  # 目录
    assert {"title": "Electrical Specifications", "page": 2} in toc  # 章节 → 页码
    page = use(proxy, "datasheet_read_page", mpn=MPN, page=2)["data"]  # 读页
    assert "| Isolation | 2.4 GHz | 25 |" in page["text"] and page["version"] == found["version"]  # 原文与版本
    assert use(proxy, "datasheet_read_page", mpn=MPN, page=9)["error"]["type"] == "not_found"  # 没有这页


# 功能：检索——中文同义词"隔离度"找到第 2 页并给片段；多词短语"insertion loss"；关掉同义词后中文查不到。
def test_search_with_synonyms():
    proxy, _, _, _ = make()  # 装配
    use(proxy, "find_datasheet", mpn=MPN)  # 入库
    hits = use(proxy, "datasheet_search", mpn=MPN, query="隔离度")["data"]  # 中文：第 1、2 页各出现一次
    assert [h["page"] for h in hits] == [1, 2] and "isolation" in hits[0]["snippet"].lower()  # 同分按页码
    hits = use(proxy, "datasheet_search", mpn=MPN, query="隔离度 dB")["data"]  # 加单位
    assert hits[0]["page"] == 2 and "Isolation" in hits[0]["snippet"]  # 参数表页排前
    both = use(proxy, "datasheet_search", mpn=MPN, query="插入损耗 dB")["data"]  # 短语同义词 + 单位
    assert both[0]["page"] == 2 and [h["page"] for h in both] == [2, 1]  # 两组都命中的第 2 页排前，第 1 页只命中一组
    assert use(proxy, "datasheet_search", mpn=MPN, query="封装")["data"][0]["page"] == 3  # 封装在第 3 页
    off, _, _, _ = make(use_synonyms=False)  # 关同义词
    use(off, "find_datasheet", mpn=MPN)  # 入库
    assert use(off, "datasheet_search", mpn=MPN, query="隔离度")["data"] == []  # 查不到
    assert [h["page"] for h in use(off, "datasheet_search", mpn=MPN, query="isolation")["data"]] == [1, 2]  # 原词照常


# 功能：改版——样例换成 v2：新版本入库、旧版保留可按版本读，依赖旧版的验证报告失效（F06 传播）。
def test_new_version_keeps_old_and_invalidates():
    proxy, s, store, source = make()  # 装配
    v1 = use(proxy, "find_datasheet", mpn=MPN)["data"]["version"]  # 第一版
    s.open_case("K7", day=0, actor="runtime")  # 案子
    rep = s.produce("VerificationReport:X", "VerificationReport", {"candidate": "X", "original": MPN},  # 依赖 v1 的报告
                    day=3, actor="verifier", case_id="K7",
                    deps=[{"external": f"datasheet:{MPN}", "version": v1}])  # 外部依赖
    source.use(MPN, "v2")  # 厂商改版
    ing = Ingestor(source, FixtureParser(), store, s)  # 入库器
    v2 = ing.refresh(MPN, day=5)["version"]  # 刷新
    assert v2 != v1 and [v["version"] for v in store.versions(MPN)] == [v1, v2]  # 旧版保留
    assert s.artifact_view(rep["artifact_id"], 1)["status"] == "invalid"  # 报告失效
    assert s.events(type="datasheet_ingested")[-1]["payload"]["previous"] == v1  # 记录旧版本
    old = use(proxy, "datasheet_read_page", mpn=MPN, page=2, version=v1)["data"]["text"]  # 旧版原文
    assert "| 25 |" in old and "| 23 |" in store.page(MPN, v2, 2)  # 两版内容不同
    assert ing.refresh(MPN, day=6)["version"] == v2 and len(store.versions(MPN)) == 2  # 同内容不重复入库


# 功能：作用域——Verifier(原料) 的子令牌只允许原料，读候选的 datasheet 被 F11 拒绝。
def test_scope_limits_documents():
    proxy, _, _, _ = make()  # 装配
    child = proxy.guard.authority.attenuate(tok(proxy, "orchestrator"), executor="verifier", scope=[MPN])  # 作用域
    assert proxy.call("find_datasheet", {"mpn": MPN}, token=child, day=0)["ok"]  # 原料可以
    r = proxy.call("find_datasheet", {"mpn": CAND}, token=child, day=0)  # 候选不行
    assert r["error"]["type"] == "forbidden" and CAND in r["error"]["message"]  # 拒绝


# 功能：引用校验——版本与页存在、数值出现在该页（25 与 25.0 同）、片段逐字出现；各类不通过写明原因。
def test_citation_checker():
    _, s, store, source = make()  # 装配
    v = Ingestor(source, FixtureParser(), store, s).ensure(MPN, day=0)["version"]  # 入库
    ok = {"mpn": MPN, "version": v, "page": 2}  # 引用位置
    assert check_citation(store, {**ok, "value": 25}) == []  # 数值在该页
    assert check_citation(store, {**ok, "value": "25.0", "quote": "switching   TIME"}) == []  # 规范化后相同
    assert "不在" in check_citation(store, {**ok, "value": 99})[0]  # 编造数值
    assert "不在" in check_citation(store, {**ok, "quote": "isolation 40 dB"})[0]  # 编造片段
    assert "没有第 9 页" in check_citation(store, {**ok, "page": 9})[0]  # 页不存在
    assert "没有版本" in check_citation(store, {**ok, "version": "deadbeef"})[0]  # 版本不存在
    assert check_citation(store, {**ok, "page": 3, "value": 25})  # 数值在别的页不算


# 功能：HTTP 来源（模拟传输，不联网）——拒 HTTP、白名单外、解析到内网、跳出白名单的重定向（不请求目标）、非 PDF、超大；
#       合法路径返回内容；Docling 未接入时明确报"尚未具备"。
def test_http_source_egress_and_docling():
    requested = []  # 实际发出的请求

    def handler(req: httpx.Request):  # 模拟服务端
        requested.append(str(req.url))  # 记下
        routes = {"https://www.ti.com/a.pdf": httpx.Response(200, headers={"content-type": "application/pdf"},
                                                              content=b"%PDF-1.7 ok"),  # 正常
                  "https://www.ti.com/redir.pdf": httpx.Response(302, headers={"location": "https://attacker.io/x.pdf"}),
                  "https://www.ti.com/page.html": httpx.Response(200, headers={"content-type": "text/html"}, content=b"x"),
                  "https://www.ti.com/big.pdf": httpx.Response(200, headers={"content-type": "application/pdf"},
                                                                content=b"%PDF" + b"0" * 64)}  # 超大
        return routes[str(req.url)]  # 响应

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)  # 模拟客户端
    dns = {"evil.ti.com": ["10.0.0.9"]}  # 假 DNS
    policy = EgressPolicy(["ti.com"], max_bytes=32, content_types=["application/pdf"],  # 策略
                          resolver=lambda h: dns.get(h, ["93.184.216.34"]))  # 解析

    def get(url):  # 用一个 URL 取
        return HttpSource({"X": url}, policy, client).get("X")  # 获取

    assert get("https://www.ti.com/a.pdf")[1] == b"%PDF-1.7 ok"  # 合法
    for url, why in (("http://www.ti.com/a.pdf", "HTTPS"), ("https://example.com/a.pdf", "白名单"),
                     ("https://evil.ti.com/a.pdf", "不允许的地址"), ("https://www.ti.com/redir.pdf", "重定向"),
                     ("https://www.ti.com/page.html", "内容类型"), ("https://www.ti.com/big.pdf", "上限")):  # 各规则
        with pytest.raises(FetchRejected, match=why):  # 拒绝
            get(url)  # 获取
    assert "https://attacker.io/x.pdf" not in requested and "http://www.ti.com/a.pdf" not in requested  # 被拒的不请求
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    ing = Ingestor(HttpSource({"X": "https://www.ti.com/a.pdf"}, policy, client), DoclingParser(),
                   MemoryDatasheetStore(), s)  # 真实路径
    with pytest.raises(ParserUnavailable, match="尚未具备"):  # Docling 未接入
        ing.ensure("X", day=0)  # 入库


# 功能：提交前版本检查——替代料 A 级报告依赖 datasheet v1；审批后厂商改版，提交时门控刷新 datasheet 发现新版本，报告失效、拒绝提交。
def test_presubmit_datasheet_refresh_rejects(tmp_path):
    fixtures = tmp_path / "ds"  # 可改的样例目录
    shutil.copytree(FIX, fixtures)  # 复制
    (fixtures / f"{CAND}.v2.json").write_text(  # 候选改版
        (FIX / f"{CAND}.v1.json").read_text(encoding="utf-8").replace("| 27 |", "| 20 |"), encoding="utf-8")
    vault = ApprovalVault(key=b"k" * 32)  # 令牌保管库
    holder = {}  # 延迟绑定入库器（它依赖 pipeline 的 session）
    p = code_pipeline(unlimited=True, gate=lambda proxy, claims: EvidenceGate(  # 门控接 datasheet 刷新
        vault, proxy=proxy, authority=proxy.guard.authority,
        datasheet_refresh=lambda mpn, day: holder["ing"].refresh(mpn, day=day)))
    source = FixtureSource(fixtures)  # 样例来源
    source.use(CAND, "v1")  # 当前 v1
    holder["ing"] = ing = Ingestor(source, FixtureParser(), MemoryDatasheetStore(), p.s)  # 入库器
    v1 = ing.ensure(CAND, day=0)["version"]  # 入库 v1
    chosen = {"distributor": "digikey", "stock": 900, "unit_price_usd": 0.5, "eta_day": 4, "amount_usd": 50.0}
    p.s.produce("Decision:S", "ProcurementDecision", {"feasible": True, "decisions": [
        {"mpn": CAND, "qty": 100, "chosen": chosen}]}, day=0, actor="policy", case_id="K7",
        meta={"taint": {"*": "trusted", "decisions[].chosen": "external"}})  # 替代料决策
    p.s.produce(f"VerificationReport:{CAND}", "VerificationReport",  # A 级报告，依赖 datasheet v1
                {"candidate": CAND, "original": MPN, "grade": {"grade": "A"}}, day=0, actor="verifier@K7",
                case_id="K7", meta={"taint": {"*": "trusted"}},
                deps=[{"external": f"datasheet:{CAND}", "version": v1}])  # 外部依赖
    draft = p.ex.create_draft(case_id="K7", day=0, decision_ref="Decision:S@v1", mpn=CAND)  # 草稿
    assert draft["ok"], draft  # A 级通过
    p.ex.request_approval(case_id="K7", day=0, draft_ref=draft["draft"])  # 送审
    HumanDesk(p.s, vault).approve_po(draft["draft"], by="supervisor", day=1)  # 批准
    source.use(CAND, "v2")  # 审批期间厂商改版
    r = p.ex.submit(case_id="K7", day=2, draft_ref=draft["draft"])  # 提交
    assert r["ok"] is False and "VerificationReport" in r["reasons"][0]  # 报告失效，拒绝
    assert p.s.events(type="datasheet_ingested")[-1]["payload"]["previous"] == v1  # 门控触发了刷新


# 功能：集成——Postgres 全文检索与内存实现对同样查询给出同样的页码顺序；版本元数据往返。
@pytest.mark.integration
def test_pg_store_matches_memory(migrated):
    conn, _, _ = migrated  # 迁移好的连接（测试后回滚）
    s = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    mem, pg = MemoryDatasheetStore(), PgDatasheetStore(conn)  # 两个实现
    for store in (mem, pg):  # 同样入库
        source = FixtureSource(FIX)  # 样例
        ing = Ingestor(source, FixtureParser(), store, s)  # 入库器
        ing.ensure(MPN, day=0)  # v1
        source.use(MPN, "v2")  # 改版
        ing.refresh(MPN, day=1)  # v2
        ing.ensure(CAND, day=1)  # 候选
    assert [v["version"] for v in pg.versions(MPN)] == [v["version"] for v in mem.versions(MPN)]  # 版本往返
    assert pg.versions(MPN)[0]["toc"] == mem.versions(MPN)[0]["toc"]  # 目录往返
    for mpn, q in ((MPN, "isolation"), (MPN, "隔离度"), (MPN, "insertion loss"), (MPN, "package qfn"),
                   (CAND, "isolation"), (CAND, "VDD")):  # 若干查询
        v = latest(mem, mpn)["version"]  # 最新版本
        a = search(mem, mpn, v, q, synonyms=CFG["synonyms"])  # 内存
        b = search(pg, mpn, v, q, synonyms=CFG["synonyms"])  # Postgres
        assert [h["page"] for h in a] == [h["page"] for h in b] and a, (mpn, q)  # 页码一致且有结果

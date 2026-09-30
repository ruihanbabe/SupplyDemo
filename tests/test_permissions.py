"""F11：权限 L1/L2 测试。离线部分全部 mock（脚本模型、固定样例后端、假 DNS）；集成部分连本地 Postgres 验证数据库角色。"""
import dataclasses  # 篡改令牌
import json  # 在序列化结果里找凭证原值

import pytest  # 测试框架
from kit import MPN, build, call, end, guard, open_case, tok  # 共用装配
from sqlalchemy import text  # 集成测试 SQL
from sqlalchemy.exc import ProgrammingError  # 权限不足

from business.procurement import procurement_server  # 采购写工具（R1/R2）
from models.providers import reply  # 脚本应答
from permissions.egress import EgressPolicy, egress_policy  # 出站白名单
from permissions.secrets import Credentials, Redactor  # 凭证与脱敏
from permissions.tokens import AttenuationError, CapabilityToken  # 令牌
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储
from simenv.api import Environment  # 环境
from simenv.bom import load_boms  # BOM
from simenv.params import load_params  # 参数
from tools.archive import MemoryArchive  # 留档
from tools.kv import MemoryKV  # KV
from tools.proxy import ToolProxy, load_tools_config  # 代理层
from tools.quota import QuotaGuard  # 配额
from tools.servers import (  # 各 Server
    FixtureDistributorBackend,  # 固定样例后端
    archive_server,  # 留档
    control_server,  # 编排控制
    distributor_server,  # 分销商
    environment_server,  # 环境
)

WRITE_TOOLS = {"create_po_draft", "request_approval", "submit_po", "propose_goal_revision", "report_to_planner"}  # R1/R2


@pytest.fixture(scope="module")
def env0():
    return Environment.create(load_params(), load_boms(), 42)  # 种子 42 环境（只读用）


# 功能：装配带全部 Server（含采购写工具）的代理层；backend / credentials 可替换，scope 开关可关。
# 依赖：kit.guard（权限配置 + 测试执行者 verifier）。完成：返回 (proxy, session)。
def make(env, *, backend=None, credentials=None, scope_enabled=True):
    cfg = load_tools_config()  # 工具配置
    kv = MemoryKV()  # KV
    session = Session(MemorySessionStore(), "run", clock=lambda: "t")  # 事件日志
    servers = [environment_server(lambda: env), distributor_server(backend, credentials),  # 分销商（可带凭证）
               archive_server(), control_server(lambda n, a, c: {"accepted": True}),  # 留档与控制
               procurement_server(lambda: None)]  # 采购写工具（处理函数不会被调用到的测试里）
    g = guard({"verifier": ["distributor_quotes", "read_record", "env_inventory"]},  # 测试子 Agent 角色
              scope_enabled=scope_enabled)  # 消融开关
    proxy = ToolProxy(servers, session, kv=kv, quota=QuotaGuard(kv, cfg["providers"]),  # 代理层
                      archive=MemoryArchive(), config=cfg, guard=g)  # 留档与权限
    return proxy, session  # 返回


def names(proxy, token):
    """令牌可见的工具名集合。"""
    return {t["name"] for t in proxy.visible_tools(token)}  # 可见集合


# 功能：L1——S3 问答模式下编排者看不到任何 R1/R2 工具，S1 下能看到；子 Agent 在任何模式下都看不到写工具。
def test_visibility_by_mode_and_role(env0):
    proxy, _ = make(env0)  # 装配
    s3 = names(proxy, tok(proxy, "orchestrator", mode="S3"))  # 问答模式
    s1 = names(proxy, tok(proxy, "orchestrator", mode="S1"))  # 处置模式
    assert not (s3 & WRITE_TOOLS) and WRITE_TOOLS <= s1  # 写工具只在 S1
    assert "dispatch_task" in s3 and "distributor_quotes" not in s1  # 编排者角色里没有查报价
    child = proxy.guard.authority.attenuate(tok(proxy, "orchestrator"), executor="verifier")  # 子令牌
    assert names(proxy, child) == {"distributor_quotes", "read_record", "env_inventory"}  # 只有自己的角色工具
    r = proxy.call("create_po_draft", {"decision": "D:K7@v1", "mpn": MPN},  # S3 下直接调写工具
                   token=tok(proxy, "orchestrator", mode="S3"), day=0)  # 问答模式
    assert r["error"]["type"] == "forbidden" and "模式" in r["error"]["message"]  # 按模式拒绝并引导


# 功能：L2——子 Agent 冒充编排者调派任务被拒（身份取自令牌，不信传参）；被拒附合法路径与可用工具；
#       编造的改排产工具按"不存在"返回并引导上报计划员；每次调用的事件都带代表链。
def test_call_time_check_guidance_and_chain(env0):
    proxy, session = make(env0)  # 装配
    parent = tok(proxy, "orchestrator")  # 编排者根令牌
    child = proxy.guard.authority.attenuate(parent, executor="verifier")  # Verifier 子令牌
    r = proxy.call("dispatch_task", {"executor": "verifier", "task": "x", "reason": "y"}, token=child, day=1)  # 冒充
    assert r["error"]["type"] == "forbidden" and not r["error"]["retryable"]  # 无权限、不可重试
    assert "交回编排者" in r["error"]["message"] and "distributor_quotes" in r["error"]["message"]  # 引导 + 清单
    r = proxy.call("reschedule_work_order", {"wo_id": "WO-1"}, token=parent, day=1)  # 编造的改排产工具（A6）
    assert r["error"]["type"] == "not_found" and "report_to_planner" in r["error"]["message"]  # 引导上报计划员
    ev = session.events(type="tool_call")  # 事件
    assert ev[0]["actor"] == "verifier@K7" and ev[0]["payload"]["denied"] == "role"  # 身份与拒绝类别
    chain = ev[0]["meta"]  # 代表链
    assert chain["executor"] == "verifier" and chain["on_behalf_of"] == "orchestrator@K7"  # 子 Agent 替编排者
    assert chain["case_id"] == "K7" and chain["initiated_by"] == "test" and chain["token"] == child.token_id  # 案子与发起者
    assert chain["risk"] == "R0" and ev[1]["meta"]["risk"] is None  # 风险级（不存在的工具为空）


# 功能：伪造或篡改的令牌验签失败：自己构造的令牌、改大工具集合、改作用域、别的签发者签的，一律拒绝且看不到任何工具。
def test_forged_and_tampered_tokens(env0):
    proxy, _ = make(env0)  # 装配
    real = proxy.guard.authority.attenuate(tok(proxy, "orchestrator"), executor="verifier", scope=[MPN])  # 真令牌
    forged = CapabilityToken("cap-1", "orchestrator", "K7", frozenset({"dispatch_task"}), None, "S1",  # 自己造的
                             None, None, "test", "00" * 32)  # 假签名
    widened = dataclasses.replace(real, tools=real.tools | {"dispatch_task"})  # 改大工具
    unscoped = dataclasses.replace(real, scope=None)  # 去掉作用域
    foreign = tok(guard(), "orchestrator")  # 别的签发者
    for bad in (forged, widened, unscoped, foreign, None):  # 逐个
        r = proxy.call("env_inventory", {"mpns": [MPN]}, token=bad, day=0)  # 调用
        assert r["error"]["type"] == "forbidden" and "核验" in r["error"]["message"]  # 验签失败
        assert proxy.visible_tools(bad) == []  # 什么都看不到


# 功能：权限衰减——子令牌工具与作用域只能收窄：越出父工具 / 父作用域抛错；孙令牌继承子作用域；代表链逐级记录。
def test_attenuation_only_narrows(env0):
    proxy, _ = make(env0)  # 装配
    auth = proxy.guard.authority  # 签发者
    root = tok(proxy, "orchestrator", scope=[MPN, "MAX2837"])  # 根令牌带作用域
    with pytest.raises(AttenuationError, match="工具超出"):  # 要角色外的工具
        auth.attenuate(root, executor="verifier", tools=["dispatch_task"])  # 拒绝
    with pytest.raises(AttenuationError, match="作用域超出"):  # 要父作用域外的料号
        auth.attenuate(root, executor="verifier", scope=[MPN, "ADL5602"])  # 拒绝
    child = auth.attenuate(root, executor="verifier", scope=[MPN])  # 合法收窄
    grand = auth.attenuate(child, executor="verifier", tools=["read_record"])  # 再收窄工具
    assert grand.scope == frozenset({MPN}) and grand.tools == frozenset({"read_record"})  # 继承作用域
    assert (grand.parent, grand.on_behalf_of) == (child.token_id, "verifier@K7")  # 代表链
    assert not (child.tools & WRITE_TOOLS)  # 子令牌里没有写工具


# 功能：作用域——作用域内料号可查，含作用域外料号即被拒；关掉 capability_scope 后不再按作用域拦。
def test_scope_check_and_ablation(env0):
    other = min(env0.get_bom("hackrf-one"))  # 另一颗料
    for enabled in (True, False):  # 开 / 关
        proxy, _ = make(env0, scope_enabled=enabled)  # 装配
        child = proxy.guard.authority.attenuate(tok(proxy, "orchestrator"), executor="verifier", scope=[MPN])  # 子令牌
        assert proxy.call("distributor_quotes", {"mpns": [MPN], "qty": 1}, token=child, day=0)["ok"]  # 作用域内
        r = proxy.call("distributor_quotes", {"mpns": [MPN, other], "qty": 1}, token=child, day=0)  # 含作用域外
        assert r["ok"] is (not enabled)  # 开时拒绝，关时放行
        if enabled:  # 开着
            assert other in r["error"]["message"] and "作用域" in r["error"]["message"]  # 写明越界料号


# 功能：留档只能读本案子的记录（别的案子的记录号按无权限拒绝）。
def test_read_record_limited_to_own_case(env0):
    proxy, _ = make(env0)  # 装配
    auth = proxy.guard.authority  # 签发者
    k7 = auth.attenuate(tok(proxy, "orchestrator", "K7"), executor="verifier")  # K7 的子令牌
    k8 = auth.attenuate(tok(proxy, "orchestrator", "K8"), executor="verifier")  # K8 的子令牌
    rid = proxy.call("distributor_quotes", {"mpns": [MPN], "qty": 1}, token=k7, day=0)["source"]["record_id"]  # K7 取数
    assert proxy.call("read_record", {"cursor": rid}, token=k7, day=0)["ok"]  # 本案可读
    r = proxy.call("read_record", {"cursor": rid}, token=k8, day=0)  # 别案
    assert r["error"]["type"] == "forbidden" and "不属于本案" in r["error"]["message"]  # 拒绝


# 功能：凭证隔离与脱敏——凭证只在后端；上游返回里的请求头、URL 参数与原值，以及错误信息里的原值，
#       都不出现在事件、留档与交给模型的结果中；Credentials 打印不显示值。
def test_credentials_never_leak(env0):
    secret = "sk-live-7f3a9c"  # 假凭证值
    creds = Credentials.from_env(["DIGIKEY_API_KEY"], environ={"DIGIKEY_API_KEY": secret})  # 从（假）环境变量读

    class LeakyBackend(FixtureDistributorBackend):  # 把凭证带进返回与错误的后端
        def raw_quote(self, mpn, d):  # 取原文报价
            if mpn == "BOOM":  # 触发上游错误
                raise ConnectionError(f"GET https://api.digikey.com/q?apikey={secret} 失败")  # 错误信息带凭证
            raw = super().raw_quote(mpn, d)  # 正常原文
            return {**raw, "request": {"url": f"https://api.digikey.com/q?mpn={mpn}&api_key={creds.get('DIGIKEY_API_KEY')}",
                                       "headers": {"Authorization": f"Bearer {secret}"}}}  # 请求信息

    proxy, session = make(env0, backend=LeakyBackend(), credentials=creds)  # 装配
    child = proxy.guard.authority.attenuate(tok(proxy, "orchestrator"), executor="verifier")  # 子令牌
    ok = proxy.call("distributor_quotes", {"mpns": [MPN], "qty": 1, "distributors": ["digikey"]}, token=child, day=0)
    bad = proxy.call("distributor_quotes", {"mpns": ["BOOM"], "qty": 1, "distributors": ["digikey"]}, token=child, day=0)
    assert bad["error"]["type"] == "upstream_error"  # 错误照常返回
    records = [proxy.archive.get("run", f"rec-{i}") for i in range(1, 3)]  # 留档
    dump = json.dumps([ok, bad, session.events(), records], ensure_ascii=False)  # 全部写出物
    assert secret not in dump and "***" in dump  # 原值不出现，已替换
    assert secret not in repr(creds) and "DIGIKEY_API_KEY" in repr(creds)  # 打印只见变量名
    r = Redactor(["x_api_key", "api_key", "authorization"])  # 独立脱敏器
    assert r({"X-Api-Key": "k", "n": ["https://h/x?API-KEY=abc&q=1"]}) == {"X-Api-Key": "***",  # 键名不分大小写与 -/_
                                                                        "n": ["https://h/x?API-KEY=***&q=1"]}


# 功能：反复越权计入循环检测——子 Agent 在一段里连续三次查作用域外料号，第三次后强制结束，任务以"未完成"交回；
#       task_dispatched 事件记子令牌作用域与代表链。
def test_repeated_denials_end_the_loop(env0):
    others = sorted(env0.get_bom("hackrf-one"))[:3]  # 三颗作用域外的料
    scoped = reply(tool_calls=[call("dispatch_task", executor="quoter", task="只查射频开关", reason="比价",
                                    scope=[MPN])])  # 带作用域派发
    probes = [reply(tool_calls=[call("distributor_quotes", mpns=[m], qty=1)]) for m in others]  # 三次越界
    s, seg, _, _, _ = build([scoped, *probes, end()])  # 装配
    open_case(s)  # 开案
    seg.run("K7", reason="开案", day=0)  # 跑一段
    fin = s.events(type="task_finished")[0]["payload"]  # 任务结果
    assert fin["status"] == "unfinished" and "反复越权" in fin["reason"]  # 强制结束
    disp = s.events(type="task_dispatched")[0]  # 派发事件
    assert disp["payload"]["scope"] == [MPN] and disp["meta"]["on_behalf_of"] == "orchestrator@K7"  # 作用域与代表链
    assert disp["meta"]["initiated_by"] == "runtime#1"  # 由开案事件发起
    denied = [e for e in s.events(type="tool_call") if e["payload"].get("denied") == "scope"]  # 作用域拒绝
    assert len(denied) == 3  # 三次都记下


# 功能：派发越权——编排者给子任务要父作用域外的料号时派发被拒、不登记任务。
def test_dispatch_rejects_scope_escalation(env0):
    s, seg, _, _, _ = build([])  # 装配（不需要模型）
    open_case(s)  # 开案
    auth = seg.authority  # 签发者
    root = auth.issue_root(executor="orchestrator", case_id="K7", mode=None, initiated_by="t", scope={MPN})  # 带作用域
    out = seg.dispatcher.dispatch(parent=root, day=0, executor="quoter", task="x", inputs=[], reason="y",
                                  scope=[MPN, "MAX2837"])  # 超出父作用域
    assert out["accepted"] is False and "作用域超出" in out["error"]  # 拒绝
    assert s.events(type="task_dispatched") == [] and root.mode == "S3"  # 未登记；开案没写模式按默认只读


# 功能：出站白名单——拒 HTTP、内嵌账号、IP 直连、白名单外域名、解析到内网的域名、跳出白名单的重定向、超大或非 PDF；
#       正常的厂商 HTTPS 链接放行；分销商 Server 只能访问自己的上游域名。
def test_egress_rules():
    from permissions.tokens import load_permissions_config  # 权限配置

    dns = {"www.skyworksinc.com": ["104.18.1.1"], "evil.ti.com": ["10.0.0.5"], "cdn.ti.com": ["2606:4700::1"]}  # 假 DNS
    policy = egress_policy("datasheet", load_permissions_config(), resolver=lambda h: dns.get(h, ["93.184.216.34"]))
    ok = "https://www.skyworksinc.com/-/media/SkyWorks/Documents/Products/sky13453.pdf"  # 厂商 PDF
    assert policy.check_url(ok) is None and policy.check_url("https://cdn.ti.com/x.pdf") is None  # 放行
    assert "HTTPS" in policy.check_url("http://www.skyworksinc.com/a.pdf")  # 非 HTTPS
    assert "账号" in policy.check_url("https://u:p@www.skyworksinc.com/a.pdf")  # 内嵌账号
    assert "IP" in policy.check_url("https://169.254.169.254/latest/meta-data")  # 云元数据地址
    assert "白名单" in policy.check_url("https://skyworksinc.com.attacker.io/a.pdf")  # 伪装域名
    assert "不允许的地址" in policy.check_url("https://evil.ti.com/a.pdf")  # DNS 指向内网
    assert "重定向" in policy.check_redirects([ok, "https://attacker.io/a.pdf"])  # 跳出白名单
    assert "次数" in policy.check_redirects([ok] * 7)  # 跳数超限
    assert policy.check_download("application/pdf; charset=binary", 1000) is None  # PDF 放行
    assert "类型" in policy.check_download("text/html", 1000)  # 非 PDF
    assert "上限" in policy.check_download("application/pdf", 50_000_000)  # 超大
    digikey = egress_policy("digikey", load_permissions_config(), resolver=lambda h: ["93.184.216.34"])  # 分销商 Server
    assert digikey.check_url("https://api.digikey.com/products/v4") is None  # 自己的上游
    assert "白名单" in digikey.check_url("https://api.mouser.com/api/v1")  # 别家的上游也不行
    with pytest.raises(KeyError):  # 未登记的组件默认拒绝一切
        egress_policy("unknown", load_permissions_config())  # 报错
    assert EgressPolicy([]).check_url("https://anything.com") is not None  # 空白名单拒绝一切


# 功能：集成——数据库层权限：agent_ro 能读不能写；procurement 能追加 env_write、不能写 Session 事件。
@pytest.mark.integration
def test_database_roles(migrated):
    conn, _, _ = migrated  # 迁移好的连接（事务内，测试后回滚）

    def attempt(role, sql):  # 以某角色在保存点里执行，返回是否成功
        sp = conn.begin_nested()  # 保存点（失败只回滚这一步）
        try:  # 执行
            conn.execute(text(f"SET LOCAL ROLE {role}"))  # 切角色
            conn.execute(text(sql))  # 执行语句
            sp.commit()  # 成功
            return True  # 允许
        except ProgrammingError:  # 权限不足
            sp.rollback()  # 回滚到保存点
            return False  # 拒绝
        finally:  # 恢复
            conn.execute(text("RESET ROLE"))  # 回到迁移用户

    write = ("INSERT INTO env_write (run_id, seq, day, kind, idempotency_key, payload) "  # 采购写入
             "VALUES ('r', 1, 0, 'submit_po', 'k1', '{}')")  # 一条
    event = ("INSERT INTO session_event (run_id, seq, day, wall_time, actor, type, payload, entity_keys, meta, "  # 事件
             "prev_hash, hash) VALUES ('r', 1, 0, 't', 'a', 'x', '{}', '{}', '{}', 'g', 'h')")  # 一条
    for table in ("env_write", "session_event", "tool_record", "hw_part"):  # 读
        assert attempt("supplyagent_agent_ro", f"SELECT count(*) FROM {table}")  # 只读角色能读
    assert not attempt("supplyagent_agent_ro", write)  # 不能写环境
    assert not attempt("supplyagent_agent_ro", event)  # 不能写事件
    assert not attempt("supplyagent_agent_ro", "DELETE FROM hw_part")  # 不能删
    assert attempt("supplyagent_procurement", write)  # 采购角色能追加写入
    assert not attempt("supplyagent_procurement", event)  # 不能写事件
    assert not attempt("supplyagent_procurement", "UPDATE env_write SET day = 1")  # 不能改

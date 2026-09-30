"""文件摘要（F20 · 知识库 B：人工确认的长期记忆）

spec §5.10、§6.3：长期记忆只有替代关系（H1 工程师确认）与料号别名（H3 采购员确认）两种，都须人工确认后落库（G3）。
- 存储：KBStore 接口两个实现（MemoryKBStore；PgKBStore 用迁移 0008 的 kb_entry / kb_status，只追加）；当前状态 = 最新一条状态变更。
- KnowledgeBase：
  - import_seeds(rows)：原理图 Substitution 字段——"any equivalent" 丢弃，其余"厂商 料号"解析为未验证种子（带板与位号适用范围）；
    重复导入不重复写。未验证种子可被替代检索列为候选，门控不认。
  - propose(session, ...)：编排者 propose_memory 的实现。替代关系只收 VerificationReport 引用，原料、候选、等级与依赖由代码从报告取
    （不信模型给的数值，G4）；别名给出说法与料号。写 memory_proposed，并给对应人工节点发 human_request（替代 H1、别名 H3）。
  - confirm(session, 提议事件号, by, node, day)：只接受对应节点的确认；条目过 F13 长期记忆汇点（必须带确认记录、说法等不可信文本
    以引用块 {quote, source} 存放），写条目、状态"已确认"与 memory_confirmed 事件。
  - sync(events)：datasheet 出新版本 → 依赖旧版本的条目"待重验"；part_eol 事件（F04 产生）→ 涉及该料的条目"已标记 EOL"。
  - revoke_case(case_id)：来源案子判定被投毒 → 其衍生条目一键撤销。
  - inject(keys, limit)：按实体键（料号、板）匹配，最多 5 条（配置），标明来源与状态；撤销的不注入（消融 memory_injection 关闭时为空）。
  - substitutes(mpn)：替代检索子 Agent 的只读查询（kb_server 的 kb_substitutes 工具，按料号作用域）。
门控与 Policy 不读记忆（它们没有这里的入口）。
依赖：session.log、taint.sinks / labels、tools.server、sqlalchemy、pydantic。被 runtime.segment（propose_memory）、cases.router（sync）、
F16 上下文（记忆分区）、F23 替代检索与 Resolver 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # JSON 字段
import re  # 解析引用
from typing import Protocol  # 接口

from pydantic import BaseModel, Field  # 工具参数
from sqlalchemy import text  # Pg 查询

from session.log import ART_VALID, Session  # 事件日志
from taint.labels import TRUSTED, UNTRUSTED  # F13 标签
from taint.sinks import TaintSinks  # F13 汇点
from tools.server import Server, ToolDef  # 工具定义

REF = re.compile(r"^(?P<id>.+)@v(?P<ver>\d+)$")  # Artifact 引用
NODE_OF = {"substitution": "H1", "alias": "H3"}  # 种类 → 确认节点
ROLE_OF = {"H1": "engineer", "H3": "buyer"}  # 节点 → 人
STATUS_TEXT = {"unverified_seed": "未验证种子，门控不认", "confirmed": "已确认", "revalidate": "待重验",  # 状态说明
               "eol": "涉及的料已 EOL", "revoked": "已撤销"}  # 同上
INJECT_ORDER = {"confirmed": 0, "revalidate": 1, "eol": 2, "unverified_seed": 3}  # 注入优先级（撤销的不注入）


class KBStore(Protocol):
    """知识库 B 存储接口。"""

    def add_entry(self, entry: dict) -> str: ...  # 存条目，返回条目号
    def add_status(self, entry_id: str, status: str, reason: str, actor: str, day: int) -> None: ...  # 状态变更
    def entries(self) -> list[dict]: ...  # 全部条目（带当前状态）


# 功能：内存实现。依赖：无。完成：条目号 KB-N；当前状态取最后一条变更。
class MemoryKBStore:
    def __init__(self) -> None:
        self._entries: list[dict] = []  # 条目
        self._status: list[dict] = []  # 状态变更

    def add_entry(self, entry):
        eid = f"KB-{len(self._entries) + 1}"  # 条目号
        self._entries.append(json.loads(json.dumps({**entry, "entry_id": eid})))  # 副本
        return eid  # 返回

    def add_status(self, entry_id, status, reason, actor, day):
        self._status.append({"entry_id": entry_id, "status": status, "reason": reason, "actor": actor, "day": day})

    def entries(self):
        cur = {s["entry_id"]: s["status"] for s in self._status}  # 最新状态
        return [{**json.loads(json.dumps(e)), "status": cur.get(e["entry_id"])} for e in self._entries]  # 副本


# 功能：Postgres 实现。依赖：迁移 0008；调用方给连接（不提交）。完成：条目号按已有条数 + 1。
class PgKBStore:
    def __init__(self, connection) -> None:
        self.conn = connection  # 数据库连接

    def add_entry(self, entry):
        n = self.conn.execute(text("SELECT count(*) FROM kb_entry")).scalar_one() + 1  # 序号
        eid = f"KB-{n}"  # 条目号
        self.conn.execute(text("""
            INSERT INTO kb_entry (entry_id, kind, body, deps, source_case, confirmation, created_day)
            VALUES (:i, :k, CAST(:b AS JSONB), CAST(:d AS JSONB), :c, CAST(:f AS JSONB), :day)"""),
            {"i": eid, "k": entry["kind"], "b": json.dumps(entry["body"], ensure_ascii=False),  # 内容
             "d": json.dumps(entry["deps"], ensure_ascii=False), "c": entry.get("source_case"),  # 依赖与来源
             "f": json.dumps(entry.get("confirmation"), ensure_ascii=False), "day": entry["created_day"]})  # 确认
        return eid  # 返回

    def add_status(self, entry_id, status, reason, actor, day):
        self.conn.execute(text("INSERT INTO kb_status (entry_id, status, reason, actor, day) "
                               "VALUES (:i, :s, :r, :a, :d)"),
                          {"i": entry_id, "s": status, "r": reason, "a": actor, "d": day})  # 追加

    def entries(self):
        rows = self.conn.execute(text("""
            SELECT e.entry_id, e.kind, e.body, e.deps, e.source_case, e.confirmation, e.created_day,
                   (SELECT s.status FROM kb_status s WHERE s.entry_id = e.entry_id ORDER BY s.seq DESC LIMIT 1) AS status
            FROM kb_entry e ORDER BY length(e.entry_id), e.entry_id""")).mappings().all()  # 按条目号
        return [dict(r) for r in rows]  # 字典列表


def parse_seed(text_value: str) -> tuple[str | None, str] | None:
    """"厂商 料号" → (厂商, 料号)；"any equivalent" 或空返回 None。"""
    v = (text_value or "").strip()  # 规范化
    if not v or v.lower() == "any equivalent":  # 丢弃
        return None  # 不导入
    maker, _, mpn = v.rpartition(" ")  # 最后一段是料号
    return (maker or None), mpn  # 厂商与料号


class KnowledgeBase:
    # 功能：见文件摘要。依赖：存储、F13 汇点、配置（注入上限）、注入开关。
    def __init__(self, store: KBStore, *, sinks: TaintSinks | None = None, inject_limit: int = 5,
                 inject_enabled: bool = True):
        self.store = store  # 存储
        self.sinks = sinks or TaintSinks()  # F13 汇点
        self.limit, self.inject_enabled = inject_limit, inject_enabled  # 注入上限与开关

    # ------------------------------------------------------------------ 种子
    def import_seeds(self, rows: list[dict], *, day: int = 0) -> int:
        """rows: [{board, mpn, alternates, designators}]；返回新写入条数（重复导入不重复写）。"""
        have = {(e["body"]["original"], e["body"]["substitute"], e["body"]["scope"]["board"])  # 已有种子
                for e in self.store.entries() if e["kind"] == "substitution" and e["confirmation"] is None}
        n = 0  # 计数
        for r in rows:  # 每行
            seed = parse_seed(r.get("alternates"))  # 解析
            if seed is None or (r["mpn"], seed[1], r["board"]) in have:  # 丢弃或已有
                continue  # 跳过
            eid = self.store.add_entry({"kind": "substitution", "created_day": day, "deps": [], "source_case": None,  # 条目
                                        "confirmation": None,  # 种子没有确认记录
                                        "body": {"original": r["mpn"], "substitute": seed[1], "maker": seed[0],  # 替代关系
                                                 "grade": None, "report": None,  # 没有验证
                                                 "scope": {"board": r["board"], "designators": list(r["designators"])}}})
            self.store.add_status(eid, "unverified_seed", "原理图 Substitution 字段", "seed_import", day)  # 状态
            have.add((r["mpn"], seed[1], r["board"]))  # 记下
            n += 1  # 计数
        return n  # 新写入条数

    # ------------------------------------------------------------------ 提议与确认
    def propose(self, session: Session, *, case_id: str, day: int, actor: str, kind: str,
                report: str | None = None, board: str | None = None, designators: list[str] = (),
                alias: str | None = None, mpn: str | None = None) -> dict:
        """提议一条记忆；替代关系的内容与依赖由代码从 VerificationReport 取。返回 {accepted, ...}。"""
        if kind == "substitution":  # 替代关系
            m = REF.match(report or "")  # 报告引用
            rep = session.artifact_view(m["id"], int(m["ver"])) if m else None  # 报告
            if rep is None or rep["kind"] != "VerificationReport" or rep["status"] != ART_VALID:  # 不合格
                return {"accepted": False, "error": "替代关系须引用本案有效的 VerificationReport"}  # 拒绝
            g = rep["content"].get("grade")  # 等级（代码汇总）
            body = {"original": rep["content"].get("original"), "substitute": rep["content"].get("candidate"),  # 原料与候选
                    "grade": g.get("grade") if isinstance(g, dict) else g, "report": report,  # 等级与报告
                    "scope": {"board": board, "designators": list(designators)}}  # 适用范围
            deps = [{"ref": d["target"], "version": d["target_version"]} for d in session.store.deps(session.run_id)
                    if d["artifact_id"] == rep["artifact_id"] and d["version"] == rep["version"]  # 报告的依赖
                    and d["dep_kind"] == "external"]  # 外部数据（datasheet 版本等）
        elif kind == "alias":  # 料号别名
            if not alias or not mpn:  # 缺字段
                return {"accepted": False, "error": "别名须给出说法与料号"}  # 拒绝
            body, deps = {"alias": alias, "mpn": mpn}, []  # 内容
        else:  # 其他
            return {"accepted": False, "error": f"不支持的记忆种类：{kind}（只有 substitution、alias）"}  # 拒绝
        ev = session.append("memory_proposed", day=day, actor=actor, case_id=case_id,  # 提议事件
                            payload={"kind": kind, "body": body, "deps": deps},  # 内容与依赖
                            entity_keys={"mpn": [x for x in (body.get("original"), body.get("substitute"),
                                                             body.get("mpn")) if x]})  # 实体键
        node = NODE_OF[kind]  # 确认节点
        session.append("human_request", day=day, actor=actor, case_id=case_id,  # 通知人工
                       payload={"role": ROLE_OF[node], "node": node, "proposal_seq": ev["seq"]}, caused_by=ev["seq"])
        return {"accepted": True, "proposal_seq": ev["seq"], "note": f"已提交 {node} 确认，确认后才落库"}  # 接受

    def confirm(self, session: Session, proposal_seq: int, *, by: str, node: str, day: int) -> str:
        """人工确认落库；节点不对、汇点不过抛 ValueError。返回条目号。"""
        ev = session.store.event(session.run_id, proposal_seq)  # 提议
        if ev is None or ev["type"] != "memory_proposed":  # 不是提议
            raise ValueError(f"事件 #{proposal_seq} 不是记忆提议")  # 报错
        kind = ev["payload"]["kind"]  # 种类
        if node != NODE_OF[kind]:  # 节点不对（如别名由工程师确认）
            raise ValueError(f"{kind} 须由 {NODE_OF[kind]} 确认，收到 {node}")  # 报错
        body = dict(ev["payload"]["body"])  # 内容
        taint = {"*": TRUSTED}  # 代码取自报告与配置的字段可信
        if kind == "alias":  # 说法是用户 / 模型的自由文本
            taint["alias"] = UNTRUSTED  # 不可信
            body["alias"] = {"quote": body["alias"], "source": f"case:{ev['case_id']}"}  # 以引用块存放
        dec = session.append("human_decision", day=day, actor=f"human:{by}", case_id=ev["case_id"],  # 人工决定
                             payload={"node": node, "artifact": f"proposal#{proposal_seq}", "decision": "approve",
                                      "by": by, "note": ""}, caused_by=proposal_seq)  # 确认记录
        entry = {**body, "confirmation": {"node": node, "by": by, "event_seq": dec["seq"]}}  # 汇点检查对象
        reasons = self.sinks.memory_entry(entry, taint)  # F13 长期记忆汇点
        if reasons:  # 不过
            raise ValueError("；".join(reasons))  # 报错
        eid = self.store.add_entry({"kind": kind, "body": body, "deps": ev["payload"]["deps"],  # 条目
                                    "source_case": ev["case_id"], "confirmation": entry["confirmation"],  # 来源与确认
                                    "created_day": day})  # 日
        self.store.add_status(eid, "confirmed", f"{node} {by} 确认", f"human:{by}", day)  # 状态
        session.append("memory_confirmed", day=day, actor=f"human:{by}", case_id=ev["case_id"],  # 事件
                       payload={"entry_id": eid, "proposal_seq": proposal_seq}, caused_by=dec["seq"])  # 因果
        return eid  # 条目号

    # ------------------------------------------------------------------ 失效与撤销
    def sync(self, events: list[dict]) -> list[str]:
        """按新事件标记：datasheet 新版本 → 依赖旧版的条目待重验；part_eol → 涉及的条目标 EOL。返回被标记的条目号。"""
        marked = []  # 结果
        live = [e for e in self.store.entries() if e["status"] not in ("revoked",)]  # 未撤销
        for ev in events:  # 每个事件
            p = ev["payload"]  # 内容
            for e in live:  # 每个条目
                if ev["type"] == "datasheet_ingested" and p.get("previous") and any(  # datasheet 改版
                        d["ref"] == f"datasheet:{p['mpn']}" and d["version"] != p["version"] for d in e["deps"]):
                    self.store.add_status(e["entry_id"], "revalidate", f"{p['mpn']} datasheet 出新版本", "kb", ev["day"])
                    marked.append(e["entry_id"])  # 待重验
                elif ev["type"] == "part_eol" and p.get("mpn") in (e["body"].get("original"),  # EOL
                                                                   e["body"].get("substitute"), e["body"].get("mpn")):
                    self.store.add_status(e["entry_id"], "eol", f"{p['mpn']} EOL", "kb", ev["day"])  # 标记
                    marked.append(e["entry_id"])  # 记下
        return marked  # 被标记的条目

    def revoke_case(self, case_id: str, *, reason: str, actor: str, day: int) -> list[str]:
        """来源案子判定被投毒：其衍生条目一键撤销。"""
        out = [e["entry_id"] for e in self.store.entries() if e["source_case"] == case_id and e["status"] != "revoked"]
        for eid in out:  # 逐个
            self.store.add_status(eid, "revoked", reason, actor, day)  # 撤销
        return out  # 被撤销的条目

    # ------------------------------------------------------------------ 使用
    def _matches(self, e: dict, keys: dict) -> bool:
        """条目是否命中实体键（料号或板）。"""
        b = e["body"]  # 内容
        mpns = set(keys.get("mpn", []))  # 料号
        return bool(mpns & {b.get("original"), b.get("substitute"), b.get("mpn")}) or \
            bool(set(keys.get("board", [])) & {(b.get("scope") or {}).get("board")})  # 或板

    def _line(self, e: dict) -> str:
        """一条注入文本（标明来源与状态）。"""
        b, src = e["body"], (f"来源 {e['source_case']}" if e["source_case"] else "来源 原理图种子")  # 内容与来源
        who = f"，{e['confirmation']['node']} {e['confirmation']['by']} 确认" if e["confirmation"] else ""  # 确认
        if e["kind"] == "alias":  # 别名
            return f"【记忆·别名 {e['entry_id']}】\"{b['alias']['quote']}\" → {b['mpn']}（{src}{who}；{STATUS_TEXT[e['status']]}）"
        scope = b["scope"]  # 适用范围
        return (f"【记忆·替代 {e['entry_id']}】{b['original']} → {b['substitute']}"  # 替代关系
                f"（等级 {b['grade'] or '无'}；适用 {scope['board']} {'、'.join(scope['designators'][:4])}；"  # 等级与范围
                f"{src}{who}；{STATUS_TEXT[e['status']]}）")  # 来源与状态

    def inject(self, keys: dict[str, list[str]]) -> list[str]:
        """按实体键注入，最多 inject_limit 条；撤销的不注入；已确认优先。"""
        if not self.inject_enabled:  # 消融
            return []  # 不注入
        hits = [e for e in self.store.entries() if e["status"] in INJECT_ORDER and self._matches(e, keys)]  # 命中
        hits.sort(key=lambda e: (INJECT_ORDER[e["status"]], e["entry_id"]))  # 优先级
        return [self._line(e) for e in hits[:self.limit]]  # 文本

    def substitutes(self, mpn: str) -> list[dict]:
        """某料的替代关系（含未验证种子，标明门控不认；撤销的不返回）。"""
        return [{"entry_id": e["entry_id"], "substitute": e["body"]["substitute"], "grade": e["body"]["grade"],  # 替代料
                 "scope": e["body"]["scope"], "status": e["status"], "gate_accepts": False,  # 门控只认验证报告
                 "note": STATUS_TEXT[e["status"]]}  # 说明
                for e in self.store.entries()  # 全部条目
                if e["kind"] == "substitution" and e["body"]["original"] == mpn and e["status"] != "revoked"]  # 过滤


def case_memory(kb: KnowledgeBase):
    """F16 编排者上下文的记忆分区：按案子的目标料号（开案与并入）注入。返回 (session, case_id) → 行列表。"""
    def lines(session: Session, case_id: str) -> list[str]:  # 注入函数
        mpns = set()  # 目标料号
        for e in session.events(case_id=case_id):  # 本案事件
            if e["type"] in ("case_opened", "case_target_added"):  # 开案或并入
                mpns.update(e["entity_keys"].get("mpn", []))  # 料号
        return kb.inject({"mpn": sorted(mpns)})  # 注入
    return lines  # 注入函数


class KBSubstitutesArgs(BaseModel):
    """查替代关系。"""
    mpn: str = Field(min_length=1)  # 原料号


def kb_server(kb: KnowledgeBase) -> Server:
    """知识库 B 的只读工具（替代检索子 Agent 用；按料号作用域）。"""
    return Server("knowledge_base", [ToolDef(
        "kb_substitutes", "查某颗料已知的替代关系（含原理图未验证种子；任何条目都须重新验证后门控才认）。"
        "例：kb_substitutes(mpn='CL05C470JB5NNNC')", KBSubstitutesArgs,
        lambda a, ctx: kb.substitutes(a.mpn),  # 查询
        entity_keys=lambda a: {"mpn": [a.mpn]}, scoped=True)])  # 实体键与作用域

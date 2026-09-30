"""文件摘要（F23 · 两个 LLM 函数：Resolver 与 Explainer）

LLM 函数 = 单次调用、结构化输出（spec §2 执行者分类），以代码执行者的形式注册，调用走 F08 适配层（校验、修复、降级、记录）。
- resolver_code → ResolvedPart（spec §2.3 料号解析，三层）：
  ① 规则层：大小写与空格规范化、去掉配置里的包装后缀，精确匹配 BOM 目录 → 直接确定（不调 LLM）；
  ② 检索层：知识库 B 已确认的别名精确匹配 → 直接确定；否则按料号前缀、品类口语叫法（categories.yaml names）、相似度找候选；
  ③ LLM 消歧：只能从候选里选并给置信度；选了列表外的料号、没有候选或置信度低于阈值 → 交采购员确认（H3 human_request），不擅自定。
- explainer_code → Explanation（spec §3.2）：按受众写说明，必定加载 Explainer Skill；输出里的数字须能在输入 Artifact 中找到，
  找不到的列入 unconfirmed，整段标为不可信（F16 只以引用块展示），全部有出处时标已校验。
依赖：models.types、knowledge_base.assets / kb、taint.labels、agents.schemas、runtime.dispatch.CodeResult、标准库 difflib / re。
被 agents.nodes 使用；load_agents_config() 读 config/agents.yaml。
"""
from __future__ import annotations  # 类型注解延迟求值

import difflib  # 相似度
import json  # 输入序列化
import re  # 数字与规范化

import yaml  # 读品类叫法

from agents.schemas import ExplanationOut, ResolveChoice  # 输出模型
from knowledge_base.assets import KNOWLEDGE, load_skill  # 知识资产
from models.types import Message  # 消息
from runtime.dispatch import CodeResult  # 代码执行者返回
from taint.labels import TRUSTED, UNTRUSTED, VERIFIED  # 标签

NUM = re.compile(r"\d+(?:\.\d+)?")  # 数字


def load_agents_config() -> dict:
    """读 config/agents.yaml。"""
    return yaml.safe_load((KNOWLEDGE.parent / "agents.yaml").read_text(encoding="utf-8"))  # 读 YAML


def _norm(s: str) -> str:
    """料号规范化：去空白、转大写。"""
    return re.sub(r"\s+", "", s or "").upper()  # 规范化


def numbers_in(value) -> set[str]:
    """递归收集任意结构里出现的数字（字符串里的数字也算，含字典键）。"""
    if isinstance(value, dict):  # 字典
        return set().union(*(numbers_in(k) | numbers_in(v) for k, v in value.items())) if value else set()  # 键与值
    if isinstance(value, list):  # 列表
        return set().union(*(numbers_in(v) for v in value)) if value else set()  # 逐项
    if isinstance(value, bool) or value is None:  # 布尔与空
        return set()  # 无
    return set(NUM.findall(str(value)))  # 数字或文本


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
def resolver_code(*, adapter, kb, get_env, config: dict, session):
    rc = config["resolver"]  # 配置
    cats = yaml.safe_load((KNOWLEDGE / "categories.yaml").read_text(encoding="utf-8"))["categories"]  # 品类与叫法

    def catalog() -> list[str]:  # BOM 目录（全部板）
        return sorted({m for bom in get_env().state.boards.values() for m in bom})  # 料号

    def rules(query: str, mpns: list[str]) -> str | None:  # ① 规则层
        q = _norm(query)  # 规范化
        variants = {q} | {q[: -len(s)] for s in rc["strip_suffixes"] if q.endswith(_norm(s)) and len(q) > len(s)}
        by_norm = {_norm(m): m for m in mpns}  # 规范化 → 原料号
        return next((by_norm[v] for v in variants if v in by_norm), None)  # 精确命中

    def retrieve(query: str, mpns: list[str]) -> list[str]:  # ② 检索层：候选
        q, low = _norm(query), query.lower()  # 规范化
        cands = [m for m in mpns if len(q) >= 4 and _norm(m).startswith(q)]  # 前缀
        for c in cats:  # 品类口语叫法
            if any(n.lower() in low for n in c.get("names", [])):  # 描述里出现
                cands += [m for m in mpns if re.match(c["pattern"], m)]  # 该品类的料
        cands += difflib.get_close_matches(q, [_norm(m) for m in mpns], n=rc["max_candidates"], cutoff=0.8)  # 相似
        by_norm = {_norm(m): m for m in mpns}  # 还原
        out = []  # 去重保序
        for m in cands:  # 逐个
            m = by_norm.get(_norm(m), m)  # 原料号
            if m not in out:  # 去重
                out.append(m)  # 收下
        return out[: rc["max_candidates"]]  # 截断

    def run(task, inputs, ctx) -> CodeResult:  # 代码执行者
        query = ctx["params"].get("query", "")  # 用户说法
        mpns = catalog()  # 目录
        base = {"query": {"quote": query, "source": "user"}}  # 用户原话以引用结构存放
        hit = rules(query, mpns)  # ① 规则层
        if hit:  # 命中
            return CodeResult({**base, "mpn": hit, "layer": "rules", "confidence": 1.0, "candidates": [hit],
                               "needs_confirmation": False}, taint={"*": TRUSTED, "query": UNTRUSTED, "mpn": VERIFIED})
        alias = next((e for e in kb.store.entries() if e["kind"] == "alias" and e["status"] == "confirmed"  # ② 已确认别名
                      and e["body"]["alias"]["quote"].strip() == query.strip()), None)  # 精确匹配说法
        if alias:  # 命中
            return CodeResult({**base, "mpn": alias["body"]["mpn"], "layer": "alias", "confidence": 1.0,
                               "candidates": [alias["body"]["mpn"]], "needs_confirmation": False,
                               "memory": alias["entry_id"]}, taint={"*": TRUSTED, "query": UNTRUSTED, "mpn": VERIFIED})
        cands = retrieve(query, mpns)  # 候选
        choice, layer = None, "retrieval"  # 结果
        if cands:  # ③ LLM 消歧（只能从候选里选）
            res = adapter.call("resolver", [Message("system", "你只做一件事：判断用户说的是下列候选中的哪一个，不能给列表外的料号。"
                                                              "拿不准就给低置信度。只输出 JSON。"),
                                            Message("user", f"用户说法：{query}\n候选：{json.dumps(cands)}")],
                               output=ResolveChoice, day=ctx["day"], actor=f"resolver@{ctx['case_id']}",
                               case_id=ctx["case_id"])  # 结构化输出
            choice, layer = res.output, "llm"  # 选择
        ok = (choice is not None and choice.mpn in cands and choice.confidence >= rc["confidence_threshold"])  # 可采信
        content = {**base, "mpn": choice.mpn if ok else None, "layer": layer,  # 结果
                   "confidence": choice.confidence if choice else 0.0, "candidates": cands,  # 置信度与候选
                   "needs_confirmation": not ok,  # 是否交人
                   **({"rejected_choice": choice.mpn} if choice and choice.mpn not in cands else {})}  # 列表外的选择
        if not ok:  # 交采购员确认（H3）
            session.append("human_request", day=ctx["day"], actor=f"resolver@{ctx['case_id']}", case_id=ctx["case_id"],
                           payload={"role": "buyer", "node": "H3", "query": query, "candidates": cands})  # 通知
        return CodeResult(content, taint={"*": TRUSTED, "query": UNTRUSTED,  # 标签
                                          "mpn": VERIFIED if ok else UNTRUSTED})  # 选中的料号在目录里

    return run  # 代码执行者


# ---------------------------------------------------------------------------
# Explainer
# ---------------------------------------------------------------------------
def explainer_code(*, adapter, config: dict, skills: bool):
    audiences = config["explainer"]["audiences"]  # 受众

    def run(task, inputs, ctx) -> CodeResult:  # 代码执行者
        who = ctx["params"].get("audience", "buyer")  # 受众
        if who not in audiences:  # 未知受众
            return CodeResult({"audience": who}, status="unfinished", reason=f"未知受众：{who}")  # 未完成
        facts = [{"artifact": f"{a['artifact_id']}@v{a['version']}", "kind": a["kind"], "content": a["content"]}
                 for a in inputs]  # 输入事实
        system = ("你是 Explainer，只回答：怎么把这些结论讲给这个受众听？只能复述输入里已有的事实与数字。只输出 JSON。"
                  + (("\n\n" + load_skill("explainer")["text"]) if skills else ""))  # 必定加载 Skill
        res = adapter.call("explainer", [Message("system", system),  # LLM 函数
                                         Message("user", f"受众：{audiences[who]}\n任务：{task}\n"
                                                         f"事实：{json.dumps(facts, ensure_ascii=False)}")],
                           output=ExplanationOut, day=ctx["day"], actor=f"explainer@{ctx['case_id']}",
                           case_id=ctx["case_id"])  # 结构化输出
        text = res.output.text  # 说明
        unknown = sorted(numbers_in(text) - numbers_in([a["content"] for a in inputs]))  # 找不到出处的数字
        return CodeResult({"audience": who, "text": text, "unconfirmed": unknown,  # 内容
                           "sources": [f["artifact"] for f in facts]},  # 依据
                          taint={"*": TRUSTED, "text": UNTRUSTED if unknown else VERIFIED})  # 有编造数字即不可信

    return run  # 代码执行者

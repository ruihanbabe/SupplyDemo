"""文件摘要（F23 · 五个 LLM 执行者的注册项）

llm_executors(...) 返回 {执行者名: ExecutorSpec}，并入 F22 派发器注册表。隔离、作用域、引用校验、等级汇总都由代码挂点保证（spec §3.1 原则 4）：
- part_profile（子 Agent）→ PartProfile：工具为文档四件套；作用域 = 原料 + 板级文档；必定加载 PartAnalyst Skill，注入术语与品类比对清单；
  post：每条约束的引用经 F19 校验，不通过标"无法确认"，通过的约束 F13 标已校验；依赖所引 datasheet 版本。
- substitute_search（子 Agent，知识库 B 来源）→ CandidateSet：作用域 = 原料；输出模型不含等级字段。
- verifier（子 Agent）→ VerificationReport：brief 只给 PartProfile 的约束、单个候选与比对清单（没有价格、没有检索过程；消融
  verifier_isolation 关闭时原样给全部输入）；作用域 = 原料 + 候选；必定加载替代料评估 Skill。post：清单外的项忽略、缺的项按证据不足；
  每项的引用逐条校验，不通过降为证据不足（消融 citation_check 关闭时不降）；等级由代码 grade() 按清单汇总，等级字段标可信；
  依赖两颗料 datasheet 的最新版本（F19 改版即失效）。品类没有比对清单时以"未完成"交回。
- resolver、explainer（LLM 函数，代码组件内调用适配层）：见 agents.functions。
依赖：runtime.dispatch、datasheets.cite / store、business.decide.grade、knowledge_base.assets、taint.labels、agents.schemas / functions。
被 orchestrator.assembly 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 输入序列化

from agents.functions import explainer_code, resolver_code  # 两个 LLM 函数
from agents.schemas import CandidateSetOut, PartProfileOut, VerificationOut  # 输出模型
from business.decide import grade  # 等级汇总器
from datasheets.cite import check_citation  # 引用校验
from datasheets.store import latest  # 最新版本
from knowledge_base.assets import Glossary, category_of, checklist, load_skill  # 知识资产
from runtime.dispatch import CodeResult, ExecutorSpec  # 注册项
from taint.labels import TRUSTED, UNTRUSTED, VERIFIED  # 标签


def _first(inputs: list[dict], kind: str) -> dict | None:
    """输入里第一个指定类型的 Artifact。"""
    return next((a for a in inputs if a["kind"] == kind), None)  # 第一个


def _skill(name: str, enabled: bool) -> str:
    """必定加载的 Skill 正文（消融 skill_injection 关闭时为空）。"""
    return ("\n\n" + load_skill(name)["text"]) if enabled else ""  # 正文


def _check_all(store, citations: list[dict], enabled: bool) -> list[str]:
    """一组引用逐条校验，返回原因（关掉校验时恒为空）。"""
    if not enabled:  # 消融
        return []  # 不校验
    return [r for c in citations for r in check_citation(store, c)]  # 逐条


def _cited_deps(citations: list[dict]) -> list[dict]:
    """所引 datasheet 版本 → 外部依赖（去重）。"""
    seen = sorted({(c["mpn"], c["version"]) for c in citations})  # 去重
    return [{"external": f"datasheet:{m}", "version": v} for m, v in seen]  # 依赖


# 功能：见文件摘要。依赖：datasheet 存储、知识库 B、术语表、模型适配层（两个 LLM 函数）、环境取值函数（Resolver 目录）、开关。
def llm_executors(*, store, kb, glossary: Glossary, adapter, get_env, switches: dict, agents_config: dict,
                  session) -> dict[str, ExecutorSpec]:
    iso, cite, skills = switches["verifier_isolation"], switches["citation_check"], switches["skill_injection"]  # 开关

    # ------------------------------------------------------------------ 原料画像
    def profile_brief(task, inputs, params):  # 输入：料号、板、术语、比对清单
        mpn, board = params.get("mpn", ""), params.get("board")  # 料号与板
        cat = category_of(mpn)  # 品类
        cl = checklist(cat) if (cat and skills) else None  # 比对清单
        terms = glossary.inject(f"{task} {' '.join(i['name'] for i in (cl or {}).get('items', []))}", [cat] if cat else None)
        return (f"任务：{task}\n料号：{mpn}\n板：{board or '（未指定）'}（板级文档键 board:{board}）\n"  # 基本信息
                + (f"品类比对清单：{json.dumps(cl['items'], ensure_ascii=False)}\n" if cl else "")  # 清单
                + ("\n".join(terms) + "\n" if terms else "")  # 术语
                + "每条约束写 citation（mpn、version、page，可带 value 或 quote），找不到证据就不写 citation。")  # 要求

    def profile_scope(params, inputs):  # 作用域：原料 + 板级文档
        return [params.get("mpn", "")] + ([f"board:{params['board']}"] if params.get("board") else [])  # 料号

    def profile_post(content, inputs, ctx):  # 引用校验与打标
        taint, cites = {"*": UNTRUSTED}, []  # 标签与引用
        for i, c in enumerate(content["constraints"]):  # 每条约束
            cit = c.get("citation")  # 引用
            why = _check_all(store, [cit], cite) if cit else ["没有引用"]  # 校验
            c["confirmed"] = not why  # 是否确认
            if why:  # 不通过
                c["unconfirmed_reason"] = "；".join(why)  # 写明
            else:  # 通过
                taint[f"constraints[{i}]"] = VERIFIED  # 去污
                cites.append(cit)  # 依赖
        return CodeResult(content, _cited_deps(cites), taint=taint)  # 结果

    # ------------------------------------------------------------------ 替代检索
    def search_brief(task, inputs, params):  # 输入：原料号（只从 PartProfile 或参数取）
        prof = _first(inputs, "PartProfile")  # 画像
        original = params.get("original") or (prof and prof["content"]["mpn"])  # 原料
        return f"任务：{task}\n原料号：{original}\n来源：知识库 B（kb_substitutes）。只列候选，不评等级。"  # 输入

    def search_scope(params, inputs):  # 作用域：原料
        prof = _first(inputs, "PartProfile")  # 画像
        return [params.get("original") or (prof["content"]["mpn"] if prof else "")]  # 原料

    # ------------------------------------------------------------------ Verifier
    def verifier_parts(params, inputs):  # 原料、候选、品类、清单
        prof = _first(inputs, "PartProfile")  # 画像
        original = params.get("original") or (prof["content"]["mpn"] if prof else "")  # 原料
        cat = category_of(original)  # 品类
        return original, params.get("candidate", ""), cat, (checklist(cat) if cat else None)  # 结果

    def verifier_brief(task, inputs, params):  # 结构隔离：只给约束、单个候选、清单
        original, cand, cat, cl = verifier_parts(params, inputs)  # 基本信息
        head = (f"任务：{task}\n原料：{original}\n候选：{cand}\n"  # 两颗料
                + (f"品类比对清单（逐项给结论）：{json.dumps(cl['items'], ensure_ascii=False)}\n" if cl else ""))  # 清单
        if not iso:  # 消融：全部输入原样给
            return head + "输入：\n" + "\n".join(json.dumps(a["content"], ensure_ascii=False) for a in inputs)  # 原样
        prof = _first(inputs, "PartProfile")  # 画像
        cons = [{k: c[k] for k in ("param", "requirement", "hard")} for c in (prof["content"]["constraints"] if prof else [])]
        terms = glossary.inject(" ".join(i["name"] for i in (cl or {}).get("items", [])), [cat] if cat else None)  # 术语
        return (head + f"原料约束：{json.dumps(cons, ensure_ascii=False)}\n" + ("\n".join(terms) + "\n" if terms else "")
                + "每项结论附 citations（原料与候选各自的 datasheet 版本 + 页码 + 数值或片段）；只能从曲线读出的判 insufficient。")

    def verifier_scope(params, inputs):  # 作用域：原料 + 该候选
        original, cand, _, _ = verifier_parts(params, inputs)  # 两颗料
        return [original, cand]  # 只这两颗

    def verifier_post(content, inputs, ctx):  # 引用校验 → 降级 → 代码汇总等级
        original, cand, cat, cl = verifier_parts(ctx["params"], inputs)  # 基本信息
        if cl is None:  # 没有比对清单
            return CodeResult({"candidate": cand, "original": original, "category": cat},  # 未完成
                              status="unfinished", reason=f"品类 {cat or '未分类'} 没有比对清单，无法验证")  # 原因
        by_id = {i["id"]: i for i in content["items"] if i["id"] in {x["id"] for x in cl["items"]}}  # 清单内的项
        items, taint, cites = [], {"*": UNTRUSTED, "grade": TRUSTED}, []  # 结果、标签、引用
        for n, spec_item in enumerate(cl["items"]):  # 按清单逐项
            it = by_id.get(spec_item["id"], {"id": spec_item["id"], "result": "insufficient", "citations": []})  # 缺项
            it = {**it}  # 副本
            why = _check_all(store, it["citations"], cite)  # 引用校验
            if it["result"] != "insufficient" and cite and not it["citations"]:  # 结论没有引用
                why = ["结论没有引用"]  # 原因
            if why:  # 不通过：降为证据不足
                it.update(result="insufficient", downgraded="；".join(why))  # 降级
            else:  # 通过
                taint[f"items[{n}]"] = VERIFIED  # 去污
                cites += it["citations"]  # 依赖
            items.append(it)  # 收下
        g = grade(cl, {i["id"]: i["result"] for i in items})  # 代码汇总等级
        deps = [{"external": f"datasheet:{m}", "version": v["version"]}  # 两颗料的最新 datasheet 版本
                for m in (original, cand) if (v := latest(store, m))]  # 有才依赖
        ignored = sorted({i["id"] for i in content["items"]} - {x["id"] for x in cl["items"]})  # 清单外的项
        return CodeResult({"candidate": cand, "original": original, "category": cat, "items": items,  # 内容
                           "grade": g, "ignored_items": ignored}, deps, taint=taint)  # 等级与标签

    return {
        "part_profile": ExecutorSpec("part_profile", "PartProfile",  # 原料画像
                                     system="你是原料画像子 Agent，只回答：这颗料在这块板上哪些参数是硬约束？只输出 JSON。"
                                     + _skill("part_analyst", skills),
                                     tools=["find_datasheet", "datasheet_toc", "datasheet_search", "datasheet_read_page"],
                                     output=PartProfileOut, brief=profile_brief, scope=profile_scope, post=profile_post),
        "substitute_search": ExecutorSpec("substitute_search", "CandidateSet",  # 替代检索（知识库 B 来源）
                                          system="你是替代检索子 Agent，只回答：这个来源里有哪些候选？不评等级、不看价格。只输出 JSON。",
                                          tools=["kb_substitutes"], output=CandidateSetOut, brief=search_brief,
                                          scope=search_scope),
        "verifier": ExecutorSpec("verifier", "VerificationReport",  # Verifier
                                 system="你是 Verifier，只回答：这个候选在每个比对项上满足吗？证据在哪？不给等级、不找新候选。只输出 JSON。"
                                 + _skill("substitute_evaluation", skills),
                                 tools=["find_datasheet", "datasheet_toc", "datasheet_search", "datasheet_read_page"],
                                 output=VerificationOut, brief=verifier_brief, scope=verifier_scope, post=verifier_post),
        "resolver": ExecutorSpec("resolver", "ResolvedPart",  # Resolver（三层）
                                 code=resolver_code(adapter=adapter, kb=kb, get_env=get_env, config=agents_config,
                                                    session=session)),
        "explainer": ExecutorSpec("explainer", "Explanation",  # Explainer
                                  code=explainer_code(adapter=adapter, config=agents_config, skills=skills)),
    }

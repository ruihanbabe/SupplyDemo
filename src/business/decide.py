"""文件摘要（F21 · Sourcing、Policy、等级汇总器）

- sourcing()：输入需求列表 [{mpn, qty}]（多缺料预留，spec §1.7），经 F07 代理层逐料查报价（缓存、配额、留档、注入照常），
  每条报价推算到货日（有货：今天 + 备货 + 运输天数，取环境分销商参数），不可用来源写入 missing；返回内容与外部数据依赖
  （quote:<分销商>:<料号>，版本 = 取数日，有效期 = 代理层给出的 valid_until），供 F06 失效传播与过期。
- decide()：Policy。AVL / 禁用过滤、须满足数量、单张金额上限；按价格、到货日、库存余量加权打分，每一步写进结果；同分按分销商名。
- grade()：等级汇总器。按品类比对清单的规则（自上而下第一条命中）与 Verifier 逐项结论算出 A / B / C 与理由。
全部纯代码、同输入同输出（spec §3.1 原则 2）。依赖：tools.proxy（仅 sourcing）、PyYAML。被 business.components、F12 门控、F23 Verifier 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from pathlib import Path  # 配置路径

import yaml  # 读配置

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
CONFIG = ROOT / "config"  # 配置目录


def load_yaml(name: str) -> dict:
    """读 config/ 下的 YAML。"""
    return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8"))  # 读取


# ---------------------------------------------------------------------------
# sourcing：比价
# 功能：见文件摘要。
# 依赖：proxy.call("distributor_quotes")；distributors 为环境的分销商物流参数（ship_days / transit_days）。
# 完成：数量不是数字（"unknown"，B3）时不算"能满足"；整个查询失败时该料所有来源记为缺失并附错误类型。
# ---------------------------------------------------------------------------
def sourcing(proxy, demands: list[dict], *, distributors: dict, day: int, case_id: str | None,
             actor: str) -> tuple[dict, list[dict]]:
    items, deps = [], []  # 结果与依赖
    for d in demands:  # 每条需求
        r = proxy.call("distributor_quotes", {"mpns": [d["mpn"]], "qty": d["qty"]},  # 经代理层查报价
                       executor="sourcing", actor=actor, day=day, case_id=case_id)  # 代码组件身份
        if not r["ok"]:  # 整体失败
            items.append({"mpn": d["mpn"], "qty": d["qty"], "offers": [],  # 无报价
                          "missing": sorted(distributors), "error": r["error"]["type"]})  # 全部缺失
            continue  # 下一条
        offers = []  # 报价
        for row in r["data"]:  # 每家
            stock, price = row["stock"], row["unit_price_usd"]  # 库存与单价
            fills = isinstance(stock, int) and stock >= d["qty"]  # 能否满足
            logi = distributors.get(row["distributor"], {})  # 物流参数
            lead = row["lead_days"] if isinstance(row["lead_days"], int) else None  # 交期
            eta = (day + logi["ship_days"] + logi["transit_days"] if fills and logi  # 有货：备货 + 运输
                   else day + lead if lead is not None else None)  # 无货：按交期；未知为空
            offers.append({"distributor": row["distributor"], "stock": stock, "unit_price_usd": price,  # 报价
                           "lead_days": row["lead_days"], "fills": fills, "eta_day": eta})  # 推算
            deps.append({"external": f"quote:{row['distributor']}:{d['mpn']}",  # 外部数据依赖
                         "version": f"day{r['source']['fetched_at']}",  # 版本 = 取数日
                         "valid_until_day": r["source"]["valid_until"]})  # 有效期（模拟日）
        got = {o["distributor"] for o in offers}  # 有返回的来源
        items.append({"mpn": d["mpn"], "qty": d["qty"], "offers": offers,  # 报价
                      "missing": sorted(set(distributors) - got), "record_id": r["source"]["record_id"],  # 缺失与留档
                      "warnings": r["warnings"]})  # 警告
    return {"day": day, "items": items}, deps  # 内容与依赖


# ---------------------------------------------------------------------------
# decide：Policy 决策
# 功能：见文件摘要。
# 依赖：policy 配置（config/policy.yaml）。
# 完成：每条需求输出 chosen（或 None + 原因）、filtered（被过滤的来源与原因）、scores（逐项分数），feasible 为全部需求都有选择。
# ---------------------------------------------------------------------------
def decide(quote: dict, policy: dict) -> dict:
    w = policy["weights"]  # 权重
    out = []  # 每条需求的决策
    for item in quote["items"]:  # 每条需求
        filtered, ok = [], []  # 被过滤、通过
        for o in item["offers"]:  # 每家报价
            price = o["unit_price_usd"]  # 单价
            reason = (None if o["distributor"] in policy["avl"] else "不在 AVL")  # AVL
            reason = reason or ("禁用分销商" if o["distributor"] in policy["banned"] else None)  # 禁用
            reason = reason or (None if isinstance(price, (int, float)) else "单价未知")  # 单价须已知
            reason = reason or ("现货不足" if policy["require_full_fill"] and not o["fills"] else None)  # 数量
            reason = reason or ("到货日未知" if o["eta_day"] is None else None)  # 到货日须已知
            amount = round(price * item["qty"], 4) if isinstance(price, (int, float)) else None  # 金额
            reason = reason or ("超过单张金额上限" if amount > policy["max_po_amount_usd"] else None)  # 金额上限
            (filtered if reason else ok).append({**o, "amount_usd": amount, **({"reason": reason} if reason else {})})
        scores = []  # 打分
        if ok:  # 有可行报价
            pmin = min(o["unit_price_usd"] for o in ok)  # 最低价
            emin = min(o["eta_day"] for o in ok)  # 最早到货
            for o in ok:  # 每家
                s_price = round(pmin / o["unit_price_usd"], 4)  # 价格分
                s_eta = round(emin / o["eta_day"], 4) if o["eta_day"] else 1.0  # 到货分
                s_stock = round(min(1.0, o["stock"] / (item["qty"] * policy["stock_margin_target"])), 4)  # 余量分
                total = round(w["price"] * s_price + w["eta"] * s_eta + w["stock_margin"] * s_stock, 4)  # 加权
                scores.append({"distributor": o["distributor"], "price": s_price, "eta": s_eta,  # 分项
                               "stock_margin": s_stock, "total": total})  # 总分
        best = max(scores, key=lambda s: (s["total"], [-ord(c) for c in s["distributor"]]), default=None)  # 同分按名
        chosen = next((o for o in ok if best and o["distributor"] == best["distributor"]), None)  # 选中
        out.append({"mpn": item["mpn"], "qty": item["qty"], "chosen": chosen,  # 决策
                    "reason": "按配置权重得分最高" if chosen else "无可行报价",  # 理由
                    "filtered": [{"distributor": f["distributor"], "reason": f["reason"]} for f in filtered],  # 过滤
                    "scores": sorted(scores, key=lambda s: s["distributor"]),  # 分数
                    "missing": item.get("missing", [])})  # 缺失来源
    return {"policy_version": policy["version"], "decisions": out,  # 版本与决策
            "feasible": bool(out) and all(d["chosen"] for d in out)}  # 是否全部可行


# ---------------------------------------------------------------------------
# grade：等级汇总器
# 功能：见文件摘要。
# 依赖：config/checklists/<品类>.yaml。
# 完成：清单里没有结论的项按"证据不足"算；返回 grade、命中的规则与涉及的项。
# ---------------------------------------------------------------------------
def grade(checklist: dict, results: dict[str, str]) -> dict:
    items = {i["id"]: i for i in checklist["items"]}  # 比对项
    normalized = {k: results.get(k, "insufficient") for k in items}  # 缺结论 = 证据不足
    unknown = sorted(set(results) - set(items))  # 清单外的项
    for rule in checklist["rules"]:  # 自上而下
        hit = [k for k, v in normalized.items()  # 命中的项
               if items[k]["severity"] == rule["when"]["severity"] and v == rule["when"]["result"]]  # 条件
        if hit:  # 命中
            return {"grade": rule["grade"], "rule": rule["when"], "items": hit, "ignored": unknown,  # 等级与理由
                    "results": normalized}  # 逐项结论
    return {"grade": checklist["default_grade"], "rule": None, "items": [], "ignored": unknown,  # 默认等级
            "results": normalized}  # 逐项结论

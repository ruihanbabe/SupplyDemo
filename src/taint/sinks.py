"""文件摘要（F13 · 汇点检查与疑似指令检测）

TaintSinks 按 config/taint.yaml 检查数据能否流入某个汇点（spec §5.5 汇点表），返回拒绝原因列表（空 = 通过）：
- po_content(labels)：采购单字段（料号、数量、供应商、单价）只能取可信 / 外部结构化 / 已校验。
- policy_inputs(quote_artifact)：Policy 打分读入的报价字段不得为不可信。
- memory_entry(entry, taint)：长期记忆只接受带 H1 确认记录的条目；不可信文本必须是引用块结构 {quote, source}。
- outbound(url, label, policy)：从不可信内容读出的网址须过 F11 出站白名单（可信来源的网址同样过白名单，只是原因不同）。
消融开关 taint_tracking 关闭时所有汇点都放行（照常打标，便于 Eval 对比拦截率）。
InjectionDetector：按配置正则在文本里找疑似指令（B1 兜底），只报告、不改数据。
load_taint_config()：读配置。
依赖：taint.labels、permissions.egress（出站）、PyYAML。被 business（采购单、Policy）、tools.proxy（检测）、F20（记忆）、F19（下载）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import re  # 检测正则
from pathlib import Path  # 配置路径

import yaml  # 读配置

from taint.labels import UNTRUSTED, label_of  # 标签

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
TAINT_CONFIG = ROOT / "config" / "taint.yaml"  # 默认配置


def load_taint_config(path: Path = TAINT_CONFIG) -> dict:
    """读污点追踪配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


# ---------------------------------------------------------------------------
# TaintSinks：汇点检查
# 功能：见文件摘要。
# 依赖：配置 sinks 段；消融开关。
# 完成：原因写明字段与标签，直接作为 gate_rejected 或"未完成"的原因。
# ---------------------------------------------------------------------------
class TaintSinks:
    def __init__(self, config: dict | None = None, *, enabled: bool = True):
        self.config = config or load_taint_config()  # 配置
        self.enabled = enabled  # 消融开关 taint_tracking

    def po_content(self, labels: dict[str, str]) -> list[str]:
        """采购单字段 → 标签；不在允许标签内的字段逐个给原因。"""
        if not self.enabled:  # 消融
            return []  # 放行
        rule = self.config["sinks"]["po_content"]  # 规则
        return [f"采购单字段 {f} 来源为 {labels.get(f, UNTRUSTED)}，只接受 {'/'.join(rule['allowed'])}"  # 原因
                for f in rule["fields"] if labels.get(f, UNTRUSTED) not in rule["allowed"]]  # 逐字段

    def policy_inputs(self, quote: dict) -> list[str]:
        """SourcingQuote 里每条报价的打分字段不得为禁用标签。"""
        if not self.enabled:  # 消融
            return []  # 放行
        rule = self.config["sinks"]["policy_scoring"]  # 规则
        taint = (quote.get("meta") or {}).get("taint") or {}  # 标签表
        reasons = []  # 原因
        for i, item in enumerate(quote["content"].get("items", [])):  # 每条需求
            for j, _ in enumerate(item.get("offers", [])):  # 每家报价
                for f in rule["fields"]:  # 打分字段
                    label = label_of(taint, f"items[{i}].offers[{j}].{f}")  # 标签
                    if label in rule["forbidden"]:  # 禁用
                        reasons.append(f"Policy 不读取不可信字段：items[{i}].offers[{j}].{f}（{label}）")  # 原因
        return reasons  # 结果

    def memory_entry(self, entry: dict, taint: dict) -> list[str]:
        """长期记忆条目：须有指定人工节点的确认记录；不可信的文本字段须为引用块结构。"""
        if not self.enabled:  # 消融
            return []  # 放行
        node = self.config["sinks"]["memory"]["confirmation_node"]  # 确认节点
        reasons = []  # 原因
        conf = entry.get("confirmation") or {}  # 确认记录
        if conf.get("node") != node or not conf.get("by") or not conf.get("event_seq"):  # 缺确认
            reasons.append(f"长期记忆只接受带 {node} 人工确认记录（node、by、event_seq）的条目")  # 原因
        for k, v in entry.items():  # 每个字段
            if k != "confirmation" and label_of(taint, k) == UNTRUSTED and isinstance(v, str):  # 裸露的不可信文本
                reasons.append(f"字段 {k} 为不可信文本，只能以引用块 {{quote, source}} 存放")  # 原因
        return reasons  # 结果

    def outbound(self, url: str, label: str, policy) -> list[str]:
        """出站请求：网址须过出站白名单；不可信来源的网址在原因里标明来源。"""
        reason = policy.check_url(url)  # F11 出站检查（白名单本身不受消融影响）
        if reason is None:  # 通过
            return []  # 放行
        origin = "不可信内容中读出的网址" if label == UNTRUSTED else "网址"  # 来源说明
        return [f"{origin}被拒：{reason}"]  # 原因


# ---------------------------------------------------------------------------
# InjectionDetector：疑似指令检测
# 功能：递归扫描数据里的字符串，命中配置正则即报告字段路径与命中片段。
# 依赖：配置 injection_patterns。被 ToolProxy 在交付给模型前调用（结果只进警告与事件）。
# 完成：不分大小写；片段截断到 40 字符，避免把大段外部文本复制进事件。
# ---------------------------------------------------------------------------
class InjectionDetector:
    def __init__(self, patterns: list[str]):
        self.patterns = [re.compile(p, re.IGNORECASE) for p in patterns]  # 编译

    def scan(self, data, path: str = "") -> list[dict]:
        """返回 [{path, match}]。"""
        if isinstance(data, dict):  # 字典
            return [h for k, v in data.items() for h in self.scan(v, f"{path}.{k}" if path else str(k))]  # 逐项
        if isinstance(data, list):  # 列表
            return [h for i, v in enumerate(data) for h in self.scan(v, f"{path}[{i}]")]  # 逐项
        if isinstance(data, str):  # 文本
            for p in self.patterns:  # 每个模式
                m = p.search(data)  # 搜索
                if m:  # 命中
                    return [{"path": path, "match": m.group(0)[:40]}]  # 一个字段报一次
        return []  # 无命中

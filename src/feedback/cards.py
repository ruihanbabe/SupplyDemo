"""文件摘要（F14 · 反馈卡片与重规划分级）

spec §5.7"统一反馈卡片"：由代码生成，不用 LLM 总结；只给事实与合法下一步，不给堆栈、不给模型自评。
- level(event, config)：按配置把信号分为 must（必须重规划，立即唤醒）/ batch（汇总通知）/ None（只记录）；
  task_finished 只有未完成 / 失败才算信号。
- make_card(session, event, config)：卡片文本。格式：
    【类型】对象
    发生了什么：……
    影响：<本案 Artifact 最新版本中已失效的> 已失效；<仍有效的> 仍有效
    可选下一步：…… / ……
    需要重规划：是|否
- load_feedback_config()：读 config/feedback.yaml。
依赖：session.log、PyYAML。被 feedback.loop 使用；F16 上下文把 feedback 事件的 text 原样放进"新反馈"分区。
"""
from __future__ import annotations  # 类型注解延迟求值

from pathlib import Path  # 配置路径

import yaml  # 读配置

from session.log import ART_INVALID, ART_VALID, Session  # 事件日志与状态

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
FEEDBACK_CONFIG = ROOT / "config" / "feedback.yaml"  # 默认配置
ACTIONS = {"draft": "建采购单草稿", "request_approval": "送审", "submit": "提交采购单",  # 门控动作的中文名
           "report_to_planner": "上报计划员"}  # 上报


def load_feedback_config(path: Path = FEEDBACK_CONFIG) -> dict:
    """读反馈与升级配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


def level(event: dict, config: dict) -> str | None:
    """信号分级：must / batch / None。"""
    lv = config["levels"].get(event["type"])  # 配置分级
    if event["type"] == "task_finished" and event["payload"].get("status") == "done":  # 正常完成的子任务
        return None  # 已在批次汇总里交回，不另发卡片
    return lv  # 分级


def _what(event: dict) -> tuple[str, str]:
    """(对象, 发生了什么)：按事件类型从 payload 取事实。"""
    p, t = event["payload"], event["type"]  # 内容与类型
    if t == "gate_rejected":  # 门控拒绝
        return (f"{ACTIONS.get(p['action'], p['action'])} {p.get('artifact') or ''}".strip(),  # 对象
                "；".join(p["reasons"]))  # 原因
    if t == "invalidation_notice":  # 失效通知
        root = p.get("root", {})  # 根因
        cause = root.get("ref") and f"{root['ref']} 变为 {root.get('new_version')}" or root.get("kind", "")  # 根因描述
        return "、".join(p["artifacts"]), f"{cause}，依赖它的 Artifact 失效"  # 对象与事实
    if t in ("claim_conflict", "deviation"):  # 冲突与偏差：payload 自带说明
        return p.get("po_id") or f"{p.get('distributor', '')} {p.get('mpn', '')}".strip(), p.get("message") or p.get("text", "")
    if t == "task_finished":  # 子任务未完成
        return p["task_id"], f"状态 {p['status']}：{p['reason']}"  # 状态与原因
    if t == "human_decision":  # 人工应答
        return p["artifact"], f"{p['node']} {p['by']} {'批准' if p['decision'] == 'approve' else '否决'}{('：' + p['note']) if p.get('note') else ''}"
    return t, str(p)  # 兜底


def _impact(session: Session, case_id: str) -> str:
    """影响：本案每个 Artifact 最新版本的状态（已失效 / 仍有效）。"""
    status = session.artifact_status()  # 状态表
    latest: dict[str, dict] = {}  # ID → 最新版本
    for a in session.store.artifacts(session.run_id):  # 全部版本
        if a["case_id"] == case_id and a["version"] >= latest.get(a["artifact_id"], {}).get("version", 0):  # 本案更高版本
            latest[a["artifact_id"]] = a  # 记下
    bad = [f"{a['artifact_id']}@v{a['version']}" for a in latest.values()  # 已失效
           if status[(a["artifact_id"], a["version"])] == ART_INVALID]  # 过滤
    good = [a["artifact_id"] for a in latest.values() if status[(a["artifact_id"], a["version"])] == ART_VALID]  # 仍有效
    return f"{'、'.join(bad) or '无'} 已失效；{'、'.join(good) or '无'} 仍有效"  # 一行


def make_card(session: Session, event: dict, config: dict) -> str:
    """生成一张卡片（纯代码）。"""
    spec = config["cards"][event["type"]]  # 标题与下一步
    obj, what = _what(event)  # 对象与事实
    must = level(event, config) == "must"  # 是否必须重规划
    return "\n".join([f"【{spec['title']}】{obj}",  # 标题
                      f"发生了什么：{what}（第{event['day']}天）",  # 事实
                      f"影响：{_impact(session, event['case_id'])}",  # 影响
                      f"可选下一步：{' / '.join(spec['next'])}",  # 合法下一步
                      f"需要重规划：{'是' if must else '否'}"])  # 分级

"""文件摘要（F22 · 任务图、计划校验、固定模板兜底、两级目标修订）

- validate_plan(tasks, mode, registry, config, remaining_llm)：spec §2.7 代码校验——任务号唯一；执行者在当前模式节点集合且已注册；
  依赖存在且无环；输入引用格式正确；按配置节点单价估算的 LLM 调用数不超过案子剩余预算。返回原因列表（空 = 合格）。
- Planner.write(...)：write_plan 工具的实现。合格 → 产出 Plan Artifact（Plan:<案子>，新版本；已有计划时先迁入"规划中"再回"执行中"，
  计入重规划预算）并写 plan_written；不合格 → plan_rejected；自上一次成功写计划以来连续被拒达配置次数 → 退回该模式的固定模板
  （plan_fallback）。消融 plan_validation 关闭时不校验、不退回。Plan 是编排者产出，按 F13 标为不可信。
- template_tasks(mode, config)：固定模板任务（F22 兜底与 §3.6 对照组共用）。
- GoalDesk：两级目标修订的第二级（spec §2.5）。propose 写 goal_revision_proposed 并通知采购员（H4）；confirm 写 H4 人工决定与
  goal_revised（案子卡片显示新目标，F14 按人工应答唤醒重规划）；reject 写人工决定，目标不变。current_goal 取最新的已确认目标。
- load_orchestrator_config()：读 config/orchestrator.yaml。
依赖：session.log、taint.labels、PyYAML。被 runtime.segment（控制工具）、orchestrator.template / assembly、F16 上下文（当前目标）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import re  # 引用格式
from pathlib import Path  # 配置路径

import yaml  # 读配置

from session.log import ART_VALID, CASE_EXECUTING, CASE_PLANNING, Session  # 事件日志与状态
from taint.labels import ALL_UNTRUSTED  # F13：LLM 产出不可信

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
ORCHESTRATOR_CONFIG = ROOT / "config" / "orchestrator.yaml"  # 默认配置
REF = re.compile(r"^.+@v\d+$")  # Artifact 引用


def load_orchestrator_config(path: Path = ORCHESTRATOR_CONFIG) -> dict:
    """读编排者配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


def template_tasks(mode: str, config: dict) -> list[dict]:
    """该模式的固定模板任务（副本）。"""
    return [dict(t, inputs=list(t.get("inputs", []))) for t in config["templates"][mode]]  # 副本


# 功能：计划校验（代码）。依赖：模式节点集合、注册表、节点单价。完成：原因逐条写明哪个任务哪里不对。
def validate_plan(tasks: list[dict], *, mode: str, registry: set[str], config: dict, remaining_llm: int) -> list[str]:
    reasons, ids = [], [t["id"] for t in tasks]  # 原因与任务号
    dup = sorted({i for i in ids if ids.count(i) > 1})  # 重复任务号
    if dup:  # 有
        reasons.append(f"任务号重复：{', '.join(dup)}")  # 原因
    nodes = set(config["modes"][mode]["nodes"])  # 当前模式节点集合
    for t in tasks:  # 每个任务
        if t["executor"] not in nodes:  # 不在节点集合
            reasons.append(f"{t['id']}：节点 {t['executor']} 不在 {mode} 模式的节点集合里（可用：{', '.join(sorted(nodes))}）")
        elif t["executor"] not in registry:  # 在集合里但本期没有实现
            reasons.append(f"{t['id']}：节点 {t['executor']} 尚未接入")  # 原因
        missing = [d for d in t.get("depends_on", []) if d not in ids]  # 依赖不存在
        if missing:  # 有
            reasons.append(f"{t['id']}：依赖的任务不存在：{', '.join(missing)}")  # 原因
        bad = [r for r in t.get("inputs", []) if not REF.match(r)]  # 输入引用格式不对
        if bad:  # 有
            reasons.append(f"{t['id']}：输入须为 Artifact 引用（ID@v版本）：{', '.join(bad)}")  # 原因
    deps = {t["id"]: [d for d in t.get("depends_on", []) if d in ids] for t in tasks}  # 依赖图
    state: dict[str, int] = {}  # 0 访问中、1 完成

    def cyclic(n: str) -> bool:  # 深度优先找环
        if state.get(n) == 0:  # 回到访问中的节点
            return True  # 有环
        if state.get(n) == 1:  # 已完成
            return False  # 无环
        state[n] = 0  # 访问中
        found = any(cyclic(d) for d in deps.get(n, []))  # 递归
        state[n] = 1  # 完成
        return found  # 结果

    if any(cyclic(i) for i in deps):  # 有环
        reasons.append("依赖关系成环，任务无法开始")  # 原因
    cost = sum(config["plan"]["node_cost"].get(t["executor"], 0) for t in tasks)  # 预估 LLM 调用数
    if cost > remaining_llm:  # 超预算
        reasons.append(f"预估 {cost} 次 LLM 调用，超过案子剩余预算 {remaining_llm} 次")  # 原因
    return reasons  # 结果


class Planner:
    # 功能：见文件摘要。依赖：session、配置、注册表（可派发的执行者名集合）、案子 LLM 预算、校验开关。
    def __init__(self, session: Session, *, registry: set[str], max_llm_calls: int, config: dict | None = None,
                 validate: bool = True):
        self.session, self.registry = session, registry  # 依赖
        self.max_llm = max_llm_calls  # 案子 LLM 调用上限
        self.config = config or load_orchestrator_config()  # 配置
        self.validate = validate  # 消融开关 plan_validation

    def _produce(self, case_id: str, tasks: list[dict], *, day: int, actor: str, source: str) -> str:
        """产出 Plan 新版本；已有有效计划时先迁入规划中再回执行中（计入重规划）。返回引用。"""
        status = self.session.artifact_status()  # 状态
        had = any(a["artifact_id"] == f"Plan:{case_id}" and status[(a["artifact_id"], a["version"])] == ART_VALID
                  for a in self.session.store.artifacts(self.session.run_id))  # 已有有效计划
        if had and self.session.case_state(case_id)["state"] == CASE_EXECUTING:  # 重规划
            self.session.transition(case_id, CASE_PLANNING, day=day, actor=actor, reason="重规划：写新计划")  # 规划中
            self.session.transition(case_id, CASE_EXECUTING, day=day, actor=actor, reason="新计划生效")  # 回执行中
        art = self.session.produce(f"Plan:{case_id}", "Plan", {"tasks": tasks, "source": source}, day=day,  # 计划
                                   actor=actor, case_id=case_id, meta={"taint": ALL_UNTRUSTED})  # 编排者产出
        ref = f"{art['artifact_id']}@v{art['version']}"  # 引用
        self.session.append("plan_written", day=day, actor=actor, case_id=case_id,  # 事件
                            payload={"plan": ref, "replan": had, "source": source, "tasks": len(tasks)})  # 内容
        return ref  # 引用

    def write(self, token, tasks: list[dict], *, day: int) -> dict:
        """write_plan 工具：校验 → 产出 / 拒绝 / 退回模板。"""
        case_id, actor, mode = token.case_id, token.actor, token.mode  # 案子、身份、模式
        if self.validate:  # 校验
            used = len(self.session.events(case_id=case_id, type="llm_call"))  # 已用 LLM 调用
            reasons = validate_plan(tasks, mode=mode, registry=self.registry, config=self.config,  # 校验
                                    remaining_llm=self.max_llm - used)  # 剩余预算
            if reasons:  # 不合格
                self.session.append("plan_rejected", day=day, actor="plan_validator", case_id=case_id,  # 拒绝
                                    payload={"reasons": reasons})  # 原因
                last_ok = max((e["seq"] for e in self.session.events(case_id=case_id)  # 上一次成功写计划
                               if e["type"] in ("plan_written",)), default=0)  # 事件号
                streak = sum(1 for e in self.session.events(case_id=case_id, type="plan_rejected") if e["seq"] > last_ok)
                if streak >= self.config["plan"]["max_rejections"]:  # 连续被拒达上限：退回固定模板
                    ref = self._produce(case_id, template_tasks(mode, self.config), day=day, actor="plan_validator",
                                        source="template")  # 模板计划
                    self.session.append("plan_fallback", day=day, actor="plan_validator", case_id=case_id,  # 退回
                                        payload={"plan": ref, "mode": mode, "rejections": streak})  # 内容
                    return {"accepted": False, "fallback": ref, "reasons": reasons,  # 告诉编排者
                            "note": f"连续 {streak} 次计划不合格，已退回 {mode} 固定模板计划 {ref}，请按它执行"}
                return {"accepted": False, "reasons": reasons}  # 交回重写
        return {"accepted": True, "plan": self._produce(case_id, tasks, day=day, actor=actor, source="orchestrator")}


class GoalDesk:
    # 功能：见文件摘要。依赖：session。完成：未确认前目标不变；确认 / 驳回都写 H4 人工决定。
    def __init__(self, session: Session):
        self.session = session  # 事件日志

    def propose(self, *, case_id: str, day: int, actor: str, new_goal: str, reason: str) -> dict:
        """提出目标修订（交采购员确认）。"""
        if not new_goal or not reason:  # 缺内容
            return {"accepted": False, "error": "目标修订须写新目标与理由"}  # 拒绝
        ev = self.session.append("goal_revision_proposed", day=day, actor=actor, case_id=case_id,  # 提议
                                 payload={"new_goal": new_goal, "reason": reason, "old_goal": self.current_goal(case_id)})
        self.session.append("human_request", day=day, actor=actor, case_id=case_id,  # 通知采购员
                            payload={"role": "buyer", "node": "H4", "proposal_seq": ev["seq"]}, caused_by=ev["seq"])
        return {"accepted": True, "proposal_seq": ev["seq"], "note": "已交采购员确认；确认前按原目标继续"}  # 接受

    def _decide(self, proposal_seq: int, decision: str, *, by: str, day: int) -> dict:
        """写 H4 人工决定。"""
        ev = self.session.store.event(self.session.run_id, proposal_seq)  # 提议
        if ev is None or ev["type"] != "goal_revision_proposed":  # 不是提议
            raise ValueError(f"事件 #{proposal_seq} 不是目标修订提议")  # 报错
        dec = self.session.append("human_decision", day=day, actor=f"human:{by}", case_id=ev["case_id"],  # 人工决定
                                  payload={"node": "H4", "artifact": f"proposal#{proposal_seq}", "decision": decision,
                                           "by": by, "note": ""}, caused_by=proposal_seq)  # 内容
        return {"proposal": ev, "decision": dec}  # 返回

    def confirm(self, proposal_seq: int, *, by: str, day: int) -> dict:
        """采购员确认：目标生效。"""
        d = self._decide(proposal_seq, "approve", by=by, day=day)  # 人工决定
        p = d["proposal"]["payload"]  # 提议内容
        return self.session.append("goal_revised", day=day, actor=f"human:{by}", case_id=d["proposal"]["case_id"],  # 生效
                                   payload={"new_goal": p["new_goal"], "reason": p["reason"],  # 新目标与理由
                                            "proposal_seq": proposal_seq}, caused_by=d["decision"]["seq"])  # 因果

    def reject(self, proposal_seq: int, *, by: str, day: int) -> dict:
        """采购员驳回：目标不变。"""
        return self._decide(proposal_seq, "reject", by=by, day=day)["decision"]  # 人工决定

    def current_goal(self, case_id: str) -> str:
        """当前目标：最新的已确认修订，否则开案目标。"""
        revised = self.session.events(case_id=case_id, type="goal_revised")  # 已确认的修订
        if revised:  # 有
            return revised[-1]["payload"]["new_goal"]  # 最新
        return self.session.events(case_id=case_id, type="case_opened")[0]["payload"].get("goal", "（未写）")  # 开案目标

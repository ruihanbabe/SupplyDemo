"""文件摘要（F22 · 固定模板驱动器）

spec §2.7 与 §3.6"固定模板"对照配置：同样的执行者，但计划固定，不允许重规划和目标修订。
TemplateDriver.run(case_id, day)：写出该模式的模板计划（Plan，source=template）→ 按依赖顺序逐个派发模板任务（输入 = 依赖任务的产出）
并立即汇合 → S1 在策略决策之后对每颗有可行选择的料建草稿并送审（经采购执行器，门控照旧）。不用 LLM，只能跑已注册的节点。
返回 {plan, artifacts: 任务号 → 引用, drafts, approvals, skipped}。
依赖：orchestrator.planning、runtime.dispatch、business.procurement、session.log。被 Eval 对照组（F27/F28）与测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from orchestrator.planning import load_orchestrator_config, template_tasks  # 模板
from session.log import Session  # 事件日志


class TemplateDriver:
    # 功能：见文件摘要。依赖：事件日志、派发器、采购执行器、签发者（编排者身份）、配置。
    def __init__(self, session: Session, dispatcher, executor, authority, *, config: dict | None = None):
        self.session, self.dispatcher, self.executor = session, dispatcher, executor  # 依赖
        self.authority = authority  # F11 签发者
        self.config = config or load_orchestrator_config()  # 配置

    def run(self, case_id: str, *, day: int) -> dict:
        opened = self.session.events(case_id=case_id, type="case_opened")[0]  # 开案事件
        mode = opened["payload"].get("mode") or self.authority.config["default_mode"]  # 模式
        token = self.authority.issue_root(executor="orchestrator", case_id=case_id, mode=mode,  # 编排者身份
                                          initiated_by=f"template:{opened['actor']}#{opened['seq']}")  # 发起者
        tasks = template_tasks(mode, self.config)  # 模板任务
        plan = self.session.produce(f"Plan:{case_id}", "Plan", {"tasks": tasks, "source": "template"}, day=day,  # 模板计划
                                    actor="template", case_id=case_id, meta={"taint": {"*": "trusted"}})  # 代码产出
        refs, skipped = {}, []  # 任务号 → 产出引用；跳过的任务
        for t in tasks:  # 模板顺序即依赖顺序
            if t["executor"] not in self.dispatcher.registry:  # 本期未接入的节点
                skipped.append(t["id"])  # 跳过
                continue  # 下一个
            inputs = [refs[d] for d in t["depends_on"] if d in refs]  # 输入 = 依赖的产出
            out = self.dispatcher.dispatch(parent=token, day=day, executor=t["executor"], task=t["task"],  # 派发
                                           inputs=inputs, reason=t["reason"], plan_task=t["id"])  # 计划任务号
            if not out["accepted"]:  # 派发被拒
                skipped.append(t["id"])  # 跳过
                continue  # 下一个
            self.dispatcher.run_pending(case_id=case_id, day=day)  # 立即汇合
            refs[t["id"]] = self.session.events(case_id=case_id, type="task_finished")[-1]["payload"]["artifact"]
        drafts, approvals = [], []  # 草稿与送审
        decision = next((refs[t["id"]] for t in reversed(tasks) if t["executor"] == "policy" and t["id"] in refs), None)
        if mode == "S1" and decision is not None:  # 处置模式：按决策逐料建草稿并送审
            aid, _, ver = decision.rpartition("@v")  # 拆引用
            for d in self.session.artifact_view(aid, int(ver))["content"].get("decisions", []):  # 每颗料
                if not d.get("chosen"):  # 没有可行选择
                    continue  # 跳过
                r = self.executor.create_draft(case_id=case_id, day=day, decision_ref=decision, mpn=d["mpn"])  # 草稿
                if r["ok"]:  # 过门控
                    drafts.append(r["draft"])  # 记下
                    if self.executor.request_approval(case_id=case_id, day=day, draft_ref=r["draft"])["ok"]:  # 送审
                        approvals.append(r["draft"])  # 记下
        return {"plan": f"{plan['artifact_id']}@v{plan['version']}", "artifacts": refs,  # 计划与产出
                "drafts": drafts, "approvals": approvals, "skipped": skipped}  # 草稿、送审、跳过

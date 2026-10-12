"""文件摘要（F09 · 活跃段与调度循环）

- SegmentRunner.run()：一次唤醒跑一段（spec §5.3）。先查案子级预算（重规划、LLM 调用总数，从日志投影，触顶升级给人并关闭）→
  状态迁移（开启 → 规划中 → 执行中；等待 → 执行中）→ 从日志与 Artifact 现组装编排者上下文（M1 最小构建器，F16 替换）→
  编排者循环（每轮后运行本轮派发的任务批次并一次交回）→ end_segment(wait_for) 或预算触顶 / 失败 → 案子进入等待。
  进程内不保存半截对话；段中崩溃则整段重跑。
  F22：write_plan 交给 Planner（校验、重规划计数、退回固定模板）；request_human 按 kind 转目标修订（GoalDesk，H4 确认）、
  上报计划员（F12 门控）或提议记忆（F20）；编排者工具集按案子模式取配置（S1 8 个、S3 6 个）。
  F20：propose_memory 交给知识库 B 生成提议并通知 H1 / H3（未接入时明确拒绝）。
  F14：escalate_to_human 走代码升级路径（关闭案子、通知采购员）并结束本段；段结束时案子已关闭则不再迁入等待。
  F12：report_to_planner 先过上报计划员门控（须附本案有效 ImpactReport，理由里的数字须出自它），不通过写 gate_rejected。
  F11：每段开始由 Authority 给编排者签根令牌（模式取开案内容 mode，缺省按权限配置；发起者 = 开案事件的执行者与序号）。
- handle_control()：编排控制工具的实现（经 F07 代理层转交）：派任务、结束本段；目标修订 / 上报计划员 / 问用户在 M1 只记为
  human_request 事件（语义归 F22/F05/F25）。
- Scheduler.run_day()：推进环境 → env_day 事件（F10：实体键含料号与板，由 cases.monitor.env_day_keys 生成） → 路由挂点（F10）+ 到期定时器 + 排队案子 → 按固定顺序运行被唤醒的案子，
  已开始处置且未关闭的案子数有上限，超出的新案子排队 → 没有可运行案子时本日结束。
依赖：runtime.loop、runtime.dispatch、session.log、simenv.api、permissions.tokens。被 F10（路由）、F22（编排者）、F27（Eval 运行器）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 上下文摘要
from collections.abc import Callable  # 挂点类型

from cases.monitor import env_day_keys  # F10 环境事件实体键
from feedback.escalation import Escalation  # F14 升级
from gate.evidence import planner_report_gate  # F12 上报计划员门控
from permissions.tokens import Authority  # F11 根令牌签发
from runtime.dispatch import Dispatcher  # 派发与汇合
from runtime.loop import ExecutorLoop  # 执行者循环
from session.log import (  # 事件日志与状态常量
    CASE_CLOSED,  # 关闭
    CASE_EXECUTING,  # 执行中
    CASE_OPEN,  # 开启
    CASE_PLANNING,  # 规划中
    CASE_WAITING,  # 等待
    EV_CASE_TRANSITION,  # 迁移事件
    Session,  # 门面
)
from simenv.api import Environment  # 模拟环境

HUMAN_CONTROLS = {"propose_goal_revision": "goal_revision", "report_to_planner": "planner",  # 需要人的控制工具
                  "ask_user": "buyer"}  # → human_request 的对象


# ---------------------------------------------------------------------------
# minimal_context：M1 最小上下文构建器（F16 替换为完整分区组装）
# 功能：从日志与 Artifact 现组装编排者的系统提示与用户消息，不沿用任何对话。
# 依赖：session 的案子事件与 Artifact 投影。
# 完成：案子卡片（开案内容）、有效 Artifact 一行摘要、已失效 Artifact 一行、本次唤醒原因。
# ---------------------------------------------------------------------------
def minimal_context(session: Session, case_id: str, reason: str) -> tuple[str, str]:
    opened = session.events(case_id=case_id, type="case_opened")[0]  # 开案事件
    status = session.artifact_status()  # Artifact 状态
    arts = [a for a in session.store.artifacts(session.run_id) if a["case_id"] == case_id]  # 本案 Artifact
    valid = [f"{a['artifact_id']}@v{a['version']}（{a['kind']}）" for a in arts  # 有效
             if status[(a["artifact_id"], a["version"])] == "valid"]  # 过滤
    invalid = [f"{a['artifact_id']}@v{a['version']}" for a in arts  # 已失效
               if status[(a["artifact_id"], a["version"])] == "invalid"]  # 过滤
    system = (f"你是案子 {case_id} 的编排者。用控制工具派任务（可一次派多个）、在所有任务都等外部结果时结束本段。"  # 角色
              "数值只能来自工具与 Artifact，不得编造。")  # G4
    user = (f"[案子卡片] {json.dumps(opened['payload'], ensure_ascii=False)}\n"  # 卡片
            f"[有效] {'；'.join(valid) or '无'}\n[已失效] {'；'.join(invalid) or '无'}\n"  # Artifact
            f"[本次唤醒] {reason}")  # 原因
    return system, user  # 两段


class SegmentRunner:
    # 功能：见文件摘要。依赖：执行者循环、派发器、预算配置、上下文构建器。
    # 完成：编排者工具清单由调用方给（F11 以后按能力令牌收窄）。
    def __init__(self, session: Session, loop: ExecutorLoop, dispatcher: Dispatcher, *, budgets: dict,
                 orchestrator_tools: list[str] | None, authority: Authority, escalation: Escalation | None = None,
                 knowledge=None, planner=None, goals=None, tools_by_mode: dict[str, list[str]] | None = None,
                 context_builder: Callable[[Session, str, str], tuple[str, str]] = minimal_context):
        self.session, self.loop, self.dispatcher = session, loop, dispatcher  # 依赖
        self.budgets = budgets  # runtime 配置段
        self.tools = orchestrator_tools  # 编排者工具（None = 令牌可见的全部；实际再 ∩ L1 可见，F11）
        self.tools_by_mode = tools_by_mode  # F22：按模式的编排者工具集（config/orchestrator.yaml，优先于上一项）
        self.authority = authority  # F11 令牌签发者
        self.escalation = escalation or Escalation(session)  # F14 升级（工具用 escalate，不受强制升级开关影响）
        self.knowledge = knowledge  # F20 知识库 B（propose_memory；None = 未接入）
        self.planner = planner  # F22 任务图与计划校验（write_plan；None = 未接入）
        self.goals = goals  # F22 目标修订（GoalDesk；None = 只记 human_request）
        self.context_builder = context_builder  # 上下文构建器
        self.stop: dict | None = None  # 本段是否已 end_segment（{wait_for, reason}）

    # -----------------------------------------------------------------------
    # handle_control：编排控制工具的实现
    # 功能：F07 control_server 的处理函数；ctx 带案子、模拟日与调用者令牌（F11）。
    # 依赖：dispatcher、session。
    # 完成：有待运行任务时拒绝 end_segment；需要人的控制工具记 human_request（F17 统计打扰人工）；
    #       派任务以调用者令牌为父令牌签发子令牌（F11 衰减）。
    # -----------------------------------------------------------------------
    def handle_control(self, name: str, args, ctx) -> dict:
        actor = ctx.token.actor  # 身份（来自令牌）
        if name == "dispatch_task":  # 派任务
            return self.dispatcher.dispatch(parent=ctx.token, day=ctx.day,  # 登记（父令牌）
                                            executor=args.executor, task=args.task,  # 执行者与说明
                                            inputs=args.inputs, reason=args.reason,  # 输入与理由
                                            params=args.params, scope=args.scope,  # 代码组件参数与作用域
                                            plan_task=args.plan_task)  # F22 计划任务号
        if name == "write_plan":  # F22 写 / 改任务图
            if self.planner is None:  # 未接入
                return {"accepted": False, "error": "计划校验尚未接入"}  # 明确拒绝
            return self.planner.write(ctx.token, [t.model_dump() for t in args.tasks], day=ctx.day)  # 校验与产出
        if name == "request_human":  # F22 合并的三个交人提议
            if args.kind == "goal_revision":  # 目标修订
                return self._goal_revision(ctx, args.new_goal, args.reason)  # 交采购员
            if args.kind == "planner":  # 上报计划员
                return self._report_to_planner(ctx, args.impact_artifact, args.reason, args.model_dump(mode="json"))
            if self.knowledge is None:  # 提议记忆但未接入
                return {"accepted": False, "error": "知识库尚未接入"}  # 明确拒绝
            return self.knowledge.propose(self.session, case_id=ctx.case_id, day=ctx.day, actor=actor,  # 提议记忆
                                          kind=args.memory_kind or "", report=args.report, board=args.board,
                                          designators=args.designators, alias=args.alias, mpn=args.mpn)  # 内容
        if name == "propose_goal_revision" and self.goals is not None:  # 细粒度工具：同一路径
            return self._goal_revision(ctx, args.new_goal, args.reason)  # 交采购员
        if name == "report_to_planner":  # 细粒度工具：同一路径
            return self._report_to_planner(ctx, args.impact_artifact, args.reason, args.model_dump(mode="json"))
        if name == "end_segment":  # 结束本段
            if self.dispatcher.pending:  # 还有待运行任务
                return {"accepted": False, "error": "还有已派发未运行的任务，不能结束本段"}  # 拒绝
            self.stop = {"wait_for": args.wait_for, "reason": args.reason}  # 标记停止
            return {"accepted": True}  # 接受
        if name == "propose_memory":  # F20 提议长期记忆（人工确认后才落库）
            if self.knowledge is None:  # 未接入
                return {"accepted": False, "error": "知识库尚未接入"}  # 明确拒绝
            return self.knowledge.propose(self.session, case_id=ctx.case_id, day=ctx.day, actor=actor,  # 提议
                                          **args.model_dump())  # 参数
        if name == "escalate_to_human":  # F14 主动升级
            self.escalation.escalate(ctx.case_id, "orchestrator", args.reason, day=ctx.day, actor=actor)  # 关闭并通知
            self.stop = {"wait_for": "human", "reason": f"已升级给人：{args.reason}"}  # 结束本段
            return {"accepted": True, "note": "已升级给人，案子关闭"}  # 接受
        self.session.append("human_request", day=ctx.day, actor=actor, case_id=ctx.case_id,  # 需要人
                            payload={"role": HUMAN_CONTROLS[name], "tool": name,  # 对象与工具
                                     "args": args.model_dump(mode="json")})  # 内容
        return {"accepted": True, "note": "已提交给人，等待应答"}  # 接受

    def _goal_revision(self, ctx, new_goal: str | None, reason: str) -> dict:
        """目标修订：接了 GoalDesk 走 H4 确认流程，否则只记 human_request（M1 行为）。"""
        if self.goals is not None:  # F22
            return self.goals.propose(case_id=ctx.case_id, day=ctx.day, actor=ctx.token.actor,  # 交采购员
                                      new_goal=new_goal or "", reason=reason)  # 新目标与理由
        self.session.append("human_request", day=ctx.day, actor=ctx.token.actor, case_id=ctx.case_id,  # 需要人
                            payload={"role": "goal_revision", "tool": "propose_goal_revision",  # 对象
                                     "args": {"new_goal": new_goal, "reason": reason}})  # 内容
        return {"accepted": True, "note": "已提交给人，等待应答"}  # 接受

    def _report_to_planner(self, ctx, impact_artifact: str | None, reason: str, args: dict) -> dict:
        """上报计划员：先过 F12 门控（须附本案有效 ImpactReport，数字须出自它），再通知计划员。"""
        reasons = planner_report_gate(self.session, ctx.case_id, impact_artifact or "", reason)  # 检查
        if reasons:  # 不通过
            self.session.append("gate_rejected", day=ctx.day, actor="gate", case_id=ctx.case_id,  # 反馈事件
                                payload={"action": "report_to_planner", "reasons": reasons,  # 原因
                                         "artifact": impact_artifact})  # 引用
            return {"accepted": False, "error": "；".join(reasons)}  # 交回编排者
        self.session.append("human_request", day=ctx.day, actor=ctx.token.actor, case_id=ctx.case_id,  # 需要人
                            payload={"role": "planner", "node": "H5", "tool": "report_to_planner", "args": args})  # 计划员（F05 补节点）
        return {"accepted": True, "note": "已提交给人，等待应答"}  # 接受

    # -----------------------------------------------------------------------
    # run：跑一个活跃段
    # 功能：见文件摘要。
    # 依赖：_escalate_if_over_budget、context_builder、loop、dispatcher。
    # 完成：返回 {status, reason}；status 取 escalated / waiting / skipped。
    # -----------------------------------------------------------------------
    def run(self, case_id: str, *, reason: str, day: int) -> dict:
        state = self.session.case_state(case_id)  # 当前状态
        if state is None or state["state"] == CASE_CLOSED:  # 不存在或已关闭
            return {"status": "skipped", "reason": "案子不存在或已关闭"}  # 跳过
        over = self._over_budget(case_id)  # 案子级预算
        if over:  # 触顶
            self.session.close_case(case_id, "escalated", day=day, actor="runtime", reason=over)  # 升级给人
            return {"status": "escalated", "reason": over}  # 返回
        if state["state"] == CASE_OPEN:  # 新案子
            self.session.transition(case_id, CASE_PLANNING, day=day, actor="runtime", reason=reason)  # 规划中
        if self.session.case_state(case_id)["state"] in (CASE_PLANNING, CASE_WAITING):  # 规划中或被唤醒
            self.session.transition(case_id, CASE_EXECUTING, day=day, actor="runtime", reason=reason)  # 执行中
        self.session.append("segment_started", day=day, actor="runtime", case_id=case_id,  # 段开始
                            payload={"reason": reason})  # 唤醒原因
        self.stop, self.dispatcher.pending = None, []  # 段内状态从零开始（不保存半截对话）
        system, user = self.context_builder(self.session, case_id, reason)  # 现组装上下文
        rounds = self.budgets["orchestrator"]["max_rounds_per_segment"]  # 段轮数预算
        opened = self.session.events(case_id=case_id, type="case_opened")[0]  # 开案事件
        token = self.authority.issue_root(executor="orchestrator", case_id=case_id,  # F11 根令牌
                                          mode=opened["payload"].get("mode"),  # 案子模式（缺省按配置）
                                          initiated_by=f"{opened['actor']}#{opened['seq']}")  # 由谁发起
        tools = self.tools_by_mode[token.mode] if self.tools_by_mode else self.tools  # F22：按模式的工具集（≤ 8）
        res = self.loop.run(token=token, system=system, user=user,  # 编排者循环（身份取自令牌）
                            tools=tools, output=None, max_rounds=rounds, max_tool_calls=rounds * 8,  # 预算
                            day=day,  # 模拟日
                            after_round=lambda: self.dispatcher.run_pending(case_id=case_id, day=day),  # 汇合
                            should_stop=lambda: self.stop is not None)  # end_segment
        while self.dispatcher.pending:  # 编排者已给出结果但还有任务没跑（汇合关闭时）
            self.dispatcher.run_pending(case_id=case_id, day=day)  # 跑完
        wait_for, why = ((self.stop["wait_for"], self.stop["reason"]) if self.stop  # 正常结束本段
                         else ("external", res.reason))  # 预算触顶、失败或直接给出结果
        self.session.append("segment_ended", day=day, actor="runtime", case_id=case_id,  # 段结束
                            payload={"loop_status": res.status, "reason": why, "rounds": res.rounds})  # 记录原因
        if self.session.case_state(case_id)["state"] == CASE_CLOSED:  # F14：本段内已升级关闭
            return {"status": "closed", "reason": why, "loop_status": res.status}  # 不再迁入等待
        self.session.transition(case_id, CASE_WAITING, day=day, actor="runtime", wait_for=wait_for,  # 进入等待
                                reason=why)  # 原因
        return {"status": "waiting", "reason": why, "loop_status": res.status}  # 返回

    def _over_budget(self, case_id: str) -> str | None:
        """案子级预算（从日志投影）：重规划次数、LLM 调用总数；触顶返回原因。"""
        c = self.budgets["case"]  # 案子预算
        llm = len(self.session.events(case_id=case_id, type="llm_call"))  # LLM 调用数
        plans = sum(1 for e in self.session.events(case_id=case_id, type=EV_CASE_TRANSITION)  # 规划次数
                    if e["payload"]["to"] == CASE_PLANNING)  # 进入规划
        redo = sum(1 for e in self.session.events(case_id=case_id, type="task_dispatched")  # F15：重派计重规划
                   if e["payload"].get("redispatch_of"))  # 重派次数
        if plans - 1 + redo > c["max_replans"]:  # 重规划超限
            return f"重规划 {plans - 1 + redo} 次（含重派 {redo} 次），超过上限 {c['max_replans']}"  # 原因
        if llm >= c["max_llm_calls"]:  # LLM 调用超限
            return f"LLM 调用 {llm} 次，达到上限 {c['max_llm_calls']}"  # 原因
        return None  # 未触顶


class Scheduler:
    # 功能：按模拟日的调度循环。依赖：环境、日志、活跃段、路由挂点。
    # 完成：router(env_day 事件) 返回 [(案子, 唤醒原因)]，F10 cases.router.CaseRouter 实现；默认不唤醒。
    #       before_routing(day)：环境推进后、路由前的挂点（F05 模拟人工在此应答，当天的人工决定当天唤醒案子）；默认无。
    def __init__(self, env: Environment, session: Session, segments: SegmentRunner, *,
                 router: Callable[[dict], list[tuple[str, str]]] | None = None, max_active: int = 4,
                 before_routing: Callable[[int], object] | None = None):
        self.env, self.session, self.segments = env, session, segments  # 依赖
        self.router = router or (lambda event: [])  # 路由挂点
        self.before_routing = before_routing  # F05 路由前挂点
        self.max_active = max_active  # 并发上限

    def set_timer(self, case_id: str, due_day: int, reason: str, *, day: int) -> dict:
        """设定时器：到 due_day 唤醒案子。"""
        n = len(self.session.events(type="timer_set")) + 1  # 定时器序号
        return self.session.append("timer_set", day=day, actor="runtime", case_id=case_id,  # 定时器事件
                                   payload={"timer_id": f"TM-{n}", "due_day": due_day, "reason": reason})  # 内容

    def _due_timers(self, day: int) -> list[tuple[str, str]]:
        """到期且未触发的定时器，逐个写 timer_fired。"""
        fired = {e["payload"]["timer_id"] for e in self.session.events(type="timer_fired")}  # 已触发
        wakes = []  # 唤醒
        for e in self.session.events(type="timer_set"):  # 全部定时器
            p = e["payload"]  # 内容
            if p["due_day"] <= day and p["timer_id"] not in fired:  # 到期未触发
                self.session.append("timer_fired", day=day, actor="runtime", case_id=e["case_id"],  # 触发
                                    payload={"timer_id": p["timer_id"]}, caused_by=e["seq"])  # 因果
                wakes.append((e["case_id"], f"定时器：{p['reason']}"))  # 唤醒
        return wakes  # 列表

    def _active(self) -> int:
        """已开始处置（离开"开启"）且未关闭的案子数。"""
        return sum(1 for c in self.session.cases() if c["state"] not in (CASE_OPEN, CASE_CLOSED))  # 计数

    # -----------------------------------------------------------------------
    # run_day：跑一个模拟日
    # 功能：见文件摘要。
    # 依赖：env.advance、router、_due_timers、segments.run。
    # 完成：排队的新案子在有空位时（同日内有案子关闭也算）按开案顺序补上；每个案子每天最多跑一段（M1）。
    # -----------------------------------------------------------------------
    def run_day(self) -> dict:
        log = self.env.advance()  # 推进环境（注入 → 到货 → 发货 → 完工 → 预留 → 开工 → 到期）
        day = self.env.state.day  # 当天
        keys = env_day_keys(self.env.state, log)  # 当日实体键（F10：含料号与板）
        event = self.session.append("env_day", day=day, actor="env", payload=log, entity_keys=keys)  # 环境事件
        if self.before_routing is not None:  # F05：模拟人工先应答
            self.before_routing(day)  # 写人工决定，下面的路由据此唤醒
        wakes = list(self.router(event)) + self._due_timers(day)  # 路由 + 定时器
        queued = [c["case_id"] for c in self.session.cases() if c["state"] == CASE_OPEN]  # 未开始的案子（含排队）
        wakes += [(c, "排队后开始处置") for c in queued if c not in {w[0] for w in wakes}]  # 排队的也试一次
        ran, waiting = [], []  # 本日运行、本日排队
        progress = True  # 是否还有进展
        while progress:  # 直到没有可运行的
            progress = False  # 本轮先假设没有
            for case_id, reason in list(wakes):  # 按固定顺序
                state = self.session.case_state(case_id)  # 状态
                if state is None or state["state"] == CASE_CLOSED or case_id in ran:  # 不存在、已关闭或今天已跑
                    wakes.remove((case_id, reason))  # 移除
                    continue  # 下一个
                if state["state"] == CASE_OPEN and self._active() >= self.max_active:  # 新案子且满员
                    if case_id not in waiting:  # 今天首次排队
                        self.session.append("case_queued", day=day, actor="runtime", case_id=case_id,  # 排队
                                            payload={"active": self._active(), "limit": self.max_active})  # 原因
                        waiting.append(case_id)  # 记下
                    continue  # 等空位
                self.session.append("case_woken", day=day, actor="runtime", case_id=case_id,  # 唤醒
                                    payload={"reason": reason})  # 原因
                self.segments.run(case_id, reason=reason, day=day)  # 跑一段
                ran.append(case_id)  # 记下
                wakes.remove((case_id, reason))  # 移除
                progress = True  # 有进展（可能腾出空位）
        return {"day": day, "ran": ran, "queued": [c for c in waiting if c not in ran]}  # 本日结果

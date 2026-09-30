"""文件摘要（F09 · 派发与汇合）

编排者通过控制工具 dispatch_task 派任务；同一轮的派发组成一批，整批结束才把汇总交回编排者一次（spec §5.3）。
- ExecutorSpec：执行者注册项。LLM 子 Agent（系统提示、工具、输出模型、预算）或代码组件（纯函数，F21 注册；
  可返回 CodeResult 附带外部数据依赖；dispatch_task 的 params 作为结构化参数传给代码组件）。
- Dispatcher：登记待办（task_dispatched 事件）→ run_pending() 按派发顺序逐个运行（见 F09 Contract 默认值：不用真实线程，保证可复现）
  → 每个任务产出一个 Artifact（成功内容或"未完成"+原因，依赖输入 Artifact）并写 task_finished → 批次结束写 batch_joined。
  消融开关 batch_join 关闭时每次只运行一个任务，编排者每完成一个任务就被唤醒一次。
- F11：派发时由 Authority 从派发者令牌签发衰减后的子令牌（工具 = 父 ∩ 子角色 ∩ 注册工具，作用域 ⊆ 父），越界即拒绝派发；
  task_dispatched 事件 meta 记子令牌的代表链；LLM 子 Agent 凭子令牌跑循环，代码组件经 ctx["token"] 拿到子令牌调代理层。
- F13：每个产出的 Artifact 在 meta.taint 写字段标签：LLM 子 Agent 的产出一律不可信；代码组件用 CodeResult.taint 声明，
  没声明按不可信（来源不明）；标签不进给编排者的汇总文本。
依赖：runtime.loop.ExecutorLoop、session.log.Session、permissions.tokens、taint.labels、pydantic。被 runtime.segment 使用；F21/F23 注册执行者。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 汇总转文本
import re  # 解析 Artifact 引用
from collections.abc import Callable  # 代码组件类型
from dataclasses import dataclass, field  # 数据类

from pydantic import BaseModel  # 输出模型

from models.types import Message  # 回给编排者的消息
from permissions.tokens import AttenuationError, Authority  # F11 子令牌签发
from runtime.loop import ExecutorLoop  # 执行者循环
from session.log import Session  # 事件日志
from taint.labels import ALL_UNTRUSTED  # F13 缺省标签

REF = re.compile(r"^(?P<id>.+)@v(?P<ver>\d+)$")  # Artifact 引用格式：ID@v版本


# 功能：一个可被派任务的执行者。依赖：pydantic 输出模型。
# 完成：LLM 子 Agent 给 system/tools/output；代码组件给 code（输入：任务说明、输入 Artifact 列表、上下文 → 内容字典）。
@dataclass
class ExecutorSpec:
    name: str  # 执行者名（也是 F08 模型链与 F07 白名单的键）
    kind: str  # 产出的 Artifact 类型，如 VerificationReport
    system: str = ""  # 系统提示（LLM）
    tools: list[str] = field(default_factory=list)  # 可用工具（LLM）
    output: type[BaseModel] | None = None  # 输出模型（LLM）
    code: Callable[[str, list[dict], dict], dict] | None = None  # 代码组件（F21）


@dataclass
class CodeResult:
    """代码组件的返回：内容 + 额外依赖（外部数据引用，如报价留档与有效期，F21）。"""
    content: dict  # Artifact 内容
    deps: list[dict] = field(default_factory=list)  # 额外依赖（格式同 Session.produce 的 deps）
    status: str = "done"  # done / unfinished（如无可行报价仍算 done，由内容表达）
    reason: str = "完成"  # 原因
    taint: dict | None = None  # F13 字段标签（None = 来源不明，按不可信）


@dataclass
class Task:
    """一个已派发、待运行的任务。"""
    task_id: str  # 任务号
    executor: str  # 执行者
    task: str  # 说明
    inputs: list[str]  # 输入 Artifact 引用
    reason: str  # 理由
    seq: int  # task_dispatched 事件序号
    params: dict = field(default_factory=dict)  # 给代码组件的结构化参数
    token: object = None  # F11：本任务的子能力令牌（Runtime 持有，不进模型上下文）


class Dispatcher:
    # 功能：见文件摘要。依赖：注册表、执行者循环、事件日志、子 Agent 预算、F11 签发者。完成：批次汇合开关。
    def __init__(self, session: Session, loop: ExecutorLoop, registry: dict[str, ExecutorSpec], *,
                 budgets: dict, authority: Authority, batch_join: bool = True):
        self.session, self.loop, self.registry = session, loop, registry  # 依赖
        self.authority = authority  # F11 令牌签发者
        self.budgets = budgets  # config runtime 段
        self.batch_join = batch_join  # 汇合开关
        self.pending: list[Task] = []  # 本段待运行任务

    # 功能：登记一个任务（控制工具 dispatch_task 的实现）。
    # 完成：未知执行者、坏引用、子令牌越界（F11）直接报错给编排者（不登记）；parent 为派发者令牌，身份与案子取自它。
    def dispatch(self, *, parent, day: int, executor: str, task: str,
                 inputs: list[str], reason: str, params: dict | None = None,
                 scope: list[str] | None = None) -> dict:
        case_id, actor = parent.case_id, parent.actor  # 案子与派发者身份（来自令牌）
        if executor not in self.registry:  # 未注册
            return {"accepted": False, "error": f"没有执行者 {executor!r}，可用：{', '.join(sorted(self.registry))}"}
        bad = [r for r in inputs if not REF.match(r) or self.session.artifact_view(  # 引用必须存在
            REF.match(r)["id"], int(REF.match(r)["ver"])) is None]  # 查 Artifact
        if bad:  # 有坏引用
            return {"accepted": False, "error": f"输入 Artifact 不存在：{', '.join(bad)}"}  # 报错
        spec = self.registry[executor]  # 执行者注册项
        try:  # F11 权限衰减：LLM 子 Agent 按注册工具收窄，代码组件按角色
            child = self.authority.attenuate(parent, executor=executor,  # 子令牌
                                             tools=spec.tools if spec.code is None else None, scope=scope)  # 工具与作用域
        except AttenuationError as exc:  # 越界
            return {"accepted": False, "error": f"派发被拒：{exc}"}  # 报错给编排者
        n = len(self.session.events(case_id=case_id, type="task_dispatched")) + 1  # 案子内任务序号
        task_id = f"{case_id}-T{n}"  # 任务号
        ev = self.session.append("task_dispatched", day=day, actor=actor, case_id=case_id,  # 派发事件
                                 payload={"task_id": task_id, "executor": executor, "task": task,  # 内容
                                          "inputs": inputs, "reason": reason, "params": params or {},  # 输入、理由、参数
                                          "scope": sorted(child.scope) if child.scope is not None else None},  # 作用域
                                 meta=child.chain())  # F11 代表链
        self.pending.append(Task(task_id, executor, task, inputs, reason, ev["seq"], params or {}, child))  # 待运行
        return {"accepted": True, "task_id": task_id}  # 接受

    # 功能：运行待办。依赖：_run_one。完成：batch_join 开时跑完全部并写 batch_joined，关时只跑一个；返回给编排者的消息。
    def run_pending(self, *, case_id: str, day: int) -> list[Message]:
        if not self.pending:  # 没有待办
            return []  # 无消息
        batch = self.pending if self.batch_join else self.pending[:1]  # 本次运行哪些
        self.pending = [] if self.batch_join else self.pending[1:]  # 剩下的
        results = [self._run_one(t, case_id=case_id, day=day) for t in batch]  # 按派发顺序逐个运行
        joined = self.session.append("batch_joined", day=day, actor="runtime", case_id=case_id,  # 汇合事件
                                     payload={"tasks": [r["task_id"] for r in results],  # 任务
                                              "statuses": [r["status"] for r in results]})  # 状态
        summary = {"batch_seq": joined["seq"], "results": results}  # 汇总
        return [Message("user", "【任务批次完成】\n" + json.dumps(summary, ensure_ascii=False))]  # 一次交回

    # 功能：运行一个任务并产出 Artifact。依赖：ExecutorLoop 或代码组件、session.produce。
    # 完成：成功 → 内容为输出；未完成 / 失败 → {"status", "reason"}；依赖为输入 Artifact；写 task_finished。
    def _run_one(self, t: Task, *, case_id: str, day: int) -> dict:
        spec = self.registry[t.executor]  # 执行者
        actor = f"{t.executor}@{case_id}"  # Runtime 标注的身份
        inputs = [self.session.artifact_view(REF.match(r)["id"], int(REF.match(r)["ver"])) for r in t.inputs]  # 输入
        extra_deps: list[dict] = []  # 代码组件给出的额外依赖
        taint = ALL_UNTRUSTED  # F13 缺省：不可信（LLM 产出、代码组件未声明）
        if spec.code is not None:  # 代码组件
            out = spec.code(t.task, inputs, {"case_id": case_id, "day": day, "params": t.params,  # 调用
                                             "token": t.token})  # F11：代码组件凭子令牌调代理层
            if isinstance(out, CodeResult):  # 带依赖与状态
                content, status, reason, extra_deps = out.content, out.status, out.reason, out.deps  # 拆开
                taint = out.taint or ALL_UNTRUSTED  # 代码组件声明的标签
            else:  # 纯内容
                content, status, reason = out, "done", "完成"  # 默认完成
        else:  # LLM 子 Agent
            brief = "\n".join(f"- {a['artifact_id']}@v{a['version']}（{a['kind']}，{a['status']}）："  # 输入摘要
                              + json.dumps(a["content"], ensure_ascii=False) for a in inputs)  # 内容
            b = self.budgets["subagent"]  # 子 Agent 预算
            res = self.loop.run(token=t.token, system=spec.system,  # 跑执行者循环（身份取自子令牌）
                                user=f"任务：{t.task}\n输入：\n{brief or '（无）'}", tools=spec.tools,  # 输入
                                output=spec.output, max_rounds=b["max_rounds"],  # 轮数预算
                                max_tool_calls=b["max_tool_calls"], day=day)  # 工具预算
            status, reason = res.status, res.reason  # 状态与原因
            content = (res.output.model_dump(mode="json") if res.output is not None  # 结构化输出
                       else {"text": res.text}) if status == "done" else {"status": status, "reason": reason}  # 未完成
        art = self.session.produce(f"{spec.kind}:{t.task_id}", spec.kind, content, day=day, actor=actor,  # 产出
                                   case_id=case_id, caused_by=t.seq,  # 因果：派发事件
                                   deps=[{"artifact": a["artifact_id"], "version": a["version"]} for a in inputs]  # 依赖
                                   + extra_deps,  # 代码组件的外部数据依赖
                                   meta={"status": status, "taint": taint})  # 状态与字段标签（F13）
        ref = f"{art['artifact_id']}@v{art['version']}"  # 引用
        self.session.append("task_finished", day=day, actor="runtime", case_id=case_id,  # 完成事件
                            payload={"task_id": t.task_id, "status": status, "reason": reason, "artifact": ref},
                            caused_by=t.seq)  # 因果
        return {"task_id": t.task_id, "status": status, "reason": reason, "artifact": ref, "content": content}

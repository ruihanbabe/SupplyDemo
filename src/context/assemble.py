"""文件摘要（F16 · 编排者上下文分区组装）

OrchestratorContext 是 F09 SegmentRunner 的 context_builder：每次唤醒从事件日志与 Artifact 现组装（spec §5.9），
分区依次为 [系统] [案子卡片] [计划] [有效] [已失效] [历史] [新反馈]；摘要全由代码生成。
超限（编排者 24K）按固定顺序裁剪：已失效压成一行 → 从最早开始裁掉历史；系统、案子卡片、新反馈永不裁剪。
每次组装写一条 context_assembled 事件（各分区 token 估算、裁剪动作、是否仍超限）。
消融开关 context_assembly 关闭时退化为"本案全部 Artifact 原文 + 全部事件"直接塞入，不摘要、不裁剪。
依赖：session.log、context.text。被 runtime.segment.SegmentRunner 使用；F20 以后在 [系统] 之后插入记忆分区。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 事件与内容转文本

from context.text import estimate_tokens, summarize  # token 估算与代码摘要
from session.log import ART_INVALID, ART_VALID, Session  # 状态常量与日志

#: 属于"新反馈"的事件类型（spec §5.7 反馈信号中会唤醒编排者的那些）。
FEEDBACK_TYPES = ("case_woken", "invalidation_notice", "gate_rejected", "human_response", "feedback",
                  "deviation", "conflict")  # 唤醒原因、失效、门控、人工应答、反馈卡片、偏差、冲突


def _card(event: dict) -> str:
    """一个反馈事件的代码卡片（F14 的完整反馈卡片以 feedback 事件的 text 字段给出）。"""
    p = event["payload"]  # 内容
    if event["type"] == "case_woken":  # 唤醒
        return f"【唤醒】第{event['day']}天：{p.get('reason')}"  # 原因
    if event["type"] == "invalidation_notice":  # 失效通知
        return f"【失效通知】第{event['day']}天：{'、'.join(p['artifacts'])} 已失效（根因：{json.dumps(p['root'], ensure_ascii=False)}）"
    if event["type"] == "feedback" and "text" in p:  # F14 反馈卡片
        return p["text"]  # 原样
    return f"【{event['type']}】第{event['day']}天：{json.dumps(p, ensure_ascii=False)}"  # 通用


class OrchestratorContext:
    # 功能：见文件摘要。依赖：context 配置段、消融开关、Skill 文本、编排者工具名。
    # 完成：可直接作为 SegmentRunner 的 context_builder（签名 (session, case_id, reason) → (system, user)）。
    def __init__(self, cfg: dict, *, enabled: bool = True, skill: str = "", tools: list[str] = ()):
        self.cfg = cfg  # context 配置段
        self.enabled = enabled  # 消融开关
        self.skill = skill  # 编排者 Skill 文本（F20/F22 提供）
        self.tools = list(tools)  # 工具名

    def __call__(self, session: Session, case_id: str, reason: str) -> tuple[str, str]:
        system = (f"[系统]\n你是案子 {case_id} 的编排者。用控制工具派任务（可一次派多个），所有任务都在等外部结果时结束本段。"  # 角色
                  "数值只能来自工具与 Artifact，不得编造。"  # G4
                  + (f"\n{self.skill}" if self.skill else "")  # Skill
                  + (f"\n可用工具：{', '.join(self.tools)}" if self.tools else ""))  # 工具
        events = session.events(case_id=case_id)  # 本案事件
        if not self.enabled:  # 消融：原样全塞
            arts = [a for a in session.store.artifacts(session.run_id) if a["case_id"] == case_id]  # 全部 Artifact
            user = ("[全部 Artifact]\n" + json.dumps(arts, ensure_ascii=False)  # 原文
                    + "\n[全部事件]\n" + json.dumps([{k: e[k] for k in ("seq", "day", "type", "payload")}  # 事件
                                                   for e in events], ensure_ascii=False))  # 原文
            self._record(session, case_id, events, {"system": system, "all": user}, [], False)  # 记录
            return system, user  # 返回
        parts = self._partitions(session, case_id, reason, events)  # 各分区
        actions = self._trim(parts, system)  # 裁剪
        user = "\n\n".join(text for text in parts.values() if text)  # 按固定顺序拼接
        over = estimate_tokens(system + user, self.cfg) > self.cfg["orchestrator_limit"]  # 仍超限
        self._record(session, case_id, events, {"system": system, **parts}, actions, over)  # 记录
        return system, user  # 返回

    # -----------------------------------------------------------------------
    # _partitions：生成各分区
    # 功能：卡片、计划、有效、已失效、历史、新反馈。
    # 依赖：session 投影、summarize、_card。
    # 完成：新反馈 = 上一个 segment_ended 之后的反馈事件；历史 = 此前各段结束与任务完成的一行记录。
    # -----------------------------------------------------------------------
    def _partitions(self, session, case_id, reason, events) -> dict[str, str]:
        opened = next(e for e in events if e["type"] == "case_opened")["payload"]  # 开案内容
        state = session.case_state(case_id)  # 状态
        targets = {}  # 目标 → 状态
        for t in opened.get("targets", []):  # 开案时的目标
            targets[t] = "待处理"  # 初始
        for e in events:  # 目标状态更新（F10/F22 写 target_status）
            if e["type"] == "target_status":  # 更新
                targets[e["payload"]["target"]] = e["payload"]["status"]  # 覆盖
        card = (f"[案子卡片]\n目标：{opened.get('goal', '（未写）')}\n状态：{state['state']}"  # 目标与状态
                + "".join(f"\n- {t}：{s}" for t, s in targets.items()))  # 各目标
        status = session.artifact_status()  # Artifact 状态
        arts = [a for a in session.store.artifacts(session.run_id) if a["case_id"] == case_id]  # 本案 Artifact
        plans = [a for a in arts if a["kind"] == "Plan" and status[(a["artifact_id"], a["version"])] == ART_VALID]  # 计划
        plan = "[计划]\n" + (self._plan_text(plans[-1]) if plans else "尚无计划")  # 计划分区
        mx = self.cfg["summary_max_chars"]  # 摘要长度
        valid = [summarize(a, mx) for a in arts  # 有效 Artifact 摘要
                 if a["kind"] != "Plan" and status[(a["artifact_id"], a["version"])] == ART_VALID]  # 过滤
        roots = {(e["payload"]["artifact_id"], e["payload"]["version"]): e["payload"]["root"]  # 失效根因
                 for e in session.events(type="artifact_invalidated")}  # 全部失效事件
        invalid = [f"{a['artifact_id']}@v{a['version']}（{a['kind']}）根因："  # 已失效一行
                   + json.dumps(roots.get((a["artifact_id"], a["version"])), ensure_ascii=False)  # 根因
                   for a in arts if status[(a["artifact_id"], a["version"])] == ART_INVALID]  # 过滤
        ended = [e["seq"] for e in events if e["type"] == "segment_ended"]  # 各段结束
        last = ended[-1] if ended else 0  # 上一段结束
        history = [f"第{e['day']}天 段结束：{e['payload']['reason']}" if e["type"] == "segment_ended"  # 段记录
                   else f"第{e['day']}天 {e['payload']['task_id']} {e['payload']['status']} → {e['payload']['artifact']}"
                   for e in events if e["seq"] <= last and e["type"] in ("segment_ended", "task_finished")]  # 任务
        feedback = [_card(e) for e in events if e["seq"] > last and e["type"] in FEEDBACK_TYPES]  # 新反馈
        if not any(e["type"] == "case_woken" for e in events if e["seq"] > last):  # 没有唤醒事件时
            feedback.insert(0, f"【唤醒】{reason}")  # 至少写明本次唤醒原因
        return {"card": card, "plan": plan,  # 固定顺序
                "valid": "[有效]\n" + ("\n".join(valid) or "无"),  # 有效
                "invalid": "[已失效]\n" + ("\n".join(invalid) or "无"),  # 已失效
                "history": "[历史]\n" + ("\n".join(history) or "无"),  # 历史
                "feedback": "[新反馈]\n" + "\n".join(feedback)}  # 新反馈

    @staticmethod
    def _plan_text(plan: dict) -> str:
        """计划 Artifact → 任务图文本（每个任务带一句理由，spec §5.9 跨活跃段推理）。"""
        tasks = plan["content"].get("tasks", [])  # 任务
        return f"{plan['artifact_id']}@v{plan['version']}\n" + "\n".join(  # 版本
            f"- {t.get('id')} {t.get('executor')}：{t.get('task')}（理由：{t.get('reason')}；状态：{t.get('status', '待办')}）"
            for t in tasks)  # 逐个任务

    # -----------------------------------------------------------------------
    # _trim：超限裁剪
    # 功能：已失效压成一行 → 从最早开始裁历史；系统、卡片、计划、有效、新反馈不动（spec §5.9）。
    # 依赖：estimate_tokens。
    # 完成：就地修改 parts，返回裁剪动作列表。
    # -----------------------------------------------------------------------
    def _trim(self, parts: dict[str, str], system: str) -> list[str]:
        limit, actions = self.cfg["orchestrator_limit"], []  # 上限与动作

        def size():  # 当前总大小
            return estimate_tokens(system + "\n\n".join(parts.values()), self.cfg)  # 估算

        if size() > limit and parts["invalid"] != "[已失效]\n无":  # 第一步：已失效压一行
            n = parts["invalid"].count("\n")  # 条数
            parts["invalid"] = f"[已失效]\n共 {n} 个 Artifact 已失效（详情按 ID 读取）"  # 一行
            actions.append("collapse_invalid")  # 记录
        lines = parts["history"].split("\n")[1:]  # 历史行
        dropped = 0  # 裁掉的行数
        while size() > limit and lines and lines != ["无"]:  # 第二步：从最早开始裁历史
            lines.pop(0)  # 裁最早一行
            dropped += 1  # 计数
            parts["history"] = "[历史]\n" + (f"（已裁掉较早的 {dropped} 条）\n" if dropped else "") + "\n".join(lines)
        if dropped:  # 有裁剪
            actions.append(f"drop_history:{dropped}")  # 记录
        return actions  # 动作

    def _record(self, session, case_id, events, parts, actions, over) -> None:
        """写 context_assembled 事件：各分区 token 估算、裁剪动作、是否仍超限（§5.11 上下文快照）。"""
        day = events[-1]["day"] if events else 0  # 用本案最近事件的模拟日
        session.append("context_assembled", day=day, actor="runtime", case_id=case_id,  # 事件
                       payload={"partitions": {k: estimate_tokens(v, self.cfg) for k, v in parts.items()},  # 大小
                                "actions": actions, "over_limit": over, "assembly": self.enabled})  # 动作与开关

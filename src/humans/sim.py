"""文件摘要（F05 · 模拟人工）

SimulatedHumans.respond(day) 在每个模拟日（环境推进后、路由前，由 Scheduler 挂点调用）扫描全部 human_request，
对到期（请求日 + 延迟 ≤ 今天）且尚未处理的 H1–H5 请求按 config/humans.yaml 的规则调用现有人工入口（spec §4.5）：
- H2 主管（payload.draft）：延迟在 [min, max] 内按 种子 + 请求事件号 确定性选取；金额超上限驳回，否则批准（HumanDesk）。
- H1 工程师：门控发出的 B 级确认（payload.report）→ HumanDesk.confirm_substitute / reject_substitute；
  替代关系记忆（payload.proposal_seq）→ KnowledgeBase.confirm / reject。规则表：原料 → 替代料 → approve / reject，表外按 unlisted。
- H3 采购员：别名记忆（proposal_seq）命中别名表且料号一致才确认；Resolver 消歧（payload.candidates）别名表给出的料号在候选里才作答
  （BuyerDesk）；未命中不应答（spec §3.4"不确认，相关任务就不继续"）。
- H4 采购员：目标修订按 goal_revision 确认 / 驳回 / 不应答（GoalDesk）。
- H5 计划员：对上报所附 ImpactReport 中预计停线、当前仍未开工的工单，各推迟 min(预计停线天数, 上限)（PlannerDesk）。
处理判据全部来自事件日志（已有对应人工决定或 sim_human_skipped），所以重复调用、回放与崩溃恢复后都不会重复应答。
入口核对不过（如草稿已失效）写 sim_human_skipped（原因），不重试。所属案子已关闭的请求不处理。
ask_user、升级、软声明冲突等通知类请求不在 H1–H5 内，不应答。
依赖：gate.approval.HumanDesk、orchestrator.planning.GoalDesk、knowledge_base.kb.KnowledgeBase、humans.desks、session.log。
被 orchestrator.assembly（build_runtime(humans=...)）与 Eval（F27）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import hashlib  # 确定性延迟
import re  # 引用解析

from humans.desks import BuyerDesk, PlannerDesk  # 新人工入口
from session.log import CASE_CLOSED, Session  # 事件日志
from simenv.model import WO_PLANNED, WO_STOPPED  # 未开工的两种状态

REF = re.compile(r"^(?P<id>.+)@v(?P<ver>\d+)$")  # Artifact 引用
WHO = {"H1": "sim-engineer", "H2": "sim-supervisor", "H3": "sim-buyer", "H4": "sim-buyer", "H5": "sim-planner"}  # 模拟人


# ---------------------------------------------------------------------------
# SimulatedHumans
# 功能：见文件摘要。
# 依赖：各人工入口、配置、种子。
# 完成：respond 返回本日做出的动作列表 [{request, node, action}]，便于测试与日志。
# ---------------------------------------------------------------------------
class SimulatedHumans:
    def __init__(self, session: Session, *, desk, goals, knowledge, planner: PlannerDesk, buyer: BuyerDesk,
                 config: dict, seed: int = 0):
        self.session, self.desk, self.goals, self.knowledge = session, desk, goals, knowledge  # 现有入口
        self.planner, self.buyer = planner, buyer  # 新入口
        self.config, self.seed = config, seed  # 规则与种子

    # ------------------------------------------------------------------ 规则
    def delay(self, node: str, request_seq: int) -> int:
        """应答延迟：H2 在区间内按种子与请求号确定性选取，其他为固定天数。"""
        if node == "H2":  # 主管
            d = self.config["supervisor"]["delay_days"]  # 区间
            h = int(hashlib.sha256(f"{self.seed}:{request_seq}".encode()).hexdigest(), 16)  # 确定性哈希
            return d["min"] + h % (d["max"] - d["min"] + 1)  # 区间内取值
        role = {"H1": "engineer", "H3": "buyer", "H4": "buyer", "H5": "planner"}[node]  # 配置段
        return self.config[role]["delay_days"]  # 固定天数

    def _engineer(self, original: str | None, substitute: str | None) -> str:
        """工程师 mock 替代规则：approve / reject。"""
        cfg = self.config["engineer"]  # 配置
        for r in cfg.get("substitutes") or []:  # 规则表
            if r["original"] == original and r["substitute"] == substitute:  # 命中
                return r["decision"]  # 表内决定
        return cfg["unlisted"]  # 表外默认

    def _alias(self, said) -> str | None:
        """采购员别名表：说法 → 料号（说法可能是引用块 {quote}）。"""
        text = said.get("quote") if isinstance(said, dict) else said  # 取文本
        return (self.config["buyer"].get("aliases") or {}).get(text)  # 查表

    # ------------------------------------------------------------------ 已处理判据
    def _handled(self) -> tuple[set, set]:
        """已有人工决定的对象（草稿 / 报告 / proposal#n / request#n）与已跳过的请求号。"""
        decided = {e["payload"]["artifact"] for e in self.session.events(type="human_decision")}  # 已决定
        skipped = {e["payload"]["request_seq"] for e in self.session.events(type="sim_human_skipped")}  # 已跳过
        return decided, skipped  # 返回

    @staticmethod
    def _target(p: dict, seq: int) -> str | None:
        """请求对应的决定对象；None = 不是 H1–H5 可应答的请求。"""
        node = p.get("node")  # 节点
        if node == "H2" and p.get("draft"):  # 审批采购单
            return p["draft"]  # 草稿引用
        if node == "H1" and p.get("report"):  # 门控发出的 B 级确认
            return p["report"]  # 报告引用
        if node in ("H1", "H3", "H4") and p.get("proposal_seq") is not None:  # 记忆 / 目标修订提议
            return f"proposal#{p['proposal_seq']}"  # 提议
        if node == "H3" and p.get("candidates") is not None:  # 消歧
            return f"request#{seq}"  # 请求
        if node == "H5":  # 上报计划员
            return f"request#{seq}"  # 请求
        return None  # 通知类

    # ------------------------------------------------------------------ 主循环
    def respond(self, day: int) -> list[dict]:
        """处理到期未处理的请求（按事件号顺序）。"""
        decided, skipped = self._handled()  # 已处理
        done = []  # 本日动作
        for ev in self.session.events(type="human_request"):  # 全部请求（按事件号）
            p, seq = ev["payload"], ev["seq"]  # 内容与事件号
            target = self._target(p, seq)  # 决定对象
            if target is None or target in decided or seq in skipped:  # 不可应答或已处理
                continue  # 跳过
            state = self.session.case_state(ev["case_id"]) if ev["case_id"] else None  # 案子
            if state is None or state["state"] == CASE_CLOSED:  # 案子不存在或已关闭
                continue  # 不处理
            node = p["node"]  # 节点
            if day < ev["day"] + self.delay(node, seq):  # 未到应答日
                continue  # 明天再看
            try:  # 调入口
                action = self._act(node, p, ev, day)  # 按规则应答
            except ValueError as e:  # 入口核对不过
                self.session.append("sim_human_skipped", day=day, actor=f"human:{WHO[node]}", case_id=ev["case_id"],
                                    payload={"request_seq": seq, "node": node, "reason": str(e)}, caused_by=seq)  # 记跳过
                action = "skipped"  # 动作
            if action is not None:  # 做了动作（None = 规则决定不应答）
                decided.add(target)  # 同日不再重复（同一对象的多个请求）
                done.append({"request": seq, "node": node, "action": action})  # 记下
        return done  # 返回

    def _act(self, node: str, p: dict, ev: dict, day: int) -> str | None:
        """按节点规则调用入口；返回动作名，None 表示不应答。"""
        by = WHO[node]  # 模拟人
        if node == "H2":  # 主管
            cap = self.config["supervisor"].get("reject_amount_over_usd")  # 金额上限
            m = REF.match(p["draft"])  # 草稿引用
            draft = self.session.artifact_view(m["id"], int(m["ver"])) if m else None  # 草稿
            amount = float((draft or {}).get("content", {}).get("amount_usd") or 0)  # 金额
            if cap is not None and amount > cap:  # 超上限
                self.desk.reject_po(p["draft"], by=by, day=day, note=f"金额 {amount} 超过 {cap}")  # 驳回
                return "reject"  # 动作
            self.desk.approve_po(p["draft"], by=by, day=day)  # 批准（签发令牌）
            return "approve"  # 动作
        if node == "H1" and p.get("report"):  # B 级替代确认
            m = REF.match(p["report"])  # 报告引用
            rep = self.session.artifact_view(m["id"], int(m["ver"])) if m else None  # 报告
            c = (rep or {}).get("content", {})  # 内容
            if self._engineer(c.get("original"), c.get("candidate")) == "approve":  # 规则允许
                self.desk.confirm_substitute(p["report"], by=by, day=day)  # 确认
                return "approve"  # 动作
            self.desk.reject_substitute(p["report"], by=by, day=day)  # 否决
            return "reject"  # 动作
        if node in ("H1", "H3") and p.get("proposal_seq") is not None:  # 记忆提议
            prop = self.session.store.event(self.session.run_id, p["proposal_seq"])  # 提议事件
            body = prop["payload"]["body"]  # 内容
            if node == "H1":  # 替代关系
                if self._engineer(body.get("original"), body.get("substitute")) == "approve":  # 规则允许
                    self.knowledge.confirm(self.session, p["proposal_seq"], by=by, node="H1", day=day)  # 落库
                    return "approve"  # 动作
                self.knowledge.reject(self.session, p["proposal_seq"], by=by, node="H1", day=day)  # 否决
                return "reject"  # 动作
            if self._alias(body.get("alias")) == body.get("mpn"):  # 别名表一致
                self.knowledge.confirm(self.session, p["proposal_seq"], by=by, node="H3", day=day)  # 落库
                return "approve"  # 动作
            return None  # 不应答
        if node == "H3":  # 消歧
            mpn = self._alias(p.get("query"))  # 别名表
            if mpn is None or mpn not in p["candidates"]:  # 表里没有或不在候选
                return None  # 不应答
            self.buyer.resolve(ev["seq"], mpn=mpn, by=by, day=day)  # 作答
            return "approve"  # 动作
        if node == "H4":  # 目标修订
            rule = self.config["buyer"]["goal_revision"]  # 规则
            if rule == "approve":  # 确认
                self.goals.confirm(p["proposal_seq"], by=by, day=day)  # 生效
            elif rule == "reject":  # 驳回
                self.goals.reject(p["proposal_seq"], by=by, day=day)  # 不变
            return None if rule == "ignore" else rule  # 动作
        return self._planner(p, ev, day, by)  # H5

    def _planner(self, p: dict, ev: dict, day: int, by: str) -> str:
        """H5：按 ImpactReport 推迟预计停线且仍未开工的工单。"""
        ref = (p.get("args") or {}).get("impact_artifact") or ""  # 影响分析引用
        m = REF.match(ref)  # 解析
        rep = self.session.artifact_view(m["id"], int(m["ver"])) if m else None  # 报告
        if rep is None:  # 没有
            raise ValueError(f"上报没有可读的 ImpactReport：{ref!r}")  # 交给跳过
        env = self.planner.get_env()  # 当前环境
        cap = self.config["planner"]["max_postpone_days"]  # 上限
        postpone = {w: min(v["stop_days"], cap) for w, v in sorted(rep["content"].get("work_orders", {}).items())  # 推迟天数
                    if v.get("stop_days", 0) > 0 and w in env.state.work_orders  # 预计停线
                    and env.state.work_orders[w].status in (WO_PLANNED, WO_STOPPED)}  # 仍未开工
        self.planner.reply(ev["seq"], postpone=postpone, by=by, day=day)  # 回复
        return "reply"  # 动作

"""文件摘要（F10 · 订阅投递、按影响开案、同源合并、关闭条件）

CaseRouter 是 F09 Scheduler 的 router 挂点（调用签名：env_day 事件 → [(案子, 唤醒原因)]），每个模拟日一次（spec §5.2）：
① Monitor 检测缺料（cases.monitor）→ ② 处理上次路由之后的新事件：人工类事件（human_decision / approval_granted）唤醒所属案子；
   可路由事件（env_day / monitor_shortage）的实体键命中未关闭案子的订阅 → 写 event_delivered 并唤醒；
   未命中任何案子的缺料 → ③ 代码开案：沿 caused_by 追到根因，同一根因的缺料合成一个案子（每颗料一个目标，A8），
   根因已有未关闭案子则写 case_target_added 并入；消融 same_source_merge 关闭时每颗料单独开案 →
④ 关闭检查：目标料号的缺口消失写 target_resolved；全部目标消除且本案提交的采购单都已到货才以 resolved 关闭 →
⑤ 写 routing_done（记下已处理到的事件号，重启后从这里继续）。
F14：传入 FeedbackLoop 时，① 之后先做偏差比对与强制升级检查，② 中再为新信号生成反馈卡片，"必须重规划"级加入唤醒。
- subscriptions(case_id)：订阅集 = 开案实体键 ∪ 目标料号 ∪ 本案 po_submitted 的实体键 ∪ 本案 Artifact 外部依赖里的料号。
- targets(case_id)：目标及其状态（open / resolved），从 case_opened / case_target_added / target_resolved 投影。
- user_message(case_id, text)：用户中途插话只记事件并返回"本期不支持"提示，不唤醒。
依赖：cases.monitor、session.log、simenv（采购单状态）、feedback.loop（可选）、PyYAML。被装配 Runtime 的代码（F22、F27）与测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Callable  # 取值函数
from pathlib import Path  # 配置路径

import yaml  # 读配置

from cases.monitor import monitor_shortages  # Monitor
from session.log import CASE_CLOSED, Session  # 事件日志
from simenv.api import Environment  # 环境
from simenv.model import PO_ARRIVED  # 采购单已到货

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
CASES_CONFIG = ROOT / "config" / "cases.yaml"  # 默认配置
OWN = {"routing_done", "event_delivered", "case_target_added", "target_resolved", "feedback"}  # 路由与反馈自己写的事件


def load_cases_config(path: Path = CASES_CONFIG) -> dict:
    """读案子管理配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


class CaseRouter:
    # 功能：见文件摘要。依赖：事件日志、当前环境取值函数、配置、消融开关 same_source_merge。
    def __init__(self, session: Session, get_env: Callable[[], Environment], *, config: dict | None = None,
                 merge: bool = True, feedback=None):
        self.session, self.get_env = session, get_env  # 依赖
        self.feedback = feedback  # F14 反馈循环（FeedbackLoop；None = 不生成卡片、不查偏差与升级）
        self.config = config or load_cases_config()  # 配置
        self.merge = merge  # 同源合并开关

    # -----------------------------------------------------------------------
    # 投影：订阅集与目标
    # -----------------------------------------------------------------------
    def targets(self, case_id: str) -> dict[str, dict]:
        """目标：料号 → {mpn, gap, first_short_day, status}。"""
        out = {}  # 结果
        for e in self.session.events(case_id=case_id):  # 本案事件
            if e["type"] in ("case_opened", "case_target_added"):  # 开案或并入
                for t in e["payload"].get("targets", []):  # 每个目标
                    out[t["mpn"]] = {**t, "status": "open"}  # 新目标（并入时重新打开）
            elif e["type"] == "target_resolved":  # 消除
                out[e["payload"]["mpn"]]["status"] = "resolved"  # 标记
        return out  # 目标表

    def subscriptions(self, case_id: str) -> dict[str, set[str]]:
        """订阅集（从日志与 Artifact 依赖投影，代码维护）。"""
        subs: dict[str, set[str]] = {}  # 类别 → 键

        def add(keys: dict):  # 并入一组实体键
            for k, v in keys.items():  # 每类
                subs.setdefault(k, set()).update(v)  # 合并

        for e in self.session.events(case_id=case_id):  # 本案事件
            if e["type"] in ("case_opened", "case_target_added", "po_submitted"):  # 开案、并入、提交的采购单
                add(e["entity_keys"])  # 实体键
        add({"mpn": set(self.targets(case_id))})  # 目标料号
        mine = {(a["artifact_id"], a["version"]) for a in self.session.store.artifacts(self.session.run_id)
                if a["case_id"] == case_id}  # 本案 Artifact
        for d in self.session.store.deps(self.session.run_id):  # 全部依赖边
            if d["dep_kind"] == "external" and (d["artifact_id"], d["version"]) in mine \
                    and d["target"].startswith("quote:"):  # 本案依赖的报价
                add({"mpn": {d["target"].split(":", 2)[2]}})  # 报价的料号
        return subs  # 订阅集

    # -----------------------------------------------------------------------
    # __call__：一天的路由
    # 功能：见文件摘要 ①–⑤。
    # 依赖：monitor_shortages、_open_or_merge、_close_check。
    # 完成：返回唤醒列表（每个案子一次，保留第一个原因）；routing_done 记处理到的事件号。
    # -----------------------------------------------------------------------
    def __call__(self, env_event: dict) -> list[tuple[str, str]]:
        day = env_event["day"]  # 当天
        done = self.session.events(type="routing_done")  # 已完成的路由
        cursor = done[-1]["payload"]["upto"] if done else 0  # 上次处理到的事件号
        _, short_now = monitor_shortages(self.session, self.get_env().state, env_event)  # ① Monitor
        if self.feedback is not None:  # F14：偏差比对与强制升级
            self.feedback.before_routing(short_now, day)  # 写偏差 / 升级关闭
        new = [e for e in self.session.store.events(self.session.run_id, cursor) if e["type"] not in OWN]  # ② 新事件
        wakes: dict[str, str] = {}  # 案子 → 原因
        live = [c["case_id"] for c in self.session.cases() if c["state"] != CASE_CLOSED]  # 未关闭案子
        subs = {c: self.subscriptions(c) for c in live}  # 订阅集
        unmatched = []  # 未命中的缺料
        for e in new:  # 逐个事件
            if e["type"] in self.config["human"] and e["case_id"] in subs:  # 人工应答：唤醒所属案子
                wakes.setdefault(e["case_id"], f"人工应答：{e['type']}")  # 唤醒
                continue  # 下一个
            if e["type"] not in self.config["routable"]:  # 不路由的事件
                continue  # 只记日志
            hits = [c for c in live if any(set(v) & subs[c].get(k, set()) for k, v in e["entity_keys"].items())]  # 命中
            for c in hits:  # 投递
                self.session.append("event_delivered", day=day, actor="router", case_id=c,  # 投递事件
                                    payload={"event_seq": e["seq"], "type": e["type"]}, caused_by=e["seq"])  # 来源
                wakes.setdefault(c, f"订阅事件：{e['type']} #{e['seq']}")  # 唤醒
            if not hits and e["type"] == "monitor_shortage":  # 无主缺料
                unmatched.append(e)  # 待开案
        if self.feedback is not None:  # F14：反馈卡片与必须重规划的唤醒
            for c, reason in self.feedback.on_events(new, day):  # 逐个
                wakes.setdefault(c, reason)  # 唤醒
        for c, reason in self._open_or_merge(unmatched, day):  # ③ 开案 / 并入
            wakes.setdefault(c, reason)  # 唤醒
        self._close_check(short_now, day)  # ④ 关闭检查
        last = self.session.store.last_event(self.session.run_id)  # 链尾
        self.session.append("routing_done", day=day, actor="router", payload={"upto": last["seq"] + 1})  # ⑤ 进度
        return [(c, r) for c, r in wakes.items()  # 唤醒列表
                if self.session.case_state(c)["state"] != CASE_CLOSED]  # 已关闭的不唤醒

    def _root(self, event: dict) -> int:
        """沿 caused_by 追到根因事件号。"""
        seq = event["seq"]  # 起点
        while True:  # 逐级向上
            e = self.session.store.event(self.session.run_id, seq)  # 当前事件
            if e["caused_by"] is None:  # 到根
                return seq  # 根因
            seq = e["caused_by"]  # 上一级

    def _open_or_merge(self, shortages: list[dict], day: int) -> list[tuple[str, str]]:
        """按根因分组：已有未关闭案子则并入，否则开案；关闭同源合并时每颗料一组。"""
        groups: dict[str, list[dict]] = {}  # 分组键 → 缺料事件
        for e in shortages:  # 每条
            root = self._root(e)  # 根因
            key = f"root:{root}" if self.merge else f"root:{root}:{e['payload']['mpn']}"  # 分组键
            groups.setdefault(key, []).append(e)  # 归组
        by_group = {c["case_id"]: e["payload"].get("group") for c in self.session.cases()  # 案子 → 分组键
                    if c["state"] != CASE_CLOSED  # 未关闭
                    for e in self.session.events(case_id=c["case_id"], type="case_opened")}  # 开案内容
        out = []  # 唤醒
        for key, evs in groups.items():  # 每组
            targets = [{k: e["payload"][k] for k in ("mpn", "gap", "first_short_day")} for e in evs]  # 目标
            keys = {"mpn": [t["mpn"] for t in targets],  # 实体键：料号
                    "work_order": sorted({w for e in evs for w in e["payload"]["work_orders"]})}  # 与工单
            owner = next((c for c, g in by_group.items() if g == key), None)  # 同根因的未关闭案子
            if owner is not None:  # 并入（A8）
                self.session.append("case_target_added", day=day, actor="router", case_id=owner,  # 并入事件
                                    payload={"targets": targets}, entity_keys=keys, caused_by=evs[0]["seq"])  # 目标
                out.append((owner, f"同源缺料并入：{', '.join(k['mpn'] for k in targets)}"))  # 唤醒
                continue  # 下一组
            case_id = self._new_id()  # 新案子号
            text = "；".join(f"{t['mpn']} 缺 {t['gap']} 颗（第 {t['first_short_day']} 天起）" for t in targets)  # 目标文本
            oc = self.config["open_case"]  # 开案配置
            self.session.open_case(case_id, day=day, actor="monitor",  # 代码开案
                                   payload={"goal": oc["goal"].format(targets=text), "mode": oc["mode"],  # 目标与模式
                                            "targets": targets, "group": key},  # 目标表与分组键
                                   entity_keys=keys, caused_by=evs[0]["seq"])  # 实体键与因果
            out.append((case_id, f"开案：{text}"))  # 唤醒
        return out  # 唤醒列表

    def _new_id(self) -> str:
        """下一个未用过的案子号。"""
        n = len(self.session.cases()) + 1  # 候选序号
        while self.session.case_state(f"{self.config['open_case']['id_prefix']}{n}") is not None:  # 已被占用
            n += 1  # 下一个
        return f"{self.config['open_case']['id_prefix']}{n}"  # 案子号

    def _close_check(self, short_now: set[str], day: int) -> None:
        """目标缺口消失即 target_resolved；全部消除且本案采购单都已到货才关闭（提交不算完成）。"""
        env = self.get_env()  # 当前环境
        for c in self.session.cases():  # 每个案子
            if c["state"] == CASE_CLOSED:  # 已关闭
                continue  # 跳过
            targets = self.targets(c["case_id"])  # 目标
            if not targets:  # 非 Monitor 开的案子（如用户发起、问答）不按缺口关闭
                continue  # 跳过
            for mpn, t in targets.items():  # 每个目标
                if t["status"] == "open" and mpn not in short_now:  # 缺口已消失
                    self.session.append("target_resolved", day=day, actor="router", case_id=c["case_id"],  # 消除
                                        payload={"mpn": mpn}, entity_keys={"mpn": [mpn]})  # 目标
            if any(t["status"] == "open" for t in self.targets(c["case_id"]).values()):  # 仍有未消除
                continue  # 不关闭
            pos = [e["payload"]["po_id"] for e in self.session.events(case_id=c["case_id"], type="po_submitted")]  # 本案采购单
            pending = [p for p in pos if env.state.purchase_orders[p].status != PO_ARRIVED]  # 未到货
            if not pending:  # 全部到货（或本案没下单、缺口已被消除）
                self.session.close_case(c["case_id"], "resolved", day=day, actor="router",  # 关闭
                                        reason="全部目标缺口消除，本案采购单已到货")  # 原因

    def user_message(self, case_id: str, text: str, *, day: int) -> str:
        """用户中途插话：记事件、返回提示，不唤醒（spec §5.2）。"""
        notice = self.config["user_message_notice"]  # 提示
        self.session.append("user_message", day=day, actor="user", case_id=case_id,  # 记录
                            payload={"text": text, "handled": False, "notice": notice})  # 未处理
        return notice  # 返回提示

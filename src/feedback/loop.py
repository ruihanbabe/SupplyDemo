"""文件摘要（F14 · 每日反馈循环）

FeedbackLoop 由 F10 CaseRouter 每日调用，顺序固定：
① before_routing(short_now, day)：预期与偏差比对（开关 deviation_feedback）→ 对每个未关闭案子做强制升级检查（开关 forced_escalation）；
   写出的偏差事件随后与其他新事件一起被路由处理。
② on_events(events, day)：为新的信号事件生成反馈卡片（feedback 事件，开关 feedback_cards），返回"必须重规划"级的唤醒；
   关闭卡片时仍按分级唤醒（分级是重规划规则，卡片只是呈现）。
依赖：feedback.cards / deviation / escalation、session.log。被 cases.router 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Callable  # 取值函数

from feedback.cards import level, load_feedback_config, make_card  # 卡片与分级
from feedback.deviation import check_deviations  # 偏差
from feedback.escalation import Escalation  # 升级
from session.log import CASE_CLOSED, Session  # 事件日志


class FeedbackLoop:
    # 功能：见文件摘要。依赖：session、当前环境取值函数、配置、三个消融开关。
    def __init__(self, session: Session, get_env: Callable, *, config: dict | None = None,
                 cards: bool = True, deviation: bool = True, escalation: bool = True):
        self.session, self.get_env = session, get_env  # 依赖
        self.config = config or load_feedback_config()  # 配置
        self.cards, self.deviation = cards, deviation  # 开关
        self.escalation = Escalation(session, config=self.config, enabled=escalation)  # 升级

    def before_routing(self, short_now: set[str], day: int) -> None:
        """偏差比对 + 强制升级检查。"""
        if self.deviation:  # A1 偏差
            check_deviations(self.session, self.get_env(), short_now, day)  # 写偏差事件
        for c in self.session.cases():  # 每个案子
            if c["state"] != CASE_CLOSED:  # 未关闭
                self.escalation.check(c["case_id"], day)  # 强制升级

    def on_events(self, events: list[dict], day: int) -> list[tuple[str, str]]:
        """为信号生成卡片；返回必须重规划级的唤醒。"""
        wakes = []  # 唤醒
        for e in events:  # 新事件
            lv = level(e, self.config) if e["case_id"] else None  # 分级（无案子的事件只记录）
            if lv is None:  # 只记录
                continue  # 下一个
            if self.cards:  # 生成卡片
                text = make_card(self.session, e, self.config)  # 代码卡片
                self.session.append("feedback", day=day, actor="feedback", case_id=e["case_id"],  # 反馈事件
                                    payload={"signal_seq": e["seq"], "signal": e["type"], "level": lv, "text": text},
                                    caused_by=e["seq"])  # 因果
            if lv == "must":  # 必须重规划
                wakes.append((e["case_id"], f"必须重规划：{e['type']} #{e['seq']}"))  # 唤醒
        return wakes  # 结果

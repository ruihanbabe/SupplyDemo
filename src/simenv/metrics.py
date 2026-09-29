"""文件摘要（F03 环境内核 · 结果指标）

运行结束（或任意时刻）从环境状态计算 spec §4.6 的环境侧三项结果指标：
- 停线天数：所有工单"轮到它上线却缺料"的天数之和；
- 订单延期天数：所有订单过期未交齐的天数之和（交齐当天也算）；
- 采购成本：运行中经写入接口下的采购单金额之和（不含开局前的在途单）。
另两项（违规次数、打扰人工次数）的数据在 Session 事件日志里，由 F06 投影、F27 汇总，这里不重复计算。
依赖：model。被 cli（sim-run）、F04 基线、F27 Eval 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from simenv.model import WO_DONE, State, state_hash  # 工单完工常量、状态、哈希


# ---------------------------------------------------------------------------
# outcome_metrics：计算结果指标
# 功能：返回总计与按板拆分的指标，外加当前日与状态哈希（便于复现比对）。
# 依赖：State 的订单、工单、采购单；被 cli 与后续 Eval 使用。
# 完成：纯计算、不修改状态；成本为十进制字符串。
# ---------------------------------------------------------------------------
def outcome_metrics(state: State) -> dict:
    by_board = {}  # 板 → 分项指标
    for board in sorted(state.boards):  # 每块板
        orders = [o for o in state.orders.values() if o.board == board]  # 该板订单
        wos = [w for w in state.work_orders.values() if w.board == board]  # 该板工单
        by_board[board] = {  # 该板指标
            "stop_line_days": sum(w.stop_days for w in wos),  # 停线天数
            "order_delay_days": sum(o.delay_days for o in orders),  # 订单延期天数
            "orders": len(orders),  # 订单数
            "orders_completed": sum(o.completed_day is not None for o in orders),  # 已交齐订单数
            "work_orders": len(wos),  # 工单数
            "work_orders_done": sum(w.status == WO_DONE for w in wos),  # 已完工工单数
        }
    return {  # 总计
        "day": state.day,  # 当前模拟日
        "stop_line_days": sum(b["stop_line_days"] for b in by_board.values()),  # 停线天数合计
        "order_delay_days": sum(b["order_delay_days"] for b in by_board.values()),  # 延期天数合计
        "procurement_cost": state.procurement_cost(),  # 采购成本
        "by_board": by_board,  # 按板拆分
        "state_hash": state_hash(state),  # 状态哈希
    }

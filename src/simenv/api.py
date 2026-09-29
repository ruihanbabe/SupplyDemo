"""文件摘要（F03 环境内核 · 对外门面）

Environment 是模拟环境唯一的对外入口（spec §5.1：环境状态只能查询，写入只能经 Procurement 执行器）。
- 查询（类 ERP，返回纯 dict、每条带 simulated 标记）：当前日、订单、工单、库存（在库/预留/可用/在途）、采购单、运单、BOM。
- 写入：submit_purchase_order（带幂等键）；先校验、再落写入日志、再作用于内存状态。
- 推进：advance() 推进一天（注入函数由 F04 挂入）。
- 快照、分叉与崩溃恢复见 step ④ 的 store 参数（未给 store 时为纯内存运行，如预演）。
F07 的环境 MCP Server 会把这些方法包装成工具；Agent 手里没有任何改计划/改工单的方法（G8）。
依赖：model、engine。被 F07（工具层）、F21（Procurement 执行器）、cli 与测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Iterable  # 注入函数列表类型
from dataclasses import asdict  # 对象转字典

from simenv import engine  # 日推进与写入
from simenv.model import State  # 整体状态


class NotFound(KeyError):
    """查询的编号不存在。"""


class Environment:
    # -----------------------------------------------------------------------
    # 构造
    # 功能：包住一个 State 与本次运行要挂的注入函数。
    # 依赖：State 来自 seed.generate；injectors 来自 F04（F03 默认空）。
    # 完成：注入函数不进状态（不可序列化），恢复时由调用方重新提供同一组。
    # -----------------------------------------------------------------------
    def __init__(self, state: State, injectors: Iterable[engine.Injector] = ()):
        self.state = state  # 当前状态
        self.injectors = list(injectors)  # 每日注入函数

    # -----------------------------------------------------------------------
    # 推进
    # 功能：推进一天，返回当天事实摘要（到货、发货、开工、停线、完工、延期订单）。
    # 依赖：engine.advance；被 Runtime（F09）在所有案子都在等待时调用（spec §4.2）。
    # 完成：直接委托引擎。
    # -----------------------------------------------------------------------
    def advance(self) -> dict[str, list]:
        return engine.advance(self.state, self.injectors)  # 推进一天

    # -----------------------------------------------------------------------
    # 写入：提交采购单
    # 功能：外部唯一写入口；同一幂等键重复提交返回同一张单（duplicate=True），不重复下单。
    # 依赖：engine.is_duplicate / validate_write / apply_write；被 F21 Procurement 执行器调用（G2 令牌在执行器一侧校验）。
    # 完成：校验不过抛 WriteRejected，状态不变；_record 是写入日志挂点（step ④ 实现落库）。
    # -----------------------------------------------------------------------
    def submit_purchase_order(self, *, mpn: str, qty: int, distributor: str,
                              unit_price: str, idempotency_key: str) -> dict:
        write = {"kind": "submit_po", "idempotency_key": idempotency_key,  # 组装写入
                 "payload": {"mpn": mpn, "qty": qty, "distributor": distributor,  # 采购单内容
                             "unit_price": str(unit_price)}}  # 单价统一为字符串
        if engine.is_duplicate(self.state, write):  # 重复写入
            return engine.apply_write(self.state, write)  # 直接返回原单，不记日志
        engine.validate_write(self.state, write)  # 先校验，不合法的写入不进日志
        self._record(write)  # 先落日志（崩溃后可重放）
        return engine.apply_write(self.state, write)  # 再作用于内存状态

    def _record(self, write: dict) -> None:
        """写入日志挂点：纯内存运行时什么都不做。"""

    # -----------------------------------------------------------------------
    # 查询
    # 功能：给 Agent（经 F07 工具）与代码组件（F21 影响分析等）读环境状态。
    # 依赖：state；不修改任何状态。
    # 完成：按编号取单条（不存在抛 NotFound）；按条件列表（按编号排序，结果确定）；库存汇总在库/预留/可用/在途。
    # -----------------------------------------------------------------------
    def today(self) -> dict:
        return {"day": self.state.day, "horizon_days": self.state.horizon_days,  # 当前日与总天数
                "seed": self.state.seed, "simulated": True}  # 种子与模拟标记

    def get_order(self, order_id: str) -> dict:
        return asdict(self._get(self.state.orders, order_id))  # 单张订单

    def list_orders(self, board: str | None = None) -> list[dict]:
        return [asdict(o) for _, o in sorted(self.state.orders.items())  # 按订单号
                if board is None or o.board == board]  # 可按板筛选

    def get_work_order(self, wo_id: str) -> dict:
        return asdict(self._get(self.state.work_orders, wo_id))  # 单张工单

    def list_work_orders(self, board: str | None = None, status: str | None = None) -> list[dict]:
        return [asdict(w) for _, w in sorted(self.state.work_orders.items())  # 按工单号
                if (board is None or w.board == board)  # 可按板筛选
                and (status is None or w.status == status)]  # 可按状态筛选

    def work_order_needs(self, wo_id: str) -> dict[str, int]:
        return engine.need_of(self.state, self._get(self.state.work_orders, wo_id))  # 领料清单

    def get_inventory(self, mpn: str) -> dict:
        if mpn not in self.state.on_hand:  # 不是本环境管的料
            raise NotFound(mpn)  # 报不存在
        on_hand = self.state.on_hand[mpn]  # 在库
        reserved = self.state.reserved.get(mpn, 0)  # 预留
        inbound = [{"shipment_id": s.id, "po_id": s.po_id, "qty": s.qty, "eta_day": s.eta_day}  # 在途明细
                   for _, s in sorted(self.state.shipments.items())  # 按运单号
                   if s.mpn == mpn and s.status == "in_transit"]  # 该料在途的
        return {"mpn": mpn, "on_hand": on_hand, "reserved": reserved,  # 在库与预留
                "available": on_hand - reserved,  # 可用 = 在库 − 预留
                "in_transit": sum(i["qty"] for i in inbound), "inbound": inbound,  # 在途合计与明细
                "simulated": True}  # 模拟标记

    def list_inventory(self, board: str | None = None) -> list[dict]:
        mpns = self.state.boards[board] if board else self.state.on_hand  # 按板或全部料号
        return [self.get_inventory(m) for m in sorted(mpns)]  # 逐颗汇总

    def get_purchase_order(self, po_id: str) -> dict:
        return asdict(self._get(self.state.purchase_orders, po_id))  # 单张采购单

    def list_purchase_orders(self, mpn: str | None = None, status: str | None = None) -> list[dict]:
        return [asdict(p) for _, p in sorted(self.state.purchase_orders.items())  # 按单号
                if (mpn is None or p.mpn == mpn)  # 可按料号筛选
                and (status is None or p.status == status)]  # 可按状态筛选

    def get_shipment(self, shipment_id: str) -> dict:
        return asdict(self._get(self.state.shipments, shipment_id))  # 单张运单

    def list_shipments(self, mpn: str | None = None, status: str | None = None) -> list[dict]:
        return [asdict(s) for _, s in sorted(self.state.shipments.items())  # 按运单号
                if (mpn is None or s.mpn == mpn)  # 可按料号筛选
                and (status is None or s.status == status)]  # 可按状态筛选

    def get_bom(self, board: str) -> dict[str, int]:
        return dict(self._get(self.state.boards, board))  # 板 BOM 的拷贝

    @staticmethod
    def _get(table: dict, key: str):
        if key not in table:  # 编号不存在
            raise NotFound(key)  # 报不存在
        return table[key]  # 返回对象

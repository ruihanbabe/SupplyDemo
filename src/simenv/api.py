"""文件摘要（F03 环境内核 · 对外门面）

Environment 是模拟环境唯一的对外入口（spec §5.1：环境状态只能查询，写入只能经 Procurement 执行器）。
- 查询（类 ERP，返回纯 dict、每条带 simulated 标记）：当前日、订单、工单、库存（在库/预留/可用/在途）、采购单、运单、BOM。
- 写入：submit_purchase_order（带幂等键）；先校验、再落写入日志、再作用于内存状态。
- 推进：advance() 推进一天（注入函数由 F04 挂入）。
- 持久化（给了 store 时）：create 存第 0 天快照；每次 advance 后存当日"开盘"快照；每条写入先落日志再生效。
- recover：最近快照 + 重放其后写入，恢复出与未崩溃时哈希相同的状态；load：从某日快照分叉成新运行，或不带 store 做预演。
F07 的环境 MCP Server 会把这些方法包装成工具；Agent 手里没有任何改计划/改工单的方法（G8）。
依赖：model、engine、store、seed。被 F07（工具层）、F21（Procurement 执行器）、cli 与测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Iterable  # 注入函数列表类型
from dataclasses import asdict  # 对象转字典

from simenv import engine  # 日推进与写入
from simenv.model import State, state_hash  # 整体状态与哈希
from simenv.seed import generate  # 按种子生成初始状态
from simenv.store import EnvStore, SnapshotCorrupted  # 存储接口与快照损坏异常


class NotFound(KeyError):
    """查询的编号不存在。"""


class Environment:
    # -----------------------------------------------------------------------
    # 构造
    # 功能：包住一个 State、本次运行要挂的注入函数，以及可选的存储与运行编号。
    # 依赖：State 来自 seed.generate 或快照；injectors 来自 F04（F03 默认空）；store 见 store.py。
    # 完成：注入函数不进状态（不可序列化），恢复时由调用方重新提供同一组；无 store 即纯内存运行。
    # -----------------------------------------------------------------------
    def __init__(self, state: State, injectors: Iterable[engine.Injector] = (),
                 store: EnvStore | None = None, run_id: str | None = None):
        if store is not None and not run_id:  # 要持久化就必须有运行编号
            raise ValueError("给了 store 就必须给 run_id")  # 报错
        self.state = state  # 当前状态
        self.injectors = list(injectors)  # 每日注入函数
        self.store = store  # 存储（可为空）
        self.run_id = run_id  # 运行编号（可为空）

    # -----------------------------------------------------------------------
    # create：新建一次运行
    # 功能：按参数、BOM、种子生成第 0 天状态；有 store 时存下第 0 天快照作为恢复与分叉的起点。
    # 依赖：seed.generate；被 cli、F09 Runtime、F27 Eval 使用。
    # 完成：返回可直接推进的 Environment。
    # -----------------------------------------------------------------------
    @classmethod
    def create(cls, params: dict, boms: dict, seed: int, *, store: EnvStore | None = None,
               run_id: str | None = None, injectors: Iterable[engine.Injector] = ()) -> Environment:
        env = cls(generate(params, boms, seed), injectors, store, run_id)  # 生成并包装
        env._snapshot()  # 存第 0 天快照
        return env  # 返回新环境

    # -----------------------------------------------------------------------
    # 推进
    # 功能：推进一天，存当日开盘快照，返回当天事实摘要（到货、发货、开工、停线、完工、延期订单）。
    # 依赖：engine.advance；被 Runtime（F09）在所有案子都在等待时调用（spec §4.2）。
    # 完成：快照在当日机制之后、任何外部写入之前，所以只写一次、不更新。
    # -----------------------------------------------------------------------
    def advance(self) -> dict[str, list]:
        log = engine.advance(self.state, self.injectors)  # 推进一天
        self._snapshot()  # 存当日开盘快照
        return log  # 返回当天事实

    def _snapshot(self, forked_from: tuple[str, int] | None = None) -> None:
        if self.store is not None:  # 有存储才存
            self.store.save_snapshot(self.run_id, self.state.day, self.state.to_dict(),  # 整状态
                                     state_hash(self.state), forked_from)  # 哈希与分叉来源

    # -----------------------------------------------------------------------
    # recover：崩溃后恢复
    # 功能：载入该运行最近一日快照，重放该日及以后的写入；若日志里有比快照更晚的日子
    #       （进程死在"推进之后、存快照之前"），就按确定性机制补推进到那一天并补存快照。
    # 依赖：store.load_snapshot / list_writes、engine.advance / apply_write、_load_state（哈希校验）。
    # 完成：恢复出的状态哈希与未崩溃运行一致（离线与集成测试覆盖）；返回继续挂着同一 store 的 Environment。
    # -----------------------------------------------------------------------
    @classmethod
    def recover(cls, store: EnvStore, run_id: str,
                injectors: Iterable[engine.Injector] = ()) -> Environment:
        snap = store.load_snapshot(run_id)  # 最近一日快照
        if snap is None:  # 这个运行从没存过快照
            raise LookupError(f"运行 {run_id} 没有快照，无法恢复")  # 报错
        env = cls(_load_state(snap), injectors, store, run_id)  # 从快照重建
        for _, day, write in store.list_writes(run_id, from_day=snap[0]):  # 按序号重放
            while env.state.day < day:  # 写入属于更晚的一天
                env.advance()  # 确定性补推进（并补存快照）
            engine.apply_write(env.state, write)  # 重放写入（不再记日志）
        return env  # 返回恢复后的环境

    # -----------------------------------------------------------------------
    # load：从某日快照分叉或预演
    # 功能：into=(新 store, 新运行编号) 时把快照复制为新运行的起点（分叉，spec §4.6、§5.12）；
    #       into=None 时只在内存里载入（预演：随便推进、随便写，不影响任何记录，spec §5.5 审批预演）。
    # 依赖：store.load_snapshot、_load_state；被 F18 分叉重跑、F25 审批预演、F27 基线对比使用。
    # 完成：分叉快照记录来源（运行, 日）；从开盘快照起，分叉点当天的原写入不带过去。
    # -----------------------------------------------------------------------
    @classmethod
    def load(cls, store: EnvStore, run_id: str, day: int, *,
             into: tuple[EnvStore, str] | None = None,
             injectors: Iterable[engine.Injector] = ()) -> Environment:
        snap = store.load_snapshot(run_id, day)  # 取指定日快照
        if snap is None:  # 没有这天的快照
            raise LookupError(f"运行 {run_id} 没有第 {day} 天的快照")  # 报错
        state = _load_state(snap)  # 校验并还原
        if into is None:  # 预演
            return cls(state, injectors)  # 纯内存环境
        env = cls(state, injectors, *into)  # 新运行
        env._snapshot(forked_from=(run_id, day))  # 存分叉起点快照
        return env  # 返回分叉环境

    # -----------------------------------------------------------------------
    # 写入：提交采购单
    # 功能：外部唯一写入口；同一幂等键重复提交返回同一张单（duplicate=True），不重复下单。
    # 依赖：engine.is_duplicate / validate_write / apply_write；被 F21 Procurement 执行器调用（G2 令牌在执行器一侧校验）。
    # 完成：校验不过抛 WriteRejected，状态不变；有 store 时先落日志再生效（崩溃在两者之间也能重放）。
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
        if self.store is not None:  # 有存储才记日志
            self.store.append_write(self.run_id, self.state.day, write)  # 追加写入日志

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


# ---------------------------------------------------------------------------
# _load_state：把快照记录还原为 State 并校验哈希
# 功能：防止快照被改坏后悄悄继续运行。
# 依赖：State.from_dict、state_hash；被 recover 与 load 使用。
# 完成：重算哈希与存下的哈希不一致即抛 SnapshotCorrupted。
# ---------------------------------------------------------------------------
def _load_state(snap: tuple[int, dict, str]) -> State:
    _, data, expected = snap  # 拆开快照记录
    state = State.from_dict(data)  # 还原状态
    if state_hash(state) != expected:  # 哈希对不上
        raise SnapshotCorrupted(f"第 {state.day} 天快照哈希不符")  # 拒绝使用
    return state  # 返回状态

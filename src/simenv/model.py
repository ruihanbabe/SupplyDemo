"""文件摘要（F03 环境内核 · 领域模型）

本文件定义模拟工厂的全部业务对象和整体状态（spec §4.1），以及状态的规范化序列化与哈希。
- 对象：客户订单 Order、工单 WorkOrder、采购单 PurchaseOrder、运单 Shipment、整体状态 State。
- 库存直接放在 State 上：在库 on_hand、预留 reserved（每日由 engine 重算），在途由运单推出。
- 序列化：to_dict / from_dict 往返无损；canonical_json + state_hash 给快照（step ④）和复现校验用。
依赖：只用标准库。被 engine（日推进）、seed（生成初始状态）、api（查询与写入）、store（快照）使用。
"""
from __future__ import annotations  # 让类型注解延迟求值，便于前向引用

import hashlib  # 计算状态哈希（SHA-256）
import json  # 规范化 JSON 序列化
from dataclasses import asdict, dataclass, field  # 用数据类描述领域对象
from decimal import Decimal  # 金额用十进制，避免浮点误差

# ---------------------------------------------------------------------------
# 状态常量
# 功能：集中定义各对象生命周期的状态取值，避免魔法字符串散落各处。
# 依赖：无；被 engine、api、metrics 与测试引用。
# 完成：工单 4 态、采购单 3 态（环境只管已提交之后，草稿/审批中属 Harness 侧，见 Contract）、运单 2 态。
# ---------------------------------------------------------------------------
WO_PLANNED = "planned"  # 工单：已排计划，尚未开工
WO_STOPPED = "stopped"  # 工单：已到开工日、轮到它上线，但缺料停线
WO_STARTED = "started"  # 工单：已领料开工，在制
WO_DONE = "done"  # 工单：已完工，成品已计入订单

PO_SUBMITTED = "submitted"  # 采购单：已提交给分销商
PO_SHIPPED = "shipped"  # 采购单：分销商已发货（已生成运单）
PO_ARRIVED = "arrived"  # 采购单：货已到厂入库

SH_IN_TRANSIT = "in_transit"  # 运单：在途
SH_ARRIVED = "arrived"  # 运单：已到厂


# ---------------------------------------------------------------------------
# Order：客户订单
# 功能：记录一张订单要多少台哪块板、哪天到期，以及交付与延期进度。
# 依赖：被 WorkOrder.order_id 引用；engine 在完工与到期检查时更新；metrics 汇总延期天数。
# 完成：交付数量累计、完成日记录、按天累计的延期天数。
# ---------------------------------------------------------------------------
@dataclass
class Order:
    id: str  # 订单号，如 "O-001"
    board: str  # 板标识，等于 hw_part.project_id，如 "hackrf-one"
    qty: int  # 订购台数
    due_day: int  # 到期日（模拟日序号，第 1 天起）
    delivered_qty: int = 0  # 已交付台数（工单完工时累加）
    completed_day: int | None = None  # 交齐的那一天；未交齐为 None
    delay_days: int = 0  # 已累计的延期天数（到期后每过一天未交齐 +1）
    simulated: bool = True  # 模拟数据标记（spec §1.3：每条记录都标记为模拟数据）


# ---------------------------------------------------------------------------
# WorkOrder：生产工单（给定输入，G8：Agent 不能改）
# 功能：描述某条产线在某天开工生产多少台某板，工期几天；记录实际开工、完工与缺料停线天数。
# 依赖：引用 Order.id；engine 按开工日先后预留、领料、推进；metrics 汇总停线天数。
# 完成：计划字段 + 运行字段（状态、实际开工日、完工日、停线天数）。
# ---------------------------------------------------------------------------
@dataclass
class WorkOrder:
    id: str  # 工单号，如 "W-001"
    board: str  # 生产哪块板
    order_id: str  # 服务哪张订单
    line: str  # 在哪条产线上生产
    qty: int  # 生产台数
    start_day: int  # 计划开工日
    duration_days: int  # 工期（开工到完工的天数）
    status: str = WO_PLANNED  # 当前状态，见上方 WO_* 常量
    started_day: int | None = None  # 实际开工日
    done_day: int | None = None  # 实际完工日
    stop_days: int = 0  # 因缺料停线的累计天数
    simulated: bool = True  # 模拟数据标记


# ---------------------------------------------------------------------------
# PurchaseOrder：采购单（环境侧，从"已提交"开始）
# 功能：记录向哪个分销商买哪颗料多少颗、单价、何时提交、何时发货，以及对应运单。
# 依赖：由 engine.apply_write（外部写入）或 seed（初始在途）创建；engine 推进发货与到货；metrics 汇总成本。
# 完成：幂等键记录、计划发货日、状态流转所需字段；from_run 区分运行中下的单与初始在途单（成本只算前者）。
# ---------------------------------------------------------------------------
@dataclass
class PurchaseOrder:
    id: str  # 采购单号，如 "PO-001"
    mpn: str  # 料号
    qty: int  # 数量（颗）
    distributor: str  # 分销商，如 "digikey"
    unit_price: str  # 单价（十进制字符串，避免浮点），如 "1.35"
    submitted_day: int  # 提交日
    ship_day: int  # 计划发货日（提交日 + 该分销商备货天数）
    status: str = PO_SUBMITTED  # 当前状态，见 PO_* 常量
    shipment_id: str | None = None  # 发货后对应的运单号
    idempotency_key: str | None = None  # 外部写入的幂等键；初始在途单为 None
    from_run: bool = True  # True=运行中经写入接口下的单；False=种子生成的初始在途单
    simulated: bool = True  # 模拟数据标记


# ---------------------------------------------------------------------------
# Shipment：入库运单
# 功能：记录一批货预计哪天到厂；物流延误（A1，F04 注入）就是推迟这里的 eta_day。
# 依赖：由 engine 在采购单发货时创建，或由 seed 创建初始在途运单；engine 在 eta_day 办理到货。
# 完成：原始预计到货日与当前预计到货日分开存，便于 F04/F14 比对偏差。
# ---------------------------------------------------------------------------
@dataclass
class Shipment:
    id: str  # 运单号，如 "SH-001"
    po_id: str  # 对应采购单号
    mpn: str  # 料号
    qty: int  # 数量
    eta_day: int  # 当前预计到货日（可被注入推迟）
    original_eta_day: int  # 发货时的预计到货日（不变，用于对比延误）
    status: str = SH_IN_TRANSIT  # 当前状态，见 SH_* 常量
    arrived_day: int | None = None  # 实际到货日
    simulated: bool = True  # 模拟数据标记


# ---------------------------------------------------------------------------
# State：整个模拟环境在某一时刻的完整状态
# 功能：把所有对象、库存、BOM、参数与计数器装在一起，是快照、分叉、恢复的最小单位。
# 依赖：由 seed 生成、engine 推进、store 存取；只包含可 JSON 化的数据（注入函数等不放这里）。
# 完成：日序号、种子、板 BOM、产线、各对象字典、库存、分销商参数、编号计数器、幂等键索引、运行指标累计。
# ---------------------------------------------------------------------------
@dataclass
class State:
    day: int  # 当前模拟日：已执行完该日机制；0 表示初始状态、尚未推进
    seed: int  # 生成本状态所用的随机种子（复现用）
    horizon_days: int  # 模拟总天数（如 84）
    boards: dict[str, dict[str, int]]  # 板 → {料号: 单板用量}，即 BOM
    lines: list[str]  # 产线列表，如 ["L1", "L2"]
    distributors: dict[str, dict[str, int]]  # 分销商 → {"ship_days": 备货天数, "transit_days": 运输天数}
    orders: dict[str, Order] = field(default_factory=dict)  # 订单号 → 订单
    work_orders: dict[str, WorkOrder] = field(default_factory=dict)  # 工单号 → 工单
    purchase_orders: dict[str, PurchaseOrder] = field(default_factory=dict)  # 采购单号 → 采购单
    shipments: dict[str, Shipment] = field(default_factory=dict)  # 运单号 → 运单
    on_hand: dict[str, int] = field(default_factory=dict)  # 料号 → 在库数量
    reserved: dict[str, int] = field(default_factory=dict)  # 料号 → 被未开工工单预留的数量（每日重算）
    counters: dict[str, int] = field(default_factory=dict)  # 编号前缀 → 已用最大序号，如 {"PO": 3}
    idempotency: dict[str, str] = field(default_factory=dict)  # 幂等键 → 采购单号（防重复下单）

    # -----------------------------------------------------------------------
    # next_id：分配新编号
    # 功能：按前缀递增生成 "PO-004" 这样的编号；计数器存在状态里，所以快照/恢复后编号仍连续、可复现。
    # 依赖：被 engine（新运单、新采购单）与 seed 使用。
    # 完成：前缀计数、三位补零。
    # -----------------------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        n = self.counters.get(prefix, 0) + 1  # 取该前缀的下一个序号
        self.counters[prefix] = n  # 回写计数器
        return f"{prefix}-{n:03d}"  # 拼成三位补零的编号

    # -----------------------------------------------------------------------
    # to_dict / from_dict：状态与纯字典互转
    # 功能：给快照存 JSONB、给分叉深拷贝、给哈希做规范化输入。
    # 依赖：dataclasses.asdict；被 canonical_json、store、api.fork 使用。
    # 完成：嵌套对象按类型还原，往返无损（离线测试覆盖）。
    # -----------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)  # 数据类递归转字典（嵌套对象也会转）

    @classmethod
    def from_dict(cls, data: dict) -> State:
        data = json.loads(json.dumps(data))  # 深拷贝一份，避免与调用方共享可变对象
        data["orders"] = {k: Order(**v) for k, v in data["orders"].items()}  # 还原订单对象
        data["work_orders"] = {k: WorkOrder(**v) for k, v in data["work_orders"].items()}  # 还原工单
        data["purchase_orders"] = {  # 还原采购单
            k: PurchaseOrder(**v) for k, v in data["purchase_orders"].items()}  # 逐张构造
        data["shipments"] = {k: Shipment(**v) for k, v in data["shipments"].items()}  # 还原运单
        return cls(**data)  # 其余字段本就是基本类型，直接构造

    # -----------------------------------------------------------------------
    # in_transit：某料号的在途数量
    # 功能：库存三分之一"在途"由运单推出，不另存，避免两份数据不一致。
    # 依赖：被 api 的库存查询使用。
    # 完成：只统计状态为在途的运单。
    # -----------------------------------------------------------------------
    def in_transit(self, mpn: str) -> int:
        return sum(s.qty for s in self.shipments.values()  # 累加数量
                   if s.mpn == mpn and s.status == SH_IN_TRANSIT)  # 只算该料号且在途的运单

    # -----------------------------------------------------------------------
    # procurement_cost：运行中下单的采购总成本
    # 功能：spec §4.6 结果指标之一；只算运行中经写入接口下的单，不算初始在途单。
    # 依赖：被 metrics 使用。
    # 完成：Decimal 精确求和，返回字符串保持可 JSON 化。
    # -----------------------------------------------------------------------
    def procurement_cost(self) -> str:
        total = sum((Decimal(p.unit_price) * p.qty for p in self.purchase_orders.values()  # 单价×数量
                     if p.from_run), Decimal(0))  # 只算运行中下的单，初值为 0
        return str(total)  # 以字符串返回，避免浮点


# ---------------------------------------------------------------------------
# canonical_json / state_hash：规范化序列化与哈希
# 功能：同一状态无论字典插入顺序如何都得到同一字节串与同一哈希，是"同种子复现"和"恢复一致"的判据。
# 依赖：State.to_dict；被 store（快照哈希）、metrics、测试使用。
# 完成：键排序、紧凑分隔符、保留中文；SHA-256 十六进制。
# ---------------------------------------------------------------------------
def canonical_json(state: State) -> str:
    return json.dumps(state.to_dict(), sort_keys=True,  # 键排序保证顺序无关
                      separators=(",", ":"), ensure_ascii=False)  # 紧凑、不转义中文


def state_hash(state: State) -> str:
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()  # 规范 JSON 的 SHA-256

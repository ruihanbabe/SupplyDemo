"""文件摘要（F07 · 各 MCP Server 的工具定义）

- environment_server：类 ERP 只读工具五个（查库存、采购单、运单、工单、订单，spec §5.4），数据来自 F03 Environment。
  没有任何写工具、改计划或改工单的工具（G8；写入经 F21 Procurement 执行器）。
- distributor_server：查报价（多料 × 多家），后端可替换；FixtureDistributorBackend 是固定样例后端：
  按 (料号, 分销商) 的哈希确定性生成，并模仿三家各自的原文格式（库存字符串 / 嵌套 / 数字、交期带单位 / 周），
  再归一化——保留原文值，解析失败时归一化值留空（旧适配器 976bb17 的经验）。真实后端等用户授权（G5）。
- validate_quotes：返回校验（B3），缺失字段置"未知"并警告，整体结构不对抛 BadFormat。
- archive_server：按记录号 / 游标读留档（由 ToolProxy.read_record 实现，不调用上游）。
- control_server：编排控制五个工具的参数模型，调用转交给注册的处理函数（语义归 F09/F22）。
依赖：tools.server、models.types（Mpn）、simenv.api、pydantic。被 ToolProxy 装配使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import hashlib  # 固定样例的确定性哈希
import re  # 解析原文格式
from collections.abc import Callable  # 函数类型

from pydantic import BaseModel, Field  # 参数模型

from models.types import Mpn  # 料号字段标记
from simenv.api import Environment  # 模拟环境门面
from simenv.api import NotFound as EnvNotFound  # 环境查不到
from tools.server import BadFormat, CallContext, NotFound, Server, ToolDef  # 工具定义

DISTRIBUTORS = ("digikey", "mouser", "element14")  # 三家分销商


# ---------------------------------------------------------------------------
# 环境 Server
# 功能：把 Environment 的只读查询包装成工具；get_env 每次调用取当前环境（崩溃恢复后环境对象会换）。
# 依赖：simenv.api.Environment；被 ToolProxy 注册。
# 完成：五个工具各自的参数模型、实体键、查不到转 NotFound。
# ---------------------------------------------------------------------------
class InventoryArgs(BaseModel):
    """查库存参数：给料号列表或板（二选一）。"""
    mpns: list[Mpn] = Field(default_factory=list, max_length=50)  # 料号列表
    board: str | None = None  # 板


class PoArgs(BaseModel):
    """查采购单参数。"""
    mpn: Mpn | None = None  # 按料号
    status: str | None = None  # 按状态
    po_id: str | None = None  # 按单号


class ShipmentArgs(BaseModel):
    """查运单参数。"""
    mpn: Mpn | None = None  # 按料号
    status: str | None = None  # 按状态
    shipment_id: str | None = None  # 按运单号


class WorkOrderArgs(BaseModel):
    """查工单参数。"""
    board: str | None = None  # 按板
    status: str | None = None  # 按状态
    wo_id: str | None = None  # 按工单号
    with_needs: bool = False  # 是否附领料清单


class OrderArgs(BaseModel):
    """查订单参数。"""
    board: str | None = None  # 按板
    order_id: str | None = None  # 按订单号


def environment_server(get_env: Callable[[], Environment]) -> Server:
    """构造环境 Server。"""

    def guard(fn):  # 把环境的 NotFound 转成代理层的 NotFound
        def wrapped(args, ctx):  # 包装函数
            try:  # 调用
                return fn(get_env(), args)  # 执行查询
            except (EnvNotFound, KeyError) as exc:  # 查不到
                raise NotFound(f"查不到：{exc}") from None  # 转换
        return wrapped  # 返回包装

    def inventory(env, a):  # 查库存
        if a.mpns:  # 按料号
            return [env.get_inventory(m) for m in a.mpns]  # 逐颗
        return env.list_inventory(a.board)  # 按板或全部

    def pos(env, a):  # 查采购单
        return [env.get_purchase_order(a.po_id)] if a.po_id else env.list_purchase_orders(a.mpn, a.status)  # 单条或列表

    def shipments(env, a):  # 查运单
        return [env.get_shipment(a.shipment_id)] if a.shipment_id else env.list_shipments(a.mpn, a.status)  # 同上

    def work_orders(env, a):  # 查工单
        rows = [env.get_work_order(a.wo_id)] if a.wo_id else env.list_work_orders(a.board, a.status)  # 单条或列表
        if a.with_needs:  # 附领料清单
            rows = [{**r, "needs": env.work_order_needs(r["id"])} for r in rows]  # 合并
        return rows  # 结果

    def orders(env, a):  # 查订单
        return [env.get_order(a.order_id)] if a.order_id else env.list_orders(a.board)  # 单条或列表

    def keys_of(**fields):  # 实体键提取器
        return lambda a: {k: [v] if isinstance(v, str) else list(v)  # 统一成列表
                          for k, attr in fields.items() if (v := getattr(a, attr, None))}  # 有值才记

    return Server("environment", [  # 五个只读工具
        ToolDef("env_inventory", "查库存：在库、预留、可用、在途（含每张在途运单的预计到货日）。"  # 描述
                "何时用：判断缺口。例：env_inventory(mpns=['SKY13453-385LF'])", InventoryArgs, guard(inventory),
                entity_keys=keys_of(mpn="mpns", board="board")),  # 实体键
        ToolDef("env_purchase_orders", "查采购单（已提交 / 已发货 / 已到货）。例：env_purchase_orders(mpn='X')",
                PoArgs, guard(pos), entity_keys=keys_of(mpn="mpn", po="po_id")),  # 实体键
        ToolDef("env_shipments", "查入库运单与预计到货日。例：env_shipments(status='in_transit')",
                ShipmentArgs, guard(shipments), entity_keys=keys_of(mpn="mpn", shipment="shipment_id")),
        ToolDef("env_work_orders", "查生产工单（只读；改计划请上报计划员）。例：env_work_orders(board='hackrf-one', with_needs=True)",
                WorkOrderArgs, guard(work_orders), entity_keys=keys_of(board="board", work_order="wo_id")),
        ToolDef("env_orders", "查客户订单与交付、延期。例：env_orders(board='bms-c1')",
                OrderArgs, guard(orders), entity_keys=keys_of(board="board", order="order_id")),
    ])


# ---------------------------------------------------------------------------
# 分销商 Server 与固定样例后端
# 功能：查报价（多料 × 多家）；后端返回各家原文格式，normalize_* 归一化；validate_quotes 做 B3 校验。
# 依赖：hashlib（确定性）、CallContext.unavailable（配额不可用的分销商跳过）。
# 完成：同一 (料号, 分销商, 数量) 永远得到同一报价；价格按数量取阶梯价。
# ---------------------------------------------------------------------------
class QuoteArgs(BaseModel):
    """查报价参数。"""
    mpns: list[Mpn] = Field(min_length=1, max_length=10)  # 料号（不批量查，最多 10 颗）
    qty: int = Field(gt=0)  # 需求数量（决定阶梯价）
    distributors: list[str] = Field(default_factory=lambda: list(DISTRIBUTORS))  # 查哪几家


class FixtureDistributorBackend:
    """固定样例后端：确定性生成三家原文格式的报价。"""

    def raw_quote(self, mpn: str, distributor: str) -> dict:
        h = int(hashlib.sha256(f"{mpn}|{distributor}".encode()).hexdigest(), 16)  # 确定性种子
        stock = (h % 6) * (h % 997)  # 库存：约 1/6 概率为 0
        base = round(0.05 + (h >> 8) % 2000 / 100, 2)  # 基础单价 0.05–20.04 美元
        lead = 3 + (h >> 20) % 60  # 交期天数
        breaks = [(1, base), (100, round(base * 0.9, 4)), (1000, round(base * 0.8, 4))]  # 价格阶梯
        if distributor == "mouser":  # Mouser：库存字符串、交期带单位
            return {"MouserPartNumber": mpn, "AvailabilityInStock": str(stock), "LeadTime": f"{lead} Days",
                    "PriceBreaks": [{"Quantity": q, "Price": f"${p}", "Currency": "USD"} for q, p in breaks]}
        if distributor == "element14":  # element14：库存嵌套、交期裸数字、区间价
            return {"sku": mpn, "stock": {"level": stock}, "leadTime": lead,
                    "prices": [{"from": q, "cost": p} for q, p in breaks]}
        return {"ManufacturerProductNumber": mpn, "QuantityAvailable": stock,  # DigiKey：数字、交期按周
                "ManufacturerLeadWeeks": str(max(1, lead // 7)),
                "StandardPricing": [{"BreakQuantity": q, "UnitPrice": p} for q, p in breaks]}


def _num(text) -> float | None:
    """从原文里取第一个数字；取不到返回 None（不猜）。"""
    m = re.search(r"-?\d+(?:\.\d+)?", str(text)) if text is not None else None  # 匹配数字
    return float(m.group()) if m else None  # 数字或空


def normalize_quote(distributor: str, raw: dict, qty: int) -> dict:
    """把一家的原文报价归一化；保留原文，解析失败的归一化值为 None。"""
    if distributor == "mouser":  # Mouser
        stock_raw, lead_raw = raw.get("AvailabilityInStock"), raw.get("LeadTime")  # 原文
        breaks = [(int(b["Quantity"]), _num(b["Price"])) for b in raw.get("PriceBreaks") or []]  # 阶梯
        lead = _num(lead_raw)  # 天
    elif distributor == "element14":  # element14
        stock_raw, lead_raw = (raw.get("stock") or {}).get("level"), raw.get("leadTime")  # 原文
        breaks = [(int(b["from"]), _num(b["cost"])) for b in raw.get("prices") or []]  # 阶梯
        lead = _num(lead_raw)  # 天
    else:  # DigiKey
        stock_raw, lead_raw = raw.get("QuantityAvailable"), raw.get("ManufacturerLeadWeeks")  # 原文
        breaks = [(int(b["BreakQuantity"]), _num(b["UnitPrice"])) for b in raw.get("StandardPricing") or []]  # 阶梯
        weeks = _num(lead_raw)  # 周
        lead = weeks * 7 if weeks is not None else None  # 换算成天
    price = next((p for q, p in sorted(breaks, reverse=True) if q <= qty), None)  # 取适用阶梯价
    stock = _num(stock_raw)  # 库存数
    return {"distributor": distributor,  # 分销商
            "stock": int(stock) if stock is not None else None,  # 归一化库存
            "unit_price_usd": price, "lead_days": int(lead) if lead is not None else None,  # 单价与交期
            "raw": {"stock": stock_raw, "lead_time": lead_raw}}  # 原文值


def validate_quotes(rows) -> tuple[list[dict], list[str]]:
    """B3：结构不对抛 BadFormat；缺失字段置"未知"并警告，不补猜。"""
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):  # 整体结构
        raise BadFormat("报价返回不是记录列表")  # 格式异常
    warnings, out = [], []  # 警告与结果
    for r in rows:  # 每条报价
        fixed = dict(r)  # 副本
        for f in ("stock", "unit_price_usd", "lead_days"):  # 必需字段
            if fixed.get(f) is None:  # 缺失
                fixed[f] = "unknown"  # 置为未知
                warnings.append(f"{r.get('distributor')} 未返回 {r.get('mpn')} 的 {f}，已置为未知，不要自行推测")  # 警告
        out.append(fixed)  # 收下
    return out, warnings  # 结果与警告


def distributor_server(backend=None) -> Server:
    """构造分销商 Server；backend 默认为固定样例后端。"""
    backend = backend or FixtureDistributorBackend()  # 后端

    def quotes(args: QuoteArgs, ctx: CallContext):  # 处理函数
        rows = []  # 结果
        for mpn in args.mpns:  # 每颗料
            for d in args.distributors:  # 每家
                if d in ctx.unavailable:  # 配额不可用
                    continue  # 部分成功：跳过
                row = normalize_quote(d, backend.raw_quote(mpn, d), args.qty)  # 取原文并归一化
                rows.append({"mpn": mpn, **row})  # 收下
        return rows  # 列表

    return Server("distributor", [ToolDef(
        "distributor_quotes",  # 工具名
        "查多颗料在分销商的库存、单价（按数量取阶梯价）、交期。何时用：比价、确认可买量；报价 1 个模拟日内有效。"
        "例：distributor_quotes(mpns=['SKY13453-385LF'], qty=300)",  # 描述
        QuoteArgs, quotes, source="external",  # 参数、处理、来源
        providers=lambda a: [d for d in DISTRIBUTORS if d in a.distributors],  # 涉及的分销商
        validate=validate_quotes, config_key="distributor_quotes",  # 校验与时钟配置
        entity_keys=lambda a: {"mpn": list(a.mpns), "distributor": list(a.distributors)})])  # 实体键


# ---------------------------------------------------------------------------
# 留档 Server 与编排控制 Server
# 功能：留档：read_record 由 ToolProxy 特殊处理（读留档不调上游）；编排控制：五个工具转交处理函数。
# 依赖：ToolProxy（留档）；控制处理函数由 F09/F22 注册。
# 完成：参数模型；控制处理函数缺省时报"尚未具备"。
# ---------------------------------------------------------------------------
class ReadRecordArgs(BaseModel):
    """读留档参数：记录号或游标（rec-N 或 rec-N:偏移）。"""
    cursor: str = Field(pattern=r"^rec-\d+(:\d+)?$")  # 游标格式


def archive_server() -> Server:
    """留档 Server：处理函数不会被调用（ToolProxy 直接读留档）。"""
    def never(args, ctx):  # 占位
        raise AssertionError("read_record 由 ToolProxy.read_record 处理")  # 不应到达
    return Server("archive", [ToolDef("read_record", "按记录号读工具返回全文或翻页，不产生外部调用。"
                                      "例：read_record(cursor='rec-12:10')", ReadRecordArgs, never)])


class DispatchArgs(BaseModel):
    """派任务。"""
    executor: str  # 交给哪个执行者
    task: str  # 任务说明
    inputs: list[str] = Field(default_factory=list)  # 输入 Artifact 引用（ID@v版本）
    reason: str  # 一句理由（写进计划，spec §5.9）


class GoalRevisionArgs(BaseModel):
    """提出目标修订。"""
    new_goal: str  # 新目标
    reason: str  # 理由


class PlannerReportArgs(BaseModel):
    """上报计划员（G8：排产相关只能上报）。"""
    impact_artifact: str  # 影响分析 Artifact 引用
    reason: str  # 理由


class AskUserArgs(BaseModel):
    """问用户。"""
    question: str  # 问题
    options: list[str] = Field(default_factory=list)  # 候选答案


class EndSegmentArgs(BaseModel):
    """结束本活跃段。"""
    reason: str  # 理由


def control_server(handler: Callable[[str, BaseModel, CallContext], object] | None = None) -> Server:
    """编排控制 Server（仅编排者可见，白名单控制）。"""
    def forward(name):  # 为每个工具生成转交函数
        def call(args, ctx):  # 处理函数
            if handler is None:  # 尚未注册
                raise ConnectionError("编排控制尚未具备（由 F09/F22 注册处理函数）")  # 明确报"尚未具备"
            return handler(name, args, ctx)  # 转交
        return call  # 返回
    specs = [("dispatch_task", "派一个任务给子 Agent 或代码组件；可一次派多个并行。", DispatchArgs),  # 派任务
             ("propose_goal_revision", "目标需要改变时提出修订，交人确认（H4）。", GoalRevisionArgs),  # 目标修订
             ("report_to_planner", "排产相关影响只能上报计划员（H5），不能改计划或工单。", PlannerReportArgs),  # 上报
             ("ask_user", "需要采购员澄清或确认时提问。", AskUserArgs),  # 问用户
             ("end_segment", "所有任务都在等外部结果时结束本活跃段。", EndSegmentArgs)]  # 结束本段
    return Server("control", [ToolDef(n, d, m, forward(n)) for n, d, m in specs])  # 五个工具

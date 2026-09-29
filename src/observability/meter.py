"""文件摘要（F17 · 可观测计量）

在事件日志之上补计量（spec §5.11），每次 LLM / 工具调用一行，用 (运行, 事件序号) 与事件关联：
- Meter.record_llm / record_tool：由 F08 ModelAdapter、F07 ToolProxy 通过可选 meter 参数调用；真实耗时只存这里（不进哈希链）。
- context_snapshot：从 llm_call 事件取当时的完整请求（即组装的上下文），不另存副本。
- case_summary / run_summary：案子与运行汇总（LLM、token、修复降级、工具、缓存、错误、重规划、升级、打扰人工、违规）。
- quota_panel：按真实日期与提供方汇总当日用量与剩余（F26 面板读它）。
- rebuild_quota：Redis 丢失后用当日计量把日配额计数写回（spec §5.1）。
- MemoryMeterStore / PgMeterStore（迁移 0005_metering，只追加）。
依赖：session.log.Session、tools.kv（重建配额）、SQLAlchemy。被 F08、F07 调用；F26 时间线与配额面板、F27 Eval 读取。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # JSON 字段
from collections import Counter  # 计数
from collections.abc import Callable  # 日期函数
from datetime import UTC, datetime  # 真实日期
from typing import Protocol  # 接口

from sqlalchemy import text  # 参数化 SQL

from session.log import EV_CASE_CLOSED, EV_CASE_TRANSITION, Session  # 事件类型与日志

FIELDS = ("run_id", "event_seq", "kind", "case_id", "day", "wall_date", "name", "model",  # 计量字段
          "input_tokens", "output_tokens", "latency_ms", "cache_hit", "quota_used",  # 数值与配额
          "error_type", "repair", "fallback")  # 错误与修复降级


def _today() -> str:
    """真实 UTC 日期。"""
    return datetime.now(UTC).strftime("%Y-%m-%d")  # 如 2026-09-30


class MeterStore(Protocol):
    """计量存储接口。"""

    def add(self, row: dict) -> None: ...  # 追加一行
    def rows(self, run_id: str | None = None, wall_date: str | None = None) -> list[dict]: ...  # 读取


# 功能：内存实现。依赖：json 深拷贝。完成：(运行, 事件序号) 唯一。
class MemoryMeterStore:
    def __init__(self) -> None:
        self.data: list[dict] = []  # 全部行

    def add(self, row):
        if any(r["run_id"] == row["run_id"] and r["event_seq"] == row["event_seq"] for r in self.data):  # 重复
            raise ValueError(f"计量重复：{row['run_id']}#{row['event_seq']}")  # 与主键一致
        self.data.append(json.loads(json.dumps(row)))  # 存副本

    def rows(self, run_id=None, wall_date=None):
        return sorted((json.loads(json.dumps(r)) for r in self.data  # 副本
                       if (run_id is None or r["run_id"] == run_id)  # 运行筛选
                       and (wall_date is None or r["wall_date"] == wall_date)),  # 日期筛选
                      key=lambda r: (r["run_id"], r["event_seq"]))  # 固定顺序


# 功能：Postgres 实现。依赖：metering 表；调用方给连接。完成：quota_used 以 JSONB 存。
class PgMeterStore:
    def __init__(self, connection) -> None:
        self.conn = connection  # 连接

    def add(self, row):
        self.conn.execute(text(f"""
            INSERT INTO metering({', '.join(FIELDS)})
            VALUES ({', '.join(':' + f for f in FIELDS)})"""),  # 插入
            {**row, "quota_used": json.dumps(row["quota_used"])})  # JSON 字段

    def rows(self, run_id=None, wall_date=None):
        found = self.conn.execute(text(f"""
            SELECT {', '.join(FIELDS)} FROM metering
            WHERE (CAST(:r AS TEXT) IS NULL OR run_id = :r) AND (CAST(:d AS TEXT) IS NULL OR wall_date = :d)"""),
            {"r": run_id, "d": wall_date}).mappings().all()  # 查询
        return sorted((dict(r) for r in found), key=lambda r: (r["run_id"], r["event_seq"]))  # 固定顺序


class Meter:
    # 功能：计量入口。依赖：MeterStore；today 可注入。完成：两种调用的行格式统一。
    def __init__(self, store: MeterStore, today: Callable[[], str] = _today):
        self.store = store  # 存储
        self.today = today  # 真实日期

    def record_llm(self, *, run_id: str, event_seq: int, case_id, day: int, endpoint: str, model: str,
                   input_tokens: int, output_tokens: int, latency_ms: int, repair: bool, fallback: bool,
                   error_type: str | None) -> None:
        """记一次 LLM 调用；限流拒绝的调用不算扣了配额。"""
        self.store.add({"run_id": run_id, "event_seq": event_seq, "kind": "llm", "case_id": case_id,  # 身份
                        "day": day, "wall_date": self.today(), "name": endpoint, "model": model,  # 时间与端点
                        "input_tokens": input_tokens, "output_tokens": output_tokens,  # token
                        "latency_ms": latency_ms, "cache_hit": False,  # 耗时
                        "quota_used": [] if error_type == "rate_limited" else [endpoint],  # 配额
                        "error_type": error_type, "repair": repair, "fallback": fallback})  # 结论

    def record_tool(self, *, run_id: str, event_seq: int, case_id, day: int, tool: str,
                    quota_used: list[str], cache_hit: bool, latency_ms: int, error_type: str | None) -> None:
        """记一次工具调用；quota_used 为实际扣了配额的提供方。"""
        self.store.add({"run_id": run_id, "event_seq": event_seq, "kind": "tool", "case_id": case_id,  # 身份
                        "day": day, "wall_date": self.today(), "name": tool, "model": None,  # 时间与工具
                        "input_tokens": 0, "output_tokens": 0, "latency_ms": latency_ms,  # 数值
                        "cache_hit": cache_hit, "quota_used": sorted(quota_used),  # 缓存与配额
                        "error_type": error_type, "repair": False, "fallback": False})  # 结论


# 功能：按事件序号取当时的完整请求（上下文快照）。依赖：session。完成：不是 llm_call 事件返回 None。
def context_snapshot(session: Session, seq: int) -> dict | None:
    event = session.store.event(session.run_id, seq)  # 取事件
    return event["payload"]["request"] if event and event["type"] == "llm_call" else None  # 请求体


# ---------------------------------------------------------------------------
# case_summary / run_summary：汇总
# 功能：spec §5.11"每个案子"一行；运行汇总另附环境结果指标。
# 依赖：session 事件、计量行。
# 完成：重规划 = 进入 planning 的次数减 1（首次规划不算）；升级 = 以 escalated 关闭；打扰人工 = human_request 事件数。
# ---------------------------------------------------------------------------
def case_summary(session: Session, store: MeterStore, case_id: str | None) -> dict:
    rows = [r for r in store.rows(session.run_id) if case_id is None or r["case_id"] == case_id]  # 计量行
    events = session.events(case_id=case_id)  # 事件
    llm = [r for r in rows if r["kind"] == "llm"]  # LLM 行
    tool = [r for r in rows if r["kind"] == "tool"]  # 工具行
    planning = sum(1 for e in events if e["type"] == EV_CASE_TRANSITION and e["payload"]["to"] == "planning")  # 规划次数
    cases = {e["case_id"] for e in events  # 进入过规划的案子（每个案子首次规划不算重规划）
             if e["type"] == EV_CASE_TRANSITION and e["payload"]["to"] == "planning"}  # 规划迁移
    return {
        "case_id": case_id,  # 案子（None 表示全运行）
        "llm_calls": len(llm),  # LLM 调用数
        "input_tokens": sum(r["input_tokens"] for r in llm),  # 输入 token
        "output_tokens": sum(r["output_tokens"] for r in llm),  # 输出 token
        "repairs": sum(r["repair"] for r in llm), "fallbacks": sum(r["fallback"] for r in llm),  # 修复与降级
        "tool_calls": len(tool), "cache_hits": sum(r["cache_hit"] for r in tool),  # 工具与缓存
        "errors": dict(sorted(Counter(r["error_type"] for r in rows if r["error_type"]).items())),  # 错误分布
        "replans": max(0, planning - len(cases)) if case_id is None else max(0, planning - 1),  # 重规划
        "escalations": sum(1 for e in events if e["type"] == EV_CASE_CLOSED  # 升级给人
                           and e["payload"]["outcome"] == "escalated"),  # 以升级关闭
        "human_interruptions": sum(1 for e in events if e["type"] == "human_request"),  # 打扰人工
        "violations": sum(1 for e in events if e["type"] == "violation"),  # 违规
        "latency_ms": sum(r["latency_ms"] for r in rows),  # 总耗时
    }


def run_summary(session: Session, store: MeterStore, env_metrics: dict | None = None) -> dict:
    """运行汇总：全部案子合计 + 各案子 + 环境结果指标（F03 metrics.outcome_metrics）。"""
    case_ids = [e["case_id"] for e in session.events(type="case_opened")]  # 开案顺序
    return {"total": case_summary(session, store, None),  # 合计
            "cases": [case_summary(session, store, c) for c in case_ids],  # 各案子
            "environment": env_metrics}  # 环境指标


# 功能：配额面板数据。依赖：计量行、提供方配置。完成：used = 当日实际扣配额次数，remaining = 上限 − used（无上限为 None）。
def quota_panel(store: MeterStore, providers: dict, wall_date: str) -> dict:
    used = Counter(p for r in store.rows(wall_date=wall_date) for p in r["quota_used"])  # 当日用量
    return {p: {"used": used.get(p, 0),  # 用量
                "limit": conf["daily_limit"],  # 上限
                "remaining": None if conf["daily_limit"] is None else max(0, conf["daily_limit"] - used.get(p, 0))}  # 剩余
            for p, conf in sorted(providers.items())}  # 每个提供方


# 功能：Redis 丢失后重建当日配额计数。依赖：kv.set_quota。完成：与 QuotaGuard 使用同一键名格式。
def rebuild_quota(kv, store: MeterStore, providers: dict, wall_date: str) -> dict[str, int]:
    used = Counter(p for r in store.rows(wall_date=wall_date) for p in r["quota_used"])  # 当日用量
    for p, n in used.items():  # 每个提供方
        if providers.get(p, {}).get("daily_limit") is not None:  # 有日配额才需要计数
            kv.set_quota(f"quota:{p}:{wall_date}", n, 2 * 86400)  # 写回计数
    return dict(used)  # 返回重建的计数

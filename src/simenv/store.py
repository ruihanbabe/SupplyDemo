"""文件摘要（F03 环境内核 · 快照与写入日志存储）

本文件定义环境持久化的两类记录及其两种实现（spec §4.6 快照与分叉、§5.1 存储）：
- 快照 env_snapshot：每个模拟日"开盘"（当日机制执行完、外部写入之前）存一次整状态 JSON + SHA-256；只写一次、不更新。
- 写入日志 env_write：外部写入只追加，带运行内序号与幂等键；先落日志再作用于内存，崩溃后据此重放。
- MemoryEnvStore：离线测试与预演用；PgEnvStore：Postgres 实现（迁移 0002_env 建表）。
两种实现接口相同（EnvStore 协议），Environment 不关心用的是哪种。
依赖：model（规范化 JSON 与哈希）、SQLAlchemy（Pg 实现）。被 api.Environment 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # JSON 序列化
from typing import Protocol  # 存储接口协议

from sqlalchemy import text  # 参数化 SQL


class SnapshotCorrupted(RuntimeError):
    """快照内容与存下的哈希对不上（被篡改或写坏）。"""


# ---------------------------------------------------------------------------
# EnvStore：存储接口
# 功能：规定 Environment 需要的四个操作，内存与 Postgres 各实现一份。
# 依赖：无；被 api.Environment 依赖。
# 完成：快照写入/读取（指定日或最近一日）、写入日志追加/按日读取。
# ---------------------------------------------------------------------------
class EnvStore(Protocol):
    def save_snapshot(self, run_id: str, day: int, state: dict, state_hash: str,  # 存快照
                      forked_from: tuple[str, int] | None = None) -> None: ...  # 可带分叉来源

    def load_snapshot(self, run_id: str, day: int | None = None  # 读快照；day=None 取最近一日
                      ) -> tuple[int, dict, str] | None: ...  # 返回 (日, 状态字典, 哈希) 或 None

    def append_write(self, run_id: str, day: int, write: dict) -> int: ...  # 追加写入，返回序号

    def list_writes(self, run_id: str, from_day: int) -> list[tuple[int, int, dict]]: ...  # (序号, 日, 写入)


# ---------------------------------------------------------------------------
# MemoryEnvStore：内存实现
# 功能：与 PgEnvStore 行为一致的内存版，离线测试和"预演"用。
# 依赖：json（深拷贝，模拟落库后与内存对象脱钩）。
# 完成：快照同日不可覆盖、幂等键在运行内唯一，与数据库约束一致。
# ---------------------------------------------------------------------------
class MemoryEnvStore:
    def __init__(self) -> None:
        self.snapshots: dict[tuple[str, int], dict] = {}  # (运行, 日) → 快照记录
        self.writes: dict[str, list[dict]] = {}  # 运行 → 写入记录列表

    def save_snapshot(self, run_id, day, state, state_hash, forked_from=None) -> None:
        if (run_id, day) in self.snapshots:  # 同一运行同一天已存过
            raise ValueError(f"快照已存在：{run_id} 第 {day} 天")  # 与主键约束一致
        self.snapshots[(run_id, day)] = {"state": json.loads(json.dumps(state)),  # 深拷贝状态
                                         "hash": state_hash, "forked_from": forked_from}  # 哈希与来源

    def load_snapshot(self, run_id, day=None):
        days = [d for (r, d) in self.snapshots if r == run_id]  # 这个运行的所有快照日
        if day is None:  # 没指定日
            day = max(days, default=None)  # 取最近一日
        if day is None or (run_id, day) not in self.snapshots:  # 没有快照
            return None  # 返回空
        rec = self.snapshots[(run_id, day)]  # 取记录
        return day, json.loads(json.dumps(rec["state"])), rec["hash"]  # 返回深拷贝

    def append_write(self, run_id, day, write) -> int:
        log = self.writes.setdefault(run_id, [])  # 该运行的日志
        if any(w["write"]["idempotency_key"] == write["idempotency_key"] for w in log):  # 幂等键重复
            raise ValueError(f"幂等键重复：{write['idempotency_key']}")  # 与唯一约束一致
        log.append({"seq": len(log) + 1, "day": day, "write": json.loads(json.dumps(write))})  # 追加
        return len(log)  # 返回序号

    def list_writes(self, run_id, from_day):
        return [(w["seq"], w["day"], json.loads(json.dumps(w["write"])))  # 深拷贝返回
                for w in self.writes.get(run_id, []) if w["day"] >= from_day]  # 只要该日及以后


# ---------------------------------------------------------------------------
# PgEnvStore：Postgres 实现
# 功能：把快照与写入日志存进 env_snapshot / env_write（迁移 0002_env）。
# 依赖：调用方给一个 SQLAlchemy Connection；本类不提交事务——
#       正式运行时调用方用自动提交连接（每条写入立即持久，崩溃后可重放），测试里用会回滚的事务。
# 完成：状态以规范化 JSON 存 JSONB；读取时由 Environment 重算哈希校验；序号按运行递增。
# ---------------------------------------------------------------------------
class PgEnvStore:
    def __init__(self, connection) -> None:
        self.conn = connection  # 数据库连接

    def save_snapshot(self, run_id, day, state, state_hash, forked_from=None) -> None:
        self.conn.execute(text("""
            INSERT INTO env_snapshot(run_id, day, state, state_hash, forked_from_run, forked_from_day)
            VALUES (:run, :day, CAST(:state AS JSONB), :hash, :frun, :fday)"""),  # 插入一行快照
            {"run": run_id, "day": day, "hash": state_hash,  # 运行、日、哈希
             "state": json.dumps(state, sort_keys=True, ensure_ascii=False),  # 规范化 JSON
             "frun": forked_from[0] if forked_from else None,  # 分叉来源运行
             "fday": forked_from[1] if forked_from else None})  # 分叉来源日

    def load_snapshot(self, run_id, day=None):
        row = self.conn.execute(text("""
            SELECT day, state, state_hash FROM env_snapshot
            WHERE run_id = :run AND (CAST(:day AS INTEGER) IS NULL OR day = :day)
            ORDER BY day DESC LIMIT 1"""), {"run": run_id, "day": day}).first()  # 指定日或最近一日
        return None if row is None else (row[0], row[1], row[2])  # 无则 None

    def append_write(self, run_id, day, write) -> int:
        return self.conn.execute(text("""
            INSERT INTO env_write(run_id, seq, day, kind, idempotency_key, payload)
            SELECT :run, COALESCE(MAX(seq), 0) + 1, :day, :kind, :key, CAST(:payload AS JSONB)
            FROM env_write WHERE run_id = :run
            RETURNING seq"""),  # 序号 = 该运行已有最大序号 + 1
            {"run": run_id, "day": day, "kind": write["kind"],  # 运行、日、种类
             "key": write["idempotency_key"],  # 幂等键
             "payload": json.dumps(write["payload"], sort_keys=True, ensure_ascii=False)},  # 内容
        ).scalar_one()  # 返回新序号

    def list_writes(self, run_id, from_day):
        rows = self.conn.execute(text("""
            SELECT seq, day, kind, idempotency_key, payload FROM env_write
            WHERE run_id = :run AND day >= :day ORDER BY seq"""),  # 按序号重放
            {"run": run_id, "day": from_day}).all()  # 该日及以后
        return [(r[0], r[1], {"kind": r[2], "idempotency_key": r[3], "payload": r[4]})  # 还原写入
                for r in rows]  # 逐行

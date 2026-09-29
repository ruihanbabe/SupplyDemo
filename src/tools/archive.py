"""文件摘要（F07 · 工具返回留档）

每次工具调用交付给执行者的数据都留档（spec §4.3、G5：每次真实响应随 Session 留档，Eval 读留档回放）：
- 记录原始返回 raw（缓存与留档存真实原始数据）与交付数据 delivered（返回校验 + 注入叠加之后），Eval 能区分哪些是注入的。
- 缓存命中也写一条记录（from_cache_of 指向原记录），这样每次交付都有自己的记录号，分页游标总能读到当次交付的全文。
- MemoryArchive（离线）与 PgArchive（迁移 0004_tool_record，只追加）。
依赖：SQLAlchemy（Pg 实现）。被 tools.proxy（写入、分页读取）与留档 Server 使用；F18 数据回放按记录读取。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 深拷贝与序列化
from typing import Protocol  # 接口

from sqlalchemy import text  # 参数化 SQL

FIELDS = ("run_id", "record_id", "case_id", "day", "tool", "providers", "args", "raw",  # 记录字段
          "delivered", "warnings", "from_cache_of")  # 其余字段


class Archive(Protocol):
    """留档接口。"""

    def put(self, record: dict) -> str: ...  # 存一条，返回分配的记录号
    def get(self, run_id: str, record_id: str) -> dict | None: ...  # 按记录号取


# 功能：内存实现。依赖：json 深拷贝。完成：记录号按运行递增 rec-1、rec-2……
class MemoryArchive:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict] = {}  # (运行, 记录号) → 记录

    def put(self, record):
        n = sum(1 for (r, _) in self.records if r == record["run_id"]) + 1  # 该运行下一个序号
        rid = f"rec-{n}"  # 记录号
        self.records[(record["run_id"], rid)] = json.loads(json.dumps({**record, "record_id": rid}))  # 存副本
        return rid  # 返回记录号

    def get(self, run_id, record_id):
        found = self.records.get((run_id, record_id))  # 查
        return json.loads(json.dumps(found)) if found else None  # 副本或空


# 功能：Postgres 实现。依赖：tool_record 表；调用方给连接（不提交）。完成：记录号 = 该运行已有条数 + 1。
class PgArchive:
    def __init__(self, connection) -> None:
        self.conn = connection  # 数据库连接

    def put(self, record):
        n = self.conn.execute(text("SELECT count(*) FROM tool_record WHERE run_id = :r"),  # 已有条数
                              {"r": record["run_id"]}).scalar_one() + 1  # 下一个序号
        rid = f"rec-{n}"  # 记录号
        row = {**record, "record_id": rid, **{k: json.dumps(record[k], ensure_ascii=False)  # JSON 字段
                                              for k in ("providers", "args", "raw", "delivered", "warnings")}}  # 五个
        self.conn.execute(text("""
            INSERT INTO tool_record(run_id, record_id, case_id, day, tool, providers, args, raw,
                                    delivered, warnings, from_cache_of)
            VALUES (:run_id, :record_id, :case_id, :day, :tool, CAST(:providers AS JSONB),
                    CAST(:args AS JSONB), CAST(:raw AS JSONB), CAST(:delivered AS JSONB),
                    CAST(:warnings AS JSONB), :from_cache_of)"""), row)  # 插入
        return rid  # 返回记录号

    def get(self, run_id, record_id):
        row = self.conn.execute(text(f"SELECT {', '.join(FIELDS)} FROM tool_record "  # 查询
                                     "WHERE run_id = :r AND record_id = :i"),  # 主键
                                {"r": run_id, "i": record_id}).mappings().first()  # 一行
        return dict(row) if row else None  # 字典或空

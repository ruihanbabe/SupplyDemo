"""文件摘要（F06 · Session 存储）

事件、Artifact、依赖边三类记录的存储接口与两种实现（spec §5.1 存储：三张表，全部只追加）。
- SessionStore 协议：追加/读取事件、按去重键查事件、存/读 Artifact 版本、按依赖反查下游。
- MemorySessionStore：离线测试用，行为与 Pg 实现一致（主键、唯一约束）。
- PgSessionStore：Postgres 实现（迁移 0003_session）；不提交事务，由调用方决定（正式运行用自动提交连接）。
记录都是纯 dict；哈希、投影、失效传播等逻辑在 session.log，不在存储层。
依赖：SQLAlchemy（Pg 实现）。被 session.log.Session 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # JSON 深拷贝与序列化
from typing import Protocol  # 接口协议

from sqlalchemy import text  # 参数化 SQL

#: 事件记录的字段（与 session_event 表列一一对应）。
EVENT_FIELDS = ("run_id", "seq", "case_id", "day", "wall_time", "actor", "type", "payload",  # 基本字段
                "entity_keys", "caused_by", "dedup_key", "meta", "prev_hash", "hash")  # 其余字段
#: Artifact 记录的字段（与 artifact 表列一一对应）。
ARTIFACT_FIELDS = ("run_id", "artifact_id", "version", "kind", "case_id", "content",  # 基本字段
                   "content_hash", "produced_seq", "day", "meta")  # 其余字段
#: 依赖边的字段（与 artifact_dep 表列一一对应）；dep_kind 为 artifact 或 external。
DEP_FIELDS = ("run_id", "artifact_id", "version", "dep_kind", "target", "target_version",  # 边的两端
              "valid_until_day")  # 外部数据按模拟日的有效期（含当日），Artifact 依赖为空


def _copy(value):
    """JSON 往返深拷贝：模拟落库后与内存对象脱钩。"""
    return json.loads(json.dumps(value))  # 深拷贝


# ---------------------------------------------------------------------------
# SessionStore：存储接口
# 功能：规定 Session 需要的全部读写操作；两种实现行为一致。
# 依赖：无；被 session.log.Session 依赖。
# 完成：事件 5 个操作、Artifact 3 个操作、依赖反查 2 个操作。
# ---------------------------------------------------------------------------
class SessionStore(Protocol):
    def append_event(self, event: dict) -> None: ...  # 追加事件（序号与哈希由调用方算好）
    def last_event(self, run_id: str) -> dict | None: ...  # 该运行最后一个事件
    def events(self, run_id: str, since_seq: int = 0) -> list[dict]: ...  # 按序号读事件
    def event(self, run_id: str, seq: int) -> dict | None: ...  # 按序号取单个事件
    def event_by_dedup(self, run_id: str, dedup_key: str) -> dict | None: ...  # 按去重键取事件
    def put_artifact(self, artifact: dict, deps: list[dict]) -> None: ...  # 存一个 Artifact 版本及依赖
    def artifact(self, run_id: str, artifact_id: str, version: int) -> dict | None: ...  # 取某版本
    def artifacts(self, run_id: str) -> list[dict]: ...  # 该运行全部 Artifact 版本
    def deps(self, run_id: str) -> list[dict]: ...  # 该运行全部依赖边
    def dependents(self, run_id: str, dep_kind: str, target: str) -> list[dict]: ...  # 反查依赖某目标的边


# ---------------------------------------------------------------------------
# MemorySessionStore：内存实现
# 功能：离线测试与预演用；按运行分桶。
# 依赖：_copy。
# 完成：事件 (运行, 序号) 唯一、去重键运行内唯一、Artifact (运行, ID, 版本) 唯一，与表约束一致。
# ---------------------------------------------------------------------------
class MemorySessionStore:
    def __init__(self) -> None:
        self._events: dict[str, list[dict]] = {}  # 运行 → 事件列表（按序号）
        self._artifacts: dict[tuple[str, str, int], dict] = {}  # (运行, ID, 版本) → Artifact
        self._deps: list[dict] = []  # 全部依赖边

    def append_event(self, event: dict) -> None:
        log = self._events.setdefault(event["run_id"], [])  # 该运行的事件列表
        if log and log[-1]["seq"] >= event["seq"]:  # 序号必须递增
            raise ValueError(f"事件序号冲突：{event['seq']}")  # 与主键约束一致
        if event["dedup_key"] and any(e["dedup_key"] == event["dedup_key"] for e in log):  # 去重键重复
            raise ValueError(f"去重键重复：{event['dedup_key']}")  # 与唯一约束一致
        log.append(_copy(event))  # 追加副本

    def last_event(self, run_id):
        log = self._events.get(run_id, [])  # 该运行的事件
        return _copy(log[-1]) if log else None  # 最后一个或空

    def events(self, run_id, since_seq=0):
        return [_copy(e) for e in self._events.get(run_id, []) if e["seq"] > since_seq]  # 序号之后的

    def event(self, run_id, seq):
        return next((_copy(e) for e in self._events.get(run_id, []) if e["seq"] == seq), None)  # 按序号

    def event_by_dedup(self, run_id, dedup_key):
        return next((_copy(e) for e in self._events.get(run_id, [])  # 遍历该运行
                     if e["dedup_key"] == dedup_key), None)  # 去重键命中

    def put_artifact(self, artifact, deps):
        key = (artifact["run_id"], artifact["artifact_id"], artifact["version"])  # 主键
        if key in self._artifacts:  # 已存在
            raise ValueError(f"Artifact 版本已存在：{key}")  # 不可变
        self._artifacts[key] = _copy(artifact)  # 存副本
        self._deps.extend(_copy(d) for d in deps)  # 存依赖边
        self._deps.sort(key=lambda d: (d["run_id"], d["artifact_id"], d["version"],  # 与 Pg 实现同序
                                       d["dep_kind"], d["target"]))  # 固定顺序

    def artifact(self, run_id, artifact_id, version):
        found = self._artifacts.get((run_id, artifact_id, version))  # 查主键
        return _copy(found) if found else None  # 副本或空

    def artifacts(self, run_id):
        return [_copy(a) for (r, _, _), a in sorted(self._artifacts.items()) if r == run_id]  # 按主键排序

    def deps(self, run_id):
        return [_copy(d) for d in self._deps if d["run_id"] == run_id]  # 该运行的边

    def dependents(self, run_id, dep_kind, target):
        return [_copy(d) for d in self._deps  # 遍历全部边
                if d["run_id"] == run_id and d["dep_kind"] == dep_kind and d["target"] == target]  # 命中目标


# ---------------------------------------------------------------------------
# PgSessionStore：Postgres 实现
# 功能：三张表 session_event / artifact / artifact_dep 的读写。
# 依赖：调用方给 SQLAlchemy Connection；表由迁移 0003_session 建立。
# 完成：JSON 字段以 JSONB 存取；真实时间存 ISO 字符串（TEXT 列，与内存实现逐字一致）；读出后在 Python 里按固定顺序排序。
# ---------------------------------------------------------------------------
class PgSessionStore:
    def __init__(self, connection) -> None:
        self.conn = connection  # 数据库连接

    def append_event(self, event):
        row = {**event, **{k: json.dumps(event[k], sort_keys=True, ensure_ascii=False)  # JSON 字段序列化
                           for k in ("payload", "entity_keys", "meta")}}  # 三个 JSON 字段
        self.conn.execute(text("""
            INSERT INTO session_event(run_id, seq, case_id, day, wall_time, actor, type, payload,
                                      entity_keys, caused_by, dedup_key, meta, prev_hash, hash)
            VALUES (:run_id, :seq, :case_id, :day, :wall_time, :actor, :type, CAST(:payload AS JSONB),
                    CAST(:entity_keys AS JSONB), :caused_by, :dedup_key, CAST(:meta AS JSONB),
                    :prev_hash, :hash)"""), row)  # 插入一行

    def _event_rows(self, where: str, params: dict) -> list[dict]:
        rows = self.conn.execute(text(f"""
            SELECT run_id, seq, case_id, day, wall_time, actor, type, payload, entity_keys,
                   caused_by, dedup_key, meta, prev_hash, hash
            FROM session_event WHERE {where} ORDER BY seq"""), params).mappings().all()  # 按序号读
        return [dict(r) for r in rows]  # 转普通字典

    def last_event(self, run_id):
        rows = self._event_rows("run_id = :r AND seq = (SELECT max(seq) FROM session_event "  # 最大序号
                                "WHERE run_id = :r)", {"r": run_id})  # 该运行
        return rows[0] if rows else None  # 最后一个或空

    def events(self, run_id, since_seq=0):
        return self._event_rows("run_id = :r AND seq > :s", {"r": run_id, "s": since_seq})  # 序号之后的

    def event(self, run_id, seq):
        rows = self._event_rows("run_id = :r AND seq = :s", {"r": run_id, "s": seq})  # 按序号
        return rows[0] if rows else None  # 单个或空

    def event_by_dedup(self, run_id, dedup_key):
        rows = self._event_rows("run_id = :r AND dedup_key = :k", {"r": run_id, "k": dedup_key})  # 去重键
        return rows[0] if rows else None  # 单个或空

    def put_artifact(self, artifact, deps):
        row = {**artifact, **{k: json.dumps(artifact[k], sort_keys=True, ensure_ascii=False)  # JSON 字段
                              for k in ("content", "meta")}}  # 两个 JSON 字段
        self.conn.execute(text("""
            INSERT INTO artifact(run_id, artifact_id, version, kind, case_id, content, content_hash,
                                 produced_seq, day, meta)
            VALUES (:run_id, :artifact_id, :version, :kind, :case_id, CAST(:content AS JSONB),
                    :content_hash, :produced_seq, :day, CAST(:meta AS JSONB))"""), row)  # 存版本
        if deps:  # 有依赖边
            self.conn.execute(text("""
                INSERT INTO artifact_dep(run_id, artifact_id, version, dep_kind, target,
                                         target_version, valid_until_day)
                VALUES (:run_id, :artifact_id, :version, :dep_kind, :target, :target_version,
                        :valid_until_day)"""), deps)  # 批量存边

    def artifact(self, run_id, artifact_id, version):
        rows = [a for a in self.artifacts(run_id)  # 该运行全部版本
                if a["artifact_id"] == artifact_id and a["version"] == version]  # 过滤主键
        return rows[0] if rows else None  # 单个或空

    def artifacts(self, run_id):
        rows = self.conn.execute(text(f"""
            SELECT {', '.join(ARTIFACT_FIELDS)} FROM artifact WHERE run_id = :r
            """), {"r": run_id}).mappings().all()  # 该运行全部版本
        return sorted((dict(r) for r in rows),  # 在 Python 里排序，避免数据库排序规则与内存实现不一致
                      key=lambda a: (a["artifact_id"], a["version"]))  # 按主键

    def deps(self, run_id):
        rows = self.conn.execute(text(f"""
            SELECT {', '.join(DEP_FIELDS)} FROM artifact_dep WHERE run_id = :r
            """), {"r": run_id}).mappings().all()  # 全部边
        return sorted((dict(r) for r in rows),  # Python 排序，理由同上
                      key=lambda d: (d["artifact_id"], d["version"], d["dep_kind"], d["target"]))  # 固定顺序

    def dependents(self, run_id, dep_kind, target):
        return [d for d in self.deps(run_id) if d["dep_kind"] == dep_kind and d["target"] == target]  # 命中

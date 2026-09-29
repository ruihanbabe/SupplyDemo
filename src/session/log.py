"""文件摘要（F06 · Session 事件日志、Artifact、失效传播与投影）

Session 是 Agent 侧唯一事实来源的门面（spec §5.1）：
- 事件日志：append() 追加事件，带哈希链（真实时间不进哈希，便于全回放比对）；去重键重复时返回原事件；
  verify_chain() 校验整条链；causal_chain() 沿 caused_by 倒查因果。
- Artifact：produce() 存新版本（内容不可变、记依赖到 Artifact 版本或外部数据版本 + 模拟日有效期），并写"产出"事件。
- 失效传播：invalidate_external() / expire() / invalidate() 找出受影响的有效 Artifact 连同全部下游，逐个写"失效"事件，
  并给每个受影响案子写一条"失效通知"事件（F10 据此唤醒案子）。消融开关关掉时外部变化不产生失效。
- 投影：artifact_status() 与 case_state() 从日志折叠得出；案子状态迁移非法时在追加前拒绝。
依赖：session.store（存储）、infrastructure.config（消融开关）。被 F07（工具调用事件）、F09（重建上下文）、F10（路由）、
F12（门控）、F14（反馈）、F17（计量关联）、F18（回放）等使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import hashlib  # SHA-256
import json  # 规范化 JSON
from collections.abc import Callable  # 时钟函数类型
from datetime import UTC, datetime  # 真实时间

from session.store import SessionStore  # 存储接口

GENESIS = "0" * 64  # 链首事件的"上一事件哈希"

# ---------------------------------------------------------------------------
# 事件类型与案子状态常量
# 功能：集中定义本 Feature 用到的事件类型与案子状态机；后续 Feature 可追加自己的事件类型（不受此处限制）。
# 依赖：无；被本文件与测试使用。
# 完成：案子 5 个状态 + 3 种关闭结果 + 3 种等待原因；合法迁移表。
# ---------------------------------------------------------------------------
EV_CASE_OPENED = "case_opened"  # 案子开启
EV_CASE_TRANSITION = "case_transition"  # 案子状态迁移（payload.to / payload.wait_for）
EV_CASE_CLOSED = "case_closed"  # 案子关闭（payload.outcome）
EV_ARTIFACT_PRODUCED = "artifact_produced"  # Artifact 产出
EV_ARTIFACT_INVALIDATED = "artifact_invalidated"  # Artifact 失效（payload.root 为根因）
EV_INVALIDATION_NOTICE = "invalidation_notice"  # 给案子的失效通知

CASE_OPEN, CASE_PLANNING, CASE_EXECUTING, CASE_WAITING, CASE_CLOSED = (  # 案子状态
    "open", "planning", "executing", "waiting", "closed")  # 开启、规划中、执行中、等待、关闭
CLOSE_OUTCOMES = {"resolved", "abandoned", "escalated"}  # 关闭结果：已解决、已放弃、已升级给人
WAIT_FOR = {"human", "arrival", "external"}  # 等待原因：人工、到货、外部
TRANSITIONS = {  # 合法迁移（关闭另由 close_case 处理，任何未关闭状态都可关闭）
    CASE_OPEN: {CASE_PLANNING},  # 开启 → 规划中
    CASE_PLANNING: {CASE_EXECUTING},  # 规划中 → 执行中
    CASE_EXECUTING: {CASE_WAITING, CASE_PLANNING},  # 执行中 → 等待 / 重规划
    CASE_WAITING: {CASE_EXECUTING, CASE_PLANNING},  # 等待 → 被唤醒继续执行 / 重规划
}

ART_VALID, ART_INVALID, ART_SUPERSEDED = "valid", "invalid", "superseded"  # Artifact 状态


class IllegalTransition(ValueError):
    """案子状态迁移不合法。"""


def canonical(value) -> str:
    """规范化 JSON：键排序、紧凑、保留中文；哈希与比对都用它。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)  # 规范化


def sha256(text: str) -> str:
    """字符串的 SHA-256 十六进制。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()  # 计算哈希


# ---------------------------------------------------------------------------
# event_hash：一个事件的链上哈希
# 功能：hash = SHA-256(prev_hash + 规范化 JSON(除真实时间与本哈希外的全部字段))。
# 依赖：canonical、sha256；被 Session.append 与 verify_chain 使用。
# 完成：真实时间不进哈希（全回放时真实时间必然不同，其余字段应逐条相同，spec §5.12）。
# ---------------------------------------------------------------------------
def event_hash(event: dict) -> str:
    body = {k: v for k, v in event.items() if k not in ("wall_time", "hash")}  # 去掉真实时间与哈希本身
    return sha256(event["prev_hash"] + canonical(body))  # 链式哈希


def _now() -> str:
    """默认真实时钟：UTC ISO 字符串。"""
    return datetime.now(UTC).isoformat()  # 当前 UTC 时间


class Session:
    # -----------------------------------------------------------------------
    # 构造
    # 功能：绑定一次运行（run_id）与存储；消融开关与真实时钟可注入（测试用固定时钟）。
    # 依赖：SessionStore 实现；propagate 来自 config/harness.yaml 的 ablation.invalidation_propagation。
    # 完成：不在内存里缓存任何状态，所有读取都走存储，崩溃重启后行为相同。
    # -----------------------------------------------------------------------
    def __init__(self, store: SessionStore, run_id: str, *, propagate: bool = True,
                 clock: Callable[[], str] = _now):
        self.store = store  # 存储
        self.run_id = run_id  # 运行号
        self.propagate = propagate  # 失效传播开关
        self.clock = clock  # 真实时钟

    # -----------------------------------------------------------------------
    # append：追加一个事件
    # 功能：分配序号、接上哈希链后写入存储；带去重键且已存在时直接返回原事件（允许重复投递）。
    # 依赖：event_hash、store；被本类其他方法与所有后续 Feature 调用。
    # 完成：实体键规范为 {类别: 排序去重的列表}；caused_by 必须指向已存在的事件。
    # -----------------------------------------------------------------------
    def append(self, type: str, *, day: int, actor: str, case_id: str | None = None,
               payload: dict | None = None, entity_keys: dict[str, list[str]] | None = None,
               caused_by: int | None = None, dedup_key: str | None = None,
               meta: dict | None = None) -> dict:
        if dedup_key is not None:  # 带去重键
            existing = self.store.event_by_dedup(self.run_id, dedup_key)  # 查是否已有
            if existing is not None:  # 已有
                return existing  # 返回原事件，不重复记录
        if caused_by is not None and self.store.event(self.run_id, caused_by) is None:  # 因果指向不存在
            raise ValueError(f"caused_by 指向不存在的事件：{caused_by}")  # 拒绝
        last = self.store.last_event(self.run_id)  # 链尾
        event = {  # 组装事件
            "run_id": self.run_id,  # 运行号
            "seq": (last["seq"] + 1) if last else 1,  # 序号
            "case_id": case_id,  # 案子号
            "day": day,  # 模拟日
            "wall_time": self.clock(),  # 真实时间（不进哈希）
            "actor": actor,  # 执行者（由 Harness 标注，不信模型自报）
            "type": type,  # 事件类型
            "payload": payload or {},  # 内容
            "entity_keys": {k: sorted(set(v)) for k, v in sorted((entity_keys or {}).items()) if v},  # 实体键
            "caused_by": caused_by,  # 因果
            "dedup_key": dedup_key,  # 去重键
            "meta": meta or {},  # 元数据（留给 F11 代表链、F13 污点等）
            "prev_hash": last["hash"] if last else GENESIS,  # 上一事件哈希
        }
        event["hash"] = event_hash(event)  # 本事件哈希
        self.store.append_event(event)  # 写入存储
        return event  # 返回事件

    # -----------------------------------------------------------------------
    # verify_chain：校验整条哈希链
    # 功能：逐个重算哈希并检查前后衔接与序号连续；返回第一个出错的序号，全对返回 None。
    # 依赖：event_hash；被测试、F18 全回放比对、F26 时间线使用。
    # 完成：检测内容篡改、删除、插入、重排。
    # -----------------------------------------------------------------------
    def verify_chain(self) -> int | None:
        prev, expected_seq = GENESIS, 1  # 链首
        for e in self.store.events(self.run_id):  # 按序号遍历
            if e["seq"] != expected_seq or e["prev_hash"] != prev or event_hash(e) != e["hash"]:  # 任一不符
                return e["seq"]  # 返回出错序号
            prev, expected_seq = e["hash"], expected_seq + 1  # 前进
        return None  # 全部正确

    def events(self, *, case_id: str | None = None, type: str | None = None,
               since_seq: int = 0) -> list[dict]:
        """按案子、类型、序号筛选事件（按序号排序）。"""
        return [e for e in self.store.events(self.run_id, since_seq)  # 序号之后的
                if (case_id is None or e["case_id"] == case_id)  # 案子筛选
                and (type is None or e["type"] == type)]  # 类型筛选

    def causal_chain(self, seq: int) -> list[dict]:
        """沿 caused_by 从某事件倒查到根，返回从根到该事件的列表。"""
        chain = []  # 结果
        current = self.store.event(self.run_id, seq)  # 起点
        while current is not None:  # 直到根
            chain.append(current)  # 记录
            cause = current["caused_by"]  # 上一环
            current = self.store.event(self.run_id, cause) if cause is not None else None  # 继续
        return list(reversed(chain))  # 从根到当前

    # -----------------------------------------------------------------------
    # 案子状态机（spec §5.1）
    # 功能：open_case / transition / close_case 追加案子事件；case_state 从日志投影当前状态。
    # 依赖：append、events；被 F10（开案、关闭）、F09（挂起、唤醒）、F22（规划）使用。
    # 完成：迁移前先投影当前状态再按 TRANSITIONS 检查；等待必须写原因；关闭必须写结果。
    # -----------------------------------------------------------------------
    def open_case(self, case_id: str, *, day: int, actor: str, payload: dict | None = None,
                  entity_keys: dict | None = None, caused_by: int | None = None) -> dict:
        if self.case_state(case_id) is not None:  # 案子号已被使用
            raise IllegalTransition(f"案子已存在：{case_id}")  # 拒绝
        return self.append(EV_CASE_OPENED, day=day, actor=actor, case_id=case_id,  # 开案事件
                           payload=payload, entity_keys=entity_keys, caused_by=caused_by)  # 内容与因果

    def transition(self, case_id: str, to: str, *, day: int, actor: str, wait_for: str | None = None,
                   reason: str = "", caused_by: int | None = None) -> dict:
        current = self.case_state(case_id)  # 当前状态
        if current is None or to not in TRANSITIONS.get(current["state"], set()):  # 不合法迁移
            raise IllegalTransition(f"{case_id}: {current and current['state']} → {to} 不合法")  # 拒绝
        if (to == CASE_WAITING) != (wait_for is not None) or (wait_for and wait_for not in WAIT_FOR):  # 等待原因
            raise IllegalTransition("进入等待必须且只能写明等待原因：human / arrival / external")  # 拒绝
        payload = {"to": to, "reason": reason, **({"wait_for": wait_for} if wait_for else {})}  # 迁移内容
        return self.append(EV_CASE_TRANSITION, day=day, actor=actor, case_id=case_id,  # 迁移事件
                           payload=payload, caused_by=caused_by)  # 因果

    def close_case(self, case_id: str, outcome: str, *, day: int, actor: str, reason: str = "",
                   caused_by: int | None = None) -> dict:
        current = self.case_state(case_id)  # 当前状态
        if current is None or current["state"] == CASE_CLOSED or outcome not in CLOSE_OUTCOMES:  # 不合法
            raise IllegalTransition(f"{case_id}: 不能以 {outcome!r} 关闭")  # 拒绝
        return self.append(EV_CASE_CLOSED, day=day, actor=actor, case_id=case_id,  # 关闭事件
                           payload={"outcome": outcome, "reason": reason}, caused_by=caused_by)  # 结果

    def case_state(self, case_id: str) -> dict | None:
        """投影一个案子的当前状态：{state, wait_for, outcome, opened_seq, last_seq}；不存在返回 None。"""
        state = None  # 未开案
        for e in self.events(case_id=case_id):  # 该案子的全部事件
            if e["type"] == EV_CASE_OPENED:  # 开案
                state = {"case_id": case_id, "state": CASE_OPEN, "wait_for": None,  # 初始投影
                         "outcome": None, "opened_seq": e["seq"], "last_seq": e["seq"]}  # 序号
            elif state and e["type"] == EV_CASE_TRANSITION:  # 迁移
                state.update(state=e["payload"]["to"], wait_for=e["payload"].get("wait_for"),  # 新状态
                             last_seq=e["seq"])  # 最近序号
            elif state and e["type"] == EV_CASE_CLOSED:  # 关闭
                state.update(state=CASE_CLOSED, wait_for=None, outcome=e["payload"]["outcome"],  # 关闭结果
                             last_seq=e["seq"])  # 最近序号
        return state  # 投影结果

    def cases(self) -> list[dict]:
        """全部案子的当前状态（按开案先后）。"""
        ids = [e["case_id"] for e in self.events(type=EV_CASE_OPENED)]  # 开案顺序
        return [self.case_state(c) for c in ids]  # 逐个投影

    # -----------------------------------------------------------------------
    # produce：产出一个 Artifact 新版本
    # 功能：版本号自动 +1；内容不可变并记内容哈希；依赖写成边；追加"产出"事件，旧版本由投影推为"被替代"。
    # 依赖：store、append；被所有产出 Artifact 的组件（F21 确定性组件、F23 子 Agent、F22 编排者）调用。
    # 完成：Artifact 依赖必须指向已存在的版本；外部依赖须给数据版本，可给有效期（含当日的最后一个模拟日）。
    #       依赖格式：{"artifact": ID, "version": n} 或 {"external": 引用, "version": 数据版本, "valid_until_day": d}。
    # -----------------------------------------------------------------------
    def produce(self, artifact_id: str, kind: str, content: dict, *, day: int, actor: str,
                case_id: str | None = None, deps: list[dict] = (), caused_by: int | None = None,
                meta: dict | None = None) -> dict:
        versions = [a["version"] for a in self.store.artifacts(self.run_id)  # 已有版本
                    if a["artifact_id"] == artifact_id]  # 同一 ID
        version = max(versions, default=0) + 1  # 新版本号
        edges = [self._edge(artifact_id, version, d) for d in deps]  # 规范化依赖边
        event = self.append(EV_ARTIFACT_PRODUCED, day=day, actor=actor, case_id=case_id,  # 产出事件
                            payload={"artifact_id": artifact_id, "version": version, "kind": kind},  # 身份
                            entity_keys={"artifact": [f"{artifact_id}@v{version}"]},  # 实体键
                            caused_by=caused_by)  # 因果
        artifact = {"run_id": self.run_id, "artifact_id": artifact_id, "version": version,  # 主键
                    "kind": kind, "case_id": case_id, "content": content,  # 类型、案子、内容
                    "content_hash": sha256(canonical(content)),  # 内容哈希（F12 审批令牌绑定它）
                    "produced_seq": event["seq"], "day": day, "meta": meta or {}}  # 出处
        self.store.put_artifact(artifact, edges)  # 存储
        return artifact  # 返回新版本

    def _edge(self, artifact_id: str, version: int, dep: dict) -> dict:
        """把一个依赖声明规范化为依赖边，并校验 Artifact 依赖的目标存在。"""
        base = {"run_id": self.run_id, "artifact_id": artifact_id, "version": version}  # 边的起点
        if "artifact" in dep:  # 依赖另一个 Artifact 版本
            if self.store.artifact(self.run_id, dep["artifact"], dep["version"]) is None:  # 目标不存在
                raise ValueError(f"依赖的 Artifact 不存在：{dep['artifact']}@v{dep['version']}")  # 拒绝
            return {**base, "dep_kind": "artifact", "target": dep["artifact"],  # Artifact 边
                    "target_version": str(dep["version"]), "valid_until_day": None}  # 无有效期
        return {**base, "dep_kind": "external", "target": dep["external"],  # 外部数据边
                "target_version": str(dep["version"]),  # 数据版本（如取数日、PDF 哈希）
                "valid_until_day": dep.get("valid_until_day")}  # 按模拟日的有效期

    # -----------------------------------------------------------------------
    # artifact_status / artifact_view：Artifact 状态投影
    # 功能：失效事件优先 → 失效；否则存在更高版本 → 被替代；否则有效。
    # 依赖：store、events；被失效传播、F09 上下文重建、F12 门控（只认有效）使用。
    # 完成：一次读全量事件与版本构建状态表，避免逐个查询。
    # -----------------------------------------------------------------------
    def artifact_status(self) -> dict[tuple[str, int], str]:
        invalid = {(e["payload"]["artifact_id"], e["payload"]["version"])  # 被失效的版本
                   for e in self.events(type=EV_ARTIFACT_INVALIDATED)}  # 来自失效事件
        latest: dict[str, int] = {}  # ID → 最高版本
        for a in self.store.artifacts(self.run_id):  # 全部版本
            latest[a["artifact_id"]] = max(latest.get(a["artifact_id"], 0), a["version"])  # 取最高
        status = {}  # (ID, 版本) → 状态
        for a in self.store.artifacts(self.run_id):  # 全部版本
            key = (a["artifact_id"], a["version"])  # 主键
            status[key] = (ART_INVALID if key in invalid  # 失效优先
                           else ART_SUPERSEDED if a["version"] < latest[a["artifact_id"]]  # 被替代
                           else ART_VALID)  # 有效
        return status  # 状态表

    def artifact_view(self, artifact_id: str, version: int | None = None) -> dict | None:
        """取某 Artifact（默认最新版本）连同状态与依赖；不存在返回 None。"""
        versions = [a for a in self.store.artifacts(self.run_id) if a["artifact_id"] == artifact_id]  # 全部版本
        if version is not None:  # 指定版本
            versions = [a for a in versions if a["version"] == version]  # 过滤
        if not versions:  # 没有
            return None  # 空
        a = versions[-1]  # 最新（已按版本排序）
        deps = [d for d in self.store.deps(self.run_id)  # 该版本的依赖边
                if d["artifact_id"] == a["artifact_id"] and d["version"] == a["version"]]  # 过滤
        return {**a, "status": self.artifact_status()[(a["artifact_id"], a["version"])], "deps": deps}  # 合并

    # -----------------------------------------------------------------------
    # 失效传播（spec §5.1）
    # 功能：invalidate_external：外部数据出了新版本，依赖旧版本的有效 Artifact 失效；
    #       expire：有效期已过（valid_until_day < 今天）的外部依赖使 Artifact 失效；
    #       invalidate：直接让指定 Artifact 失效（如 F22 目标修订后决策作废）。
    #       三者都沿依赖图传递到全部下游，逐个写失效事件，再按案子写失效通知。
    # 依赖：artifact_status、store.deps、append；消融开关 propagate 关闭时外部变化与过期直接返回空。
    # 完成：只处理当前有效的版本（被替代、已失效的不再重复标记）；处理顺序按 (ID, 版本) 排序，结果确定。
    # -----------------------------------------------------------------------
    def invalidate_external(self, ref: str, new_version: str, *, day: int, actor: str = "harness",
                            caused_by: int | None = None) -> list[tuple[str, int]]:
        if not self.propagate:  # 消融：没有依赖图的 Harness
            return []  # 什么都不失效
        roots = [(d["artifact_id"], d["version"]) for d in self.store.dependents(self.run_id, "external", ref)  # 依赖该数据
                 if d["target_version"] != str(new_version)]  # 且依赖的是旧版本
        root = {"kind": "external_changed", "ref": ref, "new_version": str(new_version)}  # 根因
        return self._invalidate(roots, root, day=day, actor=actor, caused_by=caused_by)  # 传播

    def expire(self, *, day: int, actor: str = "harness",
               caused_by: int | None = None) -> list[tuple[str, int]]:
        if not self.propagate:  # 消融
            return []  # 什么都不失效
        by_ref: dict[str, list[tuple[str, int]]] = {}  # 过期数据引用 → 依赖它的 Artifact
        for d in self.store.deps(self.run_id):  # 全部边
            if d["dep_kind"] == "external" and d["valid_until_day"] is not None and d["valid_until_day"] < day:  # 已过期
                by_ref.setdefault(d["target"], []).append((d["artifact_id"], d["version"]))  # 按引用归组
        hit = []  # 本次失效的全部 Artifact
        for ref in sorted(by_ref):  # 每个过期引用单独记根因
            root = {"kind": "expired", "ref": ref, "day": day}  # 根因
            hit += self._invalidate(by_ref[ref], root, day=day, actor=actor, caused_by=caused_by)  # 传播
        return hit  # 返回失效集合

    def invalidate(self, targets: list[tuple[str, int]], reason: str, *, day: int, actor: str,
                   caused_by: int | None = None) -> list[tuple[str, int]]:
        root = {"kind": "direct", "reason": reason}  # 根因：直接作废（不受消融开关影响，属业务决定）
        return self._invalidate(targets, root, day=day, actor=actor, caused_by=caused_by)  # 传播

    def _invalidate(self, roots: list[tuple[str, int]], root: dict, *, day: int, actor: str,
                    caused_by: int | None) -> list[tuple[str, int]]:
        status = self.artifact_status()  # 当前状态
        downstream: dict[tuple[str, str], list[tuple[str, int]]] = {}  # (目标 ID, 目标版本) → 依赖它的 Artifact
        for d in self.store.deps(self.run_id):  # 全部边
            if d["dep_kind"] == "artifact":  # 只看 Artifact 之间的边
                downstream.setdefault((d["target"], d["target_version"]), []).append(  # 反向索引
                    (d["artifact_id"], d["version"]))  # 依赖方
        hit: set[tuple[str, int]] = set()  # 已失效集合
        frontier = sorted(set(roots))  # 待处理
        while frontier:  # 广度优先
            node = frontier.pop(0)  # 取一个
            if node in hit or status.get(node) != ART_VALID:  # 已处理或本就不是有效
                continue  # 跳过
            hit.add(node)  # 标记
            frontier = sorted(set(frontier) | set(downstream.get((node[0], str(node[1])), [])))  # 加入下游
        ordered = sorted(hit)  # 确定顺序
        by_case: dict[str | None, list[str]] = {}  # 案子 → 受影响 Artifact
        arts = {(a["artifact_id"], a["version"]): a for a in self.store.artifacts(self.run_id)}  # 索引
        for aid, ver in ordered:  # 逐个写失效事件
            case = arts[(aid, ver)]["case_id"]  # 所属案子
            self.append(EV_ARTIFACT_INVALIDATED, day=day, actor=actor, case_id=case,  # 失效事件
                        payload={"artifact_id": aid, "version": ver, "root": root},  # 带根因
                        entity_keys={"artifact": [f"{aid}@v{ver}"]}, caused_by=caused_by)  # 实体键
            by_case.setdefault(case, []).append(f"{aid}@v{ver}")  # 按案子归组
        for case in sorted(c for c in by_case if c is not None):  # 每个受影响案子一条通知
            self.append(EV_INVALIDATION_NOTICE, day=day, actor=actor, case_id=case,  # 失效通知
                        payload={"artifacts": by_case[case], "root": root},  # 受影响清单与根因
                        entity_keys={"artifact": by_case[case]}, caused_by=caused_by)  # 实体键
        return ordered  # 返回失效集合

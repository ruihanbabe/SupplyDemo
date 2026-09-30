"""文件摘要（F18 · 唤醒边界分叉）

spec §5.12"分叉重跑"：选某个案子的某次唤醒（case_woken 事件）为分叉点——复制分叉点之前的事件日志与 Artifact，加上当日环境快照
（§4.6）——之后用新条件（提示词、模型、配置）真实运行，生成分支与原运行对比。只能在唤醒边界分叉：此时上下文本就从日志重建（§5.3）。
fork_at_wake(src, wake_seq, dst, env_store=..., env_into=...)：
- 按原顺序把 wake_seq 之前的事件重新追加到新运行（序号不变，caused_by 仍然成立；新运行号下重新成链）；
- 复制分叉点之前产出的 Artifact 版本与依赖边（换成新运行号）；
- 写 forked_from（源运行、唤醒事件号、日、案子）；
- 给了环境存储时：取源运行当日开盘快照载入新运行，并按幂等键补上当天分叉点之前已完成（po_submitted）的写入。
返回新环境（没给环境存储则为 None）。分叉后 ToolProxy.archive_runs 加上源运行号，即可读分叉前的留档。
依赖：session.log、simenv.api。被 Eval（F27）、调试与测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from session.log import Session  # 事件日志
from simenv.api import Environment  # 环境


# 功能：见文件摘要。依赖：源 / 新 Session 的存储、环境存储。完成：分叉点不是 case_woken 时报错。
def fork_at_wake(src: Session, wake_seq: int, dst: Session, *, env_store=None,
                 env_into: tuple | None = None) -> Environment | None:
    wake = src.store.event(src.run_id, wake_seq)  # 分叉点
    if wake is None or wake["type"] != "case_woken":  # 不是唤醒边界
        raise ValueError(f"只能在唤醒边界（case_woken）分叉：事件 #{wake_seq} 是 {wake and wake['type']}")  # 拒绝
    if dst.store.last_event(dst.run_id) is not None:  # 新运行必须是空的（序号才能对齐）
        raise ValueError(f"分叉目标运行 {dst.run_id} 不是空的")  # 拒绝
    before = [e for e in src.store.events(src.run_id) if e["seq"] < wake_seq]  # 分叉点之前的事件
    for e in before:  # 按原顺序
        dst.append(e["type"], day=e["day"], actor=e["actor"], case_id=e["case_id"], payload=e["payload"],  # 重新追加
                   entity_keys=e["entity_keys"], caused_by=e["caused_by"], dedup_key=e["dedup_key"],  # 实体键、因果、去重
                   meta=e["meta"])  # 元数据
    edges = src.store.deps(src.run_id)  # 源运行的依赖边
    for a in src.store.artifacts(src.run_id):  # 源运行的 Artifact 版本
        if a["produced_seq"] < wake_seq:  # 分叉点之前产出的
            mine = [{**d, "run_id": dst.run_id} for d in edges  # 该版本的依赖边
                    if d["artifact_id"] == a["artifact_id"] and d["version"] == a["version"]]  # 过滤
            dst.store.put_artifact({**a, "run_id": dst.run_id}, mine)  # 复制
    dst.append("forked_from", day=wake["day"], actor="fork", case_id=wake["case_id"],  # 分叉记录
               payload={"run_id": src.run_id, "wake_seq": wake_seq, "day": wake["day"]})  # 源运行与分叉点
    if env_store is None:  # 不带环境
        return None  # 只分叉日志
    env = Environment.load(env_store, src.run_id, wake["day"], into=env_into or (env_store, dst.run_id))  # 当日开盘快照
    done = {e["payload"]["idempotency_key"] for e in before if e["type"] == "po_submitted"}  # 分叉点前已完成的写入
    for _, d, w in env_store.list_writes(src.run_id, wake["day"]):  # 当天及以后的写入
        if d == wake["day"] and w["idempotency_key"] in done:  # 当天、分叉点前完成的
            p = w["payload"]  # 写入内容
            env.submit_purchase_order(mpn=p["mpn"], qty=p["qty"], distributor=p["distributor"],  # 补做（按幂等键）
                                      unit_price=p["unit_price"], idempotency_key=w["idempotency_key"])  # 同一幂等键
    return env  # 分叉环境

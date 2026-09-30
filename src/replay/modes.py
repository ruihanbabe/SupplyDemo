"""文件摘要（F18 · 回放模式）

spec §5.12 三种模式：实时（现状）/ 数据回放（外部数据取留档，LLM 真实调用）/ 全回放（外部数据与 LLM 都回放）。
回放时留档缺失直接报错，不回退到真实调用。
- ArchiveReplay：数据回放源。按源运行的 tool_call 事件（外部来源工具）建队列：（工具, 参数）→ 依次的原事件与留档记录；
  take() 取第 N 次调用对应的一条；队列空抛 ReplayMissing。F07 代理层传入它后，外部工具不调后端、不扣配额、不熔断，
  事件的簿记字段（缓存命中、尝试次数、熔断、注入）照抄原事件，交付的原始数据取留档。
- ReplayTransport：全回放的模型传输。按端点依次回放源运行 llm_call 的响应（记录的失败按原类型重抛）；请求哈希与原记录不一致
  抛 ReplayDiverged（代码或提示词改了，回放不再成立）。
依赖：session.log（规范化 JSON 与哈希）、models.types。被 tools.proxy（数据回放）与装配 Runtime 的代码（F27、测试）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from models.types import ModelUnavailable  # 模型传输失败（回放原失败）
from session.log import Session, canonical, sha256  # 事件日志与哈希


class ReplayMissing(RuntimeError):
    """回放时留档缺失（不回退真实调用）。"""


class ReplayDiverged(RuntimeError):
    """全回放时请求与原记录不一致（代码、提示词或上下文变了）。"""


class ArchiveReplay:
    # 功能：见文件摘要。依赖：源运行的 Session 与留档。完成：只收外部来源工具（external_tools）的调用；原调用失败的也入队（重现失败类型）。
    def __init__(self, source: Session, archive, external_tools: set[str]):
        self.source, self.archive = source, archive  # 源运行与留档
        self.queues: dict[tuple[str, str], list[dict]] = {}  # (工具, 参数) → 原事件
        for e in source.events(type="tool_call"):  # 源运行的工具调用
            p = e["payload"]  # 内容
            if p["tool"] in external_tools:  # 外部来源
                self.queues.setdefault((p["tool"], canonical(p["args"])), []).append(p)  # 入队

    @property
    def run_id(self) -> str:
        """留档源运行号（写进运行清单）。"""
        return self.source.run_id  # 源运行

    def take(self, tool: str, args: dict) -> tuple[dict, dict | None]:
        """取下一条：(原事件内容, 留档记录或 None=原调用失败)；缺失抛 ReplayMissing。"""
        q = self.queues.get((tool, canonical(args)))  # 队列
        if not q:  # 没有
            raise ReplayMissing(f"留档里没有这次调用：{tool} {canonical(args)}")  # 不回退真实调用
        orig = q.pop(0)  # 下一条
        if orig["record_id"] is None:  # 原调用失败
            return orig, None  # 重现失败
        rec = self.archive.get(self.source.run_id, orig["record_id"])  # 留档
        if rec is None:  # 留档丢了
            raise ReplayMissing(f"留档记录缺失：{self.source.run_id} {orig['record_id']}")  # 报错
        if rec.get("from_cache_of"):  # 原来是缓存命中：取数日是被缓存那条的日子
            first = self.archive.get(self.source.run_id, rec["from_cache_of"])  # 被缓存的记录
            rec = {**rec, "fetched_day": first["day"] if first else rec["day"]}  # 取数日
        return orig, rec  # 返回


class ReplayTransport:
    """全回放：某端点的模型传输，依次吐出源运行记录的响应。"""

    def __init__(self, source: Session, name: str, *, strict: bool = True):
        self.name, self.strict = name, strict  # 端点名与是否校验请求哈希
        self.queue = [e["payload"] for e in source.events(type="llm_call") if e["payload"]["endpoint"] == name]  # 队列

    def send(self, endpoint: dict, body: dict) -> dict:
        if not self.queue:  # 用完
            raise ReplayMissing(f"端点 {self.name} 的 llm_call 记录已用完")  # 不回退真实调用
        rec = self.queue.pop(0)  # 下一条
        if self.strict and sha256(canonical(body)) != rec["prompt_hash"]:  # 请求变了
            raise ReplayDiverged(f"端点 {self.name} 第 {rec['attempt']} 次尝试的请求与原记录不一致")  # 报错
        if "error" in rec:  # 原调用失败
            raise ModelUnavailable(rec["error"]["kind"] or "upstream", rec["error"]["message"])  # 按原类型重抛
        return rec["response"]  # 原响应

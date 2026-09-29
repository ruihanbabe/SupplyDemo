"""文件摘要（F07 · 工具代理层）

ToolProxy.call() 是执行者调用任何工具的唯一入口（G1），按固定管道执行（spec §5.4）：
  ① 白名单（执行者 → 允许的工具；F11 用能力令牌替换）② 参数校验 ③ 测试注入挂点 faults（F18）
  ④ 查缓存（真实时间 TTL）⑤ 配额与限流（按提供方，部分不可用时部分成功）⑥ 调用处理函数
  ⑦ 返回校验（B3）⑧ 写缓存 ⑨ 注入叠加 overlays（§4.3，缓存存原始数据、每次现叠加）
  ⑩ 留档（原始 + 交付）⑪ 裁剪分页 ⑫ 记 tool_call 事件；任何一步失败都返回统一错误结构并记事件。
另有 read_record()：留档 Server 的实现，按记录号与游标翻页，不产生上游调用。
依赖：tools.server / result / kv / quota / archive、session.log.Session、PyYAML。
被 F09 Runtime（执行者调用工具）、F21 确定性组件（查环境与报价）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 缓存序列化
import time  # 计量耗时（F17）
from collections.abc import Callable  # 挂点函数类型
from pathlib import Path  # 配置路径

import yaml  # 读配置
from pydantic import ValidationError  # 参数校验错误

from session.log import Session, canonical, sha256  # 事件日志与哈希工具
from tools.archive import Archive  # 留档
from tools.kv import KV  # 缓存
from tools.quota import QuotaGuard  # 配额
from tools.result import error_result, ok_result  # 统一返回
from tools.server import BadFormat, CallContext, NotFound, Server, ToolDef  # 工具定义

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
TOOLS_CONFIG = ROOT / "config" / "tools.yaml"  # 默认配置

#: 注入叠加：(工具名, 参数字典, 数据, 模拟日) → 新数据（F04 实现具体注入）。
Overlay = Callable[[str, dict, object, int], object]  # 类型别名
#: 测试注入：(工具名, 参数字典, 模拟日) → 错误类型或 None（F18 实现具体注入）。
Fault = Callable[[str, dict, int], str | None]  # 类型别名


def load_tools_config(path: Path = TOOLS_CONFIG) -> dict:
    """读工具代理层配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


class ToolProxy:
    # -----------------------------------------------------------------------
    # 构造
    # 功能：注册 Server、绑定事件日志、缓存、配额、留档、白名单与挂点、消融开关。
    # 依赖：见文件摘要；whitelist 为 {执行者: 允许工具名集合}，"*" 表示全部（仅测试与代码组件用）。
    # 完成：工具名全局唯一（重名报错）。
    # -----------------------------------------------------------------------
    def __init__(self, servers: list[Server], session: Session, *, kv: KV, quota: QuotaGuard,
                 archive: Archive, config: dict, whitelist: dict[str, set[str]],
                 overlays: list[Overlay] = (), faults: list[Fault] = (),
                 switches: dict[str, bool] | None = None, meter=None):
        self.meter = meter  # F17 计量（可选，不传即不计量）
        self.tools: dict[str, ToolDef] = {}  # 工具名 → 定义
        for server in servers:  # 每个 Server
            for tool in server.tools:  # 每个工具
                if tool.name in self.tools:  # 重名
                    raise ValueError(f"工具重名：{tool.name}")  # 报错
                self.tools[tool.name] = tool  # 登记
        self.session, self.kv, self.quota, self.archive = session, kv, quota, archive  # 依赖
        self.config, self.whitelist = config, whitelist  # 配置与白名单
        self.overlays, self.faults = list(overlays), list(faults)  # 挂点
        sw = switches or {}  # 消融开关
        self.validate_returns = sw.get("tool_return_validation", True)  # 返回校验开关

    def visible_tools(self, executor: str) -> list[dict]:
        """执行者能看到的工具（MCP 形状）；L1 可见性由 F11 进一步收窄。"""
        allowed = self.whitelist.get(executor, set())  # 允许集合
        return [t.schema() for n, t in sorted(self.tools.items()) if "*" in allowed or n in allowed]  # 过滤

    # -----------------------------------------------------------------------
    # call：调用一个工具
    # 功能：见文件摘要的管道。
    # 依赖：本类各私有步骤。
    # 完成：返回统一结构；每次调用恰好一条 tool_call 事件（成功或失败）。
    # -----------------------------------------------------------------------
    def call(self, name: str, args: dict, *, executor: str, actor: str, day: int,
             case_id: str | None = None, caused_by: int | None = None) -> dict:
        info = {"tool": name, "args": args, "executor": executor,  # 事件基础内容
                "_t0": time.monotonic(), "_quota_used": []}  # 计量用：开始时刻、实际扣配额的提供方
        tool = self.tools.get(name)  # ① 找工具
        allowed = self.whitelist.get(executor, set())  # 允许集合
        if tool is None or not ("*" in allowed or name in allowed):  # 不存在或不在白名单
            visible = [t["name"] for t in self.visible_tools(executor)]  # 可用工具
            kind = "not_found" if tool is None else "forbidden"  # 错误类型
            msg = f"工具 {name!r} {'不存在' if tool is None else '不在你的权限内'}，可用工具：{', '.join(visible) or '无'}"  # 信息
            return self._finish(error_result(kind, msg), info, {}, actor, day, case_id, caused_by)  # 返回错误
        try:  # ② 参数校验
            params = tool.params.model_validate(args)  # 校验
        except ValidationError as exc:  # 不合格
            msg = "；".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())  # 逐条
            return self._finish(error_result("param_error", msg), info, {}, actor, day, case_id, caused_by)  # 返回
        if name == "read_record":  # 留档 Server：只读留档，不走上游管道
            return self.read_record(params.cursor, executor=executor, actor=actor, day=day, case_id=case_id)
        keys = tool.entity_keys(params)  # 实体键
        for fault in self.faults:  # ③ 测试注入（§5.12）
            kind = fault(name, args, day)  # 是否注入错误
            if kind:  # 注入
                info["injected_fault"] = kind  # 标明测试注入
                return self._finish(error_result(kind, f"测试注入：{kind}"), info, keys, actor, day, case_id, caused_by)
        tconf = self.config["tools"].get(tool.config_key or "", {})  # 该工具的两个时钟配置
        ckey = f"cache:{name}:{sha256(canonical(params.model_dump(mode='json')))}"  # 缓存键
        cached = self.kv.get(ckey) if tconf.get("cache_ttl_s") else None  # ④ 查缓存
        if cached is not None:  # 命中
            hit = json.loads(cached)  # {raw, data, warnings, fetched_day, record_id}
            info.update(cache_hit=True, from_record=hit["record_id"])  # 记命中
            return self._deliver(tool, params, hit["raw"], hit["data"], hit["warnings"], hit["fetched_day"],  # 交付
                                 info, keys, actor, day, case_id, caused_by, from_cache_of=hit["record_id"])
        info["cache_hit"] = False  # 未命中
        unavailable = set()  # ⑤ 配额
        providers = tool.providers(params)  # 涉及的提供方
        for p in providers:  # 逐个检查
            if self.quota.try_acquire(p) is not None:  # 不放行
                unavailable.add(p)  # 记下
        if unavailable and unavailable == set(providers):  # 全部不可用
            return self._finish(error_result("rate_limited", f"来源暂不可用（配额 / 限流）：{', '.join(sorted(unavailable))}"),
                                info, keys, actor, day, case_id, caused_by)  # 返回限流
        info["_quota_used"] = [p for p in providers if p not in unavailable]  # 实际扣了配额的
        ctx = CallContext(day=day, case_id=case_id, executor=executor, unavailable=unavailable)  # 上下文
        try:  # ⑥ 调用
            raw = tool.handler(params, ctx)  # 原始数据
        except NotFound as exc:  # 查不到
            return self._finish(error_result("not_found", str(exc)), info, keys, actor, day, case_id, caused_by)
        except BadFormat as exc:  # 整体无法解析
            return self._finish(error_result("bad_format", str(exc)), info, keys, actor, day, case_id, caused_by)
        except TimeoutError as exc:  # 超时
            return self._finish(error_result("timeout", str(exc)), info, keys, actor, day, case_id, caused_by)
        except ConnectionError as exc:  # 上游错误
            return self._finish(error_result("upstream_error", str(exc)), info, keys, actor, day, case_id, caused_by)
        warnings = [f"来源 {p} 本次不可用（配额 / 限流），结果不含该来源" for p in sorted(unavailable)]  # 部分成功
        data = raw  # 默认原样
        if tool.validate is not None and self.validate_returns:  # ⑦ 返回校验（B3）
            try:  # 校验
                data, found = tool.validate(raw)  # 补"未知"并给警告
            except BadFormat as exc:  # 整体无法解析
                return self._finish(error_result("bad_format", str(exc)), info, keys, actor, day, case_id, caused_by)
            warnings += found  # 合并警告
        if tconf.get("cache_ttl_s") and not unavailable:  # ⑧ 写缓存（部分成功不缓存）
            info["_cache_key"] = ckey  # 留档后再写缓存（需要记录号）
        return self._deliver(tool, params, raw, data, warnings, day, info, keys, actor, day, case_id, caused_by)

    # -----------------------------------------------------------------------
    # _deliver：叠加注入、留档、写缓存、分页、记事件
    # 功能：管道 ⑧–⑫。
    # 依赖：overlays、archive、kv、_finish。
    # 完成：来源信息里 fetched_at / valid_until 用模拟日；缓存命中时沿用原取数日（所以过期照样过期）。
    # -----------------------------------------------------------------------
    def _deliver(self, tool: ToolDef, params, raw, data, warnings, fetched_day, info, keys, actor,
                 day, case_id, caused_by, from_cache_of: str | None = None) -> dict:
        args = params.model_dump(mode="json")  # 规范化参数
        delivered = data  # 交付数据
        for overlay in self.overlays:  # ⑨ 注入叠加（每次现叠加）
            delivered = overlay(tool.name, args, delivered, day)  # 叠加
        info["overlaid"] = canonical(delivered) != canonical(data)  # 是否被注入改动
        providers = tool.providers(params)  # 提供方
        record_id = self.archive.put({"run_id": self.session.run_id, "case_id": case_id, "day": day,  # ⑩ 留档
                                      "tool": tool.name, "providers": providers, "args": args,  # 调用信息
                                      "raw": raw, "delivered": delivered, "warnings": warnings,  # 原始与交付
                                      "from_cache_of": from_cache_of})  # 缓存来源
        ckey = info.pop("_cache_key", None)  # ⑧ 写缓存
        if ckey:  # 需要缓存
            ttl = self.config["tools"][tool.config_key]["cache_ttl_s"]  # 真实秒 TTL
            self.kv.set(ckey, json.dumps({"raw": raw, "data": data, "warnings": warnings,  # 存原始与校验后数据
                                          "fetched_day": fetched_day, "record_id": record_id},  # 取数日与记录
                                         ensure_ascii=False), ttl)  # TTL
        valid_days = self.config["tools"].get(tool.config_key or "", {}).get("valid_days", 0)  # 有效天数
        source = {"provider": ",".join(providers), "fetched_at": fetched_day,  # 来源与取数日
                  "valid_until": fetched_day + valid_days, "record_id": record_id}  # 有效期与记录号
        page, truncated, cursor = self._page(delivered, 0, record_id)  # ⑪ 裁剪
        return self._finish(ok_result(page, source=source, warnings=warnings, truncated=truncated,  # ⑫
                                      next_cursor=cursor), info, keys, actor, day, case_id, caused_by)

    def _page(self, data, offset: int, record_id: str):
        """列表按页裁剪；返回 (本页, 是否截断, 下一页游标)。"""
        size = self.config["page_size"]  # 每页条数
        if not isinstance(data, list):  # 非列表不分页
            return data, False, None  # 原样
        page = data[offset:offset + size]  # 本页
        more = offset + size < len(data)  # 是否还有
        return page, more, (f"{record_id}:{offset + size}" if more else None)  # 游标指向留档

    # -----------------------------------------------------------------------
    # read_record：按记录号 / 游标读留档（留档 Server 的实现）
    # 功能：spec §5.4"按记录号读原始返回"；翻页只读留档，不调用上游、不扣配额。
    # 依赖：archive、_page、_finish。
    # 完成：游标格式 rec-N:偏移；记录不存在返回 not_found。
    # -----------------------------------------------------------------------
    def read_record(self, cursor: str, *, executor: str, actor: str, day: int,
                    case_id: str | None = None) -> dict:
        record_id, _, offset = cursor.partition(":")  # 拆游标
        info = {"tool": "read_record", "args": {"cursor": cursor}, "executor": executor,  # 事件内容
                "_t0": time.monotonic(), "_quota_used": []}  # 计量用
        rec = self.archive.get(self.session.run_id, record_id)  # 取记录
        if rec is None or (offset and not offset.isdigit()):  # 不存在或游标坏
            return self._finish(error_result("not_found", f"留档记录不存在：{cursor}"), info, {}, actor, day,
                                case_id, None)  # 返回错误
        page, truncated, nxt = self._page(rec["delivered"], int(offset or 0), record_id)  # 取页
        source = {"provider": "archive", "fetched_at": rec["day"], "valid_until": None, "record_id": record_id}  # 来源
        return self._finish(ok_result(page, source=source, warnings=rec["warnings"], truncated=truncated,
                                      next_cursor=nxt), info, {}, actor, day, case_id, None)  # 返回

    def _finish(self, result: dict, info: dict, keys: dict, actor: str, day: int, case_id, caused_by) -> dict:
        """记 tool_call 事件（不含交付数据本身，全文在留档）与计量，返回结果。"""
        t0, quota_used = info.pop("_t0", None), info.pop("_quota_used", [])  # 计量字段不进事件
        payload = {**info, "ok": result["ok"], "error_type": (result["error"] or {}).get("type"),  # 结论
                   "record_id": (result["source"] or {}).get("record_id"),  # 留档号
                   "warnings": result["warnings"], "truncated": result["truncated"]}  # 警告与截断
        event = self.session.append("tool_call", day=day, actor=actor, case_id=case_id, payload=payload,  # 事件
                                    entity_keys=keys, caused_by=caused_by)  # 实体键与因果
        if self.meter is not None:  # F17 计量
            self.meter.record_tool(run_id=self.session.run_id, event_seq=event["seq"], case_id=case_id,  # 身份
                                   day=day, tool=info["tool"], quota_used=quota_used,  # 工具与配额
                                   cache_hit=bool(info.get("cache_hit")),  # 缓存
                                   latency_ms=int((time.monotonic() - t0) * 1000) if t0 else 0,  # 耗时
                                   error_type=payload["error_type"])  # 错误
        return result  # 返回统一结构

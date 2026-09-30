"""文件摘要（F08 · 模型适配层门面）

ModelAdapter.call() 是 Agent 循环调用模型的唯一入口（spec §5.3）：
按执行者取模型链 → 对当前模型：限流挂点 → 组装请求 → 传输 → 解析 → 校验（工具名、参数模型、输出模型、编造料号）
→ 不过则把逐条错误回给模型修复 1 次 → 仍不过算格式失败 → 降级到下一个模型 → 全部失败抛 ModelFailure。
每次尝试（含失败）都写一条 Session 事件 llm_call，含完整请求与响应（gen_ai.* 字段，§5.11），不含凭证与真实耗时
（耗时放在返回值里给 F17 计量，不进哈希链，保证全回放的哈希链可比对）。
依赖：models.types、models.providers、session.log.Session、infrastructure.config（消融开关）。
被 F09 Runtime、F22 编排者、F23 子 Agent 使用；F07 提供 Redis 限流实现，F18 用 llm_call 记录做全回放。
F15：传入 resilience 时，每次尝试的传输失败（限流、超时、上游）按配置重试并指数退避，仍失败计入该端点熔断并抛
EndpointUnavailable；熔断中的端点不发请求直接抛 EndpointUnavailable('circuit_open')；凭证缺失（config）不重试、原样上抛。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 解析结构化输出
import re  # 去掉代码块围栏
import time  # 真实耗时（只给计量用）
from dataclasses import dataclass, field  # 结果数据类
from typing import Protocol  # 限流接口

from pydantic import BaseModel, TypeAdapter, ValidationError  # 输出与参数模型、字段类型 Schema
from pydantic.errors import (  # Schema 生成失败
    PydanticInvalidForJsonSchema,
    PydanticSchemaGenerationError,
)

from models.providers import build_request, parse_response  # 端点适配
from models.types import (  # 内部格式
    EndpointUnavailable,
    Message,
    ModelFailure,
    ModelResponse,
    ModelUnavailable,
    ToolSpec,
)
from session.log import Session, canonical, sha256  # 事件日志与哈希工具

FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)  # ```json ... ``` 围栏
REPAIR_PROMPT = "你的上一条输出不合格：\n{errors}\n请只输出修正后的结果。"  # 修复消息（Contract 默认值）


class Limiter(Protocol):
    """限流挂点：acquire 不放行时抛 ModelUnavailable('rate_limited')。Redis 实现归 F07。"""

    def acquire(self, endpoint_name: str) -> None: ...  # 申请一次调用额度


class NoLimiter:
    """默认不限流。"""

    def acquire(self, endpoint_name: str) -> None:
        return None  # 直接放行


# 功能：一次成功调用的结果。依赖：无；被调用方使用。完成：响应、校验后的输出对象或工具参数对象、用了哪个模型、尝试记录。
@dataclass
class CallResult:
    response: ModelResponse  # 最后一次（成功的）响应
    output: BaseModel | None  # 校验后的结构化输出（要求结构化输出时）
    tool_args: list[BaseModel]  # 校验后的工具参数（与 response.tool_calls 一一对应）
    endpoint: str  # 最终使用的端点
    attempts: int  # 总尝试次数
    event_seqs: list[int] = field(default_factory=list)  # 各次尝试的 llm_call 事件序号
    latency_ms: list[int] = field(default_factory=list)  # 各次尝试的真实耗时（给 F17）


class ModelAdapter:
    # -----------------------------------------------------------------------
    # 构造
    # 功能：绑定模型配置、每个端点的传输、Session、限流器与三个消融开关。
    # 依赖：providers.load_models_config 的结果；transports 为 {端点名: 有 send(endpoint, body) 的对象}。
    # 完成：消融开关从 harness 配置传入（schema_repair / model_fallback / fabricated_mpn_check）。
    # -----------------------------------------------------------------------
    def __init__(self, config: dict, transports: dict, session: Session, *,
                 limiter: Limiter | None = None, switches: dict[str, bool] | None = None,
                 meter=None, resilience=None):
        self.config = config  # 模型配置
        self.transports = transports  # 传输
        self.session = session  # 事件日志
        self.limiter = limiter or NoLimiter()  # 限流
        self.meter = meter  # F17 计量（可选，不传即不计量）
        self.resilience = resilience  # F15 重试与熔断（None = 传输失败原样上抛）
        sw = switches or {}  # 消融开关
        self.repair = sw.get("schema_repair", True)  # 修复开关
        self.fallback = sw.get("model_fallback", True)  # 降级开关
        self.mpn_check = sw.get("fabricated_mpn_check", True)  # 料号检查开关

    # -----------------------------------------------------------------------
    # call：调用模型
    # 功能：见文件摘要。
    # 依赖：_attempt、_validate；被 F09/F22/F23 调用。
    # 完成：tools 与 output 可同时给（模型可调工具或给最终输出）；known_mpns 为 None 时不做料号检查。
    # -----------------------------------------------------------------------
    def call(self, executor: str, messages: list[Message], *, tools: list[ToolSpec] = (),
             output: type[BaseModel] | None = None, known_mpns: set[str] | None = None,
             case_id: str | None = None, day: int, actor: str) -> CallResult:
        chain = self.config["executors"].get(executor) or self.config["executors"]["default"]  # 模型链
        if not self.fallback:  # 消融：不降级
            chain = chain[:1]  # 只用主模型
        schema = output.model_json_schema() if output else None  # 输出 Schema
        attempts, seqs, latencies, errors = 0, [], [], []  # 累计
        for position, name in enumerate(chain):  # 按顺序尝试每个模型
            convo = list(messages)  # 本模型的对话（修复消息只追加在这里）
            for round_ in (1, 2):  # 首轮 + 修复 1 次
                attempts += 1  # 计数
                resp, seq, ms = self._resilient(name, convo, tools, schema, case_id=case_id, day=day,  # 调一次（F15）
                                              actor=actor, attempt=attempts, repair=round_ == 2,  # 是否修复轮
                                              fallback=position > 0, errors_in=errors if round_ == 2 else [])  # 是否降级
                seqs.append(seq)  # 事件序号
                latencies.append(ms)  # 耗时
                errors, parsed, args = self._validate(resp, tools, output, known_mpns)  # 校验
                self._record_verdict(seq, errors, case_id=case_id, day=day, actor=actor)  # 记校验结论
                if not errors:  # 通过
                    return CallResult(resp, parsed, args, name, attempts, seqs, latencies)  # 返回
                if not self.repair or round_ == 2:  # 不修复，或修复也没过
                    break  # 本模型格式失败
                convo = convo + [Message("assistant", json.dumps(resp.raw["choices"][0]["message"],  # 原输出
                                                                ensure_ascii=False)),  # 以文本回给模型
                                 Message("user", REPAIR_PROMPT.format(  # 修复消息
                                     errors="\n".join(f"- {e}" for e in errors)))]  # 逐条错误
        raise ModelFailure(f"{executor}: 模型链 {chain} 全部格式失败", errors)  # 普通失败交回上层

    # -----------------------------------------------------------------------
    # _resilient：带重试与熔断的一次尝试（F15）
    # 功能：熔断中直接抛 EndpointUnavailable；传输失败按配置重试 + 退避；仍失败计入熔断并抛 EndpointUnavailable。
    # 依赖：resilience（未接入则直接调 _attempt）。
    # 完成：每次真实发出的请求都各有一条 llm_call 事件（_attempt 写）；凭证缺失等非传输错误原样上抛。
    # -----------------------------------------------------------------------
    def _resilient(self, name: str, *args, **kwargs):
        res = self.resilience  # 重试与熔断
        if res is None:  # 未接入
            return self._attempt(name, *args, **kwargs)  # 原样
        if res.is_open(name):  # 熔断中：不发请求
            raise EndpointUnavailable("circuit_open", f"模型端点 {name} 熔断中，暂不可用、可稍后重试")  # 交回执行者
        delays = res.delays()  # 各次重试前的等待
        for i in range(len(delays) + 1):  # 首次 + 重试
            try:  # 一次尝试
                out = self._attempt(name, *args, **kwargs)  # 调用
            except ModelUnavailable as exc:  # 传输失败
                kind = "upstream_error" if exc.kind == "upstream" else exc.kind  # 与工具错误类型对齐
                if kind not in res.config["retry"]["retryable"]:  # 非传输类（如凭证缺失）
                    raise  # 原样上抛
                if res.retryable(kind) and i < len(delays):  # 还能重试
                    res.sleep(delays[i])  # 指数退避
                    continue  # 再试
                res.record_failure(name)  # 计入熔断
                raise EndpointUnavailable(exc.kind, f"模型端点 {name} 重试后仍失败：{exc}") from exc  # 交回执行者
            res.record_success(name)  # 成功清零
            return out  # 结果
        raise AssertionError("不可达")  # 循环总会返回或抛出

    # -----------------------------------------------------------------------
    # _attempt：一次模型调用（一个端点、一轮）
    # 功能：限流 → 组装请求 → 传输 → 解析 → 写 llm_call 事件；传输异常也先记事件再抛出。
    # 依赖：limiter、providers、transports、session。
    # 完成：事件内容为完整请求体与原始响应（请求体里本就没有凭证）；prompt 哈希 = 请求体规范化 JSON 的 SHA-256。
    # -----------------------------------------------------------------------
    def _attempt(self, name: str, convo: list[Message], tools, schema, *, case_id, day, actor,
                 attempt: int, repair: bool, fallback: bool, errors_in: list[str]):
        endpoint = self.config["endpoints"][name]  # 端点配置
        body = build_request(endpoint, convo, list(tools), schema)  # 请求体
        base = {"gen_ai.system": endpoint["provider"], "gen_ai.request.model": endpoint["model"],  # 模型身份
                "endpoint": name, "attempt": attempt, "repair": repair, "fallback": fallback,  # 尝试信息
                "prompt_hash": sha256(canonical(body)), "request": body,  # 完整请求
                "gen_ai.request.temperature": body["temperature"], "repair_errors": errors_in}  # 温度与修复原因
        start = time.monotonic()  # 计时开始
        try:  # 限流与传输
            self.limiter.acquire(name)  # 限流挂点
            raw = self.transports[name].send(endpoint, body)  # 发送
            resp = parse_response(endpoint, raw)  # 解析
        except Exception as exc:  # 传输或解析失败（ModelUnavailable 等）
            failed = self.session.append("llm_call", day=day, actor=actor, case_id=case_id,  # 先记失败
                                         payload={**base, "error": {"type": type(exc).__name__,  # 错误类型
                                                                    "kind": getattr(exc, "kind", None),  # 细分
                                                                    "message": str(exc)}})  # 信息
            self._meter(failed["seq"], name, endpoint, 0, 0, start, repair, fallback,  # 计量失败调用
                        getattr(exc, "kind", None) or "upstream", case_id, day)  # 错误类型
            raise  # 交给上层（F15 重试熔断）
        ms = int((time.monotonic() - start) * 1000)  # 耗时（不进事件）
        event = self.session.append("llm_call", day=day, actor=actor, case_id=case_id,  # 记录
                                    payload={**base, "response": raw,  # 完整响应
                                             "gen_ai.response.finish_reasons": [resp.finish_reason],  # 结束原因
                                             "gen_ai.usage.input_tokens": resp.input_tokens,  # 输入 token
                                             "gen_ai.usage.output_tokens": resp.output_tokens})  # 输出 token
        self._meter(event["seq"], name, endpoint, resp.input_tokens, resp.output_tokens, start,  # 计量
                    repair, fallback, None, case_id, day)  # 成功调用
        return resp, event["seq"], ms  # 响应、事件序号、耗时

    def _meter(self, seq, name, endpoint, tin, tout, start, repair, fallback, error, case_id, day) -> None:
        """有 meter 时记一行 LLM 计量（F17）。"""
        if self.meter is not None:  # 可选
            self.meter.record_llm(run_id=self.session.run_id, event_seq=seq, case_id=case_id, day=day,  # 身份
                                  endpoint=name, model=endpoint["model"], input_tokens=tin,  # 端点与 token
                                  output_tokens=tout, latency_ms=int((time.monotonic() - start) * 1000),  # 耗时
                                  repair=repair, fallback=fallback,  # 修复与降级
                                  error_type="rate_limited" if error == "rate_limited" else error)  # 错误

    def _record_verdict(self, seq: int, errors: list[str], *, case_id, day, actor) -> None:
        """记下对某次 llm_call 的校验结论（通过或逐条错误），caused_by 指向该次调用。"""
        self.session.append("llm_validation", day=day, actor=actor, case_id=case_id,  # 校验事件
                            payload={"ok": not errors, "errors": errors}, caused_by=seq)  # 结论

    # -----------------------------------------------------------------------
    # _validate：校验一次响应
    # 功能：返回 (错误列表, 输出对象, 工具参数对象列表)。
    # 依赖：pydantic；_mpn_errors。
    # 完成：解析阶段问题、未知工具（附可用清单）、参数模型错误、输出 JSON 与模型错误、编造料号；
    #       给了工具但没调用且要求输出时按输出校验；既无工具调用又不要求输出时正文即结果。
    # -----------------------------------------------------------------------
    def _validate(self, resp: ModelResponse, tools, output, known_mpns):
        errors = list(resp.parse_errors)  # 解析阶段问题
        by_name = {t.name: t for t in tools}  # 工具索引
        args: list[BaseModel] = []  # 校验后的参数
        for call in resp.tool_calls:  # 每个工具调用
            spec = by_name.get(call.name)  # 找工具
            if spec is None:  # 不存在（含编造的 reschedule_work_order 之类，spec §5.5）
                errors.append(f"工具 {call.name!r} 不存在，可用工具：{', '.join(sorted(by_name)) or '无'}")  # 附清单
                continue  # 下一个
            try:  # 参数模型校验
                parsed = spec.params.model_validate(call.arguments)  # 校验
            except ValidationError as exc:  # 不合格
                errors += [f"工具 {call.name} 参数 {_loc(e)}：{e['msg']}" for e in exc.errors()]  # 逐条
                continue  # 下一个
            errors += self._mpn_errors(parsed, known_mpns, f"工具 {call.name} 参数")  # 料号检查
            args.append(parsed)  # 收下
        parsed_output = None  # 结构化输出
        if output is not None and not resp.tool_calls:  # 要求输出且没有工具调用
            text = FENCE.sub(r"\1", resp.text.strip())  # 去掉围栏
            try:  # 解析 JSON
                parsed_output = output.model_validate(json.loads(text))  # 解析并校验
            except json.JSONDecodeError:  # 不是 JSON
                errors.append("输出不是合法 JSON")  # 记错误
            except ValidationError as exc:  # 不合 Schema
                errors += [f"输出字段 {_loc(e)}：{e['msg']}" for e in exc.errors()]  # 逐条
            if parsed_output is not None:  # 解析成功
                errors += self._mpn_errors(parsed_output, known_mpns, "输出")  # 料号检查
        return errors, parsed_output, args  # 结论

    def _mpn_errors(self, obj: BaseModel, known: set[str] | None, where: str) -> list[str]:
        """找出对象里所有 Mpn 字段的值中不在已知集合的（编造料号）。"""
        if not self.mpn_check or known is None:  # 关闭或未给已知集合
            return []  # 不检查
        return [f"{where} 中的料号 {v!r} 不在已检索到的料号里，不能编造料号"  # 错误信息
                for v in _mpn_values(obj) if v not in known]  # 逐个检查


def _loc(err: dict) -> str:
    """pydantic 错误位置 → 点分路径。"""
    return ".".join(str(p) for p in err["loc"]) or "(根)"  # 路径


# ---------------------------------------------------------------------------
# _mpn_values：收集 pydantic 对象里所有 Mpn 字段的值
# 功能：递归进入嵌套模型、列表、字典值。
# 依赖：字段元数据 json_schema_extra 中的 x-mpn 标记（types.Mpn）。
# 完成：字段本身是 Mpn、或是 Mpn 列表都能收集到。
# ---------------------------------------------------------------------------
def _mpn_values(obj) -> list[str]:
    found: list[str] = []  # 结果
    if isinstance(obj, BaseModel):  # 模型对象
        for name, info in type(obj).model_fields.items():  # 每个字段
            value = getattr(obj, name)  # 字段值
            marked = any(isinstance(m, dict) and m.get("x-mpn") for m in  # 字段级标记
                         [info.json_schema_extra]) or _marked_in_metadata(info)  # 或类型元数据标记
            if marked:  # 是料号字段
                found += [value] if isinstance(value, str) else [v for v in value or [] if isinstance(v, str)]  # 收集
            else:  # 非料号字段，递归
                found += _mpn_values(value)  # 递归
    elif isinstance(obj, (list, tuple)):  # 列表
        for item in obj:  # 每个元素
            found += _mpn_values(item)  # 递归
    elif isinstance(obj, dict):  # 字典
        for item in obj.values():  # 每个值
            found += _mpn_values(item)  # 递归
    return found  # 结果


def _marked_in_metadata(info) -> bool:
    """字段类型是 list[Mpn] 时，标记藏在类型参数的元数据里；用 JSON Schema 判断最稳。"""
    try:  # 生成字段 Schema 片段
        schema = TypeAdapter(info.annotation).json_schema()  # 字段类型的 Schema
    except (PydanticSchemaGenerationError, PydanticInvalidForJsonSchema, TypeError):  # 复杂类型生成失败
        return False  # 当作未标记
    return bool(schema.get("x-mpn") or (schema.get("items") or {}).get("x-mpn"))  # 自身或元素带标记

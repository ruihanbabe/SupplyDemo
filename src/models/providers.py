"""文件摘要（F08 · 端点适配与传输）

把内部格式翻译成各端点的 OpenAI 兼容请求，并把响应解析回内部格式；端点差异全部来自 config/models.yaml 的 quirks。
- load_models_config：读配置并校验执行者引用的端点存在。
- build_request：组装请求体（temperature 固定 0；工具、tool_choice、并行工具调用、JSON 输出方式按差异配置）。
- parse_response：解析响应；参数可能是字符串或对象；工具调用可能写在正文 <tool_call> 里（兜底解析）。
- HttpTransport：真实 HTTP（httpx）；凭证只从环境变量读、只进请求头，不进请求体与任何记录（G1）。
- ScriptedTransport：测试用，按顺序返回预先写好的响应或异常，并记下收到的请求。
依赖：httpx、PyYAML、models.types。被 models.adapter 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # JSON 编解码
import os  # 读环境变量中的凭证
import re  # 正文内工具调用的兜底解析
from pathlib import Path  # 配置路径

import httpx  # HTTP 客户端
import yaml  # 读配置

from models.types import Message, ModelResponse, ModelUnavailable, ToolCall, ToolSpec  # 内部格式

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
MODELS_CONFIG = ROOT / "config" / "models.yaml"  # 默认配置
INLINE_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)  # 正文内工具调用格式


# 功能：读模型配置。依赖：PyYAML。完成：执行者引用的端点必须存在、json_mode 取值合法。
def load_models_config(path: Path = MODELS_CONFIG) -> dict:
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML
    for name, chain in config["executors"].items():  # 每个执行者的模型链
        missing = [e for e in chain if e not in config["endpoints"]]  # 未定义的端点
        if not chain or missing:  # 空链或引用不存在
            raise ValueError(f"执行者 {name} 的模型链无效：{chain}")  # 报错
    for name, ep in config["endpoints"].items():  # 每个端点
        if ep["quirks"]["json_mode"] not in ("json_schema", "json_object", "prompt"):  # 取值检查
            raise ValueError(f"端点 {name} 的 json_mode 不合法")  # 报错
    return config  # 返回配置


# ---------------------------------------------------------------------------
# build_request：内部格式 → OpenAI 兼容请求体
# 功能：见文件摘要。
# 依赖：端点配置；被 adapter 调用。
# 完成：工具数超上限报错（配置错误，不是模型错误）；json_mode=prompt / json_object 时把 Schema 写进系统提示；
#       不支持 tool_choice / 并行调用的端点不发这两个参数。
# ---------------------------------------------------------------------------
def build_request(endpoint: dict, messages: list[Message], tools: list[ToolSpec],
                  output_schema: dict | None) -> dict:
    quirks = endpoint["quirks"]  # 差异配置
    if len(tools) > quirks["max_tools"]:  # 工具过多
        raise ValueError(f"工具数 {len(tools)} 超过端点上限 {quirks['max_tools']}")  # 配置错误
    wire = [_wire_message(m) for m in messages]  # 消息转线上格式
    body = {"model": endpoint["model"], "messages": wire, "temperature": 0,  # temperature 固定 0（§5.12）
            "max_tokens": endpoint["max_tokens"]}  # 输出上限
    if tools:  # 有工具
        body["tools"] = [{"type": "function", "function": {  # OpenAI 工具格式
            "name": t.name, "description": t.description,  # 名称与描述
            "parameters": t.params.model_json_schema()}} for t in tools]  # 参数 Schema
        if quirks["tool_choice"]:  # 端点支持 tool_choice
            body["tool_choice"] = "auto"  # 由模型决定
        if quirks["parallel_tool_calls"]:  # 端点支持并行调用
            body["parallel_tool_calls"] = True  # 允许一次多个
    if output_schema is not None:  # 要求结构化输出
        if quirks["json_mode"] == "json_schema":  # 原生 JSON Schema 约束
            body["response_format"] = {"type": "json_schema",  # 约束格式
                                       "json_schema": {"name": "output", "schema": output_schema}}  # Schema
        else:  # json_object 或只靠提示
            if quirks["json_mode"] == "json_object":  # 只保证是 JSON
                body["response_format"] = {"type": "json_object"}  # JSON 模式
            hint = ("只输出一个 JSON 对象，不要输出其他文字。它必须符合以下 JSON Schema：\n"  # Schema 提示
                    + json.dumps(output_schema, ensure_ascii=False))  # Schema 原文
            body["messages"] = [{"role": "system", "content": hint}] + wire  # 放在最前面
    return body  # 请求体


def _wire_message(m: Message) -> dict:
    """一条内部消息 → 线上格式。"""
    wire: dict = {"role": m.role, "content": m.content}  # 角色与正文
    if m.tool_calls:  # 助手的工具调用
        wire["tool_calls"] = [{"id": c.id, "type": "function",  # 调用 ID
                               "function": {"name": c.name,  # 工具名
                                            "arguments": json.dumps(c.arguments, ensure_ascii=False)}}  # 参数
                              for c in m.tool_calls]  # 逐个
    if m.tool_call_id:  # 工具结果
        wire["tool_call_id"] = m.tool_call_id  # 对应调用
    return wire  # 线上消息


# ---------------------------------------------------------------------------
# parse_response：OpenAI 兼容响应 → 内部格式
# 功能：见文件摘要。
# 依赖：端点配置；被 adapter 调用。
# 完成：参数字符串解析失败时记入 parse_errors（交给校验与修复）；缺调用 ID 时按序补 call-N；
#       响应整体结构不对抛 ModelUnavailable('upstream')。
# ---------------------------------------------------------------------------
def parse_response(endpoint: dict, raw: dict) -> ModelResponse:
    try:  # 取第一条选择
        choice = raw["choices"][0]  # 选择
        msg = choice["message"]  # 消息
    except (KeyError, IndexError, TypeError):  # 结构不对
        raise ModelUnavailable("upstream", "响应缺少 choices[0].message") from None  # 上游错误
    text = msg.get("content") or ""  # 正文
    errors: list[str] = []  # 解析问题
    calls = []  # 工具调用
    for n, c in enumerate(msg.get("tool_calls") or [], start=1):  # 原生工具调用
        fn = c.get("function", {})  # 函数部分
        calls.append(ToolCall(id=c.get("id") or f"call-{n}", name=fn.get("name", ""),  # ID 与名称
                              arguments=_arguments(fn.get("arguments"), errors, fn.get("name"))))  # 参数
    if not calls and endpoint["quirks"]["inline_tool_calls"]:  # 兜底：正文里的工具调用
        for n, found in enumerate(INLINE_CALL.findall(text), start=1):  # 逐个匹配
            try:  # 解析 JSON
                obj = json.loads(found)  # {"name":..., "arguments":...}
            except json.JSONDecodeError:  # 不是合法 JSON
                errors.append(f"第 {n} 个 <tool_call> 不是合法 JSON")  # 记问题
                continue  # 下一个
            calls.append(ToolCall(id=f"call-{n}", name=obj.get("name", ""),  # 名称
                                  arguments=_arguments(obj.get("arguments"), errors, obj.get("name"))))  # 参数
        if calls:  # 有正文内调用
            text = INLINE_CALL.sub("", text).strip()  # 从正文里去掉
    usage = raw.get("usage") or {}  # 用量
    return ModelResponse(text=text, tool_calls=calls,  # 内部格式
                         input_tokens=usage.get("prompt_tokens", 0),  # 输入 token
                         output_tokens=usage.get("completion_tokens", 0),  # 输出 token
                         finish_reason=choice.get("finish_reason"), raw=raw,  # 结束原因与原文
                         parse_errors=errors)  # 解析问题


def _arguments(value, errors: list[str], name: str | None) -> dict:
    """参数可能是 JSON 字符串、对象或缺失；统一成字典，失败记问题并返回空字典。"""
    if value is None or value == "":  # 缺失
        return {}  # 空参数
    if isinstance(value, dict):  # 已是对象
        return value  # 直接用
    try:  # 字符串
        parsed = json.loads(value)  # 解析
    except (json.JSONDecodeError, TypeError):  # 失败
        errors.append(f"工具 {name} 的参数不是合法 JSON")  # 记问题
        return {}  # 空参数
    if not isinstance(parsed, dict):  # 不是对象
        errors.append(f"工具 {name} 的参数必须是 JSON 对象")  # 记问题
        return {}  # 空参数
    return parsed  # 字典


# ---------------------------------------------------------------------------
# HttpTransport：真实 HTTP 传输
# 功能：POST {base_url}/chat/completions；凭证从环境变量读、只放请求头。
# 依赖：httpx；client 可注入（测试用 httpx.MockTransport，不联网）。
# 完成：超时、429、5xx、非 JSON 响应分别映射为 ModelUnavailable 的 timeout / rate_limited / upstream；缺凭证为 config。
# ---------------------------------------------------------------------------
class HttpTransport:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client()  # HTTP 客户端

    def send(self, endpoint: dict, body: dict) -> dict:
        key = os.environ.get(endpoint["api_key_env"])  # 凭证只从环境变量读
        if not key:  # 没配凭证
            raise ModelUnavailable("config", f"未设置环境变量 {endpoint['api_key_env']}")  # 不泄露值
        try:  # 发请求
            resp = self.client.post(endpoint["base_url"].rstrip("/") + "/chat/completions",  # 地址
                                    json=body, timeout=endpoint["timeout_s"],  # 请求体与超时
                                    headers={"Authorization": f"Bearer {key}"})  # 凭证只在请求头
        except httpx.TimeoutException:  # 超时
            raise ModelUnavailable("timeout", "模型端点超时") from None  # 映射
        except httpx.HTTPError as exc:  # 其他网络错误
            raise ModelUnavailable("upstream", f"模型端点网络错误：{type(exc).__name__}") from None  # 映射
        if resp.status_code == 429:  # 限流
            raise ModelUnavailable("rate_limited", "模型端点限流")  # 映射
        if resp.status_code >= 400:  # 其他错误
            raise ModelUnavailable("upstream", f"模型端点返回 {resp.status_code}")  # 映射
        try:  # 解析 JSON
            return resp.json()  # 响应体
        except ValueError:  # 非 JSON
            raise ModelUnavailable("upstream", "模型端点返回的不是 JSON") from None  # 映射


# ---------------------------------------------------------------------------
# ScriptedTransport：测试用固定应答
# 功能：按顺序吐出预先写好的响应字典或异常；记录收到的每个请求体供断言。
# 依赖：无；被离线测试与后续 Feature 的离线测试使用（不产生任何真实调用）。
# 完成：脚本用完时抛 AssertionError，避免测试悄悄多调用。
# ---------------------------------------------------------------------------
class ScriptedTransport:
    def __init__(self, script: list):
        self.script = list(script)  # 待吐出的应答
        self.requests: list[dict] = []  # 收到的请求

    def send(self, endpoint: dict, body: dict) -> dict:
        self.requests.append(json.loads(json.dumps(body)))  # 记下请求副本
        if not self.script:  # 脚本用完
            raise AssertionError(f"{endpoint['model']} 被多调用了一次")  # 测试失败
        item = self.script.pop(0)  # 取下一个应答
        if isinstance(item, Exception):  # 预设的异常
            raise item  # 抛出
        return item  # 响应


def reply(text: str = "", tool_calls: list[dict] | None = None, tokens: tuple[int, int] = (10, 5)) -> dict:
    """测试辅助：构造一个 OpenAI 兼容响应字典。tool_calls 元素形如 {"name":..., "arguments": 字符串或对象}。"""
    calls = [{"id": f"c{n}", "type": "function", "function": c}  # 工具调用
             for n, c in enumerate(tool_calls or [], start=1)]  # 逐个
    message = {"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})}  # 消息
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],  # 选择
            "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}  # 用量

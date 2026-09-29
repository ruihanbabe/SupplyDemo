"""F08：模型适配层离线测试。全部使用 ScriptedTransport / httpx.MockTransport，不产生任何真实调用。"""
import httpx  # 用 MockTransport 测真实传输类，不联网
import pytest  # 测试框架
from pydantic import BaseModel  # 输出与参数模型

from infrastructure.config import load_harness_config  # 消融开关
from models.adapter import ModelAdapter  # 被测：门面
from models.providers import (  # 被测：传输
    HttpTransport,
    ScriptedTransport,
    load_models_config,
    reply,
)
from models.types import Message, ModelFailure, ModelUnavailable, Mpn, ToolSpec  # 内部格式
from session.log import Session  # 事件日志
from session.store import MemorySessionStore  # 内存存储

KNOWN = {"SKY13453-385LF", "MAX2837"}  # 已检索到的料号


class QuoteArgs(BaseModel):
    """查报价工具的参数。"""
    mpns: list[Mpn]  # 料号列表（每个都要在已知集合）
    qty: int  # 数量


class Pick(BaseModel):
    """结构化输出：选中的料号与理由。"""
    mpn: Mpn  # 选中的料号
    reason: str  # 理由


TOOLS = [ToolSpec("get_quotes", "查多个料号在三家分销商的报价。例：get_quotes(mpns=[...], qty=300)", QuoteArgs)]  # 工具


# 功能：搭一个适配层：两个端点各一个脚本传输；可覆盖消融开关。
def make(glm: list, qwen: list, **switches) -> tuple[ModelAdapter, Session, dict]:
    session = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    transports = {"glm-flash": ScriptedTransport(glm), "qwen-free": ScriptedTransport(qwen)}  # 传输
    sw = {**load_harness_config()["ablation"], **switches}  # 默认开关 + 覆盖
    return ModelAdapter(load_models_config(), transports, session, switches=sw), session, transports  # 返回


def ask(adapter: ModelAdapter, **kw):
    """用固定消息调用一次。"""
    return adapter.call("default", [Message("user", "给 SKY13453 找报价")], day=16, actor="verifier@K7",  # 调用
                        case_id="K7", known_mpns=KNOWN, **kw)  # 已知料号


# 功能：两种端点差异解析成同一内部格式：GLM 原生工具调用（参数字符串），Qwen 正文里的 <tool_call>（参数对象）。
def test_quirks_normalize_to_same_internal_format():
    args = '{"mpns": ["SKY13453-385LF"], "qty": 300}'  # 参数字符串
    a, _, t = make([reply(tool_calls=[{"name": "get_quotes", "arguments": args}])], [])  # GLM 应答
    r1 = ask(a, tools=TOOLS)  # 调用
    inline = '<tool_call>{"name": "get_quotes", "arguments": {"mpns": ["SKY13453-385LF"], "qty": 300}}</tool_call>'  # 正文调用
    qwen_only, _, tq = make([reply("x")] * 2, [reply(inline)], schema_repair=False)  # GLM 两次失败后降级
    r2 = ask(qwen_only, tools=TOOLS, output=Pick)  # 要么工具调用要么输出
    assert r1.tool_args == r2.tool_args == [QuoteArgs(mpns=["SKY13453-385LF"], qty=300)]  # 同一内部结果
    assert r2.endpoint == "qwen-free" and r2.response.text == ""  # 正文里的调用被剥离
    glm_req, qwen_req = t["glm-flash"].requests[0], tq["qwen-free"].requests[0]  # 两个请求
    assert glm_req["tool_choice"] == "auto" and glm_req["temperature"] == 0  # GLM 支持 tool_choice
    assert "tool_choice" not in qwen_req and "parallel_tool_calls" not in qwen_req  # Qwen 不发不支持的参数
    assert qwen_req["messages"][0]["role"] == "system" and "JSON Schema" in qwen_req["messages"][0]["content"]  # 提示里给 Schema


# 功能：Schema 不过 → 修复 1 次成功；修复消息里有逐条错误；每次尝试都有 llm_call 事件。
def test_repair_once_then_success():
    a, s, t = make([reply('{"mpn": "MAX2837"}'),  # 缺 reason
                    reply('```json\n{"mpn": "MAX2837", "reason": "引脚兼容"}\n```')], [])  # 修复后合格（带围栏）
    r = ask(a, output=Pick)  # 调用
    assert r.output == Pick(mpn="MAX2837", reason="引脚兼容") and r.attempts == 2  # 第二次成功
    repair_msg = t["glm-flash"].requests[1]["messages"][-1]["content"]  # 修复消息
    assert "reason" in repair_msg and "不合格" in repair_msg  # 带逐条错误
    calls = s.events(type="llm_call")  # 调用记录
    assert [c["payload"]["repair"] for c in calls] == [False, True]  # 第二次是修复轮
    assert [v["payload"]["ok"] for v in s.events(type="llm_validation")] == [False, True]  # 校验结论


# 功能：修复仍失败 → 降级到备用成功；备用也失败 → ModelFailure。
def test_fallback_then_failure():
    a, s, _ = make([reply("不是 JSON"), reply("还不是")],  # 主模型两次失败
                   [reply('{"mpn": "MAX2837", "reason": "ok"}')])  # 备用成功
    r = ask(a, output=Pick)  # 调用
    assert r.endpoint == "qwen-free" and r.attempts == 3  # 降级成功
    assert s.events(type="llm_call")[-1]["payload"]["fallback"] is True  # 记为降级
    b, _, _ = make([reply("x"), reply("x")], [reply("x"), reply("x")])  # 全部失败
    with pytest.raises(ModelFailure) as exc:  # 普通失败
        ask(b, output=Pick)  # 调用
    assert exc.value.errors == ["输出不是合法 JSON"]  # 带最后一次错误


# 功能：未知工具名（如编造的改排产工具）报错并附可用工具清单；编造料号被拒并修复。
def test_unknown_tool_and_fabricated_mpn():
    a, s, _ = make([reply(tool_calls=[{"name": "reschedule_work_order", "arguments": "{}"}]),  # 编造工具
                    reply(tool_calls=[{"name": "get_quotes",  # 修复：换成合法工具但料号编造
                                       "arguments": '{"mpns": ["SKY99999"], "qty": 1}'}])],  # 编造料号
                   [reply(tool_calls=[{"name": "get_quotes",  # 备用：合法
                                       "arguments": '{"mpns": ["MAX2837"], "qty": 1}'}])])  # 已知料号
    r = ask(a, tools=TOOLS)  # 调用
    errors = [v["payload"]["errors"] for v in s.events(type="llm_validation")]  # 各次错误
    assert errors[0] == ["工具 'reschedule_work_order' 不存在，可用工具：get_quotes"]  # 附清单
    assert "不能编造料号" in errors[1][0] and errors[2] == []  # 编造料号被拒，备用通过
    assert r.tool_args[0].mpns == ["MAX2837"]  # 最终参数


# 功能：三个消融开关各自生效。
def test_ablation_switches():
    fabricated = reply('{"mpn": "FAKE-1", "reason": "x"}')  # 编造料号的输出
    a, _, _ = make([fabricated], [], fabricated_mpn_check=False)  # 关掉料号检查
    assert ask(a, output=Pick).output.mpn == "FAKE-1"  # 放行
    b, _, _ = make([reply("x")], [reply('{"mpn": "MAX2837", "reason": "ok"}')], schema_repair=False)  # 关修复
    assert ask(b, output=Pick).attempts == 2  # 主模型只试 1 次就降级
    c, _, _ = make([reply("x"), reply("x")], [], model_fallback=False)  # 关降级
    with pytest.raises(ModelFailure):  # 不换备用
        ask(c, output=Pick)  # 失败


# 功能：传输层异常先记事件再抛 ModelUnavailable；真实 HTTP 传输凭证只进请求头，不进请求体与事件。
def test_transport_errors_and_credentials(monkeypatch):
    a, s, _ = make([ModelUnavailable("rate_limited", "限流")], [])  # 主模型限流
    with pytest.raises(ModelUnavailable):  # 交给上层（F15）
        ask(a, output=Pick)  # 调用
    assert s.events(type="llm_call")[0]["payload"]["error"]["kind"] == "rate_limited"  # 已记录
    seen = {}  # 记下 HTTP 请求

    def handler(request: httpx.Request) -> httpx.Response:  # 假的模型端点
        seen["auth"], seen["body"] = request.headers["Authorization"], request.content.decode()  # 记录
        return httpx.Response(200, json=reply('{"mpn": "MAX2837", "reason": "ok"}'))  # 合格应答

    monkeypatch.setenv("ZHIPU_API_KEY", "sk-test-secret")  # 测试凭证（只在本测试进程）
    session = Session(MemorySessionStore(), "r", clock=lambda: "t")  # 事件日志
    http = HttpTransport(httpx.Client(transport=httpx.MockTransport(handler)))  # 不联网的 HTTP
    adapter = ModelAdapter(load_models_config(), {"glm-flash": http}, session)  # 适配层
    ask(adapter, output=Pick)  # 调用
    assert seen["auth"] == "Bearer sk-test-secret" and "sk-test-secret" not in seen["body"]  # 只在请求头
    assert "sk-test-secret" not in str(session.events())  # 不进任何事件
    monkeypatch.delenv("ZHIPU_API_KEY")  # 去掉凭证
    with pytest.raises(ModelUnavailable) as exc:  # 缺凭证
        ask(adapter, output=Pick)  # 调用
    assert exc.value.kind == "config" and "ZHIPU_API_KEY" in str(exc.value)  # 只报变量名

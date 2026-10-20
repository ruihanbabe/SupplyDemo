"""F42 的受控 Harness 与 Pi JSONL 协议边界。

Python 持有任务上下文、工具与 Trace；Node 只是一次 turn 的无状态协议端点。
这里没有 Provider、数据库连接串、shell 或文件系统工具。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

from psycopg import AsyncConnection

from product_api.trace import TraceRepository

PROTOCOL_VERSION = 1
MAX_JSONL_BYTES = 16_384
PI_CORE_ENGINE = "pi-agent-core@0.73.1"
_SECRET_FIELD = re.compile(r"(?:authorization|cookie|credential|password|secret|token)", re.IGNORECASE)
_SAFE_CALL_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_FAILURE_CODES = frozenset({"model_failed", "tool_failed", "sidecar_exited", "timeout"})


class HarnessProtocolError(Exception):
    """Sidecar 违反固定 v1 协议，调用者必须失败关闭。"""


class HarnessToolError(Exception):
    """Sidecar 请求了未允许、格式不合法或失败的工具。"""


class HarnessTurnError(HarnessProtocolError):
    """可被 F35 映射的稳定 turn 失败分类。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


ToolHandler = Callable[[dict[str, object]], object | Awaitable[object]]
ToolArgumentValidator = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class ToolDefinition:
    """Python 授权的一个精确 R0 工具及其参数校验器。"""

    name: str
    handler: ToolHandler
    validator: ToolArgumentValidator | None = None
    risk_level: str = "r0"


@dataclass(frozen=True)
class HarnessContext:
    """已由上游业务服务验证后的最小 turn 输入，不承载原始外部文本。"""

    organization_id: UUID
    run_id: UUID
    case_id: UUID | None
    task: str
    fact_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.task or len(self.task) > 1_000:
            raise ValueError("任务必须是 1–1000 字符的摘要")
        if len(self.fact_references) > 32 or any(not ref or len(ref) > 160 for ref in self.fact_references):
            raise ValueError("事实引用必须是最多 32 条的有限标识")


@dataclass(frozen=True)
class TurnResult:
    """返回协议状态和可选结构化结果；永不返回未解析的模型正文。"""

    turn_id: UUID
    status: str
    tool_calls: tuple[str, ...]
    event_count: int
    output: dict[str, object] | None = None


class TraceSink(Protocol):
    """Adapter 所需的最小审计写口；调用方决定其 SQL 事务范围。"""

    async def record(self, event_type: str, metadata: dict[str, object], sequence: int) -> None: ...


class AgentEnginePort(Protocol):
    """The turn-level boundary consumed by the durable Worker."""

    async def run_turn(
        self, context: HarnessContext, tools: ToolRegistry, trace: TraceSink,
        cancel_event: asyncio.Event | None = None,
    ) -> TurnResult: ...


@dataclass
class MemoryTraceSink:
    """仅用于离线契约测试，不能作为产品恢复事实。"""

    events: list[dict[str, object]] = field(default_factory=list)

    async def record(self, event_type: str, metadata: dict[str, object], sequence: int) -> None:
        self.events.append({"eventType": event_type, "metadata": metadata, "sequence": sequence})


class SqlTraceSink:
    """将已验证事件与最小 checkpoint 写进调用方的 F40 SQL 事务。"""

    def __init__(
        self,
        connection: AsyncConnection[dict[str, object]],
        traces: TraceRepository,
        trace_run_id: UUID,
        case_id: UUID | None,
    ) -> None:
        self._connection = connection
        self._traces = traces
        self._trace_run_id = trace_run_id
        self._case_id = case_id

    async def record(self, event_type: str, metadata: dict[str, object], sequence: int) -> None:
        checkpoint = {
            "protocolVersion": PROTOCOL_VERSION,
            "sequence": sequence,
            "eventType": event_type,
            "callId": metadata.get("callId"),
        }
        artifact_id = await self._traces.append_artifact(
            self._connection,
            self._trace_run_id,
            self._case_id,
            uuid4(),
            "pi_checkpoint",
            f"Pi checkpoint {sequence}: {event_type}",
            checkpoint,
        )
        await self._traces.append_event(
            self._connection,
            self._trace_run_id,
            self._case_id,
            event_type,
            f"Pi protocol event {event_type}",
            metadata,
            artifact_id,
        )


class ToolRegistry:
    """Python 侧固定工具白名单；Node 提供的名字从不被动态导入或解释。"""

    def __init__(self, tools: tuple[ToolDefinition, ...]) -> None:
        names = [tool.name for tool in tools]
        if (
            not names
            or len(names) > 16
            or len(names) != len(set(names))
            or any(not re.fullmatch(r"[a-z][a-z0-9_.-]{1,95}", name) for name in names)
        ):
            raise ValueError("工具名必须是 1–16 个唯一的受限标识")
        if any(tool.risk_level != "r0" for tool in tools):
            raise ValueError("F42/F36 当前只允许 R0 只读工具")
        if any(tool.name != "supply.impact" and tool.validator is None for tool in tools):
            raise ValueError("非历史 supply.impact 工具必须提供显式参数校验器")
        self._tools = {tool.name: tool for tool in tools}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    async def execute(self, name: str, arguments: object) -> object:
        if name not in self._tools or not isinstance(arguments, dict):
            raise HarnessToolError("工具不在当前白名单或参数不是对象")
        tool = self._tools[name]
        validator = tool.validator or self._validate_supply_impact
        try:
            validator(arguments)
        except HarnessToolError:
            raise
        except (TypeError, ValueError) as error:
            raise HarnessToolError("工具参数校验失败") from error
        result = tool.handler(arguments)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _validate_supply_impact(arguments: dict[str, object]) -> None:
        if set(arguments) - {"partNumber", "asOf"}:
            raise HarnessToolError("supply.impact 参数不在固定 schema 内")
        if not isinstance(arguments.get("partNumber"), str) or not arguments["partNumber"]:
            raise HarnessToolError("supply.impact 必须提供 partNumber")
        if "asOf" in arguments and not isinstance(arguments["asOf"], str):
            raise HarnessToolError("supply.impact.asOf 必须是字符串")


class PiRuntimeAdapter:
    """管理一次短生命周期 Node JSONL 子进程，任何协议偏差都会终止它。"""

    def __init__(
        self, command: tuple[str, ...], *, timeout_seconds: float = 5.0,
        gateway_url: str | None = None, gateway_capability: str | None = None,
    ) -> None:
        if not command:
            raise ValueError("Pi sidecar command 不能为空")
        if (gateway_url is None) != (gateway_capability is None):
            raise ValueError("Gateway 地址与单 Run 能力必须同时配置")
        self._command = command
        self._timeout_seconds = timeout_seconds
        self._gateway_url = gateway_url
        self._gateway_capability = gateway_capability

    async def run_turn(
        self,
        context: HarnessContext,
        tools: ToolRegistry,
        trace: TraceSink,
        cancel_event: asyncio.Event | None = None,
    ) -> TurnResult:
        child_environment = {key: os.environ[key] for key in ("PATH", "LANG", "TZ") if key in os.environ}
        if self._gateway_url is not None:
            child_environment["SUPPLYAGENT_GATEWAY_URL"] = self._gateway_url
            child_environment["SUPPLYAGENT_GATEWAY_CAPABILITY"] = self._gateway_capability or ""
        process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_environment,
        )
        assert process.stdin and process.stdout
        turn_id, sequence, calls = uuid4(), 0, []
        expected, reported_failure = "turn.started", False
        try:
            await self._write(
                process,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "type": "turn.start",
                    "turnId": str(turn_id),
                    "task": context.task,
                    "context": {"factReferences": list(context.fact_references)},
                    "tools": [{"name": name} for name in tools.names],
                },
            )
            while True:
                event = await self._read_or_cancel(process, cancel_event)
                if event is None:
                    await self._cancel(process, turn_id)
                    sequence += 1
                    await trace.record("pi.turn_cancelled", {"code": "cancelled"}, sequence)
                    return TurnResult(turn_id, "cancelled", tuple(calls), sequence)
                sequence += 1
                event_type = self._validate_event(event, turn_id, expected)
                metadata = self._event_metadata(event)
                await trace.record(f"pi.{event_type.replace('.', '_')}", metadata, sequence)
                if event_type == "turn.started":
                    expected = "tool.call_or_complete"
                elif event_type == "tool.call":
                    call_id, name = str(event["callId"]), str(event["name"])
                    result = await self._execute_or_cancel(tools, name, event["arguments"], cancel_event)
                    if result is None:
                        await self._cancel(process, turn_id)
                        sequence += 1
                        await trace.record("pi.turn_cancelled", {"code": "cancelled"}, sequence)
                        return TurnResult(turn_id, "cancelled", tuple(calls), sequence)
                    calls.append(name)
                    await self._write(
                        process,
                        {
                            "protocolVersion": PROTOCOL_VERSION,
                            "type": "tool.result",
                            "turnId": str(turn_id),
                            "callId": call_id,
                            "result": result,
                        },
                    )
                elif event_type == "turn.completed":
                    await self._close_stdin(process)
                    await self._wait(process)
                    output = event.get("output")
                    return TurnResult(
                        turn_id,
                        "completed",
                        tuple(calls),
                        sequence,
                        output if isinstance(output, dict) else None,
                    )
                elif event_type == "turn.cancelled":
                    await self._close_stdin(process)
                    await self._wait(process)
                    return TurnResult(turn_id, "cancelled", tuple(calls), sequence)
                elif event_type == "model.delta":
                    continue
                else:
                    reported_failure = True
                    raise HarnessTurnError(str(event["code"]))
        except HarnessToolError:
            await trace.record("pi.turn_failed", {"code": "tool_failed"}, sequence + 1)
            raise HarnessTurnError("tool_failed")
        except HarnessTurnError as error:
            if not reported_failure:
                await trace.record("pi.turn_failed", {"code": error.code}, sequence + 1)
            raise
        except BaseException:
            await self._terminate(process)
            raise
        finally:
            if process.returncode is None:
                await self._terminate(process)

    async def _read_or_cancel(
        self, process: asyncio.subprocess.Process, cancel_event: asyncio.Event | None
    ) -> dict[str, object] | None:
        if cancel_event is None:
            return await self._read(process)
        read_task = asyncio.create_task(self._read(process))
        cancel_task = asyncio.create_task(cancel_event.wait())
        done, pending = await asyncio.wait({read_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return None if cancel_task in done else read_task.result()

    @staticmethod
    async def _execute_or_cancel(
        tools: ToolRegistry,
        name: str,
        arguments: object,
        cancel_event: asyncio.Event | None,
    ) -> object | None:
        if cancel_event is None:
            return await tools.execute(name, arguments)
        execute_task = asyncio.create_task(tools.execute(name, arguments))
        cancel_task = asyncio.create_task(cancel_event.wait())
        done, pending = await asyncio.wait({execute_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return None if cancel_task in done else execute_task.result()

    async def _read(self, process: asyncio.subprocess.Process) -> dict[str, object]:
        assert process.stdout
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=self._timeout_seconds)
        except TimeoutError as error:
            raise HarnessTurnError("timeout") from error
        if not line:
            stderr = (await process.stderr.read()).decode("utf-8", "replace") if process.stderr else ""
            raise HarnessTurnError("sidecar_exited") from RuntimeError(stderr[:200])
        if len(line) > MAX_JSONL_BYTES:
            raise HarnessProtocolError("sidecar JSONL 行超限")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise HarnessProtocolError("sidecar 输出不是 JSON") from error
        if not isinstance(payload, dict):
            raise HarnessProtocolError("sidecar JSONL 必须是对象")
        self._reject_secret_fields(payload)
        return payload

    @staticmethod
    async def _write(process: asyncio.subprocess.Process, payload: dict[str, object]) -> None:
        assert process.stdin
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_JSONL_BYTES:
            raise HarnessProtocolError("JSONL 请求超限")
        process.stdin.write(encoded)
        await process.stdin.drain()

    @staticmethod
    async def _cancel(process: asyncio.subprocess.Process, turn_id: UUID) -> None:
        try:
            await PiRuntimeAdapter._write(
                process,
                {"protocolVersion": PROTOCOL_VERSION, "type": "turn.cancel", "turnId": str(turn_id)},
            )
        except (BrokenPipeError, ConnectionResetError):
            # The sidecar may close stdin immediately after emitting turn.cancelled.
            pass

    @staticmethod
    async def _close_stdin(process: asyncio.subprocess.Process) -> None:
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
            await process.stdin.wait_closed()

    async def _wait(self, process: asyncio.subprocess.Process) -> None:
        code = await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
        if code != 0:
            raise HarnessTurnError("sidecar_exited")

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
            except TimeoutError:
                process.kill()
                await process.wait()

    @staticmethod
    def _validate_event(event: Mapping[str, object], turn_id: UUID, expected: str) -> str:
        event_type = event.get("type")
        if event.get("protocolVersion") != PROTOCOL_VERSION or event.get("turnId") != str(turn_id):
            raise HarnessProtocolError("sidecar 协议版本或 turn 标识不匹配")
        if event_type == "turn.started" and expected == "turn.started":
            PiRuntimeAdapter._exact_one_of(
                event,
                (
                    {"protocolVersion", "type", "turnId"},
                    {"protocolVersion", "type", "turnId", "engine"},
                ),
            )
            if "engine" in event and event["engine"] != PI_CORE_ENGINE:
                raise HarnessProtocolError("sidecar Pi Core 版本不匹配")
            return str(event_type)
        if event_type == "tool.call" and expected == "tool.call_or_complete":
            PiRuntimeAdapter._exact(event, {"protocolVersion", "type", "turnId", "callId", "name", "arguments"})
            if not isinstance(event.get("callId"), str) or not _SAFE_CALL_ID.fullmatch(event["callId"]):
                raise HarnessProtocolError("tool callId 非法")
            return str(event_type)
        if event_type == "turn.completed" and expected == "tool.call_or_complete":
            PiRuntimeAdapter._exact_one_of(
                event,
                (
                    {"protocolVersion", "type", "turnId", "status"},
                    {"protocolVersion", "type", "turnId", "status", "output"},
                ),
            )
            if event.get("status") != "completed":
                raise HarnessProtocolError("turn.completed 状态非法")
            if "output" in event and not isinstance(event["output"], dict):
                raise HarnessProtocolError("turn.completed output 必须是对象")
            return str(event_type)
        if event_type == "model.delta" and expected == "tool.call_or_complete":
            PiRuntimeAdapter._exact(event, {"protocolVersion", "type", "turnId", "delta"})
            if not isinstance(event["delta"], str) or not event["delta"]:
                raise HarnessProtocolError("model.delta 非法")
            return str(event_type)
        if event_type == "turn.cancelled" and expected in {"turn.started", "tool.call_or_complete", "turn.cancelled"}:
            PiRuntimeAdapter._exact(event, {"protocolVersion", "type", "turnId", "code"})
            if event["code"] != "cancelled":
                raise HarnessProtocolError("turn.cancelled 状态非法")
            return str(event_type)
        if event_type == "turn.failed" and expected in {"turn.started", "tool.call_or_complete"}:
            PiRuntimeAdapter._exact(event, {"protocolVersion", "type", "turnId", "code"})
            if event["code"] not in _FAILURE_CODES:
                raise HarnessProtocolError("turn.failed 状态非法")
            return str(event_type)
        raise HarnessProtocolError("sidecar 事件不在允许顺序中")

    @staticmethod
    def _exact(event: Mapping[str, object], keys: set[str]) -> None:
        if set(event) != keys:
            raise HarnessProtocolError("sidecar 事件含未知或缺失字段")

    @staticmethod
    def _exact_one_of(event: Mapping[str, object], allowed: tuple[set[str], ...]) -> None:
        if set(event) not in allowed:
            raise HarnessProtocolError("sidecar 事件含未知或缺失字段")

    @staticmethod
    def _event_metadata(event: Mapping[str, object]) -> dict[str, object]:
        metadata = {key: event[key] for key in ("callId", "name", "code", "engine") if key in event}
        if event.get("type") == "model.delta":
            metadata["length"] = len(str(event["delta"]))
        return metadata

    @staticmethod
    def _reject_secret_fields(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if _SECRET_FIELD.search(key):
                    raise HarnessProtocolError("协议不允许敏感字段")
                PiRuntimeAdapter._reject_secret_fields(child)
        elif isinstance(value, list):
            for child in value:
                PiRuntimeAdapter._reject_secret_fields(child)


class Harness:
    """F42 的最小组装器：只把确定性上下文和 Python 白名单交给 Adapter。"""

    def __init__(self, adapter: AgentEnginePort, tools: ToolRegistry) -> None:
        self._adapter = adapter
        self._tools = tools

    async def execute(
        self, context: HarnessContext, trace: TraceSink, cancel_event: asyncio.Event | None = None
    ) -> TurnResult:
        return await self._adapter.run_turn(context, self._tools, trace, cancel_event)

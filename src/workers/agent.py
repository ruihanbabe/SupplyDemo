"""A worker that reasons with a model inside a bounded tool loop.

Two phases: explore with tools and no output schema, then one call with the schema and
no tools. Tools plus a constrained answer in one request is where providers disagree,
and the round cap holds because the last call has nothing to reach for.

Which tools are offered is the registry's decision (scenario, permissions), and what the
answer means is the subclass's — `conclude` is where any rule about the content applies.
"""
from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar
from uuid import uuid4

from contracts import errors
from contracts.llm import (
    ChatMessage,
    ModelBackend,
    ModelError,
    ModelRequest,
    ModelResult,
    ToolCall,
    ToolSpec,
)
from contracts.worker import Usage, WorkerTask
from infrastructure.agent_config import AgentConfig
from tools.registry import Invocation, ToolOutcome, ToolRegistry
from workers.base import Outcome, Worker, WorkerFailure


@dataclass(frozen=True)
class ToolObservation:
    call: ToolCall
    outcome: ToolOutcome


class AgentWorker(Worker):
    kind = "agent"
    max_tool_rounds: ClassVar[int] = 4
    #: JSON Schema of the final answer.
    output_schema: ClassVar[dict[str, Any]]

    def __init__(self, backend: ModelBackend, registry: ToolRegistry, config: AgentConfig,
                 invocation: Callable[[WorkerTask], Invocation]) -> None:
        if config.worker != self.name:
            raise ValueError(f"config for {config.worker!r} given to {self.name!r}")
        self._backend = backend
        self._registry = registry
        self._config = config
        #: Per task: carries run, trace, scenario, session and audit sink.
        self._invocation = invocation

    @abstractmethod
    def brief(self, task: WorkerTask) -> Sequence[ChatMessage]:
        """The opening messages for this task."""

    @abstractmethod
    def finalize(self, task: WorkerTask) -> ChatMessage:
        """The message that asks for the structured answer."""

    @abstractmethod
    def conclude(self, task: WorkerTask, answer: dict[str, Any],
                 observations: Sequence[ToolObservation]) -> Outcome:
        """Turn the structured answer into this worker's content."""

    def execute(self, task: WorkerTask, *, deadline: float | None) -> Outcome:
        invocation = self._invocation(task)
        tools = tuple(self._registry.specs(scenario=invocation.scenario, worker=self.name,
                                           session=invocation.session))
        messages = list(self.brief(task))
        observations: list[ToolObservation] = []
        spent = Usage()

        for _ in range(self.max_tool_rounds):
            result, spent = self._call(task, messages, tools, None, deadline, spent)
            messages.append(ChatMessage(role="assistant", content=result.content,
                                        tool_calls=result.tool_calls))
            if not result.wants_tools:
                break
            for call in result.tool_calls:
                outcome = self._registry.dispatch(call, invocation)
                observations.append(ToolObservation(call, outcome))
                messages.append(ChatMessage(role="tool", content=outcome.for_model(),
                                            tool_call_id=call.id))

        messages.append(self.finalize(task))
        final, spent = self._call(task, messages, (), self.output_schema, deadline, spent)
        if final.structured_output is None:
            raise WorkerFailure(errors.MALFORMED_RESPONSE, "no structured answer",
                                usage=spent)
        return replace(self.conclude(task, final.structured_output, observations),
                       usage=spent)

    def _call(self, task: WorkerTask, messages: list[ChatMessage],
              tools: tuple[ToolSpec, ...], schema: dict[str, Any] | None,
              deadline: float | None, spent: Usage) -> tuple[ModelResult, Usage]:
        timeout = self._config.timeout_seconds
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerFailure(errors.TIMEOUT, usage=spent)
            timeout = min(timeout, remaining)
        try:
            result = self._backend.complete(ModelRequest(
                call_id=str(uuid4()), model=self._config.model, messages=tuple(messages),
                tools=tools, output_schema=schema, temperature=self._config.temperature,
                max_tokens=self._config.max_tokens, timeout_seconds=timeout,
                trace_id=task.trace_id))
        except ModelError as exc:
            raise WorkerFailure(exc.code, str(exc), usage=spent) from exc
        return result, replace(
            spent,
            prompt_tokens=(spent.prompt_tokens or 0) + (result.usage.prompt_tokens or 0),
            output_tokens=(spent.output_tokens or 0) + (result.usage.output_tokens or 0))

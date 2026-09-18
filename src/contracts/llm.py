"""Model provider port and the types that cross it.

Core layers depend on this module, never on a vendor SDK. Swapping providers must not
change the meaning of anything below; see ARCHITECTURE.md's Infrastructure row.

Every field here exists because llm_call (docs/spec/data-model.md) has to record it:
an LLM call that leaves no auditable trace is not acceptable in this project.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation the model asked for.

    `arguments` stays the raw string the model produced. It is parsed and validated at
    the tool boundary, not here: model output is untrusted input, and keeping the raw
    text lets the audit record show exactly what was asked for, including malformed JSON.
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    #: Set on role="tool" messages to bind the result to the call that requested it.
    tool_call_id: str | None = None


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class Completion:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    model: str
    finish_reason: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ModelError(RuntimeError):
    """A provider call that did not succeed.

    Carried explicitly rather than returned as an empty completion: ARCHITECTURE.md
    requires that external failures are never disguised as success.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


@runtime_checkable
class ModelProvider(Protocol):
    """The only surface core layers may use to reach a language model."""

    name: str

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> Completion:
        ...

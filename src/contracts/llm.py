"""The model port and the types that cross it.

Core layers depend on this module, never on a vendor SDK. Swapping backends must not
change the meaning of anything below; see ARCHITECTURE.md's Model Gateway row and
DECISIONS.md D05.

Every field here exists because llm_call (docs/spec/data-model.md) has to record it: an
LLM call that leaves no auditable trace is not acceptable in this project.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from contracts.errors import CAPABILITY_UNSUPPORTED, RETRYABLE

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
class ModelCapabilities:
    """What a backend can actually do.

    Declared rather than assumed. Harness does not presume every provider shares OpenAI's
    semantics: some have no native tool calling, some cannot return a schema-constrained
    object, some report no usage at all. A request that needs an absent capability is
    refused up front instead of quietly losing the constraint.
    """

    native_tool_calling: bool = False
    parallel_tool_calls: bool = False
    structured_output: bool = False
    streaming: bool = False
    cancellation: bool = False
    usage_reporting: bool = False
    vision_input: bool = False
    max_context_tokens: int | None = None


@dataclass(frozen=True)
class ModelRequest:
    """One bounded call, fully described.

    `context_bundle_id` is the link back to what the model was shown. It is optional
    only until F14 exists; once ContextCompiler is in place every production request
    carries one, because "what did this call actually see" has to stay answerable.
    """

    call_id: str
    model: str
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    parallel_tool_calls: bool = False
    output_schema: dict[str, Any] | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    stream: bool = False
    timeout_seconds: float | None = None
    context_bundle_id: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class ModelResult:
    """One normalised answer, whichever backend produced it."""

    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    structured_output: dict[str, Any] | None = None
    model: str = ""
    backend: str = ""
    finish_reason: str = "unknown"
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    call_id: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class StreamEvent:
    """One step of a streamed answer.

    Only two kinds exist on purpose. `text` carries a visible delta; `done` carries the
    assembled ModelResult, tool calls included. Backends fragment tool-call arguments
    across chunks in vendor-specific ways, so they are reassembled behind this port and
    surfaced once, whole — a half-parsed call is never handed to the caller.
    """

    kind: Literal["text", "done"]
    text: str = ""
    result: ModelResult | None = None


class ModelError(RuntimeError):
    """A call that did not succeed.

    Carried explicitly rather than returned as an empty result: ARCHITECTURE.md requires
    that external failures are never disguised as success.
    """

    def __init__(self, code: str, message: str, *, retryable: bool | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        # Retryability follows from the code unless a caller overrides it, so two raise
        # sites for the same failure cannot disagree about whether it may be retried.
        self.retryable = code in RETRYABLE if retryable is None else retryable
        self.retry_after = retry_after


def negotiate(request: ModelRequest, capabilities: ModelCapabilities) -> None:
    """Refuse a request the backend cannot honour, before it is sent.

    This is the single place D05's "explicitly refuse or degrade, never silently change
    the semantics" is enforced. Every backend calls it first; the check is here rather
    than in each backend so that adding a vendor cannot accidentally skip it.
    """
    unmet = []
    if request.tools and not capabilities.native_tool_calling:
        unmet.append("native_tool_calling")
    if request.parallel_tool_calls and not capabilities.parallel_tool_calls:
        unmet.append("parallel_tool_calls")
    if request.output_schema is not None and not capabilities.structured_output:
        unmet.append("structured_output")
    if request.stream and not capabilities.streaming:
        unmet.append("streaming")
    if unmet:
        raise ModelError(
            CAPABILITY_UNSUPPORTED,
            f"Backend cannot honour: {', '.join(unmet)}. The request is refused rather "
            f"than sent with the constraint removed.")


@runtime_checkable
class ModelBackend(Protocol):
    """The only surface core layers may use to reach a language model."""

    name: str

    def capabilities(self) -> ModelCapabilities:
        ...

    def complete(self, request: ModelRequest) -> ModelResult:
        ...

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        """The same call as complete(), delivered incrementally.

        A backend that cannot stream must not fake it by chunking a finished answer:
        that would report a latency the user never experienced. negotiate() refuses the
        request instead, and the caller falls back to complete() knowingly.
        """
        ...

"""The only door between a model and anything that does work.

A model never receives a callable. It receives the specs this registry chooses to inject
and every invocation comes back here to be validated and dispatched, so an invented tool
name, a malformed argument or an unauthorised write is stopped on the server side rather
than in a prompt (ARCHITECTURE.md's Tool Registry row).

Four outcome states, never collapsed: `ok`, `partial`, `not_found` and `error` mean
different things to a buyer and to the runtime (invariant 3).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from contracts.llm import ToolCall, ToolSpec

logger = logging.getLogger("supplyagent.tools")

Effect = Literal["read", "write"]
Status = Literal["ok", "partial", "not_found", "error"]


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool actually produced, plus how far it got."""

    status: Status
    content: Any = None
    error_code: str | None = None
    message: str | None = None

    def for_model(self) -> str:
        """The string handed back as the tool message.

        The status travels with the payload on purpose: a model that sees only an empty
        list cannot tell "there are none" from "the lookup failed", and would report the
        second as the first.
        """
        return json.dumps(
            {"status": self.status, "content": self.content,
             "error_code": self.error_code, "message": self.message},
            ensure_ascii=False, default=str)


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    effect: Effect
    handler: Callable[[dict[str, Any], Any], ToolOutcome]
    #: A tool may be declared and still refuse to run — Q-03 keeps the procurement
    #: draft writers deferred until a target system and its idempotency are known.
    available: bool = True
    unavailable_reason: str = ""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def specs(self, *, effects: tuple[Effect, ...] = ("read",)) -> list[ToolSpec]:
        """Only what this scenario is allowed to use gets injected.

        Filtering here rather than in the prompt is the point: a tool the model was never
        shown is a tool it cannot be talked into calling.
        """
        return [tool.spec for tool in self._tools.values() if tool.effect in effects]

    def describe(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def dispatch(self, call: ToolCall, context: Any, *,
                 approved_write_ids: frozenset[str] = frozenset()) -> ToolOutcome:
        tool = self._tools.get(call.name)
        if tool is None:
            # not_found, not error: the tool does not exist, nothing failed.
            return ToolOutcome("not_found", error_code="unknown_tool",
                               message=f"No tool named {call.name!r}")
        if not tool.available:
            return ToolOutcome("error", error_code="tool_deferred",
                               message=tool.unavailable_reason or "Tool is not available yet")
        if tool.effect == "write" and call.id not in approved_write_ids:
            # Invariant 4. The refusal is server-side and unconditional; no prompt
            # wording can produce the approval, only a PermissionDecision can.
            return ToolOutcome("error", error_code="permission_required",
                               message="Write tools need an approved PermissionDecision")
        try:
            arguments = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return ToolOutcome("error", error_code="invalid_arguments",
                               message="Arguments were not valid JSON")
        if not isinstance(arguments, dict):
            return ToolOutcome("error", error_code="invalid_arguments",
                               message="Arguments must be a JSON object")
        missing = [key for key in tool.spec.parameters.get("required", [])
                   if key not in arguments]
        if missing:
            return ToolOutcome("error", error_code="invalid_arguments",
                               message=f"Missing required argument(s): {', '.join(missing)}")
        try:
            return tool.handler(arguments, context)
        except Exception as exc:
            logger.exception("Tool %s raised", call.name)
            return ToolOutcome("error", error_code="tool_failed", message=str(exc)[:200])

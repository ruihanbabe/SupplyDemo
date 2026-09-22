"""An offline stand-in for a model provider.

It exists so the whole chat path — streaming, tool dispatch, the UI — can be exercised
without a key, a network call or a cent of spend. It is NOT the ReplayBackend of F10:
there are no content-addressed fixtures and nothing here is replayable evidence. Every
answer it produces is labelled `stub` all the way to the browser, because
ARCHITECTURE.md invariant 10 requires real, simulated, cached and replayed data to stay
distinguishable at every layer.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from contracts.llm import ChatMessage, Completion, StreamEvent, ToolCall, ToolSpec, Usage

#: Small enough to see the stream arrive, large enough not to look like a stutter.
_CHUNK = 6


def _looks_like_a_project_question(text: str) -> bool:
    return bool(re.search(r"项目|project|bom|物料|清单", text, re.IGNORECASE))


class StubProvider:
    """Satisfies contracts.llm.ModelProvider without leaving the process."""

    name = "stub"

    def __init__(self, *, offer_tool_call: bool = True) -> None:
        self._offer_tool_call = offer_tool_call

    def _script(self, messages: list[ChatMessage], tools: list[ToolSpec] | None) -> Completion:
        last_user = next((m.content or "" for m in reversed(messages) if m.role == "user"), "")
        already_ran_a_tool = any(m.role == "tool" for m in messages)
        tool_names = {tool.name for tool in tools or ()}

        if (self._offer_tool_call and not already_ran_a_tool
                and "list_projects" in tool_names
                and _looks_like_a_project_question(last_user)):
            # Exercises the real dispatch path: the registry validates and runs this
            # exactly as it would a call from a live model.
            return Completion(
                content=None,
                tool_calls=(ToolCall(id="stub-1", name="list_projects", arguments="{}"),),
                model="stub", finish_reason="tool_calls", usage=Usage())

        if already_ran_a_tool:
            body = ("以上项目清单来自确定性查询，不是我生成的数值。"
                    "（这是离线 stub 回复，未调用任何真实模型。）")
        else:
            body = (f"收到：{last_user[:80]}\n"
                    "当前运行在离线 stub 模式，没有真实模型参与。"
                    "把 SUPPLYAGENT_LLM_ENABLED 设为 true 并配好 key 即可切换到 GLM。")
        return Completion(content=body, tool_calls=(), model="stub",
                          finish_reason="stop", usage=Usage(prompt_tokens=0, output_tokens=0))

    def complete(self, messages: list[ChatMessage], *, tools: list[ToolSpec] | None = None,
                 **_: object) -> Completion:
        return self._script(messages, tools)

    def stream(self, messages: list[ChatMessage], *, tools: list[ToolSpec] | None = None,
               **_: object) -> Iterator[StreamEvent]:
        completion = self._script(messages, tools)
        for index in range(0, len(completion.content or ""), _CHUNK):
            yield StreamEvent("text", text=(completion.content or "")[index:index + _CHUNK])
        yield StreamEvent("done", completion=completion)

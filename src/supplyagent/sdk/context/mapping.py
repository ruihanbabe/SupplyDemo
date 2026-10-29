"""Validate the two pinned Pi text/tool projections; never serialize Provider bodies."""

import json
import re

from supplyagent.sdk.context.compiler import text_content
from supplyagent.service.persistence.context_store import ContextError


def call_id(identifier, api):
    if api == "openai-responses":
        return re.sub(r"[^a-zA-Z0-9_-]", "_", identifier.split("|")[0])[:64].rstrip("_")
    if "|" in identifier:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", identifier.split("|")[0])[:40]
    return identifier


def fields(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ContextError("context_projection_changed")


def expected_messages(projection, api):
    result = []
    if projection["systemPrompt"]:
        result.append(("system", projection["systemPrompt"]))
    for message in projection["messages"]:
        role = message["role"]
        if role == "user":
            result.append(("user", text_content(message["content"])))
        elif role == "assistant":
            if api == "openai-completions":
                text = "".join(
                    b["text"]
                    for b in message["content"]
                    if b["type"] == "text" and b["text"].strip()
                )
                if text:
                    result.append(("assistant", text))
            for block in message["content"]:
                if block["type"] == "text" and api == "openai-responses":
                    result.append(("assistant", block["text"]))
                elif block["type"] == "toolCall":
                    identifier = call_id(block["id"], api)
                    result.append(("call", identifier, block["name"], block["arguments"]))
                elif block["type"] == "thinking" and block.get("thinkingSignature"):
                    if api == "openai-responses":
                        result.append(("reasoning", json.loads(block["thinkingSignature"])))
                    else:
                        raise ContextError("context_unsupported_content")
        elif role == "toolResult":
            identifier = call_id(message["toolCallId"], api)
            result.append(("result", identifier, text_content(message["content"])))
        else:
            raise ContextError("context_projection_changed")
    return result


def actual_messages(payload, api):
    result = []
    for message in payload["input" if api == "openai-responses" else "messages"]:
        kind = message.get("type")
        role = message.get("role")
        if kind == "function_call":
            fields(message, {"type", "id", "call_id", "name", "arguments"})
            result.append(
                ("call", message["call_id"], message["name"], json.loads(message["arguments"]))
            )
        elif kind == "function_call_output":
            fields(message, {"type", "call_id", "output"})
            result.append(("result", message["call_id"], message["output"]))
        elif kind == "reasoning":
            result.append(("reasoning", message))
        elif role == "tool":
            fields(message, {"role", "content", "tool_call_id"})
            result.append(("result", message["tool_call_id"], message["content"]))
        else:
            fields(
                message,
                {"type", "id", "role", "content", "status", "phase"}
                if api == "openai-responses"
                else {"role", "content", "tool_calls"},
            )
            if role not in {"system", "developer", "user", "assistant"}:
                raise ContextError("context_projection_changed")
            content = message.get("content")
            if isinstance(content, list):
                allowed = {"input_text", "output_text", "text"}
                if any(p.get("type") not in allowed for p in content):
                    raise ContextError("context_unsupported_content")
                for part in content:
                    fields(part, {"type", "text", "annotations"})
                    if part.get("annotations"):
                        raise ContextError("context_projection_changed")
                content = "\n".join(p["text"] for p in content)
            if content:
                result.append(("system" if role in {"system", "developer"} else role, content))
            for call in message.get("tool_calls", []):
                fields(call, {"id", "type", "function"})
                fields(call["function"], {"name", "arguments"})
                if call.get("type") != "function":
                    raise ContextError("context_projection_changed")
                result.append(
                    (
                        "call",
                        call["id"],
                        call["function"]["name"],
                        json.loads(call["function"]["arguments"]),
                    )
                )
    return result


def validate_projection(projection, payload, api):
    try:
        _validate_projection(projection, payload, api)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ContextError("context_projection_changed") from None


def _validate_projection(projection, payload, api):
    if api not in {"openai-responses", "openai-completions"}:
        raise ContextError("context_unsupported_content")
    messages = expected_messages(projection, api)
    identifiers = [m[1] for m in messages if m[0] == "call"]
    if len(set(identifiers)) != len(identifiers) or messages != actual_messages(payload, api):
        raise ContextError("context_projection_changed")
    expected = [
        (t.get("wireName", t["name"]), t["inputSchema"], t["description"])
        for t in projection["tools"]
    ]
    actual = []
    for item in payload.get("tools", []):
        fields(
            item,
            {"type", "function"}
            if api == "openai-completions"
            else {"type", "name", "description", "parameters", "strict"},
        )
        tool = item.get("function", item)
        if api == "openai-completions":
            fields(tool, {"name", "description", "parameters", "strict"})
        if tool.get("strict", False) is not False:
            raise ContextError("context_projection_changed")
        actual.append((tool["name"], tool["parameters"], tool["description"]))
    if expected != actual:
        raise ContextError("context_projection_changed")

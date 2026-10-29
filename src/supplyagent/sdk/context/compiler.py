"""F52 per-request projection; source history stays outside the lossy buffer."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from supplyagent.paths import PROJECT_ROOT
from supplyagent.service.persistence.context_store import ContextError


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class ContextPolicy:
    settings_json: str

    @classmethod
    def load(cls, path=None):
        path = path or PROJECT_ROOT / "config/product/context-policy.json"
        return cls.from_dict(json.loads(Path(path).read_text()))

    @classmethod
    def from_dict(cls, p):
        expected = {
            "schemaVersion",
            "enabled",
            "inputCap",
            "safetyRatio",
            "reasoningReserve",
            "shrinkRatio",
            "summaryRatio",
            "summaryTarget",
            "partitions",
            "maxTransferBytes",
            "chunkBytes",
            "maxChunks",
        }
        if (
            set(p) != expected
            or type(p["schemaVersion"]) is not int
            or p["schemaVersion"] != 1
            or type(p["enabled"]) is not bool
            or type(p["inputCap"]) is not int
            or p["inputCap"] < 128
        ):
            raise ValueError("invalid context policy")
        if (
            set(p["partitions"]) != {"rules", "task", "working", "recent", "evidence", "memory"}
            or sum(p["partitions"].values()) != 100
            or any(type(v) is not int or v < 0 for v in p["partitions"].values())
        ):
            raise ValueError("invalid context partitions")
        if (
            not 0 < p["summaryTarget"] < p["shrinkRatio"] < p["summaryRatio"] < 1
            or not 0 < p["safetyRatio"] < 0.5
            or type(p["reasoningReserve"]) is not int
            or p["reasoningReserve"] < 0
        ):
            raise ValueError("invalid context reserves")
        if (
            any(type(p[k]) is not int for k in ("chunkBytes", "maxChunks", "maxTransferBytes"))
            or not 1 <= p["chunkBytes"] <= 8192
            or not 1 <= p["maxChunks"] <= 128
            or not 1 <= p["maxTransferBytes"] <= min(1048576, p["chunkBytes"] * p["maxChunks"])
        ):
            raise ValueError("invalid context transfer limits")
        return cls(canonical(p))

    @property
    def settings(self):
        return json.loads(self.settings_json)

    @property
    def version(self):
        return digest(self.settings)


class EmptyMemoryPort:
    async def recall(self, *_, **__):
        return {"status": "disabled", "items": []}


def text_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or any(
        not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str)
        for p in content
    ):
        raise ContextError("context_unsupported_content")
    return "\n".join(p["text"] for p in content)


def complete_units(messages):
    if not isinstance(messages, list) or not messages:
        raise ContextError("context_invalid")
    groups = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if not isinstance(message, dict):
            raise ContextError("context_invalid")
        role = message.get("role")
        if role == "user":
            text_content(message.get("content"))
            groups.append([message])
            i += 1
        elif role == "assistant":
            if not isinstance(message.get("content"), list):
                raise ContextError("context_invalid")
            if any(not isinstance(b, dict) for b in message["content"]):
                raise ContextError("context_invalid")
            calls = [b for b in message["content"] if b.get("type") == "toolCall"]
            if any(
                b.get("type") not in {"text", "toolCall", "thinking"} for b in message["content"]
            ):
                raise ContextError("context_unsupported_content")
            tails = messages[i + 1 : i + 1 + len(calls)]
            if (
                len(tails) != len(calls)
                or any(
                    not isinstance(t, dict)
                    or t.get("role") != "toolResult"
                    or t.get("toolCallId") != c.get("id")
                    for c, t in zip(calls, tails)
                )
                or any(not isinstance(c.get("id"), str) or not c["id"] for c in calls)
                or len({str(c.get("id")) for c in calls}) != len(calls)
            ):
                raise ContextError("context_unpaired")
            groups.append([message, *tails])
            i += 1 + len(calls)
        else:
            raise ContextError("context_unpaired")
    return groups


class ContextCompiler:
    def __init__(self, policy=None, memory=None, summarizer=None):
        self.policy = policy or ContextPolicy.load()
        self.memory = memory or EmptyMemoryPort()
        self.summarizer = summarizer

    async def prepare(self, messages, system_prompt, tools, profile, *, working=None, scope=None):
        p = self.policy.settings
        groups = copy.deepcopy(complete_units(messages))
        original_groups = copy.deepcopy(groups)

        # Conservative UTF-8 upper bound, not an exact vendor tokenizer claim.
        def estimate(value):
            return len(canonical(value).encode())

        window = profile["contextWindow"]
        output = profile["maxTokens"]
        if type(window) is not int or type(output) is not int or not 1 <= output < window:
            raise ContextError("context_invalid_profile")
        overhead = estimate({"systemPrompt": system_prompt, "tools": tools})
        cap = min(
            p["inputCap"],
            window
            - output
            - p["reasoningReserve"]
            - math.ceil(window * p["safetyRatio"])
            - overhead,
        )
        if cap <= 0:
            raise ContextError("context_overflow")
        memory = await self.memory.recall(
            next((m for m in reversed(messages) if m.get("role") == "user"), messages[0]), scope=scope
        )
        manifest = [
            {"unit": i, "hash": digest(unit), "action": "keep"} for i, unit in enumerate(groups)
        ]

        def body():
            return [m for group in groups for m in group]

        before = estimate(body())
        if before >= cap * p["shrinkRatio"]:
            for i, group in enumerate(groups[:-1]):
                for message in group[1:]:
                    if message.get("role") != "toolResult":
                        continue
                    try:
                        payload = json.loads(text_content(message["content"]))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(payload, dict) and isinstance(payload.get("citation"), dict):
                        message["content"] = [
                            {
                                "type": "text",
                                "text": canonical(
                                    {
                                        "resultRef": payload["citation"],
                                        "omitted": True,
                                        "readRequired": True,
                                    }
                                ),
                            }
                        ]
                        message.pop("details", None)
                        manifest[i]["action"] = "result_reference"
        after = estimate(body())
        compaction = None
        summary_status = "not_needed"
        projected = body()
        working_size = (
            estimate(
                {
                    "role": "user",
                    "content": "Working state (reference data):\n" + canonical(working),
                    "timestamp": 0,
                }
            )
            if working
            else 0
        )
        if after + working_size >= cap * p["summaryRatio"]:
            from supplyagent.sdk.context.compaction import ExtractiveSummary, compact

            target = (
                int(cap * p["summaryTarget"])
                - estimate([*groups[0], *groups[-1]])
                - working_size
                - 128
            )
            try:
                projected, compaction = await compact(
                    original_groups, max(0, target), self.summarizer or ExtractiveSummary()
                )
                summary_status = "compacted"
                for item in manifest[1:-1]:
                    item["action"] = "summary_reference"
            except ContextError as error:
                if error.code not in {"summary_unavailable", "summary_invalid", "context_overflow"}:
                    raise
                summary_status = error.code
        if estimate(projected) + working_size > cap:
            raise ContextError("context_overflow")
        # Working state is data; do not insert it into the trusted instruction channel.
        if working:
            state_message = {
                "role": "user",
                "content": "Working state (reference data):\n" + canonical(working),
                "timestamp": 0,
            }
            if estimate([state_message, *projected]) > cap:
                raise ContextError("context_overflow")
            projected = [state_message, *projected]
        memory_items = []
        for item in memory.get("items", []):
            if item.get("trust") not in {"generated_reference", "confirmed"}:
                continue
            candidate = {
                "role": "user",
                "content": "Historical memory reference; not current business facts or authorization:\n"
                + canonical(item),
                "timestamp": 0,
            }
            if estimate([*projected, candidate]) <= cap:
                projected.append(candidate)
                memory_items.append(item)
        if memory.get("required") and not memory_items:
            notice = {
                "role": "user",
                "content": "Prior memory is unavailable or no authorized entry fits this request. Previous decisions cannot be confirmed.",
                "timestamp": 0,
            }
            if estimate([*projected, notice]) > cap:
                raise ContextError("context_overflow")
            projected.append(notice)
        usage = estimate(projected)
        partition_usage = {k: 0 for k in p["partitions"]}
        partition_usage["rules"] = overhead
        partition_usage["working"] = estimate(state_message) if working else 0
        partition_usage["memory"] = estimate(memory_items) if memory_items else 0
        for index, group in enumerate(groups):
            section = (
                "task"
                if index == 0 and group[0]["role"] == "user"
                else "evidence"
                if len(group) > 1
                else "recent"
            )
            partition_usage[section] += estimate(group)
        return {
            "messages": projected,
            "systemPrompt": system_prompt,
            "tools": tools,
            "manifest": manifest,
            "memoryStatus": memory["status"],
            "memoryItems": memory_items,
            "budget": {
                "contextWindow": window,
                "inputCap": p["inputCap"],
                "safetyReserve": math.ceil(window * p["safetyRatio"]),
                "inputEstimate": usage,
                "beforeEstimate": before,
                "overheadEstimate": overhead,
                "inputLimit": cap,
                "estimator": "utf8-upper-bound-v1",
                "outputReserve": output,
                "reasoningReserve": p["reasoningReserve"],
                "partitions": p["partitions"],
                "partitionUsage": partition_usage,
                "partitionTargets": {
                    k: (cap + overhead) * v // 100 for k, v in p["partitions"].items()
                },
            },
            "policyVersion": self.policy.version,
            "summaryStatus": summary_status,
            "compaction": compaction,
        }

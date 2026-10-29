"""Closed-prefix compaction and bounded reads; neither creates long-term memory."""

from __future__ import annotations

import json

from supplyagent.sdk.context.compiler import canonical, complete_units, digest, text_content
from supplyagent.service.persistence.context_store import ContextError


class ExtractiveSummary:
    """Keep original text, deduplicate exact paragraphs, and replace tool bodies with references."""

    async def summarize(self, units, target):
        paragraphs, seen, references = [], set(), []
        for group in units:
            for message in group:
                role = message["role"]
                if role == "toolResult":
                    payload = json.loads(text_content(message["content"]))
                    ref = (
                        payload.get("citation", payload.get("resultRef"))
                        if isinstance(payload, dict)
                        else None
                    )
                    if not ref:
                        raise ContextError("summary_unavailable")
                    references.append(ref)
                    # Unknowns and conditions are preserved verbatim, never inferred as facts.
                    extras = {
                        k: payload[k]
                        for k in ("unknowns", "conflicts", "asOf", "conditions")
                        if k in payload
                    }
                    text = canonical({"resultRef": ref, **extras})
                elif role == "assistant":
                    text = "\n".join(
                        b.get("text", b.get("thinking", ""))
                        for b in message["content"]
                        if b["type"] in {"text", "thinking"}
                    )
                else:
                    text = text_content(message["content"])
                for paragraph in text.split("\n"):
                    if paragraph and (role, paragraph) not in seen:
                        seen.add((role, paragraph))
                        paragraphs.append(f"[{role}] {paragraph}")
        text = "Closed history (verbatim reference; not current business facts):\n" + "\n".join(
            paragraphs
        )
        if len(text.encode()) > target:
            raise ContextError("summary_unavailable")
        return {"text": text, "references": references, "strategy": "verbatim-paragraphs-v1"}


async def compact(groups, target, summarizer):
    # Keep the task and newest complete unit verbatim; the middle is a closed prefix.
    if len(groups) < 3:
        raise ContextError("context_overflow")
    covered = groups[1:-1]
    try:
        summary = await summarizer.summarize(covered, target)
    except ContextError:
        raise
    except (OSError, TimeoutError, ValueError, TypeError):
        raise ContextError("summary_unavailable") from None
    if (
        not isinstance(summary, dict)
        or not isinstance(summary.get("text"), str)
        or not summary["text"]
        or len(summary["text"].encode()) > target
    ):
        raise ContextError("summary_invalid")
    # Only the built-in extractive implementation is accepted without a separate semantic evaluator.
    # Alternative summarizers must return the same source-grounded text, not unsupported assertions.
    grounded = await ExtractiveSummary().summarize(covered, target)
    if summary["text"] != grounded["text"] or summary.get("references") != grounded["references"]:
        raise ContextError("summary_invalid")
    message = {"role": "user", "content": summary["text"], "timestamp": 0}
    projection = [*groups[0], message, *groups[-1]]
    complete_units(projection)
    return projection, {
        **summary,
        "start": len(groups[0]) + 1,
        "end": sum(len(group) for group in groups[:-1]),
        "inputHash": digest(covered),
        "unitHashes": [digest(group) for group in covered],
    }


class ContextReader:
    """One invocation's read allowance, bound to an authorized conversation, never a file URI."""

    def __init__(self, store, conversation_id, *, byte_budget=32768, page_bytes=8192):
        if not 1 <= page_bytes <= byte_budget <= 1048576:
            raise ValueError("invalid context read budget")
        self.store, self.conversation_id = store, conversation_id
        self.remaining, self.page_bytes = byte_budget, page_bytes

    async def read(self, level, *, after=0, evidence=None):
        if level not in {"L0", "L1", "L2"}:
            raise ValueError("invalid context read level")
        if evidence is not None:
            payload = await self.store.read_evidence(self.conversation_id, evidence)
            entries = list(payload.items()) if isinstance(payload, dict) else [("value", payload)]
            if type(after) is not int or not 0 <= after <= len(entries):
                raise ValueError("invalid evidence page")
            if level == "L0":
                value = {
                    "ref": evidence,
                    "fields": [k for k, _ in entries],
                    "bytes": len(canonical(payload).encode()),
                }
            else:
                page = []
                for key, item in entries[after:]:
                    if len(canonical([*page, [key, item]]).encode()) > self.page_bytes - 512:
                        break
                    page.append([key, item])
                if not page and after < len(entries):
                    raise ContextError("context_read_fragment_too_large")
                value = {
                    "ref": evidence,
                    "fields": dict(page),
                    "next": after + len(page) if after + len(page) < len(entries) else None,
                }
        elif level == "L1":
            value = await self.store.read_compaction(self.conversation_id)
        else:
            rows = await self.store.read_range(self.conversation_id, after=after, limit=1)
            value = {
                "messages": [
                    {
                        "ordinal": r["ordinal"],
                        "role": r["role"],
                        "hash": r["content_hash"],
                        **({"payload": r["payload"]} if level == "L2" else {}),
                    }
                    for r in rows
                ],
                "next": rows[-1]["ordinal"] if rows else None,
            }
        size = len(canonical(value).encode())
        if size > self.page_bytes:
            raise ContextError("context_read_fragment_too_large")
        if size > self.remaining:
            raise ContextError("context_read_budget_exceeded")
        self.remaining -= size
        return {"value": value, "bytes": size, "estimate": size, "remaining": self.remaining}

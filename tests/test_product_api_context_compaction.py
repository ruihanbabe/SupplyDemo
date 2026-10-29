"""Closed source compaction preserves exact negation/time/unknown text."""

import asyncio
import copy

import pytest

from product_api.context_compaction import ContextReader
from product_api.context_runtime import ContextCompiler, ContextPolicy
from product_api.context_store import ContextError


def history():
    text = "2026-10-27：供应商尚未确认；不能承诺到货。仅当报价有效才复核。原因是样品未通过。"
    return [
        {"role": "user", "content": "Investigate"},
        *[{"role": "assistant", "content": [{"type": "text", "text": text}]} for _ in range(30)],
        {"role": "user", "content": "Latest question remains unknown"},
    ]


def test_compaction_keeps_current_task_and_tail_without_rewriting_protected_text():
    settings = ContextPolicy.load().settings
    settings["inputCap"] = 1800
    messages = history()
    original = copy.deepcopy(messages)
    value = asyncio.run(
        ContextCompiler(ContextPolicy.from_dict(settings)).prepare(
            messages, "rules", [], {"contextWindow": 32768, "maxTokens": 128}
        )
    )
    assert value["summaryStatus"] == "compacted"
    assert value["messages"][0] == messages[0] and value["messages"][-1] == messages[-1]
    assert messages[1]["content"][0]["text"] in value["messages"][1]["content"]
    assert value["compaction"]["start"] == 2 and value["compaction"]["end"] == 31
    assert messages == original


@pytest.mark.parametrize("invalid", [False, True])
def test_failed_or_untrusted_summary_does_not_drop_original_source(invalid):
    class Summary:
        async def summarize(self, units, target):
            if not invalid:
                raise TimeoutError
            return {"text": "到货已确认", "references": []}

    settings = ContextPolicy.load().settings
    settings["inputCap"] = 1800
    messages = history()
    with pytest.raises(ContextError, match="context_overflow"):
        asyncio.run(
            ContextCompiler(ContextPolicy.from_dict(settings), summarizer=Summary()).prepare(
                messages, "rules", [], {"contextWindow": 32768, "maxTokens": 128}
            )
        )
    assert len(messages) == 32


def test_no_safe_reduction_is_explicit_overflow_not_head_truncation():
    messages = [{"role": "user", "content": "大" * 2000}]
    with pytest.raises(ContextError, match="context_overflow"):
        asyncio.run(
            ContextCompiler().prepare(
                messages, "rules", [], {"contextWindow": 2000, "maxTokens": 128}
            )
        )
    assert len(messages[0]["content"]) == 2000


def test_reader_is_bounded_and_counts_repeated_authorized_reads():
    class Store:
        async def read_range(self, *args, **kwargs):
            return [
                {
                    "ordinal": 1,
                    "role": "user",
                    "content_hash": "a" * 64,
                    "payload": {"text": "not confirmed"},
                }
            ]

    async def scenario():
        reader = ContextReader(Store(), "conv", byte_budget=400, page_bytes=200)
        first = await reader.read("L0")
        assert "payload" not in first["value"]["messages"][0]
        second = await reader.read("L2")
        assert second["remaining"] < first["remaining"]
        with pytest.raises(ContextError, match="context_read_budget_exceeded"):
            await reader.read("L2")

    asyncio.run(scenario())

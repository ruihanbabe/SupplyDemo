"""Worker envelope, agent tool loop, and the first service worker (sourcing)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import ClassVar

import pytest

from contracts import errors
from contracts.llm import (
    ChatMessage,
    ModelCapabilities,
    ModelError,
    ModelResult,
    ToolCall,
    ToolSpec,
    Usage,
)
from contracts.worker import Budget, WorkerTask
from infrastructure.agent_config import AgentConfig, load_worker_permissions
from permissions.policy import load_policy
from tools.registry import Invocation, RegisteredTool, ToolOutcome, ToolRegistry
from workers.agent import AgentWorker
from workers.base import Outcome, ServiceWorker, Worker, WorkerFailure
from workers.sourcing.worker import OfferComparison, SourcingWorker


@dataclass(frozen=True)
class Verdict:
    label: str


# ---------- 信封 ----------

class Scripted(Worker):
    name = "probe"
    kind = "service"
    purposes = frozenset({"check"})
    content_type = Verdict

    def __init__(self, action):
        self.action = action

    def execute(self, task, *, deadline):
        return self.action(task, deadline)


def task(worker="probe", purpose="check", **kwargs) -> WorkerTask:
    return WorkerTask.create(worker, purpose, **kwargs)


def test_a_task_for_another_worker_is_refused():
    result = Scripted(lambda t, d: Outcome("ok", Verdict("x"))).run(task(worker="other"))
    assert (result.status, result.error_code) == ("error", errors.WRONG_WORKER)


def test_an_undeclared_purpose_is_refused():
    result = Scripted(lambda t, d: Outcome("ok", Verdict("x"))).run(task(purpose="nope"))
    assert result.error_code == errors.UNKNOWN_PURPOSE


@pytest.mark.parametrize("budget", [Budget(max_seconds=0), Budget(max_tokens=0),
                                    Budget(max_external_calls=0)])
def test_an_empty_budget_never_starts(budget):
    started = []
    worker = Scripted(lambda t, d: started.append(1) or Outcome("ok", Verdict("x")))
    assert worker.run(task(budget=budget)).error_code == errors.BUDGET_EXHAUSTED
    assert not started


def test_a_deadline_is_handed_down_on_the_monotonic_clock():
    seen = {}
    Scripted(lambda t, d: seen.update(d=d) or Outcome("ok", Verdict("x"))).run(
        task(budget=Budget(max_seconds=5)))
    assert 4 < seen["d"] - time.monotonic() <= 5


def test_a_crash_becomes_an_error_that_names_only_the_type():
    def boom(t, d):
        raise RuntimeError("https://api.example/?key=secret")
    result = Scripted(boom).run(task())
    assert (result.status, result.error_code) == ("error", errors.WORKER_CRASHED)
    assert result.message == "RuntimeError"


def test_a_named_failure_keeps_what_it_spent():
    def fail(t, d):
        raise WorkerFailure("timeout", usage=_usage(prompt=40))
    result = Scripted(fail).run(task())
    assert (result.error_code, result.usage.prompt_tokens) == ("timeout", 40)


def test_usable_content_must_have_the_declared_type():
    result = Scripted(lambda t, d: Outcome("ok", "a paragraph")).run(task())
    assert result.error_code == errors.CONTENT_TYPE_MISMATCH


def test_not_found_needs_no_content():
    assert Scripted(lambda t, d: Outcome("not_found")).run(task()).status == "not_found"


def test_an_error_never_carries_content_and_refs_are_deduplicated():
    error = Scripted(lambda t, d: Outcome("error", Verdict("x"))).run(task())
    assert error.content is None
    ok = Scripted(lambda t, d: Outcome("ok", Verdict("x"), evidence_refs=("b", "a", "b")))
    assert ok.run(task()).evidence_refs == ("b", "a")


def _usage(prompt=0, output=0):
    from contracts.worker import Usage as WorkerUsage
    return WorkerUsage(prompt_tokens=prompt, output_tokens=output)


# ---------- Agent 工具循环 ----------

class FakeBackend:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def capabilities(self):
        return ModelCapabilities(native_tool_calling=True, structured_output=True,
                                 usage_reporting=True)

    def complete(self, request):
        self.requests.append(request)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def reply(*, tools=(), structured=None, prompt=10, output=5):
    return ModelResult(content=None, tool_calls=tuple(tools), structured_output=structured,
                       usage=Usage(prompt_tokens=prompt, output_tokens=output))


def lookup(call_id="c1"):
    return ToolCall(id=call_id, name="lookup", arguments="{}")


def config(worker="judge"):
    return AgentConfig(worker=worker, provider="fake", model="m", temperature=0.0,
                       max_tokens=100, timeout_seconds=30)


def tool_registry(handler=None):
    registry = ToolRegistry()
    registry.register(RegisteredTool(
        spec=ToolSpec("lookup", "probe", {"type": "object", "properties": {}}),
        effect="read",
        handler=handler or (lambda a, c: ToolOutcome("ok", content={"n": 1}))))
    return registry


class Judge(AgentWorker):
    name = "judge"
    purposes = frozenset({"check"})
    content_type = Verdict
    output_schema: ClassVar[dict] = {"type": "object"}
    max_tool_rounds = 2

    def brief(self, task):
        return [ChatMessage(role="user", content="judge")]

    def finalize(self, task):
        return ChatMessage(role="user", content="answer")

    def conclude(self, task, answer, observations):
        self.observations = observations
        return Outcome("ok", Verdict(answer["label"]))


def judge(backend, registry=None):
    return Judge(backend, registry or tool_registry(), config(),
                 lambda t: Invocation(worker="judge"))


def test_explore_then_finalize_with_no_tools_offered_at_the_end():
    backend = FakeBackend(reply(tools=[lookup()]), reply(), reply(structured={"label": "ok"}))
    worker = judge(backend)
    result = worker.run(task(worker="judge"))
    assert (result.status, result.content) == ("ok", Verdict("ok"))
    assert [bool(r.tools) for r in backend.requests] == [True, True, False]
    assert backend.requests[-1].output_schema == {"type": "object"}
    assert backend.requests[1].messages[-1].role == "tool"
    assert len(worker.observations) == 1


def test_the_round_cap_holds_because_the_last_call_has_no_tools():
    backend = FakeBackend(reply(tools=[lookup("a")]), reply(tools=[lookup("b")]),
                          reply(structured={"label": "capped"}))
    result = judge(backend).run(task(worker="judge"))
    assert result.content == Verdict("capped")
    assert len(backend.requests) == 3


def test_usage_is_summed_across_every_call():
    backend = FakeBackend(reply(), reply(structured={"label": "x"}, prompt=7, output=3))
    usage = judge(backend).run(task(worker="judge")).usage
    assert (usage.prompt_tokens, usage.output_tokens) == (17, 8)


def test_no_structured_answer_is_malformed():
    backend = FakeBackend(reply(), reply())
    assert judge(backend).run(task(worker="judge")).error_code == errors.MALFORMED_RESPONSE


def test_a_model_error_keeps_its_code_and_what_was_spent():
    backend = FakeBackend(reply(), ModelError(errors.RATE_LIMITED, "slow down"))
    result = judge(backend).run(task(worker="judge"))
    assert (result.error_code, result.usage.prompt_tokens) == (errors.RATE_LIMITED, 10)


def test_a_passed_deadline_stops_the_loop_between_rounds():
    def slow(a, c):
        time.sleep(0.06)
        return ToolOutcome("ok", content={})
    backend = FakeBackend(reply(tools=[lookup()]), reply(structured={"label": "x"}))
    result = judge(backend, tool_registry(slow)).run(
        task(worker="judge", budget=Budget(max_seconds=0.05)))
    assert result.error_code == errors.TIMEOUT
    assert len(backend.requests) == 1


def test_the_model_call_timeout_never_exceeds_what_is_left():
    backend = FakeBackend(reply(), reply(structured={"label": "x"}))
    judge(backend).run(task(worker="judge", budget=Budget(max_seconds=2)))
    assert all(r.timeout_seconds <= 2 for r in backend.requests)


def test_a_config_for_another_worker_is_rejected():
    with pytest.raises(ValueError):
        Judge(FakeBackend(), tool_registry(), config("other"), lambda t: Invocation())


# ---------- sourcing ----------

def comparison(status="ok", rows=None, **extra) -> ToolOutcome:
    rows = rows if rows is not None else [
        {"provider": "digikey", "status": "ok", "evidence_ids": ["e1", "e2"],
         "match_status": "exact", "provenance": "real"},
        {"provider": "mouser", "status": "ok", "evidence_ids": ["e3"],
         "match_status": "exact", "provenance": "real"},
        {"provider": "element14", "status": "error", "error_code": "timeout",
         "provenance": "unknown"}]
    content = {"mpn": "IRFZ44NPBF", "rows": rows, "stock_disagreement": True,
               "prices_comparable": False, "currencies": ["CNY", "USD"]}
    return ToolOutcome(status, content=content, **extra)


def sourcing(outcome: ToolOutcome, *, worker="sourcing"):
    registry = ToolRegistry(permission_check=lambda tool, inv: (
        None if tool.permission in load_policy(inv.worker).worker_permissions
        else "denied"))
    calls = []

    def handler(arguments, _context):
        calls.append(arguments)
        return outcome
    registry.register(RegisteredTool(
        spec=ToolSpec("compare_supplier_offers", "probe",
                      {"type": "object", "properties": {"mpn": {"type": "string"}},
                       "required": ["mpn"]}),
        effect="read", handler=handler, permission="sourcing.read"))
    return SourcingWorker(registry, lambda t: Invocation(worker=worker)), calls


def sourcing_task(**inputs):
    return task(worker="sourcing", purpose="compare_offers", inputs=inputs)


def test_every_source_is_kept_apart_with_its_own_evidence():
    worker, calls = sourcing(comparison("partial", message="1 家未给出完整结果"))
    result = worker.run(sourcing_task(mpn="IRFZ44NPBF"))
    assert calls == [{"mpn": "IRFZ44NPBF"}]
    assert result.status == "partial"
    assert isinstance(result.content, OfferComparison)
    assert [a.provider for a in result.content.answers] == ["digikey", "mouser", "element14"]
    assert result.content.answers[2].error_code == "timeout"
    assert result.evidence_refs == ("e1", "e2", "e3")
    assert result.content.prices_comparable is False
    assert result.narrative == "1 家未给出完整结果"


def test_a_failed_comparison_stays_an_error():
    worker, _ = sourcing(ToolOutcome("error", error_code="all_sources_failed"))
    result = worker.run(sourcing_task(mpn="X"))
    assert (result.status, result.error_code, result.content) == \
        ("error", "all_sources_failed", None)


def test_a_missing_mpn_never_reaches_the_tool():
    worker, calls = sourcing(comparison())
    assert worker.run(sourcing_task()).error_code == errors.BAD_REQUEST
    assert not calls


def test_sourcing_holds_only_the_permission_its_file_declares():
    assert load_worker_permissions("sourcing") == frozenset({"sourcing.read"})
    assert load_worker_permissions("action") == frozenset()


def test_the_registry_refuses_a_service_worker_without_the_grant():
    worker, calls = sourcing(comparison(), worker="action")
    # The envelope checks the name first; bypass it to test the registry gate alone.
    outcome = ServiceWorker.call_tool(worker, sourcing_task(mpn="X"),
                                      "compare_supplier_offers", {"mpn": "X"})
    assert (outcome.status, outcome.error_code) == ("error", "permission_required")
    assert not calls

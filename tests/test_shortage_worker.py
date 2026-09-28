"""The shortage worker: offline mapping against a stub tool, and the real chain end to end
on the PCB-Stimulator slice with simulated supply."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from contracts import errors
from contracts.llm import ToolSpec
from contracts.worker import WorkerTask
from persistence.import_normalized import import_normalized
from persistence.procurement import ProcurementRepository
from persistence.seed_business_rules import apply_rules
from persistence.seed_simulated_supply import seed
from tools.catalog_tools import ToolContext, build_registry
from tools.registry import Invocation, RegisteredTool, ToolOutcome, ToolRegistry
from workers.shortage.worker import ShortageReport, ShortageWorker


def task(**inputs) -> WorkerTask:
    return WorkerTask.create("shortage", "compute_shortage", inputs=inputs)


def body(rows=(), unresolved=(), shortage_count=None):
    return {"project_id": "P", "production_qty": "50", "need_by_date": "2026-11-27",
            "run_id": "00000000-0000-4000-8000-000000000001", "components_checked": 12,
            "rows": list(rows), "unresolved": list(unresolved),
            "shortage_count": len(rows) if shortage_count is None else shortage_count}


ROW = {"component_id": "part_a", "mpn": "XHP-2", "required_qty": "400",
       "allocatable_qty": "300", "shortage_qty": "100", "why": "库存被其他需求占用",
       "reason_codes": ["resource_competition"]}


def stub(outcome: ToolOutcome):
    calls = []
    registry = ToolRegistry()
    registry.register(RegisteredTool(
        spec=ToolSpec("compute_shortage", "stub", {"type": "object", "properties": {}}),
        effect="read", handler=lambda a, c: calls.append(a) or outcome))
    return ShortageWorker(registry, lambda t: Invocation(worker="shortage")), calls


def test_rows_become_typed_quantities_with_reason_codes():
    worker, calls = stub(ToolOutcome("ok", content=body([ROW]), provenance="sample"))
    result = worker.run(task(project_id="P", production_qty=50))
    assert calls == [{"project_id": "P", "production_qty": "50"}]
    report = result.content
    assert isinstance(report, ShortageReport)
    assert report.short[0].shortage_qty == Decimal(100)
    assert report.short[0].reason_codes == ("resource_competition",)
    assert report.provenance == "sample"
    assert report.truncated is False


def test_unresolved_lines_keep_the_result_partial():
    worker, _ = stub(ToolOutcome("partial", content=body(
        [ROW], unresolved=[{"line_id": "L16", "reason": "candidate_selection_required"}]),
        message="1 行未能展开"))
    result = worker.run(task(project_id="P", production_qty="50"))
    assert result.status == "partial"
    assert result.content.unresolved[0].reason == "candidate_selection_required"


def test_a_capped_row_list_is_marked_truncated():
    worker, _ = stub(ToolOutcome("partial", content=body([ROW], shortage_count=14)))
    assert worker.run(task(project_id="P", production_qty=50)).content.truncated is True


@pytest.mark.parametrize("inputs", [{}, {"project_id": "P"},
                                    {"project_id": "P", "production_qty": "abc"},
                                    {"project_id": "P", "production_qty": 0},
                                    {"project_id": "P", "production_qty": "NaN"}])
def test_bad_inputs_never_reach_the_tool(inputs):
    worker, calls = stub(ToolOutcome("ok", content=body()))
    assert worker.run(task(**inputs)).error_code == errors.BAD_REQUEST
    assert not calls


def test_a_failed_expansion_stays_an_error():
    worker, _ = stub(ToolOutcome("error", error_code="expansion_blocked"))
    result = worker.run(task(project_id="P", production_qty=50))
    assert (result.status, result.error_code, result.content) == \
        ("error", "expansion_blocked", None)


@pytest.mark.integration
def test_the_slice_computes_the_pinned_shortages_through_the_real_registry(migrated):
    conn, _, _ = migrated
    import_normalized(conn)
    apply_rules(conn)
    # The whole test is one transaction, so now() is its start and a rule stamped by
    # Python a moment later would not be in force yet. Only the fixture is backdated.
    conn.execute(text("UPDATE business_rule SET effective_from = now() - interval '1 minute'"))
    seed(conn)
    registry = build_registry()
    worker = ShortageWorker(registry, lambda t: Invocation(
        data=ToolContext(repository=ProcurementRepository(conn)),
        worker="shortage", scenario="procurement"))

    result = worker.run(task(project_id="PCB-Stimulator", production_qty=50))

    assert result.status == "partial", (result.error_code, result.message)
    by_mpn = {row.mpn: row for row in result.content.short}
    assert {mpn: row.shortage_qty for mpn, row in by_mpn.items()} == {
        "B2B-XH-A(LF)(SN)": Decimal(150), "SSW-116-02-G-S": Decimal(200),
        "SSH-LX5091": Decimal(200), "5219210F": Decimal(150), "XHP-2": Decimal(100)}
    assert "after_need_by_date" in by_mpn["SSW-116-02-G-S"].reason_codes
    assert "unconfirmed" in by_mpn["SSH-LX5091"].reason_codes
    assert "eta_unknown" in by_mpn["5219210F"].reason_codes
    assert "resource_competition" in by_mpn["XHP-2"].reason_codes
    reasons = {line.line_id: line.reason for line in result.content.unresolved}
    assert reasons["PCB-Stimulator-016"] == "candidate_selection_required"
    assert result.content.provenance == "sample"

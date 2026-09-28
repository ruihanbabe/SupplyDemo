"""The internal worker: alternates' stock, offline against stub tools and end to end on
the PCB-Stimulator slice."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from contracts import errors
from contracts.llm import ToolSpec
from contracts.worker import WorkerTask
from permissions.explain import explain
from persistence.import_normalized import import_normalized
from persistence.procurement import ProcurementRepository
from persistence.seed_business_rules import apply_rules
from persistence.seed_simulated_supply import seed
from tools.catalog_tools import ToolContext, build_registry
from tools.registry import Invocation, RegisteredTool, ToolOutcome, ToolRegistry
from workers.internal.worker import AlternateAvailability, InternalWorker

INPUTS = {"project_id": "P", "component_id": "part_a", "need_by_date": "2026-11-27"}


def task(**inputs) -> WorkerTask:
    return WorkerTask.create("internal", "alternate_availability", inputs=inputs)


def lines(*alternates):
    return ToolOutcome("ok", content={"lines": [{"line_id": "L1", "alternates": [
        {"component_id": cid, "mpn": cid.upper(), "manufacturer": "M"} for cid in alternates]}]})


def stock(available="500", excluded=()):
    return ToolOutcome("ok", provenance="sample", content={
        "available_qty": available, "on_hand_qty": available,
        "excluded_transit": [{"qty": "1", "reason": reason} for reason in excluded]})


def stub(line_outcome, stock_outcomes):
    calls = []
    registry = ToolRegistry()
    registry.register(RegisteredTool(
        spec=ToolSpec("get_line_alternates", "stub", {"type": "object", "properties": {}}),
        effect="read", handler=lambda a, c: calls.append(("lines", a)) or line_outcome))
    registry.register(RegisteredTool(
        spec=ToolSpec("get_material_availability", "stub",
                      {"type": "object", "properties": {}}),
        effect="read",
        handler=lambda a, c: calls.append(("stock", a)) or stock_outcomes[a["component_id"]]))
    return InternalWorker(registry, lambda t: Invocation(worker="internal")), calls


def test_every_alternate_is_reported_with_its_own_availability():
    worker, calls = stub(lines("part_b", "part_c"),
                         {"part_b": stock("500"), "part_c": stock("0", ["eta_unknown"])})
    result = worker.run(task(**INPUTS))
    assert result.status == "ok"
    content = result.content
    assert isinstance(content, AlternateAvailability)
    assert [a.available_qty for a in content.alternates] == [Decimal(500), Decimal(0)]
    assert content.alternates[1].excluded_reasons == ("eta_unknown",)
    assert content.provenance == "sample"
    assert [name for name, _ in calls] == ["lines", "stock", "stock"]


def test_a_single_candidate_line_is_not_found_not_zero():
    worker, calls = stub(lines(), {})
    result = worker.run(task(**INPUTS))
    assert (result.status, result.error_code) == ("not_found", "no_alternates")
    assert [name for name, _ in calls] == ["lines"]


def test_an_alternate_without_a_snapshot_makes_the_result_partial():
    worker, _ = stub(lines("part_b", "part_c"), {
        "part_b": stock("500"),
        "part_c": ToolOutcome("not_found", error_code="inventory_snapshot_missing")})
    result = worker.run(task(**INPUTS))
    assert result.status == "partial"
    missing = result.content.alternates[1]
    assert (missing.status, missing.available_qty) == ("not_found", None)


def test_all_lookups_failing_is_an_error_and_all_missing_is_not_found():
    failing, _ = stub(lines("part_b"), {"part_b": ToolOutcome("error", error_code="db")})
    assert failing.run(task(**INPUTS)).status == "error"
    missing, _ = stub(lines("part_b"), {"part_b": ToolOutcome("not_found")})
    assert missing.run(task(**INPUTS)).status == "not_found"


@pytest.mark.parametrize("drop", ["project_id", "component_id", "need_by_date"])
def test_missing_inputs_never_reach_a_tool(drop):
    worker, calls = stub(lines("part_b"), {"part_b": stock()})
    inputs = {k: v for k, v in INPUTS.items() if k != drop}
    assert worker.run(task(**inputs)).error_code == errors.BAD_REQUEST
    assert not calls


def test_only_internal_may_read_internal_stock():
    """T04: the same tool, allowed for one worker and denied for another."""
    allowed = {row["tool"]: row for row in explain("internal")}
    denied = {row["tool"]: row for row in explain("spec_check")}
    assert allowed["get_material_availability"]["allowed"] is True
    assert denied["get_material_availability"]["allowed"] is False
    assert denied["get_material_availability"]["layer"] == "worker"


@pytest.fixture
def slice_db(migrated):
    conn, _, _ = migrated
    import_normalized(conn)
    apply_rules(conn)
    conn.execute(text("UPDATE business_rule SET effective_from = now() - interval '1 minute'"))
    seed(conn)
    registry = build_registry()
    return InternalWorker(registry, lambda t: Invocation(
        data=ToolContext(repository=ProcurementRepository(conn)),
        worker="internal", scenario="procurement"))


@pytest.mark.integration
def test_r2_reports_the_viking_alternate_from_the_real_database(slice_db):
    # RS Pro 707-7647 has no stock; the seed gives the Viking Tech candidate 500.
    result = slice_db.run(task(project_id="PCB-Stimulator",
                               component_id="part_d43c367a402aa887",
                               need_by_date="2026-11-27"))
    assert result.status == "ok", (result.error_code, result.message)
    by_mpn = {a.mpn: a for a in result.content.alternates}
    assert by_mpn["RS-CARBON-470R-5%-0.25W"].available_qty == Decimal(500)
    # The same RS part written without its dash is a separate candidate in the source data.
    assert "7077647" in by_mpn
    assert result.content.provenance == "sample"


@pytest.mark.integration
def test_a_single_candidate_shortage_has_no_alternates_in_the_real_bom(slice_db):
    result = slice_db.run(task(project_id="PCB-Stimulator",
                               component_id="part_fafb3ac786767f64",
                               need_by_date="2026-11-27"))
    assert (result.status, result.error_code) == ("not_found", "no_alternates")

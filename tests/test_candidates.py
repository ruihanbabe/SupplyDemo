"""F05 EV-03/04/05/07: explicit selection and honest identity uncertainty."""
from collections import Counter
from decimal import Decimal

import pytest

from persistence.import_normalized import read_normalized
from procurement_core.demand import confirmation_requests, expand_lines


def row():
    return {"line_id": "line", "quantity": Decimal(2), "qty_basis_verified": True,
            "candidates": [{"component_id": "a", "manufacturer": "Murata",
                            "mpn": "GCM1555C1H102JA16J", "identity_status": "source_asserted"},
                           {"component_id": "b", "manufacturer": "test",
                            "mpn": "IRFZ44NPbF", "identity_status": "review_required"}]}


def test_ev07_multiple_candidates_do_not_multiply_requirement():
    source = row()
    lines = expand_lines(Decimal(100), [source], {})
    assert len(lines) == 1
    assert lines[0]["required_qty"] == 200
    assert lines[0]["component_id"] is None
    requests = confirmation_requests([source], lines)
    assert requests[0]["reason"] == "candidate_selection_required"
    assert requests[0]["candidates"] == source["candidates"]


@pytest.mark.parametrize("chosen", ["a", "b"])
def test_ev03_ev04_selection_does_not_imply_identity_approval(chosen):
    source = row()
    lines = expand_lines(Decimal(1), [source], {"line": chosen})
    assert lines[0]["unresolved_reason"] == "candidate_identity_unverified"
    assert confirmation_requests([source], lines)[0]["candidates"][1]["mpn"] == "IRFZ44NPbF"
    assert source["candidates"][0]["manufacturer"] == "Murata"


def test_one_explicit_verified_candidate_satisfies_line():
    source = row()
    source["candidates"][1]["identity_status"] = "verified"
    lines = expand_lines(Decimal(10), [source], {"line": "b"})
    assert lines[0] == {"line_id": "line", "component_id": "b", "required_qty": Decimal(20),
                        "unresolved_reason": None}
    assert confirmation_requests([source], lines) == []


def test_real_normalized_multicandidate_and_missing_lines_retained():
    data = read_normalized()
    counts = Counter(c["line_id"] for c in data["bom_line_candidate"])
    assert sum(n > 1 for n in counts.values()) == 20
    rows = [{**r, "qty_basis_verified": True,
             "candidates": [{**c, "identity_status": "source_asserted"}
                            for c in data["bom_line_candidate"] if c["line_id"] == r["line_id"]]}
            for r in data["bom_line"]]
    results = expand_lines(Decimal(1), rows, {})
    assert len(results) == 121
    assert sum(r["unresolved_reason"] == "missing_candidate" for r in results) == 3
    assert all(r["component_id"] is None for r in results)
    assert len(confirmation_requests(rows, results)) == 121

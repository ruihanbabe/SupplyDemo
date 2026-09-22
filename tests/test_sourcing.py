"""F19/F30: MCP over stdio, a filtered child environment, and divergence kept intact."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tools.mcp_client import SUPPLIER_SERVER, MCPClient, MCPError, child_environment
from tools.sourcing_tools import _digest, _query_provider, _unit_price_at_moq

# ---------- 子进程环境：白名单而非继承 ----------

def test_a_child_inherits_only_what_was_declared():
    """Inheriting os.environ would hand a supplier integration every credential the
    operator happened to export. Absence is the failure nobody notices, so the list is
    explicit."""
    parent = {"PATH": "/usr/bin", "SUPPLYAGENT_LLM_ZHIPU_API_KEY": "secret",
              "AWS_SECRET_ACCESS_KEY": "also-secret", "HOME": "/root"}
    passed = child_environment(source=parent)
    assert passed == {"PATH": "/usr/bin"}
    assert "secret" not in "".join(passed.values())


def test_a_server_can_be_given_the_one_variable_it_needs():
    parent = {"PATH": "/usr/bin", "SUPPLIER_API_KEY": "k", "OTHER_KEY": "no"}
    passed = child_environment(("SUPPLIER_API_KEY",), source=parent)
    assert set(passed) == {"PATH", "SUPPLIER_API_KEY"}


# ---------- 协议 ----------

def test_handshake_and_tool_discovery():
    with MCPClient(SUPPLIER_SERVER) as client:
        names = {tool["name"] for tool in client.list_tools()}
    assert names == {"search_supplier_parts", "get_supplier_offer"}


def test_an_unknown_tool_is_a_protocol_error_not_a_result():
    with MCPClient(SUPPLIER_SERVER) as client, pytest.raises(MCPError) as caught:
        client.call_tool("no_such_tool", {})
    assert caught.value.code == "server_error"


def test_a_hanging_server_does_not_hang_the_caller():
    """readline() on a pipe has no timeout of its own; without this the whole turn would
    block on one slow supplier."""
    impatient = replace(SUPPLIER_SERVER, call_timeout=0.5)
    with MCPClient(impatient) as client, pytest.raises(MCPError) as caught:
        # farnell's IRFZ44 fixture sleeps far longer than the budget.
        client.call_tool("get_supplier_offer",
                         {"provider": "farnell", "distributor_sku": "1544411"})
    assert caught.value.code == "timeout"


# ---------- 型号匹配不自动采纳 ----------

def test_case_only_difference_is_not_reported_as_exact():
    """The BOM says IRFZ44NPbF and the distributor says IRFZ44NPBF. Whether those are the
    same orderable part is a question for a person (EV-04)."""
    row = _query_provider("digikey", "IRFZ44NPbF")
    assert row["status"] == "ok"
    assert row["match_status"] == "suffix_differs"


def test_one_sku_covering_two_part_numbers_is_ambiguous():
    """DigiKey's RLB9012-330KL-ND is mapped to both RLB9012-330KL and RLB0914-330KL in
    the source data. Picking one would invent an identity nobody asserted."""
    row = _query_provider("digikey", "RLB0914-330KL")
    assert row["match_status"] == "ambiguous"


def test_an_exact_match_says_so():
    assert _query_provider("digikey", "IRFZ44NPBF")["match_status"] == "exact"


# ---------- 四态不合并 ----------

def test_a_catalogue_miss_is_not_found_not_error():
    row = _query_provider("mouser", "TC4420CPA")
    assert row["status"] == "not_found"


def test_a_timeout_is_an_error_not_a_miss():
    """"The supplier has none" and "the supplier did not answer" lead to different
    purchasing decisions (BR-05)."""
    row = _query_provider("farnell", "IRFZ44NPbF",
                          replace(SUPPLIER_SERVER, call_timeout=0.3))
    assert row["status"] == "error"
    assert row["error_code"] == "timeout"


def test_stock_without_price_is_partial():
    row = _query_provider("farnell", "TC4420CPA")
    assert row["status"] == "partial"
    assert row["stock_qty"] == 300
    # Unknown stays unknown: a zero price would be read as free.
    assert row["unit_price_at_moq"] is None
    assert row["lead_time_days"] is None


# ---------- 分歧保留 ----------

def test_three_sources_disagreeing_on_stock_all_survive():
    rows = [_query_provider(provider, "RLB0914-330KL")
            for provider in ("digikey", "mouser", "farnell")]
    stocks = {row["provider"]: row["stock_qty"] for row in rows}
    assert stocks == {"digikey": 900, "mouser": 150, "farnell": 0}


# ---------- 定位与取价 ----------

def test_the_request_digest_is_stable_and_carries_no_credentials():
    first = _digest("digikey", "get_supplier_offer", {"distributor_sku": "X"})
    again = _digest("digikey", "get_supplier_offer", {"distributor_sku": "X"})
    other = _digest("mouser", "get_supplier_offer", {"distributor_sku": "X"})
    assert first == again != other
    assert first.startswith("sha256:")


def test_the_quoted_price_is_a_published_tier_not_an_interpolation():
    """Picking a tier keeps this an observation. Interpolating would make it arithmetic,
    and arithmetic belongs to the procurement core, where it can be checked."""
    offer = {"moq": 100, "price_breaks": [{"min_qty": 1, "unit_price": "1.09"},
                                          {"min_qty": 100, "unit_price": "0.87"},
                                          {"min_qty": 500, "unit_price": "0.74"}]}
    assert _unit_price_at_moq(offer) == "0.87"


def test_no_price_breaks_means_no_price():
    assert _unit_price_at_moq({"moq": 1, "price_breaks": []}) is None

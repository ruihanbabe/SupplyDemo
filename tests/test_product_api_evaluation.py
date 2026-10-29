"""The evaluation gate must reject hollow green and retain case identity."""

import pytest

from product_api.evaluation import load_cases, verdict


@pytest.mark.parametrize("xml,code,status", [
    ('<testsuite><testcase name="real"/></testsuite>', 0, "passed"),
    ('<testsuite><testcase><failure/></testcase></testsuite>', 0, "failed"),
    ('<testsuite><testcase><skipped/></testcase></testsuite>', 0, "skipped"),
    ('<testsuite/>', 0, "failed"),
    ('<testsuite><testcase/></testsuite>', 5, "failed"),
    ('broken', 0, "failed"),
])
def test_eval_never_accepts_empty_skipped_failed_or_collection_error(xml, code, status):
    assert verdict(xml, code)["status"] == status


def test_pinned_manifest_excludes_live_calls_and_has_both_layers():
    cases = load_cases()["cases"]
    assert {c["mode"] for c in cases} == {"offline", "integration"}
    assert len({c["id"] for c in cases}) == len(cases)

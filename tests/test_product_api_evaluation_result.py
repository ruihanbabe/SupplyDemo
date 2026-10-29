"""Independent negative oracles: stale reviews and forged evidence must fail."""

from copy import deepcopy
from uuid import uuid4

import pytest

from product_api.evaluation_result import RUBRIC, assess, digest


def fixture():
    bundle = {
        "runId": str(uuid4()), "jobId": str(uuid4()), "organizationId": str(uuid4()),
        "attempts": [], "costMicros": None, "usageState": "unknown",
        "answer": {"question": "报价有效吗", "status": "completed", "quality": "needs_review",
                   "evidence": [{"id": "observation", "content_hash": "hash"}],
                   "result": {"claims": [{"citations": [{"sourceId": "observation",
                              "fragmentHash": "hash", "version": "1", "locator": "payload",
                              "sourceKind": "intelligence_observation"}]}]}},
    }
    bundle["snapshotHash"] = digest(bundle)
    case = {"caseId": "quote", "version": 1, "organizationId": bundle["organizationId"],
            "question": "报价有效吗", "expected": {"quality": "needs_review"}}
    return bundle, case


def test_semantics_never_pass_just_because_citations_match():
    bundle, case = fixture()
    result = assess(bundle, case)
    assert result["hardConstraints"] == "passed" and result["semanticQuality"] == "pending"
    assert assess(bundle, {**case, "expected": {"absent": None}})["hardConstraints"] == "failed"
    changed = deepcopy(bundle)
    changed["answer"]["result"]["claims"][0]["citations"][0]["fragmentHash"] = "forged"
    changed["snapshotHash"] = digest({k: v for k, v in changed.items() if k != "snapshotHash"})
    assert assess(changed, case)["hardConstraints"] == "failed"
    with pytest.raises(ValueError, match="integrity"):
        assess({**bundle, "costMicros": 0}, case)


def test_review_is_bound_to_exact_case_and_snapshot():
    bundle, case = fixture()
    review = {"reviewer": "human", "rationale": "逐项对照原始证据", "caseHash": digest(case),
              "snapshotHash": bundle["snapshotHash"], "scores": {k: 2 for k in RUBRIC}}
    assert assess(bundle, case, review)["semanticQuality"] == "passed"
    with pytest.raises(ValueError, match="stale"):
        assess(bundle, {**case, "version": 2}, review)
    review["scores"][RUBRIC[0]] = 1
    assert assess(bundle, case, review)["semanticQuality"] == "needs_review"

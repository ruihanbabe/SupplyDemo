"""F26: three layers, intersection semantics, and a denial that names its origin."""
from __future__ import annotations

from pathlib import Path

import pytest

from permissions.explain import explain, render
from permissions.policy import GlobalPolicy, PermissionPolicy, SessionPolicy, load_policy

EVERYTHING = GlobalPolicy(granted=frozenset({"catalog.read", "procurement.compute",
                                             "procurement.write", "documents.read"}))


def policy(**overrides) -> PermissionPolicy:
    base = {"global_policy": EVERYTHING,
            "worker_permissions": frozenset({"catalog.read", "procurement.write"}),
            "session": SessionPolicy(mode="auto"), "worker": "probe"}
    return PermissionPolicy(**{**base, **overrides})


# ---------- 交集，不是覆盖 ----------

def test_all_three_layers_must_agree():
    assert policy().evaluate("catalog.read").allowed


def test_a_worker_cannot_grant_itself_what_the_deployment_withheld():
    """Intersection, not override. Written as override, the lowest layer could open a
    door the deployment never installed — which is the one thing a chain prevents."""
    narrow = policy(global_policy=GlobalPolicy(granted=frozenset({"documents.read"})))
    decision = narrow.evaluate("catalog.read")
    assert not decision.allowed
    assert decision.layer == "global"


def test_the_global_revoke_list_outranks_every_grant_below():
    killed = policy(global_policy=GlobalPolicy(granted=frozenset({"catalog.read"}),
                                               revoked=frozenset({"catalog.read"})))
    assert killed.evaluate("catalog.read").layer == "global"


def test_a_session_can_only_narrow():
    withheld = policy(session=SessionPolicy(mode="auto",
                                            withheld=frozenset({"catalog.read"})))
    decision = withheld.evaluate("catalog.read")
    assert not decision.allowed
    assert decision.layer == "session"


# ---------- 拒绝必须说出来自哪一层 ----------

def test_each_denial_names_the_file_to_open():
    """"Denied" alone is not actionable: the reader has to know which of three files is
    responsible."""
    assert "config/permissions.yaml" in policy(
        global_policy=GlobalPolicy()).evaluate("catalog.read").reason
    assert "config/agents/probe.yaml" in policy().evaluate("documents.read").reason


def test_worker_denial_is_distinguished_from_global_denial():
    assert policy().evaluate("documents.read").layer == "worker"
    assert policy(global_policy=GlobalPolicy()).evaluate("catalog.read").layer == "global"


# ---------- 会话档位 ----------

def test_explore_is_read_only():
    explore = policy(session=SessionPolicy(mode="explore"))
    assert explore.evaluate("catalog.read", effect="read").allowed
    assert not explore.evaluate("procurement.write", effect="write").allowed


def test_ask_refuses_the_action_not_the_permission():
    """The fix is an approval, not a config change — so the message must not read like a
    missing grant."""
    decision = policy(session=SessionPolicy(mode="ask")).evaluate(
        "procurement.write", effect="write")
    assert not decision.allowed
    assert "批准" in decision.reason


def test_auto_allows_a_granted_write():
    assert policy().evaluate("procurement.write", effect="write").allowed


# ---------- 失败关闭 ----------

def test_a_missing_global_policy_grants_nothing(tmp_path):
    """Deleting a config file must not widen access."""
    empty = GlobalPolicy.load(tmp_path / "absent.yaml")
    assert empty.granted == frozenset()


def test_an_unknown_worker_gets_nothing():
    """A typo in a worker name must not open a door."""
    assert load_policy("no-such-worker").worker_permissions == frozenset()


# ---------- explain ----------

def test_the_same_tool_is_allowed_for_one_worker_and_denied_for_another():
    """EV-67: the demonstration this whole feature exists for."""
    allowed = {row["tool"]: row for row in explain("supervisor")}
    denied = {row["tool"]: row for row in explain("spec_check")}
    assert allowed["compute_shortage"]["allowed"] is True
    assert denied["compute_shortage"]["allowed"] is False
    assert denied["compute_shortage"]["layer"] == "worker"
    assert "spec_check.yaml" in denied["compute_shortage"]["reason"]


def test_explain_separates_unavailable_from_unauthorised():
    """A tool that policy permits but that is not wired up yet is a different problem;
    reporting it as a permission denial sends someone editing the wrong file."""
    rows = {row["tool"]: row for row in explain("supervisor")}
    draft = rows["create_procurement_draft"]
    assert draft["allowed"] is False
    assert draft["layer"] == "global"  # Q-03 keeps the permission ungranted entirely


def test_explain_renders_every_registered_tool_not_just_the_reachable_ones():
    text = render(explain("evidence_check"))
    assert "list_projects" in text
    assert "deny" in text


@pytest.mark.parametrize("worker", ["supervisor", "spec_check", "evidence_check",
                                    "proposal"])
def test_every_worker_has_an_explainable_posture(worker):
    rows = explain(worker)
    assert rows
    assert all(row["layer"] for row in rows)


def test_the_repository_policy_file_is_the_one_being_read():
    assert (Path(__file__).resolve().parents[1] / "config" / "permissions.yaml").exists()

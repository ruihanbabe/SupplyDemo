"""The two calibration decisions the deterministic core waits on.

Neither is a fact discovered from data; both are decisions a person makes once and
stands behind. They live in business_rule because that is where versioned business
parameters belong (D18) — a new decision is a new version, never an edit, so a
conclusion drawn last month still points at the rule that was in force then.

`quantity_basis` is the one that unblocks arithmetic: the open-hardware BOMs give a
`quantity` column without saying whether it means per-board or total for the run. BR-01
refuses to multiply until someone says which. Saying it here, once, with an author and a
note, is the honest version of that answer.

Run with: make seed-rules
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine, text

from infrastructure.database import database_url

TENANT = "default"
OPERATOR = "seed"

RULES = {
    "bom.quantity_basis": (
        {"basis": "per_board"},
        ("开源硬件 BOM 的 quantity 列按位号数量给出，为单板用量；因此可乘生产数量（BR-01）。"
         "若将来引入按整批给量的数据源，改为新版本而非修改本条。"),
    ),
    "component.identity_policy": (
        {"accepted_status": ["source_asserted", "verified"]},
        ("当前阶段接受 BOM 自述的元件身份进入计算，身份风险以证据和 RiskEvent 呈现，"
         "不用「拒绝计算」代替。逐个核验由 spec_check 承担，核验通过者升为 verified。"),
    ),
}


def current(connection, rule_id):
    row = connection.execute(text("""
        SELECT version, value FROM business_rule
        WHERE tenant_id = :t AND rule_id = :r ORDER BY version DESC LIMIT 1"""),
        {"t": TENANT, "r": rule_id}).mappings().first()
    return dict(row) if row else None


def apply_rules(connection) -> dict[str, str]:
    now = datetime.now(UTC)
    outcome = {}
    for rule_id, (value, note) in RULES.items():
        existing = current(connection, rule_id)
        if existing and existing["value"] == value:
            # Re-running must not manufacture versions that changed nothing.
            outcome[rule_id] = f"unchanged (v{existing['version']})"
            continue
        version = (existing["version"] + 1) if existing else 1
        connection.execute(text("""
            INSERT INTO business_rule(rule_id, version, tenant_id, value, effective_from,
                                      changed_by, change_note)
            VALUES (:r, :v, :t, CAST(:val AS JSONB), :from_, :by, :note)"""),
            {"r": rule_id, "v": version, "t": TENANT, "val": json.dumps(value),
             "from_": now, "by": OPERATOR, "note": note})
        connection.execute(text("""
            INSERT INTO operator_log(log_id, tenant_id, operator, action, target_ref,
                                     before_value, after_value)
            VALUES (:id, :t, :by, 'update_threshold', CAST(:target AS JSONB),
                    CAST(:before AS JSONB), CAST(:after AS JSONB))"""),
            {"id": uuid4(), "t": TENANT, "by": OPERATOR,
             "target": json.dumps({"kind": "business_rule", "rule_id": rule_id,
                                   "version": version}),
             "before": json.dumps(existing["value"]) if existing else None,
             "after": json.dumps(value)})
        outcome[rule_id] = f"v{version}"
    return outcome


def main() -> None:
    engine = create_engine(database_url(), hide_parameters=True)
    try:
        with engine.begin() as connection:
            print("Business rules:", apply_rules(connection))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

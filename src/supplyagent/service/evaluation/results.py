"""F38 E1: private result snapshots, deterministic checks and separate human review."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from supplyagent.service.api.rbac import require_capability
from supplyagent.service.workflows.intelligence import read_answer, visible_request

RUBRIC = ("evidence_faithfulness", "question_relevance", "uncertainty_preservation")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


async def snapshot(tenants, actor, organization, run):
    """Use the original reader's current scope; never use an admin export bypass."""
    async def read(connection, membership):
        require_capability(membership.role, "crm:read")
        request = await visible_request(connection, run)
        answer = await read_answer(connection, run)
        cursor = await connection.execute(
            """select id,role,generation,status,input_context,created_at,finished_at
               from app.intelligence_attempt where mission_id=%s order by created_at,id""",
            (request["mission_id"],),
        )
        attempts = await cursor.fetchall()
        evidence_ids = [UUID(e["id"]) for e in answer["evidence"]]
        cursor = await connection.execute(
            "select id,payload->'execution' as execution from app.intelligence_observation where id=any(%s)",
            (evidence_ids,),
        )
        links = [{"observationId": str(row["id"]), **{
            key: (row["execution"] or {}).get(key)
            for key in ("invocationId", "modelCallKey", "toolCallId", "batchId", "index")
        }} for row in await cursor.fetchall()]
        # No old prompt/body export: only version fingerprints and safe execution locators.
        references = [{
            "invocationId": str(a["id"]), "role": a["role"],
            "generation": a["generation"], "status": a["status"],
            "inputHash": digest(a["input_context"]),
            "elapsedSeconds": (a["finished_at"] - a["created_at"]).total_seconds()
            if a["finished_at"] else None,
        } for a in attempts]
        usage = await (await connection.execute(
            """select count(*) as calls,coalesce(sum(input_tokens),0) as input_tokens,
               coalesce(sum(output_tokens),0) as output_tokens,coalesce(sum(cost_micros),0) as cost
               from app.model_usage where run_id=%s""", (request["trace_run_id"],)
        )).fetchone()
        reservations = await (await connection.execute(
            "select count(*) as calls,count(*) filter(where not settled) as pending from app.model_reservation where run_id=%s",
            (request["trace_run_id"],),
        )).fetchone()
        known = reservations["calls"] > 0 and not reservations["pending"] and (
            reservations["calls"] == usage["calls"]
        )
        result = {
            "schemaVersion": 1, "runId": str(run), "jobId": answer["jobId"],
            "organizationId": str(organization), "actorId": str(actor),
            "asOf": answer["asOf"], "answer": answer, "attempts": references,
            "evidenceLinks": links,
            "source": "authorized_database_snapshot",
            "costMicros": int(usage["cost"]) if known else None,
            "usageState": "settled" if known else "unknown",
            "usage": {k: int(v) for k, v in usage.items()},
            "reservations": dict(reservations),
        }
        result["snapshotHash"] = digest(result)
        return result

    return await tenants.in_organization_as_member(actor, organization, read)


def assess(bundle, case, review=None):
    """Checks use externally supplied versioned expectations, never the answer as oracle."""
    expected_hash = bundle.get("snapshotHash")
    body = {k: v for k, v in bundle.items() if k != "snapshotHash"}
    if expected_hash != digest(body):
        raise ValueError("snapshot integrity mismatch")
    if not case.get("caseId") or not case.get("version") or not case.get("expected"):
        raise ValueError("versioned nonempty expectations required")
    answer = bundle["answer"]
    checks = {"completed": answer["status"] == "completed"}
    evidence = {str(e["id"]): e for e in answer["evidence"]}
    claims = (answer.get("result") or {}).get("claims", [])
    checks["nonempty_claims"] = bool(claims)
    checks["citations"] = bool(claims) and all(
        claim.get("citations") and all(
            c.get("sourceKind") == "intelligence_observation"
            and c.get("version") == "1" and c.get("locator") == "payload"
            and c.get("sourceId") in evidence
            and c.get("fragmentHash") == evidence[c["sourceId"]]["content_hash"]
            for c in claim["citations"]
        ) for claim in claims
    )
    checks["scope"] = str(case["organizationId"]) == bundle["organizationId"]
    checks["question"] = case["question"] == answer["question"]
    # A path resolves only through the exported public business result, never SQL/code.
    for path, value in case["expected"].items():
        actual = answer
        found = True
        for key in path.split("."):
            if not isinstance(actual, dict) or key not in actual:
                found = False
                break
            actual = actual[key]
        checks[f"expected:{path}"] = found and actual == value
    semantic = "pending"
    if review is not None:
        if (
            review.get("snapshotHash") != expected_hash
            or review.get("caseHash") != digest(case)
            or not review.get("reviewer", "").strip()
            or set(review.get("scores", {})) != set(RUBRIC)
            or any(type(v) is not int or not 0 <= v <= 2 for v in review["scores"].values())
            or not review.get("rationale", "").strip()
        ):
            raise ValueError("invalid or stale human review")
        semantic = "passed" if all(v == 2 for v in review["scores"].values()) else "needs_review"
    return {
        "caseId": case["caseId"], "caseVersion": case["version"],
        "caseHash": digest(case), "snapshotHash": expected_hash,
        "runId": str(UUID(bundle["runId"])), "jobId": bundle["jobId"],
        "checks": checks, "hardConstraints": "passed" if all(checks.values()) else "failed",
        "semanticQuality": semantic, "rubricVersion": "human-readonly-v1",
        "review": review, "costMicros": bundle["costMicros"],
        "usageState": bundle["usageState"], "attempts": bundle["attempts"],
        "evidenceLinks": bundle.get("evidenceLinks", []),
    }


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--review")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = assess(
        json.loads(Path(args.snapshot).read_text()), json.loads(Path(args.case).read_text()),
        json.loads(Path(args.review).read_text()) if args.review else None,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if result["hardConstraints"] == "passed" and result["semanticQuality"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

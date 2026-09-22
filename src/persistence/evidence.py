"""Append-only storage for observations.

Writes go through record(); nothing else may insert. The ledger never decides whether an
observation is still trustworthy — that depends on a business policy version and is
answered at read time by procurement_core.freshness, so the same stored evidence can be
re-judged under a new policy without being rewritten (which is what makes regression
testing over old evidence possible at all).
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from uuid import UUID, uuid4

from sqlalchemy import text

from contracts.evidence import Observation


def content_hash(observation: Observation) -> str:
    """Identity of an observation: same question, same answer, same place.

    `retrieved_at` is excluded. Two nodes racing to look up the same field in the same
    run are making one observation, not two, and the timestamps would differ by
    milliseconds. What is *not* excluded is the locator, because the same value read from
    two different sources is genuinely two observations — that is exactly the
    disagreement the system must preserve rather than merge (BR-06).
    """
    parts = (observation.tenant_id, observation.kind, observation.subject_ref,
             observation.attribute, observation.value_raw, observation.provenance,
             observation.locator.source_name, observation.locator.request_digest,
             observation.locator.field_path, observation.locator.response_status)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class EvidenceLedger:
    """The only writer of the evidence tables."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def record(self, observation: Observation) -> tuple[UUID, bool]:
        """Store one observation. Returns (evidence_id, newly_created).

        Concurrent duplicates collapse in the database rather than being prevented by
        the caller: nine parallel nodes cannot coordinate with each other, and a
        check-then-insert would still race.
        """
        digest = content_hash(observation)
        evidence_id = uuid4()
        row = self.connection.execute(text("""
            INSERT INTO evidence (evidence_id, run_id, tenant_id, kind, subject_ref,
                                  attribute, value_raw, value_normalized, locator_kind,
                                  retrieved_at, observed_at, provenance, tool_call_id,
                                  content_hash)
            VALUES (:id, :run, :tenant, :kind, :subject, :attribute, :raw, :normalized,
                    'field', :retrieved, :observed, :provenance, :tool_call, :hash)
            ON CONFLICT (tenant_id, run_id, content_hash) DO NOTHING
            RETURNING evidence_id"""),
            {"id": evidence_id, "run": observation.run_id, "tenant": observation.tenant_id,
             "kind": observation.kind, "subject": observation.subject_ref,
             "attribute": observation.attribute, "raw": observation.value_raw,
             "normalized": observation.value_normalized,
             "retrieved": observation.retrieved_at, "observed": observation.observed_at,
             "provenance": observation.provenance, "tool_call": observation.tool_call_id,
             "hash": digest}).scalar()
        if row is None:
            existing = self.connection.execute(text("""
                SELECT evidence_id FROM evidence
                WHERE tenant_id = :tenant AND run_id = :run AND content_hash = :hash"""),
                {"tenant": observation.tenant_id, "run": observation.run_id,
                 "hash": digest}).scalar()
            return existing, False
        locator = asdict(observation.locator)
        self.connection.execute(text("""
            INSERT INTO evidence_field_locator (evidence_id, source_name, request_digest,
                                                field_path, response_status)
            VALUES (:id, :source, :digest, :path, :status)"""),
            {"id": evidence_id, "source": locator["source_name"],
             "digest": locator["request_digest"], "path": locator["field_path"],
             "status": locator["response_status"]})
        return evidence_id, True

    def supersede(self, old_evidence_id: UUID, observation: Observation) -> UUID:
        """Retire an observation by pointing it at its replacement.

        Backfill first, insert second: the old row has to stop being current before the
        new one exists, which only works because the self-reference is deferrable. The
        old row's own values are never touched — what was known at approval time stays
        readable exactly as it was recorded.
        """
        new_id, created = self.record(observation)
        if not created and new_id == old_evidence_id:
            raise ValueError("An observation cannot supersede itself")
        self.connection.execute(text("""
            UPDATE evidence SET superseded_by = :new WHERE evidence_id = :old"""),
            {"new": new_id, "old": old_evidence_id})
        return new_id

    def for_subject(self, run_id: UUID, subject_ref: str, *, kind: str | None = None,
                    include_superseded: bool = False) -> list[dict]:
        """Read an evidence set back, newest first.

        This is what fan-in reads: evidence_check asks "everything known about this
        component in this run" and decides whether it is enough to support a conclusion.
        """
        rows = self.connection.execute(text(f"""
            SELECT e.*, l.source_name, l.request_digest, l.field_path, l.response_status
            FROM evidence e
            LEFT JOIN evidence_field_locator l USING (evidence_id)
            WHERE e.run_id = :run AND e.subject_ref = :subject
              {'' if kind is None else 'AND e.kind = :kind'}
              {'' if include_superseded else 'AND e.superseded_by IS NULL'}
            ORDER BY e.retrieved_at DESC, e.created_at DESC"""),
            {"run": run_id, "subject": subject_ref,
             **({} if kind is None else {"kind": kind})}).mappings().all()
        return [dict(row) for row in rows]

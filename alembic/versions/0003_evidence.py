"""Evidence ledger: field-type observations and their locators.

Append-only, enforced by grants rather than by convention. The one narrow exception is
backfilling `superseded_by`, which is why that single column gets its own UPDATE grant:
replacing an observation means inserting the new one and pointing the old one at it, so
the question "what did we know when this was approved" stays answerable forever.

Document-type locators are absent by design. They reference `official_document`, which
belongs to F17 together with the addressing and caching that produces it; creating the
locator table now would leave a foreign key pointing at nothing.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE evidence (
            evidence_id      UUID PRIMARY KEY,
            run_id           UUID        NOT NULL REFERENCES run(run_id),
            tenant_id        TEXT        NOT NULL DEFAULT 'default',
            kind             TEXT        NOT NULL,
            subject_ref      TEXT        NOT NULL,
            attribute        TEXT        NOT NULL,
            value_raw        TEXT        NOT NULL,
            value_normalized TEXT,
            locator_kind     TEXT        NOT NULL,
            retrieved_at     TIMESTAMPTZ NOT NULL,
            observed_at      TIMESTAMPTZ,
            provenance       TEXT        NOT NULL,
            tool_call_id     UUID        REFERENCES tool_call(tool_call_id),
            content_hash     TEXT        NOT NULL,
            superseded_by    UUID        REFERENCES evidence(evidence_id)
                             DEFERRABLE INITIALLY DEFERRED,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_evidence_kind CHECK (kind IN
                ('stock','price','lead_time','lifecycle','replacement','parameter')),
            CONSTRAINT ck_evidence_locator CHECK (locator_kind IN ('field','document')),
            CONSTRAINT ck_evidence_provenance CHECK (
                provenance IN ('real','cache','sample','replay')),
            CONSTRAINT ck_evidence_not_self_superseded CHECK (
                superseded_by IS NULL OR superseded_by <> evidence_id)
        )""")
    # Two observations of the same fact, from the same query, with the same value are the
    # same observation. Enforcing that here rather than in the application is what makes
    # nine concurrent nodes safe to run without coordinating with each other.
    op.execute("""
        CREATE UNIQUE INDEX uq_evidence_content
            ON evidence (tenant_id, run_id, content_hash)""")
    op.execute("""
        CREATE INDEX ix_evidence_subject
            ON evidence (tenant_id, subject_ref, kind, retrieved_at DESC)""")

    op.execute("""
        CREATE TABLE evidence_field_locator (
            evidence_id     UUID PRIMARY KEY REFERENCES evidence(evidence_id),
            source_name     TEXT NOT NULL,
            request_digest  TEXT NOT NULL,
            field_path      TEXT NOT NULL,
            response_status TEXT NOT NULL,
            CONSTRAINT ck_field_response_status CHECK (
                response_status IN ('ok','partial','not_found'))
        )""")

    op.execute("GRANT SELECT, INSERT ON evidence, evidence_field_locator TO supplyagent_app")
    op.execute("REVOKE UPDATE, DELETE ON evidence, evidence_field_locator FROM supplyagent_app")
    # The narrow exception, granted at column granularity so the application can retire an
    # observation but can never edit what it recorded.
    op.execute("GRANT UPDATE (superseded_by) ON evidence TO supplyagent_app")


def downgrade():
    op.execute("DROP TABLE evidence_field_locator")
    op.execute("DROP TABLE evidence")

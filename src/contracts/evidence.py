"""One observation of the outside world, and how to find it again.

Evidence is deliberately narrow: it records what an external source said, never what
this system computed. Internal stock is an authoritative fact and lives in `inventory`;
a shortage is arithmetic and lives in `shortage_snapshot`. Only the outside world —
a distributor's response, a manufacturer's datasheet — produces Evidence, because only
the outside world can be wrong, go stale, or disagree with itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

#: What kind of fact was observed.
Kind = Literal["stock", "price", "lead_time", "lifecycle", "replacement", "parameter"]

#: Where the data came from. Simulated and replayed data must stay distinguishable from
#: real data at every layer (BR-09), so this travels with every single observation.
Provenance = Literal["real", "cache", "sample", "replay"]

#: How far the source's own response got. Never collapsed (BR-05): "the API said there
#: are none" and "the API did not answer" lead to different purchasing decisions.
ResponseStatus = Literal["ok", "partial", "not_found"]


@dataclass(frozen=True)
class FieldLocator:
    """Where in a structured response the value was read.

    `request_digest` is what makes the observation reproducible: it identifies the query
    that produced this response, so the same question can be asked again and compared.
    Storing the whole request would carry credentials, so it is a digest.
    """

    source_name: str
    request_digest: str
    field_path: str
    response_status: ResponseStatus = "ok"


@dataclass(frozen=True)
class Observation:
    """One value, its origin, and when it was true.

    `value_raw` keeps the source's own wording. ST writes
    "Active - Product is in volume production" where TI writes "ACTIVE"; normalising at
    write time would throw away which of them said it, and a mapping table that turns
    out to be wrong could not be corrected retroactively.

    `value_normalized` is optional on purpose: **normalisation that fails must leave it
    empty rather than guess**. A plausible-looking value is worse than an absent one.
    """

    run_id: UUID
    kind: Kind
    subject_ref: str
    attribute: str
    value_raw: str
    retrieved_at: datetime
    provenance: Provenance
    locator: FieldLocator
    value_normalized: str | None = None
    #: Only when the source states its own snapshot time (e.g. a nightly stock feed).
    #: Most distributor APIs do not, and then it must stay None — substituting
    #: retrieved_at would turn "unknown" into "known", which is the one thing this
    #: layer exists to prevent.
    observed_at: datetime | None = None
    tool_call_id: UUID | None = None
    tenant_id: str = "default"

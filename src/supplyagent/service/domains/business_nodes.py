"""F46 read-only business nodes. Rules and clocks are explicit inputs, not prompts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import AsyncConnection

from supplyagent.paths import PROJECT_ROOT
from supplyagent.service.domains.crm import CrmOpportunityFacts
from supplyagent.service.domains.supply import PostgresSupplyRepository, calculate_impact
from supplyagent.service.persistence.tenant import TenantAccessError

POLICY_PATH = PROJECT_ROOT / "config/product/followup-policy.json"


@dataclass(frozen=True)
class FollowupPolicy:
    policy_id: str
    active_stages: tuple[str, ...]
    require_opportunity_owner: bool
    require_task_owner: bool
    require_future_task: bool
    max_case_parts: int
    business_timezone: str

    @classmethod
    def load(cls, path: Path = POLICY_PATH) -> FollowupPolicy:
        raw = json.loads(path.read_text())
        expected = {
            "schemaVersion",
            "policyId",
            "provenance",
            "activeStages",
            "requireOpportunityOwner",
            "requireTaskOwner",
            "requireFutureTask",
            "maxCaseParts",
            "businessTimezone",
        }
        if set(raw) != expected or raw["schemaVersion"] != 1 or raw["provenance"] != "synthetic":
            raise ValueError("unsupported followup policy schema or provenance")
        if not isinstance(raw["policyId"], str) or not raw["policyId"]:
            raise ValueError("policy ID is required")
        stages = raw["activeStages"]
        if (
            not isinstance(stages, list)
            or not stages
            or any(not isinstance(s, str) or not s for s in stages)
        ):
            raise ValueError("activeStages must contain explicit stage names")
        if any(
            type(raw[k]) is not bool
            for k in ("requireOpportunityOwner", "requireTaskOwner", "requireFutureTask")
        ):
            raise ValueError("followup switches must be booleans")
        if type(raw["maxCaseParts"]) is not int or not 1 <= raw["maxCaseParts"] <= 100:
            raise ValueError("maxCaseParts must be bounded")
        if not isinstance(raw["businessTimezone"], str):
            raise TypeError("business timezone is required")
        try:
            ZoneInfo(raw["businessTimezone"])
        except ZoneInfoNotFoundError as error:
            raise ValueError("unknown business timezone") from error
        return cls(
            raw["policyId"],
            tuple(stages),
            raw["requireOpportunityOwner"],
            raw["requireTaskOwner"],
            raw["requireFutureTask"],
            raw["maxCaseParts"],
            raw["businessTimezone"],
        )


@dataclass(frozen=True)
class TaskSchedule:
    task_id: UUID
    status: str
    assignee_id: UUID | None
    due_at: datetime | None


@dataclass(frozen=True)
class FollowupSignal:
    code: str
    target_id: UUID
    description: str


def followup_signals(
    opportunity_id: UUID,
    stage: str,
    owner_id: UUID | None,
    tasks: tuple[TaskSchedule, ...],
    as_of: datetime,
    policy: FollowupPolicy,
) -> tuple[FollowupSignal, ...]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("analysis time must include timezone")
    if any(
        t.due_at is not None and (t.due_at.tzinfo is None or t.due_at.utcoffset() is None)
        for t in tasks
    ):
        raise ValueError("task deadlines must include timezone")
    if stage not in policy.active_stages:
        return ()
    signals = []
    if policy.require_opportunity_owner and owner_id is None:
        signals.append(
            FollowupSignal("opportunity_unassigned", opportunity_id, "当前商机尚未指派负责人。")
        )
    open_tasks = [task for task in tasks if task.status == "open"]
    for task in open_tasks:
        if task.due_at is None:
            signals.append(
                FollowupSignal(
                    "task_deadline_unknown", task.task_id, "任务没有截止时间，无法判定是否逾期。"
                )
            )
        elif task.due_at < as_of:
            signals.append(
                FollowupSignal(
                    "task_overdue", task.task_id, "截至本次分析时间，当前可见任务已逾期。"
                )
            )
        if policy.require_task_owner and task.assignee_id is None:
            signals.append(
                FollowupSignal("task_unassigned", task.task_id, "当前可见任务尚未指派负责人。")
            )
    if policy.require_future_task and not any(
        t.due_at is not None and t.due_at >= as_of for t in open_tasks
    ):
        signals.append(
            FollowupSignal(
                "no_visible_future_task", opportunity_id, "当前可见范围未找到尚未到期的开放任务。"
            )
        )
    return tuple(signals)


async def followup_context(
    connection: AsyncConnection, facts: CrmOpportunityFacts, as_of: datetime, policy: FollowupPolicy
) -> dict:
    cursor = await connection.execute(
        "select owner_id from app.crm_opportunity where id=%s", (facts.opportunity.id,)
    )
    row = await cursor.fetchone()
    if row is None:
        raise TenantAccessError("opportunity outside scope")
    cursor = await connection.execute(
        "select id,status,assignee_id,due_at from app.crm_task where opportunity_id=%s and id=any(%s::uuid[]) order by id",
        (facts.opportunity.id, [t.id for t in facts.tasks]),
    )
    schedules = tuple(
        TaskSchedule(t["id"], t["status"], t["assignee_id"], t["due_at"])
        for t in await cursor.fetchall()
    )
    return {
        "asOf": as_of,
        "policy": asdict(policy),
        "provenance": "synthetic_policy",
        "opportunityOwnerId": row["owner_id"],
        "taskSchedules": [asdict(t) for t in schedules],
        "signals": [
            asdict(s)
            for s in followup_signals(
                facts.opportunity.id,
                facts.opportunity.stage,
                row["owner_id"],
                schedules,
                as_of,
                policy,
            )
        ],
        "unknowns": ["跟进判断仅基于当前可见任务；未找到记录不表示不可见记录不存在。"],
    }


async def validate_case_parts(
    connection: AsyncConnection,
    opportunity_id: UUID,
    case_id: UUID,
    policy: FollowupPolicy,
    part_numbers: tuple[str, ...],
) -> list[str]:
    cursor = await connection.execute(
        """select c.scope from app.crm_opportunity o join app.supply_case c on c.id=o.supply_case_id
           where o.id=%s and c.id=%s""",
        (opportunity_id, case_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise TenantAccessError("opportunity supply binding changed or outside scope")
    parts = row["scope"].get("partNumbers")
    if (
        not isinstance(parts, list)
        or not parts
        or any(not isinstance(p, str) or not 1 <= len(p) <= 160 for p in parts)
    ):
        raise ValueError("case has no valid part scope")
    if set(parts) != set(part_numbers) or not part_numbers:
        raise TenantAccessError("supply case part scope changed since admission")
    parts = sorted(set(part_numbers))
    if len(parts) > policy.max_case_parts:
        raise ValueError("case part scope exceeds configured investigation budget")
    return parts


async def case_supply_context(
    connection: AsyncConnection,
    opportunity_id: UUID,
    case_id: UUID,
    as_of: datetime,
    policy: FollowupPolicy,
    part_numbers: tuple[str, ...],
) -> dict:
    parts = await validate_case_parts(connection, opportunity_id, case_id, policy, part_numbers)
    repository = PostgresSupplyRepository()
    impacts = []
    for part in parts:
        facts = await repository.load_facts(connection, part)
        impacts.append(
            asdict(
                calculate_impact(
                    facts, part, as_of.astimezone(ZoneInfo(policy.business_timezone)).date()
                )
            )
        )
    return {
        "asOf": as_of,
        "caseId": case_id,
        "parts": parts,
        "impacts": impacts,
        "scopeNote": "覆盖当前商机关联 Case 的全部指定零件；需求来自供应需求行，尚未自动核验产品/BOM 用量关系。",
        "unknowns": [item for impact in impacts for item in impact["unknowns"]],
    }

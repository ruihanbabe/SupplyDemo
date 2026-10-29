"""F36 Specialist 责任、证据与父 Agent 汇总的产品领域契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SpecialistRole(StrEnum):
    CUSTOMER_FACTS = "customer_facts"
    OPPORTUNITY_DIAGNOSIS = "opportunity_diagnosis"
    BUSINESS_POLICY = "business_policy"
    SUPPLY_RISK = "supply_risk"
    PROCUREMENT_STRATEGY = "procurement_strategy"
    QUOTE_VALIDITY = "quote_validity"


class ClaimQuality(StrEnum):
    SUPPORTED = "supported"
    CONFLICTING = "conflicting"
    STALE = "stale"
    UNSUPPORTED = "unsupported"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class ReviewVerdict(StrEnum):
    APPROVED = "approved"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class MissionQuality(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Citation:
    """指向一个不可变来源片段，不包含来源正文。"""

    source_kind: str
    source_id: str
    version: str
    locator: str
    fragment_hash: str

    def __post_init__(self) -> None:
        values = (self.source_kind, self.source_id, self.version, self.locator)
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("citation 字段必须是 1–200 字符")
        if not _SHA256.fullmatch(self.fragment_hash):
            raise ValueError("citation fragment_hash 必须是小写 SHA-256")


@dataclass(frozen=True)
class Claim:
    """Specialist 的最小可复核结论。"""

    claim_id: UUID
    organization_id: UUID
    mission_id: UUID
    subject: str
    claim_type: str
    conclusion: str
    produced_by: SpecialistRole
    quality: ClaimQuality
    citations: tuple[Citation, ...] = ()
    unknowns: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.subject or len(self.subject) > 200:
            raise ValueError("claim subject 必须是 1–200 字符")
        if not self.claim_type or len(self.claim_type) > 100:
            raise ValueError("claim type 必须是 1–100 字符")
        if not self.conclusion or len(self.conclusion) > 2_000:
            raise ValueError("claim conclusion 必须是 1–2000 字符")
        if self.quality is ClaimQuality.SUPPORTED and not self.citations:
            raise ValueError("supported claim 必须包含 citation")
        if self.quality is ClaimQuality.CONFLICTING and not self.conflicts:
            raise ValueError("conflicting claim 必须列出冲突")
        details = (*self.unknowns, *self.conflicts)
        if len(details) > 32 or any(not item or len(item) > 500 for item in details):
            raise ValueError("claim 未知或冲突必须是最多 32 条的有限摘要")


@dataclass(frozen=True)
class SpecialistAssignment:
    """父 Agent 发给一个 Specialist 的有界任务，不承载原始业务正文。"""

    organization_id: UUID
    mission_id: UUID
    role: SpecialistRole
    task: str
    input_artifact_ids: tuple[UUID, ...]
    allowed_tools: tuple[str, ...]
    runtime_run_id: UUID | None = None
    invocation_id: UUID | None = None
    instructions: str | None = None

    def __post_init__(self) -> None:
        if not self.task or len(self.task) > 1_000:
            raise ValueError("specialist task 必须是 1–1000 字符")
        if len(self.input_artifact_ids) > 32:
            raise ValueError("specialist 输入 Artifact 最多 32 个")
        if not self.allowed_tools or len(self.allowed_tools) > 16:
            raise ValueError("specialist 必须有 1–16 个显式允许工具")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("specialist 工具白名单不能重复")


@dataclass(frozen=True)
class SpecialistResult:
    artifact_id: UUID
    organization_id: UUID
    mission_id: UUID
    role: SpecialistRole
    claims: tuple[Claim, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not self.claims:
            raise ValueError("Specialist 必须显式返回 Claim，包括无法确认")
        if any(claim.produced_by is not self.role for claim in self.claims):
            raise ValueError("Claim 生产者必须与 Specialist 角色一致")
        if any(
            claim.organization_id != self.organization_id or claim.mission_id != self.mission_id
            for claim in self.claims
        ):
            raise ValueError("Claim 必须与 Specialist 结果属于同一组织和 Mission")


class SpecialistPort(Protocol):
    async def run(self, assignment: SpecialistAssignment) -> SpecialistResult: ...


@dataclass(frozen=True)
class IndependentReview:
    target_artifact_id: UUID
    reviewer: str
    verdict: ReviewVerdict
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reviewer or len(self.reviewer) > 160:
            raise ValueError("reviewer 必须是有限标识")
        if self.verdict is not ReviewVerdict.APPROVED and not self.issues:
            raise ValueError("未通过 review 必须给出问题")
        if len(self.issues) > 32 or any(not issue or len(issue) > 500 for issue in self.issues):
            raise ValueError("review 问题必须是最多 32 条的有限摘要")


@dataclass(frozen=True)
class MissionPlan:
    organization_id: UUID
    mission_id: UUID
    mission_kind: str
    stages: tuple[tuple[SpecialistRole, ...], ...]
    required_roles: tuple[SpecialistRole, ...]
    proposal_max_parallel: int = 1

    def __post_init__(self) -> None:
        planned = tuple(role for stage in self.stages for role in stage)
        if not self.mission_kind or len(self.mission_kind) > 100:
            raise ValueError("mission kind 必须是 1–100 字符")
        if not planned or len(planned) != len(set(planned)):
            raise ValueError("Mission 角色必须存在且不能重复")
        if set(planned) != set(self.required_roles):
            raise ValueError("所有已启用 Specialist 都必须是 Mission 完整性要求")
        if self.proposal_max_parallel != 1:
            raise ValueError("父 Agent ProposalDraft 写入必须保持 maxParallel=1")

    @classmethod
    def opportunity_next_action(
        cls,
        organization_id: UUID,
        mission_id: UUID,
        *,
        supply_linked: bool = False,
        procurement_needed: bool = False,
    ) -> MissionPlan:
        discovery = [SpecialistRole.CUSTOMER_FACTS, SpecialistRole.BUSINESS_POLICY]
        if supply_linked:
            discovery.append(SpecialistRole.SUPPLY_RISK)
        diagnosis = [SpecialistRole.OPPORTUNITY_DIAGNOSIS]
        if procurement_needed:
            diagnosis.append(SpecialistRole.PROCUREMENT_STRATEGY)
        stages = (tuple(discovery), tuple(diagnosis))
        return cls(
            organization_id,
            mission_id,
            "opportunity_next_action",
            stages,
            tuple(role for stage in stages for role in stage),
        )


@dataclass(frozen=True)
class ProposalDraftArtifact:
    """不可执行草稿；F41 才能把它变成真实 Proposal。"""

    mission_id: UUID
    recommendation: str
    source_artifact_ids: tuple[UUID, ...]
    claim_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class ParentRecommendation:
    mission_id: UUID
    quality: MissionQuality
    recommendation: str
    claims: tuple[Claim, ...]
    missing_roles: tuple[SpecialistRole, ...]
    issues: tuple[str, ...]
    proposal_draft: ProposalDraftArtifact | None


class ParentAgent:
    """唯一汇总入口；不读取原始工具输出，也不执行 Proposal。"""

    def aggregate(
        self,
        plan: MissionPlan,
        results: tuple[SpecialistResult, ...],
        reviews: tuple[IndependentReview, ...],
        recommendation: str,
    ) -> ParentRecommendation:
        if not recommendation or len(recommendation) > 4_000:
            raise ValueError("父 Agent recommendation 必须是 1–4000 字符")
        by_role = {result.role: result for result in results}
        if len(by_role) != len(results) or set(by_role) - set(plan.required_roles):
            raise ValueError("Specialist 结果重复或不在 MissionPlan 内")
        if any(
            result.organization_id != plan.organization_id or result.mission_id != plan.mission_id
            for result in results
        ):
            raise ValueError("Specialist 结果不属于当前组织或 Mission")

        missing = tuple(role for role in plan.required_roles if role not in by_role)
        claims = tuple(
            claim
            for role in plan.required_roles
            if role in by_role
            for claim in by_role[role].claims
        )
        issues: list[str] = []
        quality = MissionQuality.COMPLETE

        if missing:
            quality = MissionQuality.BLOCKED
            issues.append("缺少必需 Specialist：" + ",".join(role.value for role in missing))

        review_by_artifact: dict[UUID, list[IndependentReview]] = {}
        for review in reviews:
            review_by_artifact.setdefault(review.target_artifact_id, []).append(review)
        if set(review_by_artifact) - {result.artifact_id for result in results}:
            raise ValueError("review 指向不属于当前 Mission 的 Artifact")

        rejected = any(claim.quality is ClaimQuality.REJECTED for claim in claims)
        rejected = rejected or any(review.verdict is ReviewVerdict.REJECTED for review in reviews)
        if rejected:
            quality = MissionQuality.BLOCKED
            issues.append("存在被拒绝的 Claim 或 review")

        needs_review = any(claim.quality is not ClaimQuality.SUPPORTED for claim in claims)
        for result in results:
            artifact_reviews = review_by_artifact.get(result.artifact_id, [])
            if not any(review.verdict is ReviewVerdict.APPROVED for review in artifact_reviews):
                needs_review = True
                issues.append(f"{result.role.value} 缺少独立 approved review")
            if any(review.verdict is ReviewVerdict.NEEDS_REVIEW for review in artifact_reviews):
                needs_review = True
        if quality is not MissionQuality.BLOCKED and needs_review:
            quality = MissionQuality.NEEDS_REVIEW

        if quality is MissionQuality.COMPLETE and any(not result.complete for result in results):
            quality = MissionQuality.PARTIAL
            issues.append("至少一个 Specialist 只返回部分结果")

        draft = None
        if quality is MissionQuality.COMPLETE:
            draft = ProposalDraftArtifact(
                plan.mission_id,
                recommendation,
                tuple(by_role[role].artifact_id for role in plan.required_roles),
                tuple(claim.claim_id for claim in claims),
            )
        return ParentRecommendation(
            plan.mission_id,
            quality,
            recommendation,
            claims,
            missing,
            tuple(dict.fromkeys(issues)),
            draft,
        )

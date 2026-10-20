from uuid import uuid4

import pytest

from product_api.specialists import (
    Citation,
    Claim,
    ClaimQuality,
    IndependentReview,
    MissionPlan,
    MissionQuality,
    ParentAgent,
    ReviewVerdict,
    SpecialistResult,
    SpecialistRole,
)


def _citation() -> Citation:
    return Citation("crm", "opportunity-1", "version-1", "field:stage", "a" * 64)


def _result(
    plan: MissionPlan,
    role: SpecialistRole,
    *,
    quality: ClaimQuality = ClaimQuality.SUPPORTED,
    complete: bool = True,
) -> SpecialistResult:
    citations = (_citation(),) if quality is ClaimQuality.SUPPORTED else ()
    conflicts = ("CRM 阶段与会议纪要冲突",) if quality is ClaimQuality.CONFLICTING else ()
    claim = Claim(
        claim_id=uuid4(),
        organization_id=plan.organization_id,
        mission_id=plan.mission_id,
        subject="商机 opportunity-1",
        claim_type="next_action_input",
        conclusion=f"{role.value} 的受控结论",
        produced_by=role,
        quality=quality,
        citations=citations,
        conflicts=conflicts,
    )
    return SpecialistResult(
        artifact_id=uuid4(),
        organization_id=plan.organization_id,
        mission_id=plan.mission_id,
        role=role,
        claims=(claim,),
        complete=complete,
    )


def _approved(result: SpecialistResult) -> IndependentReview:
    return IndependentReview(
        target_artifact_id=result.artifact_id,
        reviewer="deterministic-citation-reviewer",
        verdict=ReviewVerdict.APPROVED,
    )


def test_supported_claim_requires_a_citation() -> None:
    with pytest.raises(ValueError, match="supported claim"):
        Claim(
            claim_id=uuid4(),
            organization_id=uuid4(),
            mission_id=uuid4(),
            subject="客户 customer-1",
            claim_type="customer_fact",
            conclusion="客户已确认交期",
            produced_by=SpecialistRole.CUSTOMER_FACTS,
            quality=ClaimQuality.SUPPORTED,
        )


def test_mission_plan_separates_parallel_discovery_from_dependent_diagnosis() -> None:
    plan = MissionPlan.opportunity_next_action(
        uuid4(),
        uuid4(),
        supply_linked=True,
        procurement_needed=True,
    )

    assert plan.stages == (
        (
            SpecialistRole.CUSTOMER_FACTS,
            SpecialistRole.BUSINESS_POLICY,
            SpecialistRole.SUPPLY_RISK,
        ),
        (SpecialistRole.OPPORTUNITY_DIAGNOSIS, SpecialistRole.PROCUREMENT_STRATEGY),
    )
    assert plan.proposal_max_parallel == 1


def test_complete_reviewed_results_create_only_a_non_executable_draft() -> None:
    plan = MissionPlan.opportunity_next_action(uuid4(), uuid4())
    results = tuple(_result(plan, role) for role in plan.required_roles)

    recommendation = ParentAgent().aggregate(
        plan,
        results,
        tuple(_approved(result) for result in results),
        "建议销售在确认审批边界后安排客户复盘。",
    )

    assert recommendation.quality is MissionQuality.COMPLETE
    assert recommendation.proposal_draft is not None
    assert recommendation.proposal_draft.mission_id == plan.mission_id
    assert not hasattr(recommendation.proposal_draft, "execute")


def test_missing_required_role_or_rejected_review_blocks_draft() -> None:
    plan = MissionPlan.opportunity_next_action(uuid4(), uuid4())
    facts = _result(plan, SpecialistRole.CUSTOMER_FACTS)
    policy = _result(plan, SpecialistRole.BUSINESS_POLICY)

    missing = ParentAgent().aggregate(
        plan,
        (facts, policy),
        (_approved(facts), _approved(policy)),
        "信息不完整，暂不建议动作。",
    )
    assert missing.quality is MissionQuality.BLOCKED
    assert missing.missing_roles == (SpecialistRole.OPPORTUNITY_DIAGNOSIS,)
    assert missing.proposal_draft is None

    diagnosis = _result(plan, SpecialistRole.OPPORTUNITY_DIAGNOSIS)
    rejected = IndependentReview(
        diagnosis.artifact_id,
        "policy-reviewer",
        ReviewVerdict.REJECTED,
        ("引用版本已撤销",),
    )
    blocked = ParentAgent().aggregate(
        plan,
        (facts, policy, diagnosis),
        (_approved(facts), _approved(policy), rejected),
        "该结果不得生成草稿。",
    )
    assert blocked.quality is MissionQuality.BLOCKED
    assert blocked.proposal_draft is None


def test_partial_and_unsupported_results_never_equal_complete() -> None:
    plan = MissionPlan.opportunity_next_action(uuid4(), uuid4())
    partial_results = tuple(
        _result(
            plan,
            role,
            complete=role is not SpecialistRole.OPPORTUNITY_DIAGNOSIS,
        )
        for role in plan.required_roles
    )
    partial = ParentAgent().aggregate(
        plan,
        partial_results,
        tuple(_approved(result) for result in partial_results),
        "结果尚未完整。",
    )
    assert partial.quality is MissionQuality.PARTIAL
    assert partial.proposal_draft is None

    review_results = tuple(
        _result(
            plan,
            role,
            quality=(
                ClaimQuality.UNSUPPORTED
                if role is SpecialistRole.CUSTOMER_FACTS
                else ClaimQuality.SUPPORTED
            ),
        )
        for role in plan.required_roles
    )
    needs_review = ParentAgent().aggregate(
        plan,
        review_results,
        tuple(_approved(result) for result in review_results),
        "客户事实仍需人工确认。",
    )
    assert needs_review.quality is MissionQuality.NEEDS_REVIEW
    assert needs_review.proposal_draft is None


def test_parent_rejects_cross_mission_or_unplanned_results() -> None:
    plan = MissionPlan.opportunity_next_action(uuid4(), uuid4())
    other_plan = MissionPlan.opportunity_next_action(plan.organization_id, uuid4())
    cross_mission = _result(other_plan, SpecialistRole.CUSTOMER_FACTS)

    with pytest.raises(ValueError, match="当前组织或 Mission"):
        ParentAgent().aggregate(plan, (cross_mission,), (), "不得接受跨任务结果。")

    unplanned = _result(plan, SpecialistRole.SUPPLY_RISK)
    with pytest.raises(ValueError, match="不在 MissionPlan"):
        ParentAgent().aggregate(plan, (unplanned,), (), "不得接受未启用角色。")


def test_result_rejects_claim_from_another_scope() -> None:
    plan = MissionPlan.opportunity_next_action(uuid4(), uuid4())
    claim = Claim(
        claim_id=uuid4(),
        organization_id=uuid4(),
        mission_id=plan.mission_id,
        subject="客户 customer-1",
        claim_type="customer_fact",
        conclusion="来自另一个组织的事实",
        produced_by=SpecialistRole.CUSTOMER_FACTS,
        quality=ClaimQuality.SUPPORTED,
        citations=(_citation(),),
    )

    with pytest.raises(ValueError, match="同一组织和 Mission"):
        SpecialistResult(
            artifact_id=uuid4(),
            organization_id=plan.organization_id,
            mission_id=plan.mission_id,
            role=SpecialistRole.CUSTOMER_FACTS,
            claims=(claim,),
            complete=True,
        )

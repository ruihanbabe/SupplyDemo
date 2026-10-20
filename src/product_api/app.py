"""F31 最小 FastAPI 外壳：健康检查、会话与组织选择。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from os import getenv
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg import AsyncConnection
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from product_api.auth import (
    AuthenticatedIdentity,
    AuthenticationError,
    AuthVerifier,
    SupabaseAuthVerifier,
    UnavailableAuthVerifier,
)
from product_api.rbac import (
    CapabilityAccessError,
    effective_capabilities,
    is_known_role,
    require_capability,
)
from product_api.supply import (
    InventoryPosition,
    PostgresSupplyRepository,
    SupplyAccessError,
    SupplyCase,
    SupplyFacts,
    SupplyImpact,
    SupplyRepository,
    UnavailableSupplyRepository,
    calculate_impact,
)
from product_api.tenant import (
    OrganizationMembership,
    PostgresTenantRepository,
    TenantAccessError,
    TenantConfigurationError,
    TenantRepository,
    UnavailableTenantRepository,
)
from product_api.trace import (
    PostgresTraceRepository,
    TraceAccessError,
    TraceEvent,
    TraceRepository,
    UnavailableTraceRepository,
)

ORGANIZATION_COOKIE = "supplyagent_organization"


class OrganizationSelection(BaseModel):
    """浏览器可提供的唯一组织选择提示。"""

    organization_id: UUID = Field(
        validation_alias=AliasChoices("organizationId", "organization_id")
    )


class OrganizationScopedRequest(BaseModel):
    """所有供应链业务请求都把组织值当作待核验的选择提示。"""

    organization_id: UUID = Field(
        validation_alias=AliasChoices("organizationId", "organization_id")
    )


CasePartNumber = Annotated[str, Field(min_length=1, max_length=160)]
CaseLocationCode = Annotated[str, Field(min_length=1, max_length=80)]
CaseSourceReference = Annotated[str, Field(min_length=1, max_length=160)]
CaseTag = Annotated[str, Field(min_length=1, max_length=40)]


class SupplyCaseScope(BaseModel):
    """Case 的业务上下文，不是成员权限 scope，也不承载任意外部正文。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    part_numbers: list[CasePartNumber] = Field(
        default_factory=list,
        validation_alias=AliasChoices("partNumbers", "part_numbers"),
        serialization_alias="partNumbers",
        max_length=32,
    )
    location_codes: list[CaseLocationCode] = Field(
        default_factory=list,
        validation_alias=AliasChoices("locationCodes", "location_codes"),
        serialization_alias="locationCodes",
        max_length=16,
    )
    source_references: list[CaseSourceReference] = Field(
        default_factory=list,
        validation_alias=AliasChoices("sourceReferences", "source_references"),
        serialization_alias="sourceReferences",
        max_length=32,
    )
    tags: list[CaseTag] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_business_context(self) -> SupplyCaseScope:
        """固定两层结构、去重引用，并限制 JSONB 的单行载荷大小。"""
        for name, values in (
            ("partNumbers", self.part_numbers),
            ("locationCodes", self.location_codes),
            ("sourceReferences", self.source_references),
            ("tags", self.tags),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"scope.{name} 不允许重复值")
        encoded = json.dumps(
            self.model_dump(by_alias=True), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > 8 * 1024:
            raise ValueError("scope 编码后不能超过 8 KiB")
        return self


class CreateSupplyCaseRequest(OrganizationScopedRequest):
    title: str = Field(min_length=1, max_length=240)
    status: str = Field(pattern="^(open|investigating|waiting|resolved|escalated)$")
    severity: str = Field(pattern="^(low|medium|high|critical)$")
    scope: SupplyCaseScope = Field(default_factory=SupplyCaseScope)


class InventoryInput(OrganizationScopedRequest):
    part_number: str = Field(
        validation_alias=AliasChoices("partNumber", "part_number"), min_length=1, max_length=160
    )
    location_code: str = Field(
        validation_alias=AliasChoices("locationCode", "location_code"), min_length=1, max_length=80
    )
    on_hand_qty: Decimal | None = Field(
        default=None, validation_alias=AliasChoices("onHandQty", "on_hand_qty"), ge=0
    )
    reserved_qty: Decimal | None = Field(
        default=None, validation_alias=AliasChoices("reservedQty", "reserved_qty"), ge=0
    )
    in_transit_qty: Decimal | None = Field(
        default=None, validation_alias=AliasChoices("inTransitQty", "in_transit_qty"), ge=0
    )
    # 每次库存 upsert 都是一次有时间戳的观测；缺它就无法拒绝迟到旧快照。
    verified_at: datetime = Field(validation_alias=AliasChoices("verifiedAt", "verified_at"))


class DemandInput(OrganizationScopedRequest):
    source_type: str = Field(
        validation_alias=AliasChoices("sourceType", "source_type"),
        pattern="^(manual|customer_order|work_order|forecast)$",
    )
    source_reference: str = Field(
        validation_alias=AliasChoices("sourceReference", "source_reference"),
        min_length=1,
        max_length=160,
    )
    line_key: str = Field(
        validation_alias=AliasChoices("lineKey", "line_key"), min_length=1, max_length=160
    )
    part_number: str = Field(
        validation_alias=AliasChoices("partNumber", "part_number"), min_length=1, max_length=160
    )
    required_qty: Decimal | None = Field(
        default=None, validation_alias=AliasChoices("requiredQty", "required_qty"), ge=0
    )
    required_on: date | None = Field(
        default=None, validation_alias=AliasChoices("requiredOn", "required_on")
    )


class ReceiptInput(OrganizationScopedRequest):
    receipt_reference: str = Field(
        validation_alias=AliasChoices("receiptReference", "receipt_reference"),
        min_length=1,
        max_length=160,
    )
    # 一张到货单可有多行料件；reference 只标识单头，line_key 才标识事实行。
    line_key: str = Field(
        validation_alias=AliasChoices("lineKey", "line_key"), min_length=1, max_length=160
    )
    part_number: str = Field(
        validation_alias=AliasChoices("partNumber", "part_number"), min_length=1, max_length=160
    )
    expected_on: date | None = Field(
        default=None, validation_alias=AliasChoices("expectedOn", "expected_on")
    )
    quantity: Decimal | None = Field(default=None, ge=0)
    status: str = Field(pattern="^(in_transit|arrived|cancelled)$")


class BeginTraceRunRequest(OrganizationScopedRequest):
    """创建审计 Run；run_kind 由受限枚举控制，不能伪造 Worker 生命周期。"""

    run_kind: str = Field(
        validation_alias=AliasChoices("runKind", "run_kind"), pattern="^(manual|api|worker|replay)$"
    )
    case_id: UUID | None = Field(default=None, validation_alias=AliasChoices("caseId", "case_id"))


class BeginSupplyRunRequest(OrganizationScopedRequest):
    case_id: UUID = Field(validation_alias=AliasChoices("caseId", "case_id"))
    part_number: str = Field(validation_alias=AliasChoices("partNumber", "part_number"), min_length=1, max_length=160)
    idempotency_key: str = Field(validation_alias=AliasChoices("idempotencyKey", "idempotency_key"), min_length=1, max_length=120)
    model_policy_id: UUID | None = Field(default=None, validation_alias=AliasChoices("modelPolicyId", "model_policy_id"))


class BeginOpportunityRunRequest(OrganizationScopedRequest):
    opportunity_id: UUID = Field(validation_alias=AliasChoices("opportunityId", "opportunity_id"))
    idempotency_key: str = Field(
        validation_alias=AliasChoices("idempotencyKey", "idempotency_key"),
        min_length=1, max_length=120,
    )
    model_policy_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("modelPolicyId", "model_policy_id")
    )


class TraceArtifactInput(OrganizationScopedRequest):
    """仅接收调用方已经脱敏后的 checkpoint 或其他 Artifact 投影。"""

    run_id: UUID = Field(validation_alias=AliasChoices("runId", "run_id"))
    case_id: UUID | None = Field(default=None, validation_alias=AliasChoices("caseId", "case_id"))
    artifact_key: UUID = Field(validation_alias=AliasChoices("artifactKey", "artifact_key"))
    artifact_type: str = Field(
        validation_alias=AliasChoices("artifactType", "artifact_type"), min_length=1, max_length=96
    )
    redacted_summary: str = Field(
        validation_alias=AliasChoices("redactedSummary", "redacted_summary"),
        min_length=1,
        max_length=2000,
    )
    redacted_payload: dict[str, object] = Field(
        validation_alias=AliasChoices("redactedPayload", "redacted_payload")
    )


class TraceEventInput(OrganizationScopedRequest):
    """只追加脱敏事件；禁止浏览器把原始正文或凭证塞进审计库。"""

    run_id: UUID = Field(validation_alias=AliasChoices("runId", "run_id"))
    case_id: UUID | None = Field(default=None, validation_alias=AliasChoices("caseId", "case_id"))
    event_type: str = Field(
        validation_alias=AliasChoices("eventType", "event_type"), min_length=1, max_length=96
    )
    redacted_summary: str = Field(
        validation_alias=AliasChoices("redactedSummary", "redacted_summary"),
        min_length=1,
        max_length=2000,
    )
    metadata: dict[str, object] = Field(default_factory=dict)
    artifact_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("artifactId", "artifact_id")
    )


def create_app(
    auth_verifier: AuthVerifier,
    tenant_repository: TenantRepository,
    supply_repository: SupplyRepository | None = None,
    trace_repository: TraceRepository | None = None,
) -> FastAPI:
    """创建可注入认证与数据库适配器的产品 API。"""
    app = FastAPI(title="SupplyAgent Product API", version="0.5")
    supply = supply_repository or UnavailableSupplyRepository()
    trace = trace_repository or UnavailableTraceRepository()

    async def identity_from_request(request: Request) -> AuthenticatedIdentity:
        """只接受 bearer token，并由 AuthVerifier 建立身份事实。"""
        authorization = request.headers.get("authorization", "")
        scheme, _, access_token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not access_token:
            raise AuthenticationError("缺少访问令牌")
        return await auth_verifier.verify_access_token(access_token)

    async def repository() -> TenantRepository:
        """将闭包中的受控 repository 注入路由。"""
        return tenant_repository

    async def supply_repository_dependency() -> SupplyRepository:
        """领域 SQL 适配器不持有连接，只能接收 RLS 事务。"""
        return supply

    async def trace_repository_dependency() -> TraceRepository:
        """Trace SQL 只能运行于身份与组织都已经建立的事务中。"""
        return trace

    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(_: Request, error: AuthenticationError) -> JSONResponse:
        """所有认证失败使用稳定且不泄漏细节的错误形状。"""
        return _error_response(401, "unauthenticated", str(error))

    @app.exception_handler(TenantAccessError)
    async def tenant_access_error_handler(_: Request, error: TenantAccessError) -> JSONResponse:
        """成员关系校验失败不能被组织选择 cookie 绕过。"""
        return _error_response(403, "organization_forbidden", str(error))

    @app.exception_handler(TenantConfigurationError)
    async def tenant_configuration_error_handler(
        _: Request, error: TenantConfigurationError
    ) -> JSONResponse:
        """数据库未配置是服务不可用，不得伪造空业务结果。"""
        return _error_response(503, "product_database_unavailable", str(error))

    @app.exception_handler(SupplyAccessError)
    async def supply_access_error_handler(_: Request, error: SupplyAccessError) -> JSONResponse:
        """供应录入能力由数据库角色函数裁决。"""
        return _error_response(403, "supply_forbidden", str(error))

    @app.exception_handler(CapabilityAccessError)
    async def capability_access_error_handler(
        _: Request, error: CapabilityAccessError
    ) -> JSONResponse:
        """角色能力不足时在业务命令进入供应链 repository 前稳定拒绝。"""
        return _error_response(403, "capability_forbidden", str(error))

    @app.exception_handler(TraceAccessError)
    async def trace_access_error_handler(_: Request, error: TraceAccessError) -> JSONResponse:
        """游标、Run 与 Artifact 的越权统一返回安全的拒绝响应。"""
        return _error_response(403, "trace_forbidden", str(error))

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _: Request, error: RequestValidationError
    ) -> JSONResponse:
        """请求 schema 失败也使用统一错误包络，不泄漏框架默认形状。"""
        return _error_response(422, "validation_error", "请求格式无效", details=error.errors())

    @app.get("/health")
    async def health() -> dict[str, object]:
        """不触碰身份或数据库的进程健康检查。"""
        return {"ok": True, "data": {"status": "ok", "service": "supplyagent-api"}}

    @app.get("/v1/session")
    async def session(
        request: Request,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
    ) -> dict[str, object]:
        """返回认证身份及其成员关系；cookie 只是被重新核验的选择提示。"""
        organizations = await tenants.list_organizations(identity.user_id)
        selected_id = request.cookies.get(ORGANIZATION_COOKIE)
        selected = _find_selected(organizations, selected_id)
        return {
            "ok": True,
            "data": {
                "user": {"userId": str(identity.user_id), "email": identity.email},
                "organizations": [_membership_payload(item) for item in organizations],
                "selectedOrganization": _membership_payload(selected) if selected else None,
            },
        }

    @app.post("/v1/organizations/select")
    async def select_organization(
        selection: OrganizationSelection,
        response: Response,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
    ) -> dict[str, object]:
        """在租户事务中重验成员关系后写入 HttpOnly 组织选择 cookie。"""

        selected = await tenants.select_organization(identity.user_id, selection.organization_id)
        response.set_cookie(
            key=ORGANIZATION_COOKIE,
            value=str(selected.organization_id),
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=60 * 60 * 12,
        )
        return {"ok": True, "data": {"selectedOrganization": _membership_payload(selected)}}

    @app.get("/v1/supply/cases")
    async def list_supply_cases(
        organization_id: UUID,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        supplies: SupplyRepository = Depends(supply_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        """在重验成员关系后的 RLS 事务内读取当前组织的 Cases。"""
        cases = await tenants.in_organization(
            identity.user_id, organization_id, supplies.list_cases
        )
        return {"ok": True, "data": {"cases": [_case_payload(item) for item in cases]}}

    @app.get("/v1/supply/facts")
    async def get_supply_facts(
        organization_id: UUID,
        part_number: str,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        supplies: SupplyRepository = Depends(supply_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        """读取一个料号的原始事实，不把未知值转换为零。"""
        facts = await tenants.in_organization(
            identity.user_id,
            organization_id,
            lambda connection: supplies.load_facts(connection, part_number),
        )
        return {"ok": True, "data": _facts_payload(facts)}

    @app.get("/v1/supply/impact")
    async def get_supply_impact(
        organization_id: UUID,
        part_number: str,
        as_of: date | None = None,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        supplies: SupplyRepository = Depends(supply_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        """按固定日期顺序投影库存；没有外部调用、LLM 或采购副作用。"""
        facts = await tenants.in_organization(
            identity.user_id,
            organization_id,
            lambda connection: supplies.load_facts(connection, part_number),
        )
        impact = calculate_impact(facts, part_number, as_of or datetime.now(UTC).date())
        return {"ok": True, "data": _impact_payload(impact)}

    @app.post("/v1/supply/cases")
    async def create_supply_case(
        payload: CreateSupplyCaseRequest,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        supplies: SupplyRepository = Depends(supply_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        """Case 录入先经过角色 capability，再进入成员级 RLS 事务。"""
        values = payload.model_dump(exclude={"organization_id", "scope"})
        values["scope"] = payload.scope.model_dump(by_alias=True)
        case_id = await _run_supply_write(
            identity.user_id,
            payload.organization_id,
            tenants,
            lambda connection: supplies.create_case(connection, **values),
        )
        return {"ok": True, "data": {"caseId": str(case_id)}}

    @app.put("/v1/supply/facts/inventory")
    async def put_inventory(
        payload: InventoryInput,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        supplies: SupplyRepository = Depends(supply_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        position_id = await _run_supply_write(
            identity.user_id,
            payload.organization_id,
            tenants,
            lambda connection: supplies.upsert_inventory(
                connection, **payload.model_dump(exclude={"organization_id"})
            ),
        )
        return {"ok": True, "data": {"inventoryPositionId": str(position_id)}}

    @app.put("/v1/supply/facts/demands")
    async def put_demand(
        payload: DemandInput,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        supplies: SupplyRepository = Depends(supply_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        demand_id = await _run_supply_write(
            identity.user_id,
            payload.organization_id,
            tenants,
            lambda connection: supplies.upsert_demand(
                connection, **payload.model_dump(exclude={"organization_id"})
            ),
        )
        return {"ok": True, "data": {"demandId": str(demand_id)}}

    @app.put("/v1/supply/facts/receipts")
    async def put_receipt(
        payload: ReceiptInput,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        supplies: SupplyRepository = Depends(supply_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        receipt_id = await _run_supply_write(
            identity.user_id,
            payload.organization_id,
            tenants,
            lambda connection: supplies.upsert_receipt(
                connection, **payload.model_dump(exclude={"organization_id"})
            ),
        )
        return {"ok": True, "data": {"receiptId": str(receipt_id)}}

    @app.post("/v1/traces/runs")
    async def begin_trace_run(
        payload: BeginTraceRunRequest,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        traces: TraceRepository = Depends(trace_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        """创建权威 SQL Run；浏览器从不直接插入 trace_run。"""
        run_id = await tenants.in_organization(
            identity.user_id,
            payload.organization_id,
            lambda connection: traces.begin_run(connection, payload.run_kind, payload.case_id),
        )
        return {"ok": True, "data": {"runId": str(run_id)}}

    @app.post("/v1/runtime/supply-runs")
    async def begin_supply_run(
        payload: BeginSupplyRunRequest,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
    ) -> dict[str, object]:
        async def create(
            connection: AsyncConnection[dict[str, object]], membership: OrganizationMembership,
        ) -> tuple[UUID, UUID, UUID]:
            require_capability(membership.role, "supply:read")
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "select * from app.begin_supply_runtime_run(%s,%s,%s,%s)",
                    (payload.case_id, payload.part_number, payload.idempotency_key, payload.model_policy_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("runtime run creation returned no IDs")
                return row["mission_id"], row["run_id"], row["job_id"]

        mission_id, run_id, job_id = await tenants.in_organization_as_member(
            identity.user_id, payload.organization_id, create,
        )
        return {"ok": True, "data": {
            "missionId": str(mission_id), "runId": str(run_id), "jobId": str(job_id),
        }}

    @app.post("/v1/runtime/opportunity-runs")
    async def begin_opportunity_run(
        payload: BeginOpportunityRunRequest,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
    ) -> dict[str, object]:
        async def create(
            connection: AsyncConnection[dict[str, object]], membership: OrganizationMembership,
        ) -> tuple[UUID, UUID, UUID]:
            require_capability(membership.role, "crm:read")
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "select * from app.begin_opportunity_runtime_run(%s,%s,%s)",
                    (payload.opportunity_id, payload.idempotency_key, payload.model_policy_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("opportunity runtime run creation returned no IDs")
                return row["mission_id"], row["run_id"], row["job_id"]

        mission_id, run_id, job_id = await tenants.in_organization_as_member(
            identity.user_id, payload.organization_id, create,
        )
        return {"ok": True, "data": {
            "missionId": str(mission_id), "runId": str(run_id), "jobId": str(job_id),
        }}

    @app.post("/v1/runtime/jobs/{job_id}/cancel")
    async def cancel_runtime_job(
        job_id: UUID, payload: OrganizationScopedRequest,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
    ) -> dict[str, object]:
        async def cancel(
            connection: AsyncConnection[dict[str, object]], membership: OrganizationMembership,
        ) -> bool:
            require_capability(membership.role, "supply:propose")
            async with connection.cursor() as cursor:
                await cursor.execute("select app.cancel_runtime_job(%s) as cancelled", (job_id,))
                row = await cursor.fetchone()
                return bool(row and row["cancelled"])

        cancelled = await tenants.in_organization_as_member(
            identity.user_id, payload.organization_id, cancel,
        )
        return {"ok": True, "data": {"cancelled": cancelled}}

    @app.post("/v1/traces/artifacts")
    async def append_trace_artifact(
        payload: TraceArtifactInput,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        traces: TraceRepository = Depends(trace_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        """追加版本化 Artifact；其完整内容指纹由 SQL 权威计算。"""
        _assert_redacted(payload.redacted_summary, payload.redacted_payload)
        artifact_id = await tenants.in_organization(
            identity.user_id,
            payload.organization_id,
            lambda connection: traces.append_artifact(
                connection,
                payload.run_id,
                payload.case_id,
                payload.artifact_key,
                payload.artifact_type,
                payload.redacted_summary,
                payload.redacted_payload,
            ),
        )
        return {"ok": True, "data": {"artifactId": str(artifact_id)}}

    @app.post("/v1/traces/events")
    async def append_trace_event(
        payload: TraceEventInput,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        traces: TraceRepository = Depends(trace_repository_dependency),  # noqa: B008
    ) -> dict[str, object]:
        """追加一条脱敏事件；哈希链序列号由 SQL 在锁定的 Run 上分配。"""
        _assert_redacted(payload.redacted_summary, payload.metadata)
        event_id = await tenants.in_organization(
            identity.user_id,
            payload.organization_id,
            lambda connection: traces.append_event(
                connection,
                payload.run_id,
                payload.case_id,
                payload.event_type,
                payload.redacted_summary,
                payload.metadata,
                payload.artifact_id,
            ),
        )
        return {"ok": True, "data": {"eventId": str(event_id)}}
    # SSE事件回放
    @app.get("/v1/traces/runs/{run_id}/events")
    async def stream_trace_events(
        run_id: UUID,
        organization_id: UUID,
        request: Request,
        after_event_id: UUID | None = None,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
        traces: TraceRepository = Depends(trace_repository_dependency),  # noqa: B008
    ) -> StreamingResponse:
        """有限 SSE 回放：Last-Event-ID 优先，连接结束后由 F33 负责实时订阅。"""
        cursor = _sse_cursor(request, after_event_id)
        events = await tenants.in_organization(
            identity.user_id,
            organization_id,
            lambda connection: _read_trace_events(traces, connection, run_id, cursor),
        )
        return StreamingResponse(
            (_sse_event(item) for item in events),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    return app


async def _run_supply_write(
    actor_id: UUID,
    organization_id: UUID,
    tenants: TenantRepository,
    operation: Callable[[AsyncConnection[dict[str, object]]], Awaitable[UUID]],
) -> UUID:
    """在同一 RLS 事务里读取角色并检查写入 capability，避免检查-写入间隙。"""

    async def authorized_operation(
        connection: AsyncConnection[dict[str, object]], membership: OrganizationMembership
    ) -> UUID:
        require_capability(membership.role, "supply:propose")
        return await operation(connection)

    return await tenants.in_organization_as_member(actor_id, organization_id, authorized_operation)


def create_default_app() -> FastAPI:
    """从环境装配默认适配器；缺配置时认证安全失败。"""
    database_url = getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    auth_url = getenv("SUPPLYAGENT_SUPABASE_AUTH_URL")
    publishable_key = getenv("SUPPLYAGENT_SUPABASE_PUBLISHABLE_KEY")
    verifier: AuthVerifier = (
        SupabaseAuthVerifier(auth_url, publishable_key)
        if auth_url and publishable_key
        else UnavailableAuthVerifier()
    )
    repository: TenantRepository = (
        PostgresTenantRepository(database_url) if database_url else UnavailableTenantRepository()
    )
    return create_app(verifier, repository, PostgresSupplyRepository(), PostgresTraceRepository())


def _membership_payload(membership: OrganizationMembership) -> dict[str, object]:
    """把内部领域对象转换为稳定的 camelCase HTTP 数据。"""
    payload = asdict(membership)
    capabilities = effective_capabilities(membership.role) if is_known_role(membership.role) else []
    return {
        "organizationId": str(payload["organization_id"]),
        "slug": payload["slug"],
        "displayName": payload["display_name"],
        "role": payload["role"],
        "capabilities": capabilities,
        "scope": payload["scope"],
    }


def _find_selected(
    organizations: list[OrganizationMembership], selected_id: str | None
) -> OrganizationMembership | None:
    """仅从认证用户已经枚举出的成员关系中解析 cookie 选择。"""
    return next((item for item in organizations if str(item.organization_id) == selected_id), None)


def _case_payload(item: SupplyCase) -> dict[str, object]:
    """把领域 Case 变成稳定的 HTTP 命名与时间格式。"""
    return {
        "caseId": str(item.id),
        "title": item.title,
        "status": item.status,
        "severity": item.severity,
        "scope": item.scope,
        "openedAt": item.opened_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }


def _facts_payload(facts: SupplyFacts) -> dict[str, object]:
    """事实 API 保留空值：null 表示尚未得到事实。"""
    return {
        "inventory": [_inventory_payload(item) for item in facts.inventory],
        "demands": [
            {
                "demandId": str(item.id),
                "sourceType": item.source_type,
                "sourceReference": item.source_reference,
                "lineKey": item.line_key,
                "partNumber": item.part_number,
                "requiredQty": _decimal_payload(item.required_qty),
                "requiredOn": item.required_on.isoformat() if item.required_on else None,
            }
            for item in facts.demands
        ],
        "receipts": [
            {
                "receiptId": str(item.id),
                "receiptReference": item.receipt_reference,
                "lineKey": item.line_key,
                "partNumber": item.part_number,
                "expectedOn": item.expected_on.isoformat() if item.expected_on else None,
                "quantity": _decimal_payload(item.quantity),
                "status": item.status,
            }
            for item in facts.receipts
        ],
    }


def _inventory_payload(item: InventoryPosition) -> dict[str, object]:
    return {
        "inventoryPositionId": str(item.id),
        "partNumber": item.part_number,
        "locationCode": item.location_code,
        "onHandQty": _decimal_payload(item.on_hand_qty),
        "reservedQty": _decimal_payload(item.reserved_qty),
        "inTransitQty": _decimal_payload(item.in_transit_qty),
        "verifiedAt": item.verified_at.isoformat() if item.verified_at else None,
    }


def _impact_payload(impact: SupplyImpact) -> dict[str, object]:
    return {
        "partNumber": impact.part_number,
        "asOf": impact.as_of.isoformat(),
        "openingAvailableQty": _decimal_payload(impact.opening_available_qty),
        "projectedAvailableQty": _decimal_payload(impact.projected_available_qty),
        "firstShortageOn": impact.first_shortage_on.isoformat()
        if impact.first_shortage_on
        else None,
        "timeline": [
            {
                "on": item.on.isoformat(),
                "kind": item.kind,
                "reference": item.reference,
                "quantity": _decimal_payload(item.quantity),
                "projectedAvailableQty": _decimal_payload(item.projected_available_qty),
            }
            for item in impact.timeline
        ],
        "unknowns": impact.unknowns,
    }


def _decimal_payload(value: Decimal | None) -> str | None:
    """用字符串跨越 JSON，避免二进制浮点数改变采购数量。"""
    return str(value) if value is not None else None


async def _read_trace_events(
    traces: TraceRepository,
    connection: AsyncConnection[dict[str, object]],
    run_id: UUID,
    after_event_id: UUID | None,
) -> list[TraceEvent]:
    """先确认 Run 在当前 RLS 视野内，再接受其事件游标。"""
    if await traces.get_run(connection, run_id) is None:
        raise TraceAccessError("Run 不属于当前组织")
    return await traces.list_events(connection, run_id, after_event_id)


def _sse_cursor(request: Request, after_event_id: UUID | None) -> UUID | None:
    """浏览器标准 Last-Event-ID 可覆盖 query；非法 UUID 由请求校验统一拒绝。"""
    header = request.headers.get("last-event-id")
    return UUID(header) if header else after_event_id


def _sse_event(event: TraceEvent) -> str:
    """SSE id 使用不可猜测的 Event UUID，data 只包含已脱敏投影。"""
    payload = {
        "eventId": str(event.id),
        "runId": str(event.run_id),
        "sequenceNo": event.sequence_no,
        "eventType": event.event_type,
        "redactedSummary": event.redacted_summary,
        "metadata": event.metadata,
        "artifactId": str(event.artifact_id) if event.artifact_id else None,
        "eventHash": event.event_hash,
        "occurredAt": event.occurred_at.isoformat(),
    }
    return f"id: {event.id}\nevent: trace\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _assert_redacted(summary: str, payload: dict[str, object]) -> None:
    """阻止最常见的凭证、原始正文和客户标识字段进入权威审计库。

    这不是 DLP 的替代品；F36/F37 仍需在知识和渠道边界实施更细的脱敏策略。
    """
    blocked_keys = {
        "authorization",
        "cookie",
        "credential",
        "email",
        "message",
        "password",
        "secret",
        "token",
    }
    if any(marker in summary.lower() for marker in ("sk-", "bearer ", "eyj")):
        raise TraceAccessError("Trace 摘要疑似含有凭证")

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in blocked_keys:
                    raise TraceAccessError(f"Trace 不允许字段：{key}")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and any(
            marker in value.lower() for marker in ("sk-", "bearer ", "eyj")
        ):
            raise TraceAccessError("Trace 数据疑似含有凭证")

    walk(payload)


def _error_response(
    status_code: int, code: str, message: str, details: object | None = None
) -> JSONResponse:
    """提供 API 全局一致的错误包络。"""
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"ok": False, "error": error})


"""
1. 用户注册登录，拿到专属 token（Supabase 签发）
2. 前端调 GET /v1/session
   → 后端验 token 得 identity，查已核验组织列表，读 cookie 对质
   → selectedOrganization: null
3. 前端看到 null → 弹出"选择组织"界面
4. 用户选了组织A → 前端发 POST /v1/organizations/select
   headers: Authorization: Bearer <token>     ← 他是谁（已核验事实的来源）
   body: {"organizationId": "A的id"}          ← 他想要哪（待核验愿望）
5. FastAPI 注入四件套：
   identity  ← 先验后注入（Depends：问 Supabase）
   tenants   ← 纯递入（Depends：仓库对象，不验证）
   selection ← body 解析+校验
   response  ← 白给的响应编辑器
6. 函数体：tenants.select_organization(张三id, A的id)
   → 开事务 → set local role supplyagent_api → 登记 RLS 上下文
   → 库里查"张三×A"成员关系
   ├─ 不是成员 → TenantAccessError → 403，cookie 不种
   └─ 是成员 → 返回卡片 selected
7. response.set_cookie("supplyagent_organization", A的id, 四道护栏, 12小时)
8. 返回 {"ok":true, "data":{"selectedOrganization": 驼峰卡片}}
9. 浏览器：存 cookie（JS 读不到）+ 更新界面
10. 之后再调 session，第2步的 null 变成 A 的卡片 → 闭环


"""

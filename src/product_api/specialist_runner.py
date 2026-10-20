"""F36 SpecialistAssignment 到 F42 AgentEnginePort 的有界执行与结果解码。"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from uuid import uuid4

from product_api.harness import (
    AgentEnginePort,
    Harness,
    HarnessContext,
    HarnessToolError,
    HarnessTurnError,
    ToolDefinition,
    ToolRegistry,
    TraceSink,
)
from product_api.specialists import (
    Citation,
    Claim,
    ClaimQuality,
    SpecialistAssignment,
    SpecialistResult,
)

MAX_SPECIALIST_OUTPUT_BYTES = 16_384
CitationValidator = Callable[[Citation], bool | Awaitable[bool]]


class SpecialistResultError(ValueError):
    """执行引擎没有返回符合 F36 Contract 的结构化结果。"""


class SpecialistRunner:
    """强制 assignment scope、工具白名单和 citation 校验的单 turn 运行器。"""

    def __init__(
        self,
        engine: AgentEnginePort,
        tools: tuple[ToolDefinition, ...],
        trace: TraceSink,
        citation_validator: CitationValidator,
    ) -> None:
        self._engine = engine
        self._tools = tools
        self._trace = trace
        self._citation_validator = citation_validator

    async def run(
        self,
        assignment: SpecialistAssignment,
        cancel_event: asyncio.Event | None = None,
    ) -> SpecialistResult:
        registry = ToolRegistry(self._tools)
        if set(registry.names) != set(assignment.allowed_tools):
            raise SpecialistResultError("实际工具集合必须与 SpecialistAssignment 完全一致")
        context = HarnessContext(
            organization_id=assignment.organization_id,
            run_id=assignment.mission_id,
            case_id=None,
            task=assignment.task,
            fact_references=tuple(f"artifact:{item}" for item in assignment.input_artifact_ids),
        )
        turn = await Harness(self._engine, registry).execute(
            context, self._trace, cancel_event
        )
        if turn.status == "cancelled":
            raise HarnessTurnError("cancelled")
        if turn.status != "completed" or turn.output is None:
            raise SpecialistResultError("Specialist turn 未完成或没有结构化 output")
        encoded = json.dumps(turn.output, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_SPECIALIST_OUTPUT_BYTES:
            raise SpecialistResultError("Specialist output 超过 16 KiB")
        return await self._decode(assignment, turn.output)

    async def _decode(
        self,
        assignment: SpecialistAssignment,
        output: Mapping[str, object],
    ) -> SpecialistResult:
        if set(output) != {"role", "complete", "claims"}:
            raise SpecialistResultError("Specialist output 字段不完整或包含未知字段")
        if output["role"] != assignment.role.value or not isinstance(output["complete"], bool):
            raise SpecialistResultError("Specialist output 角色或完成状态非法")
        raw_claims = output["claims"]
        if not isinstance(raw_claims, list) or not 1 <= len(raw_claims) <= 32:
            raise SpecialistResultError("Specialist 必须返回 1–32 个 Claim")
        claims = tuple(
            [await self._decode_claim(assignment, raw_claim) for raw_claim in raw_claims]
        )
        return SpecialistResult(
            artifact_id=uuid4(),
            organization_id=assignment.organization_id,
            mission_id=assignment.mission_id,
            role=assignment.role,
            claims=claims,
            complete=output["complete"],
        )

    async def _decode_claim(
        self,
        assignment: SpecialistAssignment,
        raw_claim: object,
    ) -> Claim:
        if not isinstance(raw_claim, dict) or set(raw_claim) != {
            "subject",
            "claimType",
            "conclusion",
            "quality",
            "citations",
            "unknowns",
            "conflicts",
        }:
            raise SpecialistResultError("Claim 字段不完整或包含未知字段")
        try:
            quality = ClaimQuality(str(raw_claim["quality"]))
        except ValueError as error:
            raise SpecialistResultError("Claim quality 非法") from error
        citations = await self._decode_citations(raw_claim["citations"])
        unknowns = self._string_tuple(raw_claim["unknowns"], "unknowns")
        conflicts = self._string_tuple(raw_claim["conflicts"], "conflicts")
        if citations and not all(await asyncio.gather(*(self._valid(item) for item in citations))):
            quality = ClaimQuality.REJECTED
            unknowns = (*unknowns, "citation_invalid")
        return Claim(
            claim_id=uuid4(),
            organization_id=assignment.organization_id,
            mission_id=assignment.mission_id,
            subject=self._string(raw_claim["subject"], "subject"),
            claim_type=self._string(raw_claim["claimType"], "claimType"),
            conclusion=self._string(raw_claim["conclusion"], "conclusion"),
            produced_by=assignment.role,
            quality=quality,
            citations=citations,
            unknowns=unknowns,
            conflicts=conflicts,
        )

    async def _decode_citations(self, raw_citations: object) -> tuple[Citation, ...]:
        if not isinstance(raw_citations, list) or len(raw_citations) > 16:
            raise SpecialistResultError("citations 必须是最多 16 条的数组")
        citations: list[Citation] = []
        for raw in raw_citations:
            if not isinstance(raw, dict) or set(raw) != {
                "sourceKind",
                "sourceId",
                "version",
                "locator",
                "fragmentHash",
            }:
                raise SpecialistResultError("Citation 字段不完整或包含未知字段")
            citations.append(
                Citation(
                    source_kind=self._string(raw["sourceKind"], "sourceKind"),
                    source_id=self._string(raw["sourceId"], "sourceId"),
                    version=self._string(raw["version"], "version"),
                    locator=self._string(raw["locator"], "locator"),
                    fragment_hash=self._string(raw["fragmentHash"], "fragmentHash"),
                )
            )
        return tuple(citations)

    async def _valid(self, citation: Citation) -> bool:
        result = self._citation_validator(citation)
        return bool(await result) if inspect.isawaitable(result) else bool(result)

    @staticmethod
    def _string(value: object, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise SpecialistResultError(f"{field} 必须是非空字符串")
        return value

    @classmethod
    def _string_tuple(cls, value: object, field: str) -> tuple[str, ...]:
        if not isinstance(value, list) or len(value) > 32:
            raise SpecialistResultError(f"{field} 必须是最多 32 条的数组")
        return tuple(cls._string(item, field) for item in value)


def validate_knowledge_search(arguments: dict[str, object]) -> None:
    """F36 knowledge.search 的固定 R0 参数 schema。"""
    if set(arguments) - {"query", "limit"}:
        raise HarnessToolError("knowledge.search 参数不在固定 schema 内")
    if not isinstance(arguments.get("query"), str) or not arguments["query"]:
        raise HarnessToolError("knowledge.search.query 必须是非空字符串")
    if "limit" in arguments and (
        isinstance(arguments["limit"], bool)
        or not isinstance(arguments["limit"], int)
        or not 1 <= arguments["limit"] <= 20
    ):
        raise HarnessToolError("knowledge.search.limit 必须是 1–20")


def validate_no_arguments(arguments: dict[str, object]) -> None:
    """服务端已绑定 scope 的 R0 工具不接受模型补充标识。"""
    if arguments:
        raise HarnessToolError("该工具不接受模型参数")

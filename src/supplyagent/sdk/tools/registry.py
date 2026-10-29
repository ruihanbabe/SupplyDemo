"""F51 P1: trusted declarations and immutable, task-scoped tool bindings."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ALIAS_VERSION = 1
BINDING_VERSION = 1
_NAME = re.compile(r"[a-z][a-z0-9_.-]{1,95}")
_WIRE_NAME = re.compile(r"[a-z][a-z0-9_]{1,63}")


class ToolUnavailableError(ValueError):
    """Resolution is restricted to a binding, without revealing the catalog."""


class ToolInputError(ValueError):
    """Arguments do not satisfy the registered input schema."""


class NoArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SupplyImpactArguments(NoArguments):
    partNumber: str = Field(min_length=1)
    asOf: str = ""


class KnowledgeSearchArguments(NoArguments):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=20)


@dataclass(frozen=True)
class ToolResource:
    name: str
    scope: str = "organization"
    units: int = 1

    def __post_init__(self):
        if (
            not _NAME.fullmatch(self.name)
            or self.scope not in {"organization", "deployment"}
            or type(self.units) is not int
            or self.units < 1
        ):
            raise ValueError("invalid tool resource")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    wire_name: str
    description: str
    input_model: type[BaseModel]
    aliases: tuple[str, ...] = ()
    version: int = 1
    risk_level: str = "r0"
    side_effect: str = "read_only"
    overlap: bool = False
    exclusive_resources: tuple[str, ...] = ()
    resources: tuple[ToolResource, ...] = ()
    compatibility: Callable | None = None

    def compatible(self, arguments, other, other_arguments):
        return (
            self.overlap
            and other.overlap
            and not set(self.exclusive_resources).intersection(other.exclusive_resources)
            and (
                self.compatibility is None or self.compatibility(arguments, other, other_arguments)
            )
        )

    @property
    def scheduling(self):
        return {
            "overlap": self.overlap,
            "exclusiveResources": list(self.exclusive_resources),
            "resources": [
                {"name": r.name, "scope": r.scope, "units": r.units} for r in self.resources
            ],
        }

    def __post_init__(self) -> None:
        if (
            not _NAME.fullmatch(self.name)
            or not _WIRE_NAME.fullmatch(self.wire_name)
            or any(not _NAME.fullmatch(alias) for alias in self.aliases)
            or not isinstance(self.aliases, tuple)
            or not 1 <= len(self.description) <= 1000
            or type(self.version) is not int
            or self.version < 1
        ):
            raise ValueError("invalid tool declaration")
        if (
            type(self.overlap) is not bool
            or not isinstance(self.resources, tuple)
            or not isinstance(self.exclusive_resources, tuple)
            or any(not isinstance(r, ToolResource) for r in self.resources)
        ):
            raise ValueError("invalid execution declaration")
        if len({(r.name, r.scope) for r in self.resources}) != len(self.resources):
            raise ValueError("duplicate resource declaration")
        if self.risk_level != "r0" or self.side_effect != "read_only":
            raise ValueError("only read-only R0 tool declarations are enabled")
        if (
            self.input_model.model_config.get("strict") is not True
            or self.input_model.model_config.get("extra") != "forbid"
        ):
            raise ValueError("tool input model must be strict and forbid extra fields")

    @property
    def schema(self) -> dict[str, object]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        for field in schema.get("properties", {}).values():
            field.pop("title", None)
            field.pop("default", None)
        return schema

    def validate(self, arguments: object) -> None:
        if not isinstance(arguments, dict):
            raise ToolInputError("工具参数不在固定 schema 内")
        try:
            self.input_model.model_validate(arguments)
        except ValidationError:
            raise ToolInputError("工具参数不在固定 schema 内") from None


@dataclass(frozen=True)
class ToolBinding:
    specs: tuple[ToolSpec, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)

    def resolve(self, name: str) -> ToolSpec:
        for spec in self.specs:
            if name in (spec.name, spec.wire_name, *spec.aliases):
                return spec
        raise ToolUnavailableError("工具不可用")

    def project(self, *, wire_names: bool = False) -> list[dict[str, object]]:
        return [
            {
                "name": spec.name,
                **({"wireName": spec.wire_name} if wire_names else {}),
                "description": spec.description,
                "inputSchema": spec.schema,
                "specVersion": spec.version,
            }
            for spec in self.specs
        ]

    @property
    def snapshot(self) -> dict[str, object]:
        projection = self.project(wire_names=True)
        digest = hashlib.sha256(
            json.dumps(
                {"tools": projection, "scheduling": [s.scheduling for s in self.specs]},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return {
            "bindingVersion": BINDING_VERSION,
            "bindingHash": digest,
            "toolAliasVersion": ALIAS_VERSION,
            "toolSpecs": {spec.name: spec.version for spec in self.specs},
        }


class ToolRegistry:
    """Catalog declarations only; handlers and execution remain in the Harness."""

    def __init__(self, specs: tuple[ToolSpec, ...]) -> None:
        if not 1 <= len(specs) <= 16:
            raise ValueError("tool catalog must contain 1–16 declarations")
        tokens: set[str] = set()
        for spec in specs:
            names = (spec.name, spec.wire_name, *spec.aliases)
            if len(set(names)) != len(names) or tokens.intersection(names):
                raise ValueError("tool names and aliases must not conflict")
            tokens.update(names)
        self._specs = MappingProxyType({spec.name: spec for spec in specs})
        self.aliases = MappingProxyType({spec.name: spec.wire_name for spec in specs})

    def bind(self, names: tuple[str, ...]) -> ToolBinding:
        if not 1 <= len(names) <= 16 or len(set(names)) != len(names):
            raise ValueError("binding requires unique canonical tool names")
        if any(name not in self._specs for name in names):
            raise ToolUnavailableError("工具不可用")
        return ToolBinding(tuple(self._specs[name] for name in names))


TOOL_REGISTRY = ToolRegistry(
    (
        ToolSpec(
            "supply.impact",
            "supply_impact",
            "Read the deterministic impact for the server-bound supply case and part; no execution or delivery commitment.",
            SupplyImpactArguments,
            overlap=True,
        ),
        ToolSpec(
            "knowledge.search",
            "knowledge_search",
            "Search currently authorized knowledge by query and return versioned evidence; no visible hit does not prove absence.",
            KnowledgeSearchArguments,
            overlap=True,
        ),
        ToolSpec(
            "crm.opportunity_facts",
            "crm_opportunity_facts",
            "Read current CRM facts and follow-up signals for the server-bound opportunity as the requesting user.",
            NoArguments,
            overlap=True,
        ),
        ToolSpec(
            "knowledge.question_evidence",
            "knowledge_question_evidence",
            "Retrieve authorized knowledge evidence for the bound question; preserve unknowns and source versions.",
            NoArguments,
            overlap=True,
        ),
        ToolSpec(
            "supply.bound_impact",
            "supply_bound_impact",
            "Read deterministic supply risk within the bound opportunity, case and BOM scope; no production scheduling.",
            NoArguments,
            overlap=True,
        ),
        ToolSpec(
            "commercial.quote_validity",
            "commercial_quote_validity",
            "Read quote validity and applicability for the bound opportunity and BOM; a quote is not reserved stock or confirmed arrival.",
            NoArguments,
            overlap=True,
        ),
        ToolSpec(
            "mission.claims",
            "mission_claims",
            "Read the explicitly assigned predecessor claims and their evidence; no new business authorization.",
            NoArguments,
            overlap=True,
        ),
    )
)

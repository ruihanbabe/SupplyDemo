"""F51 P1 catalog/binding boundaries and real Pi schema projection."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace

import pytest
from test_product_api_gateway import run_pi_fixture
from test_product_api_glm import NativeGlmFixture

from product_api.gateway import ReplayResponsesProvider
from product_api.harness import HarnessToolError, ToolDefinition
from product_api.harness import ToolRegistry as ExecutableTools
from product_api.model_tools import ALIASES
from product_api.specialist_runner import validate_no_arguments
from product_api.tool_registry import (
    TOOL_REGISTRY,
    NoArguments,
    ToolInputError,
    ToolRegistry,
    ToolSpec,
    ToolUnavailableError,
)


def _spec(name="test.read", wire="test_read", **kwargs):
    return ToolSpec(name, wire, "A test read tool.", NoArguments, **kwargs)


@pytest.mark.parametrize(
    "specs",
    [
        (_spec(), _spec()),
        (_spec(), _spec("test.other", "test_read")),
        (_spec(aliases=("test.other",)), _spec("test.other", "test_other")),
        (_spec(), _spec("test.other", "test_other", aliases=("test.read",))),
        (_spec(aliases=("test_read",)),),
    ],
)
def test_registry_rejects_collisions_in_either_direction(specs):
    with pytest.raises(ValueError, match="conflict"):
        ToolRegistry(specs)


def test_binding_resolves_exact_aliases_without_exposing_unbound_tools():
    binding = TOOL_REGISTRY.bind(("commercial.quote_validity",))
    assert binding.resolve("commercial_quote_validity").name == "commercial.quote_validity"
    with pytest.raises(ToolUnavailableError, match="^工具不可用$"):
        binding.resolve("knowledge_search")
    with pytest.raises(ToolUnavailableError, match="^工具不可用$"):
        binding.resolve("commercial_quote_validit")
    assert len(binding.project(wire_names=True)) == 1
    assert "knowledge" not in str(binding.project(wire_names=True))
    with pytest.raises(FrozenInstanceError):
        binding.specs = ()
    projection = binding.project()
    projection[0]["inputSchema"]["additionalProperties"] = True
    assert binding.project()[0]["inputSchema"]["additionalProperties"] is False
    assert binding.snapshot == TOOL_REGISTRY.bind(binding.names).snapshot
    assert ALIASES == {
        spec.name: spec.wire_name for spec in TOOL_REGISTRY.bind(tuple(ALIASES)).specs
    }
    explicit = ToolRegistry((_spec(aliases=("test.alias",)),)).bind(("test.read",))
    assert explicit.resolve("test.alias").name == "test.read"


@pytest.mark.parametrize(
    "names", [(), ("knowledge.search", "knowledge.search"), ("knowledge_search",)]
)
def test_bind_requires_available_unique_canonical_names(names):
    with pytest.raises(ValueError):
        TOOL_REGISTRY.bind(names)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("knowledge.search", {"query": "part", "limit": True}),
        ("knowledge.search", {"query": "part", "limit": "5"}),
        ("knowledge.search", {"query": "part", "limit": 21}),
        ("knowledge.search", {"query": ""}),
        ("supply.impact", {"partNumber": 42}),
        ("supply.impact", {"partNumber": "part", "asOf": None}),
        ("crm.opportunity_facts", {"organizationId": "model-supplied"}),
    ],
)
def test_schema_refuses_coercion_and_scope_injection(name, arguments):
    spec = TOOL_REGISTRY.bind((name,)).resolve(name)
    with pytest.raises(ToolInputError):
        spec.validate(arguments)


def test_schema_validation_and_alias_resolution_run_before_handler():
    async def scenario():
        received = []
        tools = ExecutableTools(
            (ToolDefinition("supply.impact", lambda args: received.append(args)),)
        )
        with pytest.raises(HarnessToolError):
            await tools.execute("supply_impact", {"partNumber": "part", "extra": True})
        with pytest.raises(HarnessToolError):
            await tools.execute("knowledge_search", {"query": "part"})
        assert received == []
        arguments = {"partNumber": "part"}
        await tools.execute("supply_impact", arguments)
        assert received == [arguments] and "asOf" not in received[0]

    asyncio.run(scenario())


def test_spec_version_changes_binding_hash_and_risk_is_not_model_configurable():
    first = ToolRegistry((_spec(),)).bind(("test.read",))
    second = ToolRegistry((replace(_spec(), version=2),)).bind(("test.read",))
    assert first.snapshot["bindingHash"] != second.snapshot["bindingHash"]
    with pytest.raises(ValueError, match="R0"):
        _spec(risk_level="r2")


@pytest.mark.parametrize("glm", [False, True])
def test_actual_pi_projects_only_bound_schema_and_description_to_provider(glm):
    async def scenario():
        name = "commercial.quote_validity"
        spec = TOOL_REGISTRY.bind((name,)).resolve(name)
        received = []
        inner = NativeGlmFixture() if glm else ReplayResponsesProvider()

        class Provider:
            async def stream(self, policy, payload):
                received.append(payload)
                async for event in inner.stream(policy, payload):
                    yield event

        result, _, _ = await run_pi_fixture(
            Provider(),
            ToolDefinition(
                name,
                lambda _: {
                    "citation": {
                        "sourceKind": "intelligence_observation",
                        "sourceId": "fixture",
                        "version": "1",
                        "locator": "payload",
                        "fragmentHash": "a" * 64,
                    }
                },
                validate_no_arguments,
            ),
            '{"role":"quote_validity"}',
            glm=glm,
        )
        assert result.status == "completed" and result.tool_calls == (name,)
        assert len(received) == 2
        for payload in received:
            assert len(payload["tools"]) == 1
            tool = payload["tools"][0]["function"] if glm else payload["tools"][0]
            assert tool["name"] == spec.wire_name
            assert tool["description"] == spec.description
            assert tool["parameters"] == spec.schema

    asyncio.run(scenario())

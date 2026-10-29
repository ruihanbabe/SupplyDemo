"""F41 human proposal inputs; model engines never receive an execution command."""

from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: UUID = Field(validation_alias=AliasChoices("organizationId", "organization_id"))


class CustomerChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str = Field(min_length=1, max_length=160)


class ProposalInput(ActionRequest):
    idempotency_key: str | None = Field(default=None, alias="idempotencyKey", min_length=1, max_length=120)
    action_kind: Literal["customer_update"] = Field(alias="actionKind")
    target_id: UUID = Field(alias="targetId")
    target_version: int = Field(alias="targetVersion", ge=1, strict=True)
    content: CustomerChange
    evidence: list[UUID] = Field(alias="evidenceArtifactIds", min_length=1, max_length=16)
    expires_at: AwareDatetime = Field(alias="expiresAt")


class ReviewInput(ActionRequest):
    decision: Literal["approved", "rejected"]


class MessageProposalInput(ActionRequest):
    channel: Literal["fixture"]
    target_id: UUID = Field(alias="targetId")
    target_version: int = Field(alias="targetVersion", ge=1, strict=True)
    destination: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=2000)
    evidence: list[UUID] = Field(alias="evidenceArtifactIds", min_length=1, max_length=16)
    expires_at: AwareDatetime = Field(alias="expiresAt")
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=120)

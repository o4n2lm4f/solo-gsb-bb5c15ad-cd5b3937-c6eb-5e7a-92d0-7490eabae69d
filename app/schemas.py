"""Pydantic models for the configuration payload and evaluation API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Configuration upload models
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrereqSpec(StrictModel):
    flag: str
    expect: bool = True


class ConditionSpec(StrictModel):
    attr: str
    op: str
    value: Any = None


class RuleSpec(StrictModel):
    id: Optional[str] = None
    conditions: list[ConditionSpec] = Field(default_factory=list)
    value: Any = True


class RolloutSpec(StrictModel):
    percentage: int
    key: str = "user_id"
    value: Any = True


class FlagSpec(StrictModel):
    enabled: bool = True
    default: Any = False
    prerequisites: list[PrereqSpec] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    rollout: Optional[RolloutSpec] = None


class ConfigSpec(StrictModel):
    flags: dict[str, FlagSpec] = Field(default_factory=dict)


class PublishConfigRequest(StrictModel):
    version: str = Field(min_length=1)
    flags: dict[str, FlagSpec] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PublishResponse(BaseModel):
    version: str
    flag_count: int
    created_at: str


class VersionInfo(BaseModel):
    version: str
    flag_count: int
    created_at: str


class ConfigResponse(BaseModel):
    version: str
    flag_count: int
    created_at: str
    config: ConfigSpec


class ChainNode(BaseModel):
    flag: str
    value: Any
    hit: bool
    reason: str
    rule_id: Optional[str] = None
    rollout: Optional[dict[str, Any]] = None
    prerequisite_of: Optional[str] = None
    expected: Optional[bool] = None


class EvalResult(BaseModel):
    flag: str
    value: Any
    hit: bool
    reason: str
    rule_id: Optional[str] = None
    rollout: Optional[dict[str, Any]] = None
    chain: list[ChainNode]


class BatchEvalResponse(BaseModel):
    version: str
    context: dict[str, Any]
    results: list[EvalResult]
    missing_flags: list[str]


# ---------------------------------------------------------------------------
# Version comparison models
# ---------------------------------------------------------------------------


class ConfigComparisonRequest(StrictModel):
    from_version: str = Field(min_length=1)
    to_version: str = Field(min_length=1)


class ImpactedFlag(BaseModel):
    flag: str
    path: list[str]


class FlagImpact(BaseModel):
    flag: str
    impacted: list[ImpactedFlag]


class ConfigComparisonResponse(BaseModel):
    from_version: str
    to_version: str
    added: list[str]
    removed: list[str]
    modified: list[str]
    impacts: list[FlagImpact]


class ErrorDetail(BaseModel):
    code: str
    message: str
    errors: Optional[list[dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Evaluation request models
# ---------------------------------------------------------------------------


class BatchEvalRequest(StrictModel):
    version: str = Field(min_length=1)
    flags: list[str] = Field(min_length=1)
    user_id: Optional[Any] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Environment pointer models
# ---------------------------------------------------------------------------


class EnvironmentUpsertRequest(StrictModel):
    version: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)


class EnvironmentResponse(BaseModel):
    name: str
    version: str
    revision: int
    created_at: str
    updated_at: str


class EnvironmentEvalRequest(StrictModel):
    flags: list[str] = Field(min_length=1)
    user_id: Optional[Any] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class EnvironmentEvalResponse(BaseModel):
    name: str
    version: str
    revision: int
    context: dict[str, Any]
    results: list[EvalResult]
    missing_flags: list[str]

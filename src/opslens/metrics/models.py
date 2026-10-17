"""Structured business semantics, never executable SQL."""

from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opslens.metrics.normalization import normalize_text

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")]
Version = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+$")]
Column = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")]
Text = Annotated[str, Field(min_length=1)]
Grain = Literal["day", "week", "month"]
ResolutionStatus = Literal["RESOLVED", "AMBIGUOUS", "UNKNOWN", "DEPRECATED", "CONFLICT"]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=True, frozen=True, allow_inf_nan=False
    )


class MetricTimeSemantics(ContractModel):
    time_column: Column
    timezone: Text = "Asia/Shanghai"
    default_grain: Grain
    allowed_grains: list[Grain] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time(self) -> Self:
        if self.default_grain not in self.allowed_grains:
            raise ValueError("default_grain must be allowed")
        if len(set(self.allowed_grains)) != len(self.allowed_grains):
            raise ValueError("duplicate allowed grain")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid timezone") from exc
        return self


class MetricFilter(ContractModel):
    column: Column
    operator: Literal["EQ", "NEQ", "IN", "NOT_IN", "IS_NULL", "IS_NOT_NULL"]
    scope: Literal["ALL", "NUMERATOR"] = "ALL"
    value: str | int | float | bool | list[str] | list[int] | None

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if self.operator in ("IN", "NOT_IN"):
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("IN/NOT_IN require a nonempty list")
        elif self.operator in ("IS_NULL", "IS_NOT_NULL"):
            if self.value is not None:
                raise ValueError("null operators require None")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError("EQ/NEQ require a non-null scalar")
        return self


class MetricDefinition(ContractModel):
    metric_id: Identifier
    name: Text
    aliases: list[Text]
    description: Text
    version: Version
    status: Literal["ACTIVE", "DEPRECATED"]
    metric_type: Literal["BASE", "DERIVED"]
    unit: Literal["CNY", "COUNT", "RATIO"]
    expression: Text
    dependencies: list[Identifier]
    source_tables: list[Identifier] = Field(min_length=1)
    source_columns: list[Column] = Field(min_length=1)
    time_semantics: MetricTimeSemantics
    filters: list[MetricFilter]
    allowed_dimensions: list[Column]
    refund_policy: Literal["NOT_APPLICABLE", "BEFORE_REFUND", "AFTER_APPROVED_REFUND"]
    owner: Text | None = None

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        aliases = [normalize_text(a) for a in self.aliases]
        if not all(aliases) or len(set(aliases)) != len(aliases):
            raise ValueError("empty or duplicate normalized alias")
        if (self.metric_type == "DERIVED") != bool(self.dependencies):
            raise ValueError("DERIVED requires dependencies; BASE must not have dependencies")
        for field in ("dependencies", "source_tables", "source_columns", "allowed_dimensions"):
            values = getattr(self, field)
            if len(set(values)) != len(values):
                raise ValueError(f"duplicate {field}")
        return self


class MetricCandidate(ContractModel):
    metric_id: Identifier
    version: Version
    name: Text
    status: Literal["ACTIVE", "DEPRECATED"]


class MetricResolution(ContractModel):
    status: ResolutionStatus
    query: str
    normalized_query: str
    matched_text: str | None = None
    metric_id: Identifier | None = None
    version: Version | None = None
    candidates: list[MetricCandidate] = Field(default_factory=list)
    reason_code: str


class SchemaValidationResult(ContractModel):
    passed: bool
    errors: list[str]

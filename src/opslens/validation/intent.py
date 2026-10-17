"""Explicit caller intent, not natural-language inference."""

from datetime import datetime
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from opslens.metrics.models import Column, Identifier, MetricFilter, Version
from opslens.sql.models import StrictModel


class TimeWindow(StrictModel):
    start: datetime
    end: datetime
    timezone: str
    boundary: Literal["CLOSED_OPEN"] = "CLOSED_OPEN"

    @model_validator(mode="after")
    def valid(self) -> Self:
        ZoneInfo(self.timezone)
        if (self.start.tzinfo is None) != (self.end.tzinfo is None) or self.start >= self.end:
            raise ValueError("INVALID_TIME_WINDOW")
        return self

    def local_bounds(self) -> tuple[datetime, datetime]:
        zone = ZoneInfo(self.timezone)
        return tuple(
            v.astimezone(zone).replace(tzinfo=None) if v.tzinfo else v
            for v in (self.start, self.end)
        )  # type: ignore[return-value]


class IntentFilter(MetricFilter):
    @model_validator(mode="after")
    def global_only(self) -> Self:
        if self.scope != "ALL":
            raise ValueError("INTENT_FILTER_MUST_BE_GLOBAL")
        return self


class AnalysisIntent(StrictModel):
    metric_id: Identifier
    metric_version: Version
    metric_output_column: Identifier
    time_window: TimeWindow | None = None
    time_grain: Literal["day", "week", "month"] | None = None
    dimensions: list[Column] = Field(default_factory=list)
    filters: list[IntentFilter] = Field(default_factory=list)
    allow_deprecated_metric: bool = False

    @model_validator(mode="after")
    def unique(self) -> Self:
        names = [d.split(".")[1] for d in self.dimensions]
        if len(names) != len(set(names)) or self.metric_output_column in names + ["time_bucket"]:
            raise ValueError("AMBIGUOUS_OUTPUT_MAPPING")
        return self

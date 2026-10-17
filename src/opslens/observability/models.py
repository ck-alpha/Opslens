"""Vendor-neutral, payload-free durable observation contract."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from opslens.llm.models import GenerationMetadata
from opslens.sql.models import StrictModel


class UsageSummary(StrictModel):
    accounting_mode: Literal["NO_LLM_CALLS", "UNKNOWN", "LOCAL_LLM"] = "UNKNOWN"
    llm_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class StepObservation(StrictModel):
    step_id: str
    step_key: str
    ordinal: int
    status: str
    attempt_count: int
    started_at: datetime | None
    finished_at: datetime | None
    last_attempt_duration_ms: float | None
    reused: bool
    interrupted: bool
    last_failure_class: str | None
    last_error_code: str | None
    artifact_types: list[str]
    artifact_fingerprints: list[str]


class EventSummary(StrictModel):
    event_count: int
    event_types: dict[str, int]
    retry_count: int
    interruption_count: int
    step_interruption_count: int
    reuse_count: int
    terminal_event: str | None


class ArtifactSummary(StrictModel):
    artifact_count: int
    artifact_types: dict[str, int]
    artifact_fingerprints: list[str]


class TimingSummary(StrictModel):
    run_wall_clock_ms: float | None
    sql_elapsed_ms: float | None
    validate_and_report_step_ms: float | None
    time_to_verified_answer_ms: float | None


class RunObservation(StrictModel):
    observability_version: Literal["m8.0"] = "m8.0"
    task_id: str
    run_id: str
    run_attempt: int
    request_fingerprint: str
    runtime_context_fingerprint: str
    final_status: str
    failure_class: str | None
    failure_code: str | None
    metric_id: str | None
    metric_version: str | None
    query_fingerprint: str | None
    evidence_fingerprint: str | None
    validation_fingerprint: str | None
    report_fingerprint: str | None
    validation_status: str | None
    policy_decision: str | None
    reason_codes: list[str]
    document_ids: list[str]
    verified_fact_count: int
    steps: list[StepObservation]
    event_summary: EventSummary
    artifact_summary: ArtifactSummary
    timings: TimingSummary
    usage: UsageSummary
    generation: list[GenerationMetadata] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    unavailable_metrics: list[str] = Field(
        default_factory=lambda: [
            "RUN_LOCK_CONFLICT_LOSERS",
            "CLIENT_ABANDONMENT",
            "USER_CANCELLATION",
            "EVIDENCE_OPEN",
            "USER_CORRECTION",
        ]
    )


class Rate(StrictModel):
    numerator: int
    denominator: int
    rate: float | None


class Quantiles(StrictModel):
    algorithm: Literal["nearest-rank"] = "nearest-rank"
    sample_count: int
    p50: float | None
    p95: float | None


class AggregateObservation(StrictModel):
    observability_version: Literal["m8.0"] = "m8.0"
    run_count: int
    status_counts: dict[str, int]
    verified_count: int
    review_count: int
    failed_count: int
    retry_rate: Rate
    interruption_rate: Rate
    step_reuse_rate: Rate
    run_latency: Quantiles
    time_to_verified_answer: Quantiles
    sql_latency: Quantiles
    step_latency: dict[str, Quantiles]
    reason_code_counts: dict[str, int]
    usage: UsageSummary
    generation: list[GenerationMetadata] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

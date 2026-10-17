"""One nearest-rank implementation; empty samples remain unknown."""

from collections import Counter
from math import ceil, isfinite

from opslens.observability.models import (
    AggregateObservation,
    Quantiles,
    Rate,
    RunObservation,
    UsageSummary,
)


def rate(numerator: int, denominator: int) -> Rate:
    return Rate(
        numerator=numerator,
        denominator=denominator,
        rate=numerator / denominator if denominator else None,
    )


def quantiles(values: list[float | None]) -> Quantiles:
    samples = sorted(v for v in values if v is not None)
    if any(not isfinite(v) or v < 0 for v in samples):
        raise ValueError("INVALID_OBSERVATION_TIMING")
    return Quantiles(
        sample_count=len(samples),
        p50=samples[ceil(len(samples) * 0.5) - 1] if samples else None,
        p95=samples[ceil(len(samples) * 0.95) - 1] if samples else None,
    )


def summarize(observations: list[RunObservation]) -> AggregateObservation:
    states = Counter(o.final_status for o in observations)
    steps = [s for o in observations for s in o.steps]
    no_llm = bool(observations) and all(
        o.usage.accounting_mode == "NO_LLM_CALLS" for o in observations
    )
    return AggregateObservation(
        run_count=len(observations),
        status_counts=dict(states),
        verified_count=sum(o.verified_fact_count > 0 for o in observations),
        review_count=states["NEEDS_REVIEW"],
        failed_count=states["FAILED"],
        retry_rate=rate(
            sum(o.event_summary.retry_count > 0 for o in observations), len(observations)
        ),
        interruption_rate=rate(
            sum(o.event_summary.interruption_count > 0 for o in observations), len(observations)
        ),
        step_reuse_rate=rate(sum(s.reused for s in steps), len(steps)),
        run_latency=quantiles([o.timings.run_wall_clock_ms for o in observations]),
        time_to_verified_answer=quantiles(
            [o.timings.time_to_verified_answer_ms for o in observations]
        ),
        sql_latency=quantiles([o.timings.sql_elapsed_ms for o in observations]),
        step_latency={
            k: quantiles([s.last_attempt_duration_ms for s in steps if s.step_key == k])
            for k in sorted({s.step_key for s in steps})
        },
        reason_code_counts=dict(Counter(c for o in observations for c in o.reason_codes)),
        usage=UsageSummary(
            accounting_mode="NO_LLM_CALLS",
            llm_calls=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )
        if no_llm
        else UsageSummary(),
    )

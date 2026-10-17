"""Windowed aggregates; sessions are not unique people and empty rates are null."""

from datetime import datetime, timedelta

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from opslens.product.events import EventWindow, ProductEventRepository, ProductEventRow


def summarize_events(repository: ProductEventRepository, since: datetime, until: datetime) -> dict:
    window = EventWindow(since=since, until=until)
    if not timedelta(0) < window.until - window.since <= timedelta(days=31):
        raise ValueError("PRODUCT_WINDOW_MUST_BE_0_TO_31_DAYS")
    row = ProductEventRow
    with Session(repository.database) as session:
        # Aggregation happens in PostgreSQL, with a required indexed time predicate.
        values = session.execute(
            select(
                row.event_type,
                func.count(),
                func.count(distinct(row.session_fingerprint)),
                func.count(distinct(row.run_id)),
            )
            .where(row.created_at >= since, row.created_at < until)
            .group_by(row.event_type)
        ).all()
    counts = {kind: {"events": n, "sessions": s, "runs": r} for kind, n, s, r in values}

    def ratio(numerator: str, denominator: str, unit: str):
        n = counts.get(numerator, {}).get(unit, 0)
        d = counts.get(denominator, {}).get(unit, 0)
        return {"numerator": n, "denominator": d, "value": n / d if d else None}

    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "counts": counts,
        "clarification_selection_rate": ratio(
            "METRIC_OPTION_SELECTED", "METRIC_CLARIFICATION_SHOWN", "sessions"
        ),
        "evidence_open_rate": ratio("EVIDENCE_OPENED", "RUN_DETAIL_OPENED", "runs"),
        "report_export_rate": ratio("REPORT_EXPORTED", "RUN_DETAIL_OPENED", "runs"),
        "rerun_request_rate": ratio("RERUN_REQUESTED", "RUN_DETAIL_OPENED", "runs"),
        "feedback_helpful_rate": ratio("FEEDBACK_HELPFUL", "RUN_DETAIL_OPENED", "runs"),
        "correction_signal_rate": ratio("FEEDBACK_NEEDS_CORRECTION", "RUN_DETAIL_OPENED", "runs"),
    }

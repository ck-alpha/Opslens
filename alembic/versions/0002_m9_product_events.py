"""Privacy-safe product events, separate from durable runtime state."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_m9_product_events"
down_revision = "0001_m7"
branch_labels = depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("session_fingerprint", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("runs.run_id", ondelete="RESTRICT"),
        ),
        sa.Column("metric_id", sa.String(64)),
        sa.Column("metric_version", sa.String(20)),
        sa.Column("reason_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("session_fingerprint ~ '^[a-f0-9]{64}$'", name="product_session_hash"),
        sa.CheckConstraint(
            "event_type IN ('SESSION_STARTED','METRIC_CLARIFICATION_REQUESTED',"
            "'METRIC_CLARIFICATION_SHOWN','METRIC_OPTION_SELECTED','DEMO_RECIPE_SELECTED',"
            "'ANALYSIS_SUBMITTED','RUN_DETAIL_OPENED','EVIDENCE_OPENED','REPORT_EXPORTED',"
            "'OBSERVABILITY_OPENED','RERUN_REQUESTED','FEEDBACK_HELPFUL',"
            "'FEEDBACK_NEEDS_CORRECTION','FEEDBACK_UNCLEAR')",
            name="product_event_type",
        ),
    )
    op.create_index("ix_product_events_created_at", "product_events", ["created_at"])
    op.execute(
        "CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON product_events "
        "FOR EACH ROW EXECUTE FUNCTION opslens_reject_mutation()"
    )


def downgrade() -> None:
    from opslens.persistence.database import require_test_database

    require_test_database(op.get_bind().engine)
    op.drop_index("ix_product_events_created_at", table_name="product_events")
    op.drop_table("product_events")

"""Application service composing knowledge, governance, evidence, and durable execution."""

from pathlib import Path
from uuid import UUID

from opslens.clarification.service import MetricClarificationService
from opslens.evidence.models import EvidenceRecord
from opslens.knowledge.index import BusinessKnowledgeIndex
from opslens.llm.models import GenerationMetadata, SQLGenerator
from opslens.observability.exporter import export_json
from opslens.observability.redaction import assert_safe
from opslens.observability.service import ObservabilityService
from opslens.product.events import EventType, ProductEvent, ProductEventRepository
from opslens.product.models import EvidenceSummary, ProductRunView, TimelineEvent
from opslens.product.presenters import HEADLINES, product_status, safe_report
from opslens.product.recipes import load_recipes
from opslens.reporting.markdown import render_markdown
from opslens.reporting.models import VerifiedReport
from opslens.runtime.codec import decode
from opslens.runtime.engine import RuntimeEngine
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.runtime.state_machine import TERMINAL
from opslens.validation.intent import AnalysisIntent


class ProductService:
    def __init__(self, runtime: RuntimeEngine, index: BusinessKnowledgeIndex, data: Path):
        self.runtime = runtime
        self.repository = runtime.repository
        self.registry = runtime.pipeline.validation.builder.registry
        self.index = index
        self.clarification = MetricClarificationService(self.registry, index)
        self.observer = ObservabilityService(self.repository)
        self.events = ProductEventRepository(runtime.database)
        self.recipes = load_recipes(data / "product/demo_recipes")

    async def analyze_local(
        self,
        question: str,
        intent: AnalysisIntent | None,
        provider: SQLGenerator,
        *,
        idempotency_key: str,
        max_repairs: int = 1,
    ):
        from opslens.llm.service import analyze

        return await analyze(
            self,
            question,
            intent,
            provider,
            idempotency_key=idempotency_key,
            max_repairs=max_repairs,
        )

    async def clarify(self, question: str):
        if not 1 <= len(question.strip()) <= 2000:
            raise ValueError("INVALID_QUESTION_LENGTH")
        return await self.clarification.clarify(question)

    async def lookup(self, question: str):
        if not 1 <= len(question.strip()) <= 2000:
            raise ValueError("INVALID_QUESTION_LENGTH")
        return await self.index.search(question, top_k=5)

    def list_demo_recipes(self):
        return [r.model_copy(deep=True) for r in self.recipes.values()]

    def load_demo_recipe(self, recipe_id: str):
        return self.recipes[recipe_id].model_copy(deep=True)

    def start(self, request: RuntimeAnalysisRequest, *, idempotency_key: str) -> ProductRunView:
        request = RuntimeAnalysisRequest.model_validate_json(request.model_dump_json())
        return self.get_run(self.runtime.start(idempotency_key, request).run_id)

    def resume(self, run_id: str) -> ProductRunView:
        return self.get_run(self.runtime.resume(str(UUID(run_id))).run_id)

    def rerun(self, task_id: str) -> ProductRunView:
        return self.get_run(self.runtime.rerun(str(UUID(task_id))).run_id)

    def get_observation(self, run_id: str):
        return self.observer.get_run(str(UUID(run_id)))

    def get_run(self, run_id: str) -> ProductRunView:
        observation = self.get_observation(run_id)  # Integrity checks and redaction first.
        run = self.repository.load_run(run_id)
        if run.status != observation.final_status:
            raise ValueError("OBSERVATION_SNAPSHOT_CHANGED")
        status = product_status(
            run.status,
            policy_decision=observation.policy_decision,
            validation_status=observation.validation_status,
            fact_count=observation.verified_fact_count,
            reason_codes=tuple(observation.reason_codes),
        )
        payload = self.repository.load_task(run.task_id).request_payload
        generation = [GenerationMetadata.model_validate(g) for g in payload.get("generation") or []]
        for metadata in generation:
            assert_safe(metadata)
        if generation and status == "FAILED" and observation.policy_decision == "ALLOW":
            status = "NEEDS_REVIEW"
        report = None
        evidence = None
        for step in self.repository.list_steps(run_id):
            for artifact in self.repository.load_step_artifacts(step.step_id):
                if (
                    artifact.content_fingerprint
                    not in observation.artifact_summary.artifact_fingerprints
                ):
                    raise ValueError("OBSERVATION_SNAPSHOT_CHANGED")
                if artifact.artifact_type not in ("verified_report", "evidence_record"):
                    continue
                value = decode(
                    artifact.artifact_type,
                    artifact.schema_version,
                    artifact.payload,
                    artifact.content_fingerprint,
                )
                if isinstance(value, VerifiedReport):
                    report = safe_report(value, status)
                elif isinstance(value, EvidenceRecord):
                    evidence = EvidenceSummary(
                        completeness=value.completeness_status,
                        provenance_status=value.provenance.lineage_status,
                        query_fingerprint=value.query_fingerprint,
                        policy_fingerprint=value.policy_fingerprint,
                        data_snapshot_fingerprint=value.data_snapshot.snapshot_fingerprint,
                        evidence_fingerprint=value.content_fingerprint,
                        citation_identities=[
                            f"{k.citation.doc_id}@{k.citation.version}:{k.citation.chunk_fingerprint}"
                            for k in value.knowledge
                        ],
                    )
                    assert_safe(evidence)
        # A second presentation guard protects against mismatched or absent reports.
        if status == "VERIFIED" and (
            report is None or report.overall_status != "PASS" or not report.verified_facts
        ):
            status = "NEEDS_REVIEW"
            if report:
                report = safe_report(report, status)
        timeline = [
            TimelineEvent(
                sequence=e.sequence,
                event_type=e.event_type,
                step_id=e.step_id,
                created_at=e.created_at,
            )
            for e in self.repository.load_events(run_id)
        ]
        for event in timeline:
            assert_safe(event)
        metric_id, metric_version = observation.metric_id, observation.metric_version
        if metric_id is None:
            # Read persisted context internally; expose only an identity validated by Registry.
            binding = self.repository.load_task(run.task_id).request_payload.get("metric_ref")
            if isinstance(binding, dict):
                declared_id, declared_version = binding.get("metric_id"), binding.get("version")
                if isinstance(declared_id, str) and isinstance(declared_version, str):
                    definition = self.registry.get(declared_id, declared_version)
                    if definition:
                        metric_id, metric_version = definition.metric_id, definition.version
        return ProductRunView(
            task_id=run.task_id,
            run_id=run_id,
            status=status,
            metric_id=metric_id,
            metric_version=metric_version,
            created_at=run.created_at,
            finished_at=run.finished_at,
            headline=HEADLINES[status],
            reason_codes=observation.reason_codes,
            has_verified_facts=status == "VERIFIED",
            has_uncertainty=bool(
                report and (report.uncertainties or report.warnings or report.interpretations)
            ),
            can_resume=run.status in ("PENDING", "RUNNING", "INTERRUPTED"),
            can_rerun=run.status in TERMINAL,
            report=report,
            observation=observation,
            evidence=evidence,
            timeline=timeline,
            generation=generation,
        )

    def get_report(self, run_id: str):
        return self.get_run(run_id).report

    def history(self, limit: int = 20, status: str | None = None):
        # Safe summary only. The bounded SQL query happens before any composition.
        return [
            self.get_run(run.run_id).model_dump(
                mode="json",
                include={
                    "run_id",
                    "task_id",
                    "status",
                    "metric_id",
                    "metric_version",
                    "created_at",
                    "finished_at",
                    "reason_codes",
                },
            )
            for run in self.repository.list_recent_runs(limit, status)
        ]

    def export(self, run_id: str, kind: str) -> str:
        if kind == "observation":
            return export_json(self.get_observation(run_id))
        report = self.get_report(run_id)
        if report is None:
            raise ValueError("REPORT_NOT_AVAILABLE")
        if kind == "markdown":
            return render_markdown(report)
        if kind == "report":
            return report.model_dump_json(indent=2)
        raise ValueError("INVALID_EXPORT_KIND")

    def record_event(
        self,
        session_fingerprint: str,
        kind: EventType,
        *,
        run_id: str | None = None,
        metric: tuple[str, str] | None = None,
    ) -> bool:
        try:
            run = self.repository.load_run(str(UUID(run_id))) if run_id else None
            if metric and self.registry.get(*metric) is None:
                raise ValueError("UNKNOWN_PRODUCT_METRIC")
            self.events.append(
                ProductEvent(
                    session_fingerprint=session_fingerprint,
                    event_type=kind,
                    run_id=UUID(run.run_id) if run else None,
                    task_id=UUID(run.task_id) if run else None,
                    metric_id=metric[0] if metric else None,
                    metric_version=metric[1] if metric else None,
                )
            )
            return True
        except Exception:
            # Best-effort telemetry never mutates a business Run or its status.
            return False

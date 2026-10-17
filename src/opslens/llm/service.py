"""Generate, govern, persist and verify, with at most one benign repair."""

import asyncio
from typing import TYPE_CHECKING

from pydantic import Field

from opslens.clarification.models import ClarificationDecision
from opslens.evidence.models import MetricBinding
from opslens.llm.context import build_prompt, can_repair
from opslens.llm.models import Generation, SQLGenerator
from opslens.product.models import ProductRunView, ProductStatus
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.sql.models import StrictModel
from opslens.validation.intent import AnalysisIntent

if TYPE_CHECKING:
    from opslens.product.service import ProductService


class LocalAnalysisResult(StrictModel):
    status: ProductStatus
    reason_codes: list[str]
    clarification: ClarificationDecision | None = None
    attempts: list[Generation] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    view: ProductRunView | None = None


async def analyze(
    service: "ProductService",
    question: str,
    intent: AnalysisIntent | None,
    provider: SQLGenerator,
    *,
    idempotency_key: str,
    max_repairs: int = 1,
) -> LocalAnalysisResult:
    if max_repairs not in (0, 1):
        raise ValueError("INVALID_REPAIR_BUDGET")
    decision = await service.clarify(question)
    if decision.action != "PROCEED":
        return LocalAnalysisResult(
            status="CLARIFICATION_REQUIRED" if decision.action == "ASK_USER" else "NEEDS_REVIEW",
            reason_codes=[decision.reason_code],
            clarification=decision,
        )
    if intent is None:
        return LocalAnalysisResult(
            status="CLARIFICATION_REQUIRED", reason_codes=["CONFIRMED_ANALYSIS_SCOPE_REQUIRED"]
        )
    intent = AnalysisIntent.model_validate_json(intent.model_dump_json())
    resolved = decision.metric_resolution
    if (intent.metric_id, intent.metric_version) != (resolved.metric_id, resolved.version):
        return LocalAnalysisResult(
            status="CLARIFICATION_REQUIRED", reason_codes=["METRIC_INTENT_MISMATCH"]
        )
    knowledge = await service.index.search(question, top_k=2, metric_ids=[intent.metric_id])
    pipeline = service.runtime.pipeline
    generations, run_ids = [], []
    feedback = None
    view = None
    for attempt in range(1, max_repairs + 2):
        prompt = build_prompt(pipeline, question, intent, knowledge, feedback)
        generation = await asyncio.to_thread(provider.generate, prompt, attempt)
        generations.append(generation)
        if generation.candidate is None:
            if generation.metadata.status == "MALFORMED_OUTPUT" and attempt <= max_repairs:
                feedback = {"reason_codes": ["MALFORMED_OUTPUT"]}
                continue
            return LocalAnalysisResult(
                status="FAILED",
                reason_codes=[generation.metadata.status],
                attempts=generations,
                run_ids=run_ids,
            )
        sql = generation.candidate.sql
        request = RuntimeAnalysisRequest(
            candidate_sql=sql,
            validation_mode="METRIC_ANALYSIS",
            intent=intent,
            metric_ref=MetricBinding(
                metric_id=intent.metric_id,
                version=intent.metric_version,
                output_columns=[intent.metric_output_column],
            ),
            citations=[h.citation for h in knowledge.hits],
            generation=[g.metadata for g in generations],
        )
        view = await asyncio.to_thread(
            service.start, request, idempotency_key=f"{idempotency_key}:candidate:{attempt}"
        )
        run_ids.append(view.run_id)
        policy = pipeline.evidence.governed.evaluate(sql)
        if (
            policy.decision == "REJECT"
            and can_repair(policy.reason_codes)
            and attempt <= max_repairs
        ):
            feedback = {"candidate_sql": sql, "reason_codes": policy.reason_codes}
            continue
        # No repair after execution or semantic validation. No oracle information
        # is ever sent to the generator, even when the result is demonstrably wrong.
        break
    assert view is not None
    return LocalAnalysisResult(
        status=view.status,
        reason_codes=view.reason_codes,
        attempts=generations,
        run_ids=run_ids,
        view=view,
    )

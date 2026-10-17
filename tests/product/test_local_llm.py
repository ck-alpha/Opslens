import asyncio
import hashlib
from uuid import uuid4

import pytest

from opslens.llm.models import Candidate, Generation, GenerationMetadata
from opslens.runtime.request import RuntimeAnalysisRequest


class Fake:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.prompts = []

    def generate(self, prompt, attempt):
        self.prompts.append(prompt)
        sql = next(self.outputs)
        return Generation(
            candidate=Candidate(sql=sql) if sql else None,
            metadata=GenerationMetadata(
                provider="fake",
                model="fake",
                prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                attempt=attempt,
                duration_ms=1.0,
                status="SUCCESS" if sql else "MALFORMED_OUTPUT",
                candidate_sql_hash=hashlib.sha256(sql.encode()).hexdigest() if sql else None,
            ),
        )


def run(service, outputs, question="2026年8月毛GMV是多少？"):
    recipe = service.load_demo_recipe("gross_gmv_august").runtime_request
    fake = Fake(outputs)
    result = asyncio.run(
        service.analyze_local(question, recipe.intent, fake, idempotency_key=str(uuid4()))
    )
    return result, fake


def test_llm_verified_history_resume_and_prompt_boundary(product_service):
    sql = product_service.load_demo_recipe("gross_gmv_august").runtime_request.candidate_sql
    result, fake = run(product_service, [sql])
    assert result.status == "VERIFIED"
    assert result.view.generation[0].provider == "fake"
    assert product_service.resume(result.view.run_id).generation == result.view.generation
    assert len(fake.prompts) == 1
    assert all(
        word not in fake.prompts[0] for word in ["reference_sql", "ground_truth", "expected_result"]
    )
    assert "orders.customer_id" not in fake.prompts[0]
    exported = product_service.export(result.view.run_id, "observation")
    assert sql not in exported
    assert '"accounting_mode": "LOCAL_LLM"' in exported
    assert '"llm_calls": 1' in exported


def test_clarification_and_missing_scope_never_call_provider(product_service):
    result, fake = run(product_service, [], "8月GMV？")
    assert result.status == "CLARIFICATION_REQUIRED" and not fake.prompts
    result = asyncio.run(
        product_service.analyze_local("gross_gmv", None, Fake([]), idempotency_key=str(uuid4()))
    )
    assert result.status == "CLARIFICATION_REQUIRED"


@pytest.mark.parametrize("initial", [None, "SELECT invalid_column FROM orders", "SELECT FROM"])
def test_single_repair_without_oracle_feedback(product_service, initial):
    sql = product_service.load_demo_recipe("gross_gmv_august").runtime_request.candidate_sql
    result, fake = run(product_service, [initial, sql])
    assert result.status == "VERIFIED" and len(result.attempts) == 2
    assert len(result.view.generation) == 2
    assert "repair_feedback" in fake.prompts[1]
    assert "reference_sql" not in fake.prompts[1]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT * FROM read_csv_auto('/tmp/x')",
        "SELECT customer_id FROM orders",
        "SELECT * FROM secret_table",
    ],
)
def test_security_rejection_never_repaired(product_service, sql):
    result, fake = run(product_service, [sql])
    assert result.status == "REJECTED" and len(fake.prompts) == 1
    assert not result.view.report.verified_facts


def test_semantic_error_no_repair_no_fact(product_service):
    sql = product_service.load_demo_recipe("gross_gmv_august").runtime_request.candidate_sql
    result, fake = run(product_service, [sql.replace("2026-09-01", "2026-08-31")])
    assert result.status == "NEEDS_REVIEW" and len(fake.prompts) == 1
    assert not result.view.report.verified_facts


def test_second_malformed_is_terminal(product_service):
    result, fake = run(product_service, [None, None])
    assert result.status == "FAILED" and len(fake.prompts) == 2 and not result.run_ids


def test_historical_request_serialization_unchanged():
    request = RuntimeAnalysisRequest(candidate_sql="SELECT 1")
    assert "generation" not in request.model_dump()

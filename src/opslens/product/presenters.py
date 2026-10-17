"""One fail-closed mapping from authoritative domain state to product state."""

import json

from opslens.product.models import ProductStatus
from opslens.reporting.models import VerifiedReport


def product_status(
    run_status: str | None = None,
    *,
    action: str | None = None,
    policy_decision: str | None = None,
    validation_status: str | None = None,
    fact_count: int = 0,
    reason_codes: tuple[str, ...] = (),
) -> ProductStatus:
    if action == "ASK_USER" or run_status == "WAITING_INPUT":
        return "CLARIFICATION_REQUIRED"
    if action == "NEEDS_REVIEW":
        return "NEEDS_REVIEW"
    if action == "PROCEED" or run_status == "PENDING":
        return "READY_TO_RUN"
    if run_status in ("RUNNING", "INTERRUPTED"):
        return "RUNNING"
    if policy_decision == "REJECT":
        return "REJECTED"
    if run_status == "SUCCEEDED" and validation_status == "PASS" and fact_count > 0:
        return "VERIFIED"
    if "DIMENSION_NOT_ALLOWED" in reason_codes:
        return "NEEDS_REVIEW"
    if run_status in ("NEEDS_REVIEW", "SUCCEEDED"):
        return "NEEDS_REVIEW"
    return "FAILED"


def safe_report(report: VerifiedReport, status: ProductStatus) -> VerifiedReport:
    # Do not export validator diagnostic dictionaries or caller-authored interpretations.
    # This is a presentation projection; report_fingerprint identifies the source artifact.
    value = report.model_dump(mode="json")
    if status != "VERIFIED" or report.overall_status != "PASS":
        value["verified_facts"] = []
    for item in value["interpretations"]:
        item["text"] = "Caller interpretation withheld; see validation status and reason code."
    for validator in value["validation_summary"]:
        for check in validator["checks"]:
            check["details"] = {}
    return VerifiedReport.model_validate_json(json.dumps(value))


HEADLINES = {
    "VERIFIED": "在当前 OpsLens 验证范围内已通过",
    "REJECTED": "BLOCKED BY POLICY — No Verified Fact",
    "NEEDS_REVIEW": "NEEDS REVIEW — No Verified Fact",
    "FAILED": "Verification failed — inspect stable reason codes; contact the operator.",
    "RUNNING": "Running verification / recovery available",
    "READY_TO_RUN": "Ready to run",
    "CLARIFICATION_REQUIRED": "Metric selection required",
}

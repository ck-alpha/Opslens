"""Verify integrity separately from current-environment drift."""

import hashlib

from opslens.evidence.builder import EvidenceBuilder, content_fingerprint
from opslens.evidence.hashing import result_hash
from opslens.evidence.ledger import EvidenceLedger
from opslens.evidence.models import EvidenceExecutionResult, EvidenceRecord
from opslens.provenance.lineage import provenance_fingerprint
from opslens.provenance.snapshot import capture_snapshot, snapshot_fingerprint
from opslens.sql.policy import SQLPolicyConfig
from opslens.validation.models import ValidationCheck, ValidationMode
from opslens.validation.policy import ValidationPolicy
from opslens.validation.status import check


def validate_evidence(
    bundle: EvidenceExecutionResult,
    builder: EvidenceBuilder,
    sql_policy: SQLPolicyConfig,
    policy: ValidationPolicy,
    mode: ValidationMode,
    ledger: EvidenceLedger | None = None,
) -> list[ValidationCheck]:
    execution, record = bundle.execution, bundle.evidence
    if execution.status != "EXECUTED":
        return [check("EVIDENCE_NOT_APPLICABLE", required=False, applicable=False)]
    if record is None:
        return [check("EVIDENCE_MISSING_AFTER_EXECUTION", "NEEDS_REVIEW")]
    record = EvidenceRecord.model_validate_json(record.model_dump_json(by_alias=True))
    checks = []

    def equal(ok: bool, code: str):
        checks.append(
            check(
                code if not ok else code.removesuffix("_MISMATCH") + "_VALID",
                "PASS" if ok else "FAIL",
            )
        )

    equal(
        content_fingerprint(record) == record.content_fingerprint,
        "EVIDENCE_CONTENT_FINGERPRINT_MISMATCH",
    )
    decision = execution.decision
    equal(
        record.query_fingerprint
        == decision.query_fingerprint
        == record.provenance.query_fingerprint
        == hashlib.sha256(record.effective_sql.encode()).hexdigest()
        and record.effective_sql == decision.effective_sql == record.provenance.effective_sql
        and record.original_sql == decision.analysis.original_sql
        and record.normalized_sql == decision.analysis.normalized_sql,
        "QUERY_IDENTITY_MISMATCH",
    )
    equal(
        (record.policy_id, record.policy_version, record.policy_fingerprint)
        == (decision.policy_id, decision.policy_version, decision.policy_fingerprint),
        "POLICY_IDENTITY_MISMATCH",
    )
    equal(
        record.provenance.provenance_fingerprint == provenance_fingerprint(record.provenance),
        "PROVENANCE_FINGERPRINT_MISMATCH",
    )
    equal(
        record.result.result_hash == result_hash(execution)
        and record.result.columns == execution.columns
        and record.result.row_count == execution.row_count,
        "RESULT_HASH_MISMATCH",
    )
    snapshot = record.data_snapshot
    equal(
        snapshot.snapshot_fingerprint == snapshot_fingerprint(snapshot)
        and snapshot.referenced_tables
        == sorted("main." + t for t in decision.analysis.physical_tables),
        "SNAPSHOT_FINGERPRINT_MISMATCH",
    )
    if policy.require_snapshot_match:
        current = capture_snapshot(builder.db, snapshot.referenced_tables)
        if current.snapshot_fingerprint != snapshot.snapshot_fingerprint:
            checks.append(check("DATA_SNAPSHOT_DRIFT", "NEEDS_REVIEW"))
    drift = policy.require_current_policy_match and (
        sql_policy.fingerprint() != record.policy_fingerprint
    )
    if record.metric and policy.require_current_registry_match:
        drift |= record.metric.metric_registry_fingerprint != builder.registry.fingerprint()
    for ref in record.knowledge:
        if (
            policy.require_current_corpus_match
            and ref.corpus_fingerprint != builder.corpus.fingerprint
        ):
            drift = True
        else:
            try:
                current_ref = builder.knowledge_ref(ref.citation)
                equal(
                    current_ref.document_content_sha256 == ref.document_content_sha256,
                    "CITATION_DOCUMENT_HASH_MISMATCH",
                )
            except ValueError:
                equal(False, "CITATION_DOCUMENT_HASH_MISMATCH")
    if drift:
        checks.append(check("ARTIFACT_VERSION_DRIFT", "NEEDS_REVIEW"))
    if mode == "METRIC_ANALYSIS" and (
        record.completeness_status != "COMPLETE" or record.provenance.lineage_status != "RESOLVED"
    ):
        checks.append(check("EVIDENCE_NOT_COMPLETE", "NEEDS_REVIEW"))
    if ledger is not None:
        equal(
            ledger.verify()
            and any(
                e.evidence.content_fingerprint == record.content_fingerprint
                for e in ledger.entries()
            ),
            "LEDGER_VERIFICATION_FAILED",
        )
    return checks

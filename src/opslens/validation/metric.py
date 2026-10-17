"""Contract proofs plus an independent result oracle are both required."""

from decimal import Decimal, InvalidOperation

from opslens.evidence.builder import EvidenceBuilder
from opslens.evidence.models import EvidenceExecutionResult
from opslens.validation.intent import AnalysisIntent
from opslens.validation.models import ValidationCheck, ValidationMode
from opslens.validation.oracle import MetricResultOracle, OracleResult, UnsupportedContract
from opslens.validation.policy import ValidationPolicy
from opslens.validation.semantic import validate_semantics
from opslens.validation.status import check


def results_agree(
    columns: list[str], rows: list, oracle: OracleResult, output: str, unit: str, tolerance: float
) -> bool:
    if len(columns) != len(set(columns)) or set(columns) != set(oracle.columns):
        return False
    keys = [c for c in oracle.columns if c != output]

    def indexed(cols, data):
        result = {}
        for row in data:
            if len(row) != len(cols):
                return None
            key = tuple(row[cols.index(k)] for k in keys)
            if key in result:
                return None
            result[key] = row[cols.index(output)]
        return result

    left, right = indexed(columns, rows), indexed(oracle.columns, oracle.rows)
    if left is None or right is None or left.keys() != right.keys():
        return False
    for key, value in left.items():
        expected = right[key]
        if value is None or expected is None:
            if value is not expected:
                return False
            continue
        try:
            a, b = Decimal(str(value)), Decimal(str(expected))
            if not a.is_finite() or not b.is_finite():
                return False
            if abs(a - b) > (Decimal(str(tolerance)) if unit == "RATIO" else 0):
                return False
        except (InvalidOperation, ValueError):
            return False
    return True


def validate_metric(
    bundle: EvidenceExecutionResult,
    intent: AnalysisIntent | None,
    mode: ValidationMode,
    builder: EvidenceBuilder,
    policy: ValidationPolicy,
    oracle: MetricResultOracle | None,
) -> list[ValidationCheck]:
    if mode == "DATA_QUERY":
        return [check("METRIC_NOT_APPLICABLE", required=False, applicable=False)]
    record = bundle.evidence
    if intent is None or record is None or record.metric is None:
        return [check("METRIC_BINDING_REQUIRED", "UNKNOWN")]
    intent = AnalysisIntent.model_validate(intent.model_dump())
    checks = []
    binding = record.metric
    if (binding.metric_id, binding.version, binding.output_columns) != (
        intent.metric_id,
        intent.metric_version,
        [intent.metric_output_column],
    ):
        checks.append(check("METRIC_IDENTITY_MISMATCH", "FAIL"))
    d = builder.registry.get(intent.metric_id, intent.metric_version)
    if d is None:
        return checks + [check("UNKNOWN_METRIC_VERSION", "UNKNOWN")]
    if d.status == "DEPRECATED" and not (
        intent.allow_deprecated_metric or policy.allow_deprecated_metrics_by_default
    ):
        checks.append(check("DEPRECATED_METRIC_REQUIRES_EXPLICIT_OVERRIDE", "NEEDS_REVIEW"))
    if record.completeness_status != "COMPLETE":
        return checks + [check("EVIDENCE_NOT_COMPLETE", "NEEDS_REVIEW")]
    provenance = next(
        (
            p
            for p in record.provenance.output_columns
            if p.output_name == intent.metric_output_column
        ),
        None,
    )
    if provenance is None:
        return checks + [check("METRIC_EXPRESSION_DEPENDENCY_MISMATCH", "FAIL")]
    try:
        checks.extend(
            validate_semantics(
                record.effective_sql,
                intent,
                d,
                builder.registry,
                builder.provenance.schema,
                {f"{c.table}.{c.column}" for c in provenance.source_columns},
            )
        )
    except UnsupportedContract:
        checks.append(check("METRIC_FORMULA_UNSUPPORTED", "UNKNOWN"))
    if oracle is None:
        checks.append(check("METRIC_ORACLE_UNAVAILABLE", "UNKNOWN"))
    else:
        try:
            expected = oracle.evaluate(intent)
        except UnsupportedContract:
            checks.append(check("METRIC_ORACLE_UNAVAILABLE", "UNKNOWN"))
        else:
            ok = results_agree(
                bundle.execution.columns,
                bundle.execution.rows,
                expected,
                intent.metric_output_column,
                d.unit,
                policy.ratio_tolerance,
            )
            checks.append(
                check(
                    "METRIC_ORACLE_MATCH" if ok else "METRIC_RESULT_MISMATCH",
                    "PASS" if ok else "FAIL",
                    details={
                        "oracle_id": expected.oracle_id,
                        "oracle_version": expected.oracle_version,
                    },
                )
            )
    if all(c.status == "PASS" for c in checks):
        checks.append(check("METRIC_VALID"))
    return checks

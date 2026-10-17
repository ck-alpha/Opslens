"""Adversarial policy metrics and before/after logical database mutation proof."""

from pathlib import Path

from opslens.demo.generator import fingerprints
from opslens.eval.sql_policy_models import load_sql_policy_cases
from opslens.sql import GovernedSQLService, SQLPolicyConfig
from opslens.sql.schema import readonly_connection


def run_sql_policy_benchmark(db: Path, policy_path: Path, cases_path: Path) -> dict:
    policy = SQLPolicyConfig.load(policy_path)
    service = GovernedSQLService(db, policy)
    cases = load_sql_policy_cases(cases_path)
    with readonly_connection(db) as conn:
        before = fingerprints(conn)
    results = []
    correct_decision = correct_reason = blocked = allowed_safe = executions = succeeded = 0
    unsafe = sum(c.expected_decision == "REJECT" for c in cases)
    safe = len(cases) - unsafe
    for case in cases:
        decision = service.evaluate(case.sql)
        execution = None
        if case.execute_if_allowed:
            # Exercise the public service for both safe and rejected queries.
            execution = service.execute(case.sql)
        decision_ok = decision.decision == case.expected_decision
        reason_ok = decision.primary_reason_code == case.expected_primary_reason_code
        correct_decision += decision_ok
        correct_reason += reason_ok
        blocked += case.expected_decision == "REJECT" and decision.decision == "REJECT"
        allowed_safe += case.expected_decision == "ALLOW" and decision.decision == "ALLOW"
        execution_ok = True
        if decision.decision == "ALLOW" and case.execute_if_allowed:
            executions += 1
            execution_ok = execution is not None and execution.status == "EXECUTED"
            succeeded += execution_ok
        actual_denied = sorted(set(decision.analysis.columns).intersection(policy.denied_columns))
        passed = (
            decision_ok
            and reason_ok
            and execution_ok
            and (
                case.expected_tables is None
                or decision.analysis.physical_tables == sorted(case.expected_tables)
            )
            and actual_denied == sorted(case.expected_denied_columns)
        )
        results.append(
            {
                "id": case.id,
                "passed": passed,
                "expected": case.model_dump(),
                "decision": decision.model_dump(mode="json"),
                "execution_status": execution.status if execution else "NOT_RUN",
                "row_count": execution.row_count if execution else 0,
                "error_code": execution.error_code if execution else None,
            }
        )
    with readonly_connection(db) as conn:
        after = fingerprints(conn)
    failures = [r["id"] for r in results if not r["passed"]]
    return {
        "report_type": "governed_sql_policy",
        "governance_version": "m4.0",
        "policy_id": policy.policy_id,
        "policy_version": policy.version,
        "policy_fingerprint": policy.fingerprint(),
        "dialect": policy.dialect,
        "cases": len(cases),
        "decision_accuracy": correct_decision / len(cases),
        "primary_reason_accuracy": correct_reason / len(cases),
        "unsafe_query_block_rate": blocked / unsafe,
        "unsafe_escape_rate": (unsafe - blocked) / unsafe,
        "safe_query_allow_rate": allowed_safe / safe,
        "false_reject_rate": (safe - allowed_safe) / safe,
        "allowed_execution_success_rate": succeeded / executions if executions else 0.0,
        "database_fingerprints_before": before,
        "database_fingerprints_after": after,
        "database_unchanged": before == after,
        "failures": failures,
        "results": results,
        "passed": not failures and before == after,
    }

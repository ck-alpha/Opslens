"""Only public execution API: evaluate every SQL before invoking the private executor."""

import hashlib
from pathlib import Path

from sqlglot import exp

from opslens.sql.analyzer import analyze
from opslens.sql.executor import DuckDBGovernedExecutor
from opslens.sql.models import GovernedSQLResult, PolicyDecision
from opslens.sql.policy import SQLPolicyConfig
from opslens.sql.schema import DatabaseSchemaSnapshot


class GovernedSQLService:
    def __init__(self, db: Path, policy: SQLPolicyConfig):
        self._policy = policy
        self._schema = DatabaseSchemaSnapshot.load(db, policy)
        self._executor = DuckDBGovernedExecutor(db)

    def evaluate(self, sql: str) -> PolicyDecision:
        analysis, tree, reasons = analyze(sql, self._policy, self._schema)
        effective = None
        warnings = []
        if not reasons and isinstance(tree, exp.Query):
            if tree.args.get("limit") is None:
                tree = tree.limit(self._policy.max_result_rows)
                warnings.append("RESULT_LIMIT_INJECTED")
            effective = tree.sql(dialect="duckdb", comments=False)
        return PolicyDecision(
            decision="REJECT" if reasons else "ALLOW",
            primary_reason_code=reasons[0] if reasons else "POLICY_ALLOW",
            reason_codes=reasons or ["POLICY_ALLOW"],
            policy_id=self._policy.policy_id,
            policy_version=self._policy.version,
            policy_fingerprint=self._policy.fingerprint(),
            analysis=analysis,
            effective_sql=effective,
            query_fingerprint=hashlib.sha256(effective.encode()).hexdigest() if effective else None,
            warnings=warnings,
        )

    def execute(self, sql: str) -> GovernedSQLResult:
        decision = self.evaluate(sql)
        if decision.decision == "REJECT":
            return GovernedSQLResult(
                status="REJECTED", decision=decision, error_code="POLICY_REJECTED"
            )
        return self._executor._execute(
            decision, self._policy.timeout_seconds, self._policy.max_result_rows
        )

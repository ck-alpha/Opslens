import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from opslens.runtime.failures import RuntimeFault, classify
from opslens.runtime.locking import lock_key
from opslens.runtime.policy import RuntimePolicy
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.runtime.state_machine import TERMINAL, TRANSITIONS, transition


@pytest.mark.parametrize(
    "kind,old,new",
    [
        (kind, old, new)
        for kind, edges in TRANSITIONS.items()
        for old, targets in edges.items()
        for new in targets
    ],
)
def test_legal_transitions(kind, old, new):
    assert transition(kind, old, new) == new


@pytest.mark.parametrize("kind", ["task", "run", "step"])
def test_terminal_transition_rejected(kind):
    with pytest.raises(RuntimeFault, match="INVALID_STATE_TRANSITION"):
        transition(kind, "SUCCEEDED", "RUNNING")


@pytest.mark.parametrize("status", sorted(TERMINAL))
def test_explicit_task_rerun(status):
    assert transition("task", status, "RUNNING", new_run=True) == "RUNNING"


def test_strict_request_and_policy():
    request = RuntimeAnalysisRequest(candidate_sql="SELECT 1")
    assert (
        request.fingerprint()
        == RuntimeAnalysisRequest.model_validate_json(
            json.dumps(dict(reversed(list(request.model_dump(mode="json").items()))))
        ).fingerprint()
    )
    with pytest.raises(ValueError):
        RuntimeAnalysisRequest.model_validate({"candidate_sql": "SELECT 1", "run_id": "no"})
    with pytest.raises(ValueError):
        RuntimePolicy(persist_result_rows=False)
    with pytest.raises(ValueError):
        RuntimePolicy(retry_backoff_ms=[-1, 0, 0])
    assert classify(RuntimeError("a secret")).model_dump() == {
        "failure_class": "NEEDS_REVIEW",
        "error_code": "UNEXPECTED_STEP_EXCEPTION",
        "error_type": "RuntimeError",
    }


def test_stable_lock_identity_is_independent_of_changed_policy():
    run_id = str(uuid4())
    result = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import sys; from opslens.runtime.locking import lock_key; "
            "print(lock_key(sys.argv[1]))",
            run_id,
        ],
        env={**os.environ, "PYTHONHASHSEED": "123"},
        text=True,
    )
    assert result.strip() == str(lock_key(run_id))
    with pytest.raises(ValueError):
        RuntimePolicy(advisory_lock_namespace=7002)
    assert -(2**63) <= lock_key(run_id) < 2**63

"""Central transition rules. Rerun is an explicit task-only operation."""

from opslens.runtime.failures import RuntimeFault

TERMINAL = {"SUCCEEDED", "FAILED", "NEEDS_REVIEW", "WAITING_INPUT"}
TRANSITIONS = {
    "task": {"PENDING": {"RUNNING"}, "RUNNING": TERMINAL},
    "run": {
        "PENDING": {"RUNNING"},
        "RUNNING": TERMINAL | {"INTERRUPTED"},
        "INTERRUPTED": {"RUNNING"},
    },
    "step": {
        "PENDING": {"RUNNING"},
        "RUNNING": {"SUCCEEDED", "FAILED", "INTERRUPTED"},
        "INTERRUPTED": {"RUNNING", "FAILED"},
        "FAILED": {"RUNNING"},
    },
}


def transition(kind: str, old: str, new: str, *, new_run: bool = False) -> str:
    if kind == "task" and new_run and old in TERMINAL and new == "RUNNING":
        return new
    if new not in TRANSITIONS.get(kind, {}).get(old, set()):
        raise RuntimeFault("INVALID_STATE_TRANSITION")
    return new

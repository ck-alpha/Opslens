"""Session-local submit guard and opaque idempotency identities."""

from dataclasses import dataclass, field
from hashlib import sha256
from uuid import uuid4


@dataclass
class SessionState:
    nonce: str = field(default_factory=lambda: uuid4().hex)
    counter: int = 0
    busy: bool = False
    telemetry_degraded: bool = False
    run_id: str | None = None
    task_id: str | None = None

    @property
    def fingerprint(self) -> str:
        return sha256(self.nonce.encode()).hexdigest()

    def submission_key(self, draft_fingerprint: str) -> str:
        self.counter += 1
        return sha256(f"{self.nonce}:{draft_fingerprint}:{self.counter}".encode()).hexdigest()

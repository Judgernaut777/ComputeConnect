"""Run tracking and cancellation.

Every generation — control-plane ``/generate`` or OpenAI-layer
``/v1/chat/completions`` — is a Run in this one registry. That is the "one
backend" invariant made concrete: capacity accounting and cancellation see
both doors.

Cancellation is cooperative and best-effort: ``cancel()`` sets an event the
streaming loop races against, and the loop closes the upstream connection,
which is how llama.cpp (and any HTTP engine) learns to stop decoding.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

#: Terminal run states.
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "refused", "disconnected"})


@dataclass
class Run:
    id: str
    provider_id: str
    model: str | None
    surface: str  # "generate" | "openai"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    state: str = "running"
    cancel_requested: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.id,
            "provider_id": self.provider_id,
            "model": self.model,
            "surface": self.surface,
            "state": self.state,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "metrics": dict(self.metrics),
        }


class RunRegistry:
    """In-memory run registry. Keeps a bounded history of finished runs."""

    def __init__(self, history_limit: int = 512) -> None:
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._history_limit = history_limit

    def create(self, provider_id: str, model: str | None, surface: str) -> Run:
        run = Run(id=uuid.uuid4().hex, provider_id=provider_id, model=model, surface=surface)
        self._runs[run.id] = run
        self._trim()
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def cancel(self, run_id: str) -> str:
        """Request cancellation. Returns a status string, never raises."""
        run = self._runs.get(run_id)
        if run is None:
            return "not_found"
        if run.state in TERMINAL_STATES:
            return "already_finished"
        run.cancel_requested = True
        run.cancel_event.set()
        return "cancelling"

    def finish(self, run: Run, state: str, **metrics: object) -> None:
        if run.state in TERMINAL_STATES:
            return
        run.state = state
        run.finished_at = time.time()
        run.metrics.update(metrics)

    def active_count(self, provider_id: str) -> int:
        return sum(
            1
            for r in self._runs.values()
            if r.provider_id == provider_id and r.state not in TERMINAL_STATES
        )

    def _trim(self) -> None:
        finished = [rid for rid, r in self._runs.items() if r.state in TERMINAL_STATES]
        excess = len(finished) - self._history_limit
        for rid in finished[: max(0, excess)]:
            self._runs.pop(rid, None)

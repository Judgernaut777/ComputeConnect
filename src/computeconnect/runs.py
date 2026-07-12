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
import json
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

#: Terminal run states. ``interrupted`` is the reconciliation state: a run that
#: was still ``running`` in a durable journal when the process died is set to it
#: on the next start — never left dangling as ``running`` (see RunJournal).
TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "refused", "disconnected", "interrupted"}
)


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


class RunJournal:
    """Durable, append-on-change record of runs for restart recovery.

    ComputeConnect is a thin proxy: it holds no generation state a restart could
    resume, and the upstream connection dies with the process. What a restart
    MUST NOT do is lose the fact that a run existed or leave it eternally
    ``running``. This SQLite journal records every run at create and at finish;
    on the next start :meth:`reconcile` transitions any row still ``running`` to
    the terminal ``interrupted`` state. Reconciled runs stay queryable via
    ``GET /runs/{id}`` so a client can tell "your in-flight run was interrupted
    by a restart; retry" from "no such run".

    Thread-safe (one lock; ``check_same_thread=False``) because the ASGI server
    and its worker tasks share one registry.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, provider_id TEXT, model TEXT, surface TEXT,
                state TEXT, created_at REAL, finished_at REAL, metrics TEXT)"""
        )
        self._conn.commit()

    def record(self, run: "Run") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?)",
                (
                    run.id, run.provider_id, run.model, run.surface, run.state,
                    run.created_at, run.finished_at, json.dumps(run.metrics),
                ),
            )
            self._conn.commit()

    def reconcile(self, now: float | None = None) -> list[str]:
        """Mark every non-terminal (orphaned ``running``) row ``interrupted``.
        Returns the reconciled run ids. Idempotent."""
        now = time.time() if now is None else now
        placeholders = ",".join("?" * len(TERMINAL_STATES))
        terminal = tuple(TERMINAL_STATES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM runs WHERE state NOT IN ({placeholders})", terminal
            ).fetchall()
            orphans = [r[0] for r in rows]
            if orphans:
                self._conn.execute(
                    f"UPDATE runs SET state='interrupted', finished_at=? "
                    f"WHERE state NOT IN ({placeholders})",
                    (now, *terminal),
                )
                self._conn.commit()
            return orphans

    def load(self, run_id: str) -> "Run | None":
        with self._lock:
            row = self._conn.execute(
                "SELECT id, provider_id, model, surface, state, created_at, "
                "finished_at, metrics FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        run = Run(id=row[0], provider_id=row[1], model=row[2], surface=row[3])
        run.state = row[4]
        run.created_at = row[5]
        run.finished_at = row[6]
        run.metrics = json.loads(row[7] or "{}")
        return run

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class RunRegistry:
    """Run registry. In-memory by default; durable + restart-reconciled when a
    :class:`RunJournal` is supplied. Keeps a bounded history of finished runs."""

    def __init__(
        self, history_limit: int = 512, journal: "RunJournal | None" = None
    ) -> None:
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._history_limit = history_limit
        self._journal = journal
        #: Run ids that were reconciled from an orphaned 'running' state on this
        #: start (empty without a journal, or on a clean restart).
        self.reconciled_run_ids: list[str] = (
            journal.reconcile() if journal is not None else []
        )

    def create(self, provider_id: str, model: str | None, surface: str) -> Run:
        run = Run(id=uuid.uuid4().hex, provider_id=provider_id, model=model, surface=surface)
        self._runs[run.id] = run
        if self._journal is not None:
            self._journal.record(run)
        self._trim()
        return run

    def get(self, run_id: str) -> Run | None:
        run = self._runs.get(run_id)
        if run is not None:
            return run
        # Fall back to the journal so reconciled/evicted runs stay queryable.
        if self._journal is not None:
            return self._journal.load(run_id)
        return None

    def cancel(self, run_id: str) -> str:
        """Request cancellation. Returns a status string, never raises."""
        run = self._runs.get(run_id)
        if run is None:
            # Might be a reconciled/finished run only in the journal.
            if self._journal is not None and self._journal.load(run_id) is not None:
                return "already_finished"
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
        if self._journal is not None:
            self._journal.record(run)

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

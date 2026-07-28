"""Best-effort publisher onto AgentConnect's shared ecosystem event bus.

See ``docs/EVENT_BUS.md`` in the AgentConnect repo (``mcp-agentconnect``) for
the full wire contract this module speaks: ``POST {bus_url}/events``, body =
the envelope minus ``seq``, ``Authorization: Bearer <publish-token>`` scoped
to exactly ``source_product="computeconnect"`` (minted via ``agentconnect
tokens publish --source-product computeconnect``).

Doctrine (EVENT_BUS.md §0), restated for this module specifically:

* The bus is a PROJECTION for observability — never a system of record, and
  never consulted to make a placement or generation decision. Nothing in
  this module is ever read back by ComputeConnect itself.
* Publishing is BEST-EFFORT and NEVER FATAL. :meth:`BusPublisher.publish`
  itself never raises and never awaits the network call: it schedules a
  bounded background task (or, with no running event loop to schedule onto,
  does nothing) and returns immediately. A dead, unreachable, slow, or
  misconfigured bus therefore cannot add so much as one event-loop tick of
  latency to a real placement or generation, let alone block one — the
  tradeoff is that a publish can silently lose a race with process shutdown
  (the background task is cancelled along with everything else in flight),
  which is the correct failure mode for a projection that must never gate
  real work.
* A publisher pre-redacts. Every call site that builds a payload passes only
  ids, names, decisions, reasons (bounded via :func:`truncate_reason`),
  counts, and the privacy tier — never a prompt, a chat message, model
  output, or a secret. The AgentConnect store re-redacts independently on
  top of this (EVENT_BUS.md §9.3); this module's own redaction is defense in
  depth, not a substitute for it.

Configuration — both required; either absent disables the publisher outright
(``enabled`` is ``False``, every method becomes a zero-I/O no-op)::

    COMPUTECONNECT_BUS_URL     e.g. http://127.0.0.1:8790
    COMPUTECONNECT_BUS_TOKEN   a publish token scoped to source_product=computeconnect

This module performs no environment lookup of its own — :mod:`config`
resolves ``COMPUTECONNECT_BUS_URL``/``COMPUTECONNECT_BUS_TOKEN`` the same way
it already resolves ``COMPUTECONNECT_TOKEN`` (env, then a config-file key,
then an explicit argument) and hands the result to :class:`BusPublisher`'s
constructor. Keeping this class free of env lookups keeps it a pure,
synchronously constructible unit for tests and embedders.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: The one ``source_product`` this process is ever allowed to claim. The
#: AgentConnect ingress enforces this token-side too (a publish token is
#: bound to exactly one product, EVENT_BUS.md §9.2) — stamping it here is
#: belt and suspenders, not the sole guard.
SOURCE_PRODUCT = "computeconnect"

#: Bounded so a dead/slow bus can never occupy the background task for long.
#: This is a backstop, not the primary "never blocks the caller" mechanism —
#: that mechanism is `publish()` never awaiting the network call at all (see
#: module docstring).
DEFAULT_CONNECT_TIMEOUT = 1.0
DEFAULT_READ_TIMEOUT = 2.0

#: Free-text fields (a refusal code, an upstream error message) are bounded
#: to this many characters before they are ever placed in a payload —
#: defense in depth alongside the store's own re-redaction (EVENT_BUS.md
#: §9.3), and consistent with the truncation every other emission site in
#: the ecosystem already applies to caller-supplied text.
REASON_MAX_CHARS = 200


def truncate_reason(text: object, limit: int = REASON_MAX_CHARS) -> str:
    """Bound a free-text reason to ``limit`` chars before it can reach a
    payload. Never raises, even on a non-string input."""
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


class BusPublisher:
    """Fire-and-forget client for ``POST {bus_url}/events``.

    ``enabled`` is ``False`` whenever ``bus_url`` or ``token`` is falsy —
    every public method is then a guaranteed zero-I/O no-op, so a call site
    can construct and use this unconditionally with no
    ``if configured:`` branch of its own.
    """

    def __init__(
        self,
        *,
        bus_url: str | None = None,
        token: str | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        self.enabled = bool(bus_url and token)
        self._bus_url = (bus_url or "").rstrip("/")
        self._token = token
        self._timeout = httpx.Timeout(connect_timeout, read=read_timeout)
        # Strong references to in-flight background tasks: asyncio does not
        # keep a Task alive by itself once nothing else holds a reference to
        # it, so an unreferenced fire-and-forget task can be garbage
        # collected mid-flight. Each task removes itself here on completion.
        self._tasks: set[asyncio.Task] = set()

    def publish(
        self,
        event_type: str,
        *,
        outcome: str | None = None,
        actor: str | None = None,
        privacy_tier: str | None = None,
        entity_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Schedule a best-effort publish. Always returns immediately and
        never raises — a disabled publisher, a missing event loop, and a
        network failure are all handled the same way: nothing propagates to
        the caller.

        ``payload`` is trusted to already be pre-redacted by the caller (see
        module docstring); this method adds no content of its own beyond the
        envelope fields.
        """
        if not self.enabled:
            return
        body: dict[str, Any] = {
            "type": event_type,
            "source_product": SOURCE_PRODUCT,
            "event_id": f"cc_{uuid.uuid4().hex}",
            "ts": time.time(),
            "outcome": outcome,
            "actor": actor,
            "privacy_tier": privacy_tier,
            "entity_id": entity_id,
            "task_id": task_id,
            "payload": payload or {},
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. invoked from a synchronous context): there
            # is nothing safe to schedule this onto. Never fatal — log and
            # move on, indistinguishable in effect from an unreachable bus.
            logger.warning(
                "computeconnect bus publish skipped (no running event loop): %s",
                event_type,
            )
            return
        task = loop.create_task(self._send(body))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, body: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(
                    f"{self._bus_url}/events",
                    json=body,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except Exception as exc:  # the bus is a projection, never fatal
            logger.warning(
                "computeconnect bus publish failed (%s): %s", body.get("type"), exc
            )

    async def drain(self) -> None:
        """Await every currently in-flight publish task.

        Test-only convenience: production call sites never await this (the
        entire point of :meth:`publish` is that nothing waits on the
        network). It exists so a test can deterministically observe a
        publish that already happened instead of polling/sleeping for a
        background task running on the server's own event loop.
        """
        pending = list(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

"""Provider registry, capacity, and the health/inventory snapshot.

A provider is *where compute is obtained from* (D1): here, a local llama.cpp
host and a simulated cloud environment. The registry owns the cached snapshot
of each provider's health and model inventory. ``/route/estimate`` reads only
this snapshot, which is what keeps it cheap and side-effect-free: at worst it
triggers one lazy GET of ``/health`` + ``/v1/models`` per provider per TTL
window, and it never touches a generation path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .engines import ModelInfo
from .runs import RunRegistry


@dataclass(frozen=True)
class ProviderSpec:
    """A registered compute provider.

    ``placement_class`` is the structural privacy attribute: ``"cloud"``
    providers are removed from the candidate set before placement unless the
    resolved tier explicitly permits cloud (privacy.py).
    """

    id: str
    placement_class: str  # "local" | "cloud"
    engine: object  # LlamaCppEngine | SimulatedCloudEngine (duck-typed)
    capabilities: tuple[str, ...]
    max_concurrency: int = 2
    estimated_quality: float = 0.5  # heuristic 0..1, documented in CONTRACT.md
    estimated_tokens_per_second: float = 10.0


@dataclass(frozen=True)
class ProviderSnapshot:
    """Point-in-time view of one provider, as placement sees it."""

    spec: ProviderSpec
    healthy: bool
    detail: str
    models: tuple[ModelInfo, ...]
    active_runs: int
    taken_at: float


@dataclass
class _CacheEntry:
    taken_at: float = 0.0
    healthy: bool = False
    detail: str = "never probed"
    models: tuple[ModelInfo, ...] = ()
    #: Inventory from the last *healthy* probe. Retained across outages so the
    #: OpenAI layer can answer 503 (known but temporarily unavailable) instead
    #: of 404 (never existed) for models whose provider is currently down.
    last_known_models: tuple[ModelInfo, ...] = ()


class ProviderRegistry:
    def __init__(
        self,
        providers: list[ProviderSpec],
        runs: RunRegistry,
        *,
        snapshot_ttl: float = 5.0,
    ) -> None:
        ids = [p.id for p in providers]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate provider ids: {ids}")
        self._providers = list(providers)
        self._runs = runs
        self._ttl = snapshot_ttl
        self._cache: dict[str, _CacheEntry] = {p.id: _CacheEntry() for p in providers}

    @property
    def providers(self) -> list[ProviderSpec]:
        return list(self._providers)

    def provider(self, provider_id: str) -> ProviderSpec | None:
        for p in self._providers:
            if p.id == provider_id:
                return p
        return None

    async def _refresh(self, spec: ProviderSpec, entry: _CacheEntry) -> None:
        try:
            health = await spec.engine.health()
            status = str(health.get("status", "unknown"))
            if status in ("unreachable", "down", "error"):
                entry.healthy, entry.detail, entry.models = False, f"engine {status}", ()
            else:
                models = await spec.engine.list_models()
                entry.healthy, entry.detail = True, status
                entry.models = tuple(models)
                entry.last_known_models = entry.models
        except Exception as exc:  # an outage is a refusal, not an exception
            entry.healthy, entry.detail, entry.models = False, f"unreachable: {exc}", ()
        entry.taken_at = time.time()

    async def snapshot(self, *, max_age: float | None = None) -> tuple[ProviderSnapshot, ...]:
        """Cached view of all providers; refreshes entries older than the TTL."""
        ttl = self._ttl if max_age is None else max_age
        now = time.time()
        out: list[ProviderSnapshot] = []
        for spec in self._providers:
            entry = self._cache[spec.id]
            if now - entry.taken_at > ttl:
                await self._refresh(spec, entry)
            out.append(
                ProviderSnapshot(
                    spec=spec,
                    healthy=entry.healthy,
                    detail=entry.detail,
                    models=entry.models,
                    active_runs=self._runs.active_count(spec.id),
                    taken_at=entry.taken_at,
                )
            )
        return tuple(out)

    def known_but_unhealthy(self, model_id: str, *, cloud_permitted: bool) -> bool:
        """True when ``model_id`` was in the last *healthy* inventory of a
        provider that is currently unhealthy.

        Distinguishes "temporarily down" from "never existed" using only
        retained cache state — no probe. Cloud providers are skipped unless
        ``cloud_permitted``, so this can never leak the existence of a model
        the caller's effective privacy tier forbids anyway. Reads the cache
        as-is: call after ``snapshot()`` for a fresh view. Best-effort and
        process-local — after a restart with the provider still down there is
        no last-healthy inventory and the answer is False.
        """
        for spec in self._providers:
            if spec.placement_class == "cloud" and not cloud_permitted:
                continue
            entry = self._cache[spec.id]
            if entry.healthy:
                continue
            if any(m.id == model_id for m in entry.last_known_models):
                return True
        return False

    def invalidate(self) -> None:
        """Force the next snapshot to re-probe every provider."""
        for entry in self._cache.values():
            entry.taken_at = 0.0

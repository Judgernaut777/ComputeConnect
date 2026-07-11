"""Candidate filtering, placement, and estimation.

The pipeline is staged, and the stages are ordered by construction:

    snapshots ──▶ filter_candidates(privacy) ──▶ CandidateSet ──▶ select / estimate

``filter_candidates`` is the **only** constructor of :class:`CandidateSet`,
and :func:`select_placement` / :func:`estimate` accept **only** a
``CandidateSet`` — never a registry or a raw provider list. Placement
therefore cannot select a cloud provider that privacy filtering removed,
because it never sees one (ARCHITECTURE §6, property 2: "placement cannot
select what it never saw"). The default-deny itself lives in
``privacy.resolve_privacy_tier`` — missing/unknown tiers resolve to
cloud-denied before this module is ever consulted.

An empty candidate set produces a :class:`PlacementRefusal` with a
machine-readable reason: a structured refusal, never a silent downgrade
(property 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engines import ModelInfo
from .privacy import ResolvedPrivacy
from .providers import ProviderSnapshot

#: Seconds of queue estimated per run already waiting beyond capacity.
_QUEUE_SECONDS_PER_BACKLOGGED_RUN = 30.0


@dataclass(frozen=True)
class Rejection:
    provider_id: str
    stage: str  # "privacy" | "health" | "capability" | "model" | "context"
    reason: str

    def to_dict(self) -> dict:
        return {"provider_id": self.provider_id, "stage": self.stage, "reason": self.reason}


@dataclass(frozen=True)
class CandidateSet:
    """The privacy-filtered candidate providers.

    Only :func:`filter_candidates` builds one. Everything downstream of
    privacy filtering works exclusively on this type.
    """

    privacy: ResolvedPrivacy
    candidates: tuple[ProviderSnapshot, ...]
    rejections: tuple[Rejection, ...]

    def contains(self, provider_id: str) -> bool:
        return any(s.spec.id == provider_id for s in self.candidates)


def filter_candidates(
    snapshots: tuple[ProviderSnapshot, ...], privacy: ResolvedPrivacy
) -> CandidateSet:
    """Stage 1 — remove every provider the resolved privacy tier forbids.

    This runs before placement ever sees a provider. A ``cloud`` placement
    class survives only when ``privacy.cloud_permitted`` is True.
    """
    kept: list[ProviderSnapshot] = []
    rejected: list[Rejection] = []
    for snap in snapshots:
        if snap.spec.placement_class == "cloud" and not privacy.cloud_permitted:
            rejected.append(
                Rejection(
                    provider_id=snap.spec.id,
                    stage="privacy",
                    reason=(
                        "cloud_not_permitted: effective tier "
                        f"'{privacy.effective}'"
                        + (" (assumed: tier missing or unknown)" if privacy.assumed else "")
                    ),
                )
            )
        else:
            kept.append(snap)
    return CandidateSet(privacy=privacy, candidates=tuple(kept), rejections=tuple(rejected))


@dataclass(frozen=True)
class Placement:
    provider: ProviderSnapshot
    model: ModelInfo
    rationale: dict


@dataclass(frozen=True)
class PlacementRefusal:
    """A structured refusal: machine-readable, never a downgrade."""

    code: str
    privacy: ResolvedPrivacy
    rejections: tuple[Rejection, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "detail": self.detail,
            "privacy": self.privacy.to_dict(),
            "rejected": [r.to_dict() for r in self.rejections],
        }


@dataclass(frozen=True)
class WorkloadSpec:
    """What the caller needs — from /route/estimate, /generate, or /v1."""

    model: str | None = None
    required_capabilities: tuple[str, ...] = ()
    context_tokens: int = 0
    max_output_tokens: int = 0
    metadata: dict = field(default_factory=dict)


def _queue_seconds(snap: ProviderSnapshot) -> float:
    backlog = snap.active_runs - snap.spec.max_concurrency + 1
    if backlog <= 0:
        return 0.0
    return backlog * _QUEUE_SECONDS_PER_BACKLOGGED_RUN


def select_placement(
    candidates: CandidateSet, workload: WorkloadSpec
) -> Placement | PlacementRefusal:
    """Stages 2..n — health, model match, capability, and context fit.

    Preference among eligible providers: local placement class first, then
    loaded model, then the shortest queue. Deterministic; ties broken by
    registration order.
    """
    rejections: list[Rejection] = list(candidates.rejections)
    eligible: list[tuple[ProviderSnapshot, ModelInfo]] = []

    for snap in candidates.candidates:
        pid = snap.spec.id
        if not snap.healthy:
            rejections.append(Rejection(pid, "health", snap.detail))
            continue
        missing = [
            c for c in workload.required_capabilities if c not in snap.spec.capabilities
        ]
        if missing:
            rejections.append(
                Rejection(pid, "capability", f"missing capabilities: {missing}")
            )
            continue
        models = list(snap.models)
        if workload.model:
            models = [m for m in models if m.id == workload.model]
            if not models:
                rejections.append(
                    Rejection(pid, "model", f"model '{workload.model}' not served here")
                )
                continue
        needed = workload.context_tokens + workload.max_output_tokens
        fitting = [m for m in models if m.context_tokens <= 0 or m.context_tokens >= needed]
        if not fitting:
            rejections.append(
                Rejection(
                    pid,
                    "context",
                    f"needs {needed} tokens; largest window is "
                    f"{max((m.context_tokens for m in models), default=0)}",
                )
            )
            continue
        # Prefer a loaded model, then the largest window (deterministic).
        fitting.sort(key=lambda m: (not m.loaded, -m.context_tokens, m.id))
        eligible.append((snap, fitting[0]))

    if not eligible:
        return PlacementRefusal(
            code="no_compliant_provider",
            privacy=candidates.privacy,
            rejections=tuple(rejections),
            detail="no provider satisfies privacy, health, capability, and fit constraints",
        )

    eligible.sort(
        key=lambda pair: (
            pair[0].spec.placement_class != "local",  # local first
            not pair[1].loaded,
            _queue_seconds(pair[0]),
        )
    )
    snap, model = eligible[0]
    return Placement(
        provider=snap,
        model=model,
        rationale={
            "provider_id": snap.spec.id,
            "placement_class": snap.spec.placement_class,
            "model": model.id,
            "privacy": candidates.privacy.to_dict(),
            "rejected": [r.to_dict() for r in rejections],
            "active_runs": snap.active_runs,
            "estimated_queue_seconds": _queue_seconds(snap),
        },
    )


def estimate(candidates: CandidateSet, workload: WorkloadSpec) -> dict:
    """The /route/estimate wire response. Pure over the snapshot — no I/O."""
    outcome = select_placement(candidates, workload)
    if isinstance(outcome, PlacementRefusal):
        return {
            "eligible": False,
            "selected_model": None,
            "runtime": None,
            "loaded": False,
            "estimated_queue_seconds": None,
            "estimated_tokens_per_second": None,
            "estimated_quality": None,
            "reason": outcome.to_dict(),
        }
    snap, model = outcome.provider, outcome.model
    return {
        "eligible": True,
        "selected_model": model.id,
        "runtime": getattr(snap.spec.engine, "name", "unknown"),
        "loaded": model.loaded,
        "estimated_queue_seconds": _queue_seconds(snap),
        "estimated_tokens_per_second": snap.spec.estimated_tokens_per_second,
        "estimated_quality": snap.spec.estimated_quality,
        "reason": outcome.rationale,
    }

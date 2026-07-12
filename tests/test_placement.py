"""Placement selection logic: capability, model match, context fit, preference."""

from __future__ import annotations

import time

from computeconnect.engines import ModelInfo, SimulatedCloudEngine
from computeconnect.placement import (
    CandidateSet,
    PlacementRefusal,
    WorkloadSpec,
    estimate,
    filter_candidates,
    select_placement,
)
from computeconnect.privacy import resolve_privacy_tier
from computeconnect.providers import ProviderSnapshot, ProviderSpec


def snap(
    pid: str,
    placement_class: str = "local",
    healthy: bool = True,
    caps: tuple = ("generate",),
    models: tuple = (),
    active_runs: int = 0,
    max_concurrency: int = 2,
) -> ProviderSnapshot:
    return ProviderSnapshot(
        spec=ProviderSpec(
            id=pid,
            placement_class=placement_class,
            engine=SimulatedCloudEngine(),
            capabilities=caps,
            max_concurrency=max_concurrency,
        ),
        healthy=healthy,
        detail="ok" if healthy else "down",
        models=models,
        active_runs=active_runs,
        taken_at=time.time(),
    )


M_SMALL = ModelInfo("small", 2048, ("generate",), True)
M_BIG = ModelInfo("big", 32768, ("generate",), True)
M_UNLOADED = ModelInfo("cold", 32768, ("generate",), False)

PUBLIC = resolve_privacy_tier("public")


def test_model_match_is_exact():
    cands = filter_candidates((snap("a", models=(M_SMALL, M_BIG)),), PUBLIC)
    placed = select_placement(cands, WorkloadSpec(model="big"))
    assert placed.model.id == "big"
    refused = select_placement(cands, WorkloadSpec(model="nope"))
    assert isinstance(refused, PlacementRefusal)
    assert any(r.stage == "model" for r in refused.rejections)


def test_context_fit_rejects_too_small_windows():
    cands = filter_candidates((snap("a", models=(M_SMALL,)),), PUBLIC)
    refused = select_placement(
        cands, WorkloadSpec(context_tokens=4000, max_output_tokens=1000)
    )
    assert isinstance(refused, PlacementRefusal)
    assert any(r.stage == "context" for r in refused.rejections)
    # A big window fits.
    cands = filter_candidates((snap("a", models=(M_SMALL, M_BIG)),), PUBLIC)
    placed = select_placement(
        cands, WorkloadSpec(context_tokens=4000, max_output_tokens=1000)
    )
    assert placed.model.id == "big"


def test_loaded_model_preferred_over_cold_one():
    cands = filter_candidates((snap("a", models=(M_UNLOADED, M_BIG)),), PUBLIC)
    placed = select_placement(cands, WorkloadSpec())
    assert placed.model.id == "big"


def test_local_class_preferred_when_both_eligible():
    fleet = (
        snap("cloudy", placement_class="cloud", models=(M_BIG,)),
        snap("homely", placement_class="local", models=(M_SMALL,)),
    )
    placed = select_placement(filter_candidates(fleet, PUBLIC), WorkloadSpec())
    assert placed.provider.spec.id == "homely"


def test_unhealthy_provider_rejected_with_stage_health():
    fleet = (snap("a", healthy=False, models=(M_BIG,)), snap("b", models=(M_SMALL,)))
    placed = select_placement(filter_candidates(fleet, PUBLIC), WorkloadSpec())
    assert placed.provider.spec.id == "b"
    rejected = placed.rationale["rejected"]
    assert any(r["provider_id"] == "a" and r["stage"] == "health" for r in rejected)


def test_estimate_wire_shape_matches_contract():
    cands = filter_candidates((snap("a", models=(M_BIG,)),), PUBLIC)
    result = estimate(cands, WorkloadSpec())
    assert set(result) == {
        "eligible",
        "selected_model",
        "runtime",
        "loaded",
        "estimated_queue_seconds",
        "estimated_tokens_per_second",
        "estimated_quality",
        "reason",
    }
    assert result["eligible"] is True
    assert result["selected_model"] == "big"
    assert result["loaded"] is True
    assert result["estimated_queue_seconds"] == 0.0


def test_queue_estimate_grows_past_capacity():
    cands = filter_candidates(
        (snap("a", models=(M_BIG,), active_runs=3, max_concurrency=2),), PUBLIC
    )
    result = estimate(cands, WorkloadSpec())
    assert result["eligible"] is True
    assert result["estimated_queue_seconds"] > 0


def _two_locals(fast_tps=80.0, fast_q=0.4, slow_tps=8.0, slow_q=0.9):
    """Two same-class (local) providers differing only in speed and quality —
    the minimal shape of a real heterogeneous fleet (a small fast model vs a
    large accurate one). ``fast`` serves an 8k model, ``slow`` a 32k model."""
    fast = ProviderSnapshot(
        spec=ProviderSpec(
            id="fast-small",
            placement_class="local",
            engine=SimulatedCloudEngine(),
            capabilities=("generate",),
            estimated_tokens_per_second=fast_tps,
            estimated_quality=fast_q,
        ),
        healthy=True,
        detail="ok",
        models=(ModelInfo("small-fast", 8192, ("generate",), True),),
        active_runs=0,
        taken_at=time.time(),
    )
    slow = ProviderSnapshot(
        spec=ProviderSpec(
            id="slow-big",
            placement_class="local",
            engine=SimulatedCloudEngine(),
            capabilities=("generate",),
            estimated_tokens_per_second=slow_tps,
            estimated_quality=slow_q,
        ),
        healthy=True,
        detail="ok",
        models=(ModelInfo("big-accurate", 32768, ("generate",), True),),
        active_runs=0,
        taken_at=time.time(),
    )
    return (fast, slow)


class TestHeterogeneousPreference:
    """Deliverable 1: the placement policy must select between two same-class
    nodes by capacity/latency/quality — a decision a static router cannot make."""

    def test_latency_preference_picks_the_fast_node(self):
        cands = filter_candidates(_two_locals(), PUBLIC)
        placed = select_placement(
            cands, WorkloadSpec(latency_preference="low_latency")
        )
        assert placed.provider.spec.id == "fast-small"

    def test_quality_preference_picks_the_accurate_node(self):
        cands = filter_candidates(_two_locals(), PUBLIC)
        placed = select_placement(cands, WorkloadSpec(quality_preference="high"))
        assert placed.provider.spec.id == "slow-big"

    def test_capacity_need_overrides_preference_via_context_fit(self):
        """A large-context workload only fits the big node's window, so it wins
        even under a latency preference — capability beats preference."""
        cands = filter_candidates(_two_locals(), PUBLIC)
        placed = select_placement(
            cands,
            WorkloadSpec(
                context_tokens=9000,  # exceeds the fast node's 8192 window
                max_output_tokens=1000,
                latency_preference="low_latency",
            ),
        )
        assert placed.provider.spec.id == "slow-big"
        assert placed.model.id == "big-accurate"

    def test_default_preference_is_stable_and_deterministic(self):
        cands = filter_candidates(_two_locals(), PUBLIC)
        # No preference: both loaded, both zero queue -> deterministic by id.
        a = select_placement(cands, WorkloadSpec()).provider.spec.id
        b = select_placement(cands, WorkloadSpec()).provider.spec.id
        assert a == b

    def test_rationale_exposes_the_considered_set(self):
        cands = filter_candidates(_two_locals(), PUBLIC)
        placed = select_placement(cands, WorkloadSpec(latency_preference="fast"))
        ids = {c["provider_id"] for c in placed.rationale["considered"]}
        assert ids == {"fast-small", "slow-big"}
        assert placed.rationale["latency_preference"] == "fast"


class TestStalenessCeiling:
    """Deliverable 5: a stale snapshot is fail-closed, and can never cause a
    privacy-wrong cloud placement."""

    def _aged(self, snapshot, age_seconds):
        # Rebuild the snapshot with an older taken_at.
        return ProviderSnapshot(
            spec=snapshot.spec,
            healthy=snapshot.healthy,
            detail=snapshot.detail,
            models=snapshot.models,
            active_runs=snapshot.active_runs,
            taken_at=time.time() - age_seconds,
        )

    def test_stale_snapshot_is_rejected_not_trusted(self):
        cands = filter_candidates((snap("a", models=(M_BIG,)),), PUBLIC)
        aged = _reaged(cands, [self._aged(cands.candidates[0], 120.0)])
        outcome = select_placement(
            aged, WorkloadSpec(), max_snapshot_age=30.0
        )
        assert isinstance(outcome, PlacementRefusal)
        stages = {r.stage for r in outcome.rejections}
        assert "stale" in stages

    def test_fresh_snapshot_under_ceiling_still_places(self):
        cands = filter_candidates((snap("a", models=(M_BIG,)),), PUBLIC)
        outcome = select_placement(cands, WorkloadSpec(), max_snapshot_age=30.0)
        assert not isinstance(outcome, PlacementRefusal)

    def test_stale_cloud_never_placed_under_restrictive_tier(self):
        """Even a stale, cloud-permitting-if-it-were-fresh snapshot cannot be
        placed under a restrictive tier: privacy filtering is structural, so it
        removed the cloud provider before staleness was ever considered."""
        from computeconnect.privacy import resolve_privacy_tier

        fleet = (
            snap("local", models=(M_BIG,)),
            snap("cloudy", placement_class="cloud", models=(M_BIG,)),
        )
        # local_only: cloud filtered structurally; then age everything out.
        cands = filter_candidates(fleet, resolve_privacy_tier("local_only"))
        assert not cands.contains("cloudy")
        aged = _reaged(cands, [self._aged(s, 999.0) for s in cands.candidates])
        outcome = select_placement(aged, WorkloadSpec(), max_snapshot_age=30.0)
        assert isinstance(outcome, PlacementRefusal)
        # The refusal cites privacy (cloud removed) and staleness (local aged),
        # never a cloud placement.
        assert outcome.privacy.cloud_permitted is False


def _reaged(base: CandidateSet, aged_candidates) -> CandidateSet:
    """A CandidateSet with its candidate tuple swapped for aged snapshots,
    preserving privacy and rejections — exercises the staleness path."""
    return CandidateSet(
        privacy=base.privacy,
        candidates=tuple(aged_candidates),
        rejections=base.rejections,
    )

"""Placement selection logic: capability, model match, context fit, preference."""

from __future__ import annotations

import time

from computeconnect.engines import ModelInfo, SimulatedCloudEngine
from computeconnect.placement import (
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

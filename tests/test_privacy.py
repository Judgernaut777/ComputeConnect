"""The structural privacy invariant, tested hard (ARCHITECTURE §6 / D5).

Unknown, missing, malformed, or restrictive tiers must never yield a cloud
candidate — before placement, not after — and an empty candidate set must be
a structured refusal, never a downgrade.
"""

from __future__ import annotations

import inspect
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
from computeconnect.privacy import (
    CLOUD_PERMITTING_TIERS,
    KNOWN_TIERS,
    MOST_RESTRICTIVE_TIER,
    resolve_privacy_tier,
)
from computeconnect.providers import ProviderSnapshot, ProviderSpec

NON_PERMITTING_INPUTS = [
    None,
    "",
    "   ",
    "local_only",
    "secret_sensitive",
    "repo_sensitive",
    "LOCAL_ONLY",  # wrong case: unknown, therefore clamped
    "totally-made-up-tier",
    "public ",  # trailing space is stripped and permits; " public" too — tested below
    123,
    {"tier": "public"},
    ["public"],
]
# "public " strips to a known tier; remove it from the deny list and assert separately.
NON_PERMITTING_INPUTS.remove("public ")


def _snap(spec: ProviderSpec, healthy: bool = True, models: tuple = ()) -> ProviderSnapshot:
    return ProviderSnapshot(
        spec=spec,
        healthy=healthy,
        detail="ok" if healthy else "unreachable: test",
        models=models,
        active_runs=0,
        taken_at=time.time(),
    )


def _fleet(local_healthy: bool = True, local_caps: tuple = ("generate",)):
    local = ProviderSpec(
        id="local-a",
        placement_class="local",
        engine=SimulatedCloudEngine(),
        capabilities=local_caps,
    )
    cloud = ProviderSpec(
        id="cloud-b",
        placement_class="cloud",
        engine=SimulatedCloudEngine(),
        capabilities=("generate", "cloud-batch"),
    )
    local_models = (ModelInfo("m-local", 8192, ("generate",), True),)
    cloud_models = (ModelInfo("m-cloud", 131072, ("generate",), True),)
    return (
        _snap(local, healthy=local_healthy, models=local_models),
        _snap(cloud, healthy=True, models=cloud_models),
    )


class TestResolvePrivacyTier:
    def test_missing_and_unknown_default_to_most_restrictive(self):
        for raw in NON_PERMITTING_INPUTS:
            resolved = resolve_privacy_tier(raw)
            assert resolved.cloud_permitted is False, raw
            if not (isinstance(raw, str) and raw.strip() in KNOWN_TIERS):
                assert resolved.effective == MOST_RESTRICTIVE_TIER, raw
                assert resolved.assumed is True, raw

    def test_known_restrictive_tiers_deny_cloud_without_assumption(self):
        for tier in sorted(KNOWN_TIERS - CLOUD_PERMITTING_TIERS):
            resolved = resolve_privacy_tier(tier)
            assert resolved.cloud_permitted is False
            assert resolved.assumed is False
            assert resolved.effective == tier

    def test_only_explicit_permitting_tiers_permit_cloud(self):
        for tier in sorted(CLOUD_PERMITTING_TIERS):
            resolved = resolve_privacy_tier(tier)
            assert resolved.cloud_permitted is True
            assert resolved.assumed is False
        # Whitespace around a known tier is tolerated, not treated as unknown.
        assert resolve_privacy_tier("public ").cloud_permitted is True


class TestStructuralFiltering:
    def test_cloud_removed_before_placement_for_every_denying_input(self):
        for raw in NON_PERMITTING_INPUTS:
            candidates = filter_candidates(_fleet(), resolve_privacy_tier(raw))
            assert all(
                s.spec.placement_class != "cloud" for s in candidates.candidates
            ), raw
            assert any(
                r.provider_id == "cloud-b" and r.stage == "privacy"
                for r in candidates.rejections
            ), raw

    def test_placement_never_reaches_cloud_even_when_local_cannot_serve(self):
        """The hard case: local is down or incapable, cloud could serve.

        A downgrade would pick cloud; the invariant demands refusal.
        """
        scenarios = {
            "local_down": _fleet(local_healthy=False),
            "local_missing_capability": _fleet(local_caps=("something-else",)),
        }
        for name, fleet in scenarios.items():
            for raw in NON_PERMITTING_INPUTS:
                candidates = filter_candidates(fleet, resolve_privacy_tier(raw))
                outcome = select_placement(
                    candidates, WorkloadSpec(required_capabilities=("generate",))
                )
                assert isinstance(outcome, PlacementRefusal), (name, raw)
                assert outcome.code == "no_compliant_provider", (name, raw)
                refusal = outcome.to_dict()
                stages = {r["stage"] for r in refusal["rejected"]}
                assert "privacy" in stages, (name, raw)
                assert refusal["privacy"]["cloud_permitted"] is False

    def test_explicit_permit_puts_cloud_back(self):
        candidates = filter_candidates(_fleet(), resolve_privacy_tier("public"))
        assert candidates.contains("cloud-b")
        # Local-first: even permitted, local wins when it can serve.
        outcome = select_placement(candidates, WorkloadSpec())
        assert outcome.provider.spec.id == "local-a"

    def test_simulated_second_provider_changes_the_placement_decision(self):
        """ARCHITECTURE §9 falsification check: the second provider must be able
        to change a placement decision, or the policy has no content."""
        candidates = filter_candidates(_fleet(), resolve_privacy_tier("public"))
        outcome = select_placement(
            candidates, WorkloadSpec(required_capabilities=("cloud-batch",))
        )
        assert outcome.provider.spec.id == "cloud-b"
        # Same workload, tier withheld: structured refusal, not a downgrade
        # (and definitely not cloud).
        denied = filter_candidates(_fleet(), resolve_privacy_tier(None))
        refused = select_placement(denied, WorkloadSpec(required_capabilities=("cloud-batch",)))
        assert isinstance(refused, PlacementRefusal)

    def test_estimate_refusal_is_machine_readable(self):
        candidates = filter_candidates(
            _fleet(local_healthy=False), resolve_privacy_tier(None)
        )
        result = estimate(candidates, WorkloadSpec())
        assert result["eligible"] is False
        assert result["selected_model"] is None
        assert result["reason"]["code"] == "no_compliant_provider"
        assert isinstance(result["reason"]["rejected"], list)

    def test_pipeline_is_structural_by_signature(self):
        """Placement and estimate accept only a CandidateSet — the type that
        exists solely as the output of the privacy filter. There is no
        registry- or list-of-providers entry point to bypass the filter."""
        for fn in (select_placement, estimate):
            params = list(inspect.signature(fn).parameters.values())
            assert params[0].annotation in ("CandidateSet", CandidateSet)

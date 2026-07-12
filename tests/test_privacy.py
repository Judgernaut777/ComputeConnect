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
    PRIVACY_STRICTNESS,
    resolve_privacy_precedence,
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

class TestPrivacyPrecedence:
    """Deliverable 3 + 2: header/body precedence is unambiguous (more
    restrictive wins) and default-deny survives conflicting inputs."""

    def test_strictness_mirror_matches_agentconnect(self):
        """Our strictness order must byte-mirror AgentConnect's, or the
        precedence rule silently disagrees with what routing enforced."""
        import sys
        from pathlib import Path

        import os

        import pytest

        src = Path(
            os.environ.get(
                "AGENTCONNECT_CORE_SRC",
                "/home/mini/mcp-agentconnect/packages/agentconnect-core/src",
            )
        )
        if not src.is_dir():
            pytest.skip("mcp-agentconnect checkout not available")
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        try:
            from agentconnect.core.models import PRIVACY_STRICTNESS as AC
        except ImportError as exc:
            # The sibling package (or its deps, e.g. pydantic) is not installed in
            # this venv — a cross-repo mirror check, not a ComputeConnect failure.
            pytest.skip(f"agentconnect.core.models not importable here: {exc}")

        assert PRIVACY_STRICTNESS == {t.value: rank for t, rank in AC.items()}

    def test_no_signal_is_default_deny(self):
        r = resolve_privacy_precedence()
        assert r.effective == MOST_RESTRICTIVE_TIER
        assert r.assumed is True
        assert r.cloud_permitted is False
        # Absent header + absent body key both arrive as None.
        r2 = resolve_privacy_precedence(None, None)
        assert r2.cloud_permitted is False and r2.assumed is True

    def test_empty_or_whitespace_signals_do_not_clobber_a_real_one(self):
        # An empty X-Privacy-Tier header must not narrow a valid body tier.
        r = resolve_privacy_precedence("", "public")
        assert r.effective == "public" and r.cloud_permitted is True
        r = resolve_privacy_precedence("   ", "public")
        assert r.cloud_permitted is True
        # ...and with only an empty header, it is still default-deny.
        assert resolve_privacy_precedence("", None).cloud_permitted is False

    def test_single_signal_from_either_channel(self):
        assert resolve_privacy_precedence("public", None).cloud_permitted is True
        assert resolve_privacy_precedence(None, "public").cloud_permitted is True
        assert resolve_privacy_precedence("local_only", None).cloud_permitted is False

    def test_header_cannot_widen_a_more_restrictive_body(self):
        """The exact LOW finding: a permissive header must not override a
        more-restrictive body."""
        r = resolve_privacy_precedence("public", "local_only")  # header, body
        assert r.effective == "local_only"
        assert r.cloud_permitted is False
        # And symmetrically, a permissive body cannot widen a restrictive header.
        r = resolve_privacy_precedence("secret_sensitive", "public")
        assert r.effective == "secret_sensitive"
        assert r.cloud_permitted is False

    def test_more_restrictive_wins_regardless_of_argument_order(self):
        a = resolve_privacy_precedence("public", "repo_sensitive")
        b = resolve_privacy_precedence("repo_sensitive", "public")
        assert a.effective == b.effective == "repo_sensitive"
        assert a.cloud_permitted is b.cloud_permitted is False

    def test_both_permissive_permits_cloud(self):
        r = resolve_privacy_precedence("public", "public_redacted")
        assert r.cloud_permitted is True
        assert r.effective == "public_redacted"  # the stricter of the two

    def test_garbage_present_signal_fails_closed_against_a_valid_one(self):
        # A present-but-garbage signal resolves to most-restrictive and wins.
        for garbage in ("definitely-not-a-tier", 123, {"tier": "public"}, ["public"]):
            r = resolve_privacy_precedence(garbage, "public")
            assert r.cloud_permitted is False, garbage
            assert r.effective == MOST_RESTRICTIVE_TIER, garbage

    def test_conflicting_inputs_property_never_widen_cloud(self):
        """Property: for every (header, body) pair, the precedence result is
        never *more* permissive than the strictest cloud-permitting single
        input would allow. Cloud is permitted only if BOTH resolve to a
        cloud-permitting tier."""
        signals = list(NON_PERMITTING_INPUTS) + ["public", "public_redacted", "public "]
        for h in signals:
            for b in signals:
                r = resolve_privacy_precedence(h, b)
                rh, rb = resolve_privacy_tier(h), resolve_privacy_tier(b)
                # cloud permitted iff every *supplied* channel permits it.
                from computeconnect.privacy import _supplied

                supplied = [x for x in (h, b) if _supplied(x)]
                if not supplied:
                    expect = False
                else:
                    expect = all(
                        resolve_privacy_tier(x).cloud_permitted for x in supplied
                    )
                assert r.cloud_permitted is expect, (h, b)


class TestStructuralFilteringSig:
    def test_pipeline_is_structural_by_signature(self):
        """Placement and estimate accept only a CandidateSet — the type that
        exists solely as the output of the privacy filter. There is no
        registry- or list-of-providers entry point to bypass the filter."""
        for fn in (select_placement, estimate):
            params = list(inspect.signature(fn).parameters.values())
            assert params[0].annotation in ("CandidateSet", CandidateSet)

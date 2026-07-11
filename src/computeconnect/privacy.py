"""Privacy-tier resolution — the input side of the structural invariant.

The tier vocabulary is AgentConnect's (``agentconnect.core.models.PrivacyTier``):
``public | public_redacted | repo_sensitive | secret_sensitive | local_only``.

The invariant (ARCHITECTURE §6, D5): **cloud execution is default-denied.**
Only an explicit, cloud-permitting tier ever puts cloud candidates back in the
set. Unknown, missing, malformed, or restrictive tiers all resolve to the safe
state. ``resolve_privacy_tier`` is the only constructor of ``ResolvedPrivacy``,
so every consumer downstream is working from a value that already made the
default-deny decision — there is no code path that inspects a raw tier string
at placement or execution time.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Tiers that explicitly permit cloud execution. Everything else — including
#: every string not in KNOWN_TIERS — denies it.
CLOUD_PERMITTING_TIERS = frozenset({"public", "public_redacted"})

KNOWN_TIERS = frozenset(
    {"public", "public_redacted", "repo_sensitive", "secret_sensitive", "local_only"}
)

#: What a missing/unknown tier is assumed to be (CA-1: "absent means the most
#: restrictive tier"). ``secret_sensitive`` is the strictest tier in
#: AgentConnect's PRIVACY_STRICTNESS ordering.
MOST_RESTRICTIVE_TIER = "secret_sensitive"


@dataclass(frozen=True)
class ResolvedPrivacy:
    """A privacy tier after the default-deny resolution.

    ``declared`` is what the caller sent (possibly None or garbage);
    ``effective`` is the tier actually enforced; ``assumed`` is True when the
    effective tier was clamped rather than taken from the caller.
    """

    declared: str | None
    effective: str
    assumed: bool
    cloud_permitted: bool

    def to_dict(self) -> dict:
        return {
            "declared": self.declared,
            "effective": self.effective,
            "assumed": self.assumed,
            "cloud_permitted": self.cloud_permitted,
        }


def resolve_privacy_tier(raw: object) -> ResolvedPrivacy:
    """Resolve a caller-supplied privacy tier, defaulting closed.

    * ``None``, empty, or non-string  → most restrictive tier, assumed.
    * A string not in the known vocabulary → most restrictive tier, assumed.
    * A known tier → itself; cloud permitted only for CLOUD_PERMITTING_TIERS.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ResolvedPrivacy(
            declared=None if raw is None else (raw if isinstance(raw, str) else str(raw)),
            effective=MOST_RESTRICTIVE_TIER,
            assumed=True,
            cloud_permitted=False,
        )
    tier = raw.strip()
    if tier not in KNOWN_TIERS:
        return ResolvedPrivacy(
            declared=tier,
            effective=MOST_RESTRICTIVE_TIER,
            assumed=True,
            cloud_permitted=False,
        )
    return ResolvedPrivacy(
        declared=tier,
        effective=tier,
        assumed=False,
        cloud_permitted=tier in CLOUD_PERMITTING_TIERS,
    )

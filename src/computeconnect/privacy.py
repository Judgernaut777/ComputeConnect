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

#: Strictness order, loosest first — a byte-for-byte mirror of AgentConnect's
#: ``agentconnect.core.models.PRIVACY_STRICTNESS`` (loosest ``public`` = 0,
#: strictest ``secret_sensitive`` = 4). Used only to compare two *resolved*
#: tiers when a caller supplies more than one privacy signal (header + body):
#: the **more restrictive** one wins, so a header can never widen a
#: more-restrictive body (CONTRACT.md "Privacy precedence").
PRIVACY_STRICTNESS = {
    "public": 0,
    "public_redacted": 1,
    "repo_sensitive": 2,
    "local_only": 3,
    "secret_sensitive": 4,
}


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


def _strictness(resolved: ResolvedPrivacy) -> int:
    """Strictness rank of a *resolved* tier. Any tier not in the map (there
    should be none, since ``resolve_privacy_tier`` only ever emits known tiers)
    ranks as the most restrictive — fail closed."""
    return PRIVACY_STRICTNESS.get(
        resolved.effective, PRIVACY_STRICTNESS[MOST_RESTRICTIVE_TIER]
    )


def _supplied(raw: object) -> bool:
    """True when a caller actually supplied a privacy signal.

    A ``None`` (absent header, absent body key) or an empty/whitespace string
    (an empty ``X-Privacy-Tier:`` header) is treated as *not supplied*, so it
    never clobbers a genuine value from the other channel. A non-string,
    non-None value (``123``, a dict) IS supplied — it is garbage, and garbage
    must fail closed rather than be silently ignored.
    """
    if raw is None:
        return False
    if isinstance(raw, str) and not raw.strip():
        return False
    return True


def resolve_privacy_precedence(*raw_inputs: object) -> ResolvedPrivacy:
    """Resolve several privacy signals (e.g. an ``X-Privacy-Tier`` header and a
    body ``privacy_tier``) into one, taking the **more restrictive**.

    Precedence rules (CONTRACT.md "Privacy precedence"):

    * No signal supplied → most restrictive tier, assumed (default deny).
    * Exactly one supplied → that one, resolved normally.
    * Two or more supplied → each is resolved independently and the strictest
      resolved tier wins. A header can therefore only ever *narrow* a body
      tier, never widen it; conflicting or malformed inputs fail closed.

    Order of ``raw_inputs`` is irrelevant to the outcome (it is a max over
    strictness); it only breaks exact ties, which are indistinguishable anyway.
    """
    supplied = [r for r in raw_inputs if _supplied(r)]
    if not supplied:
        return resolve_privacy_tier(None)
    resolved = [resolve_privacy_tier(r) for r in supplied]
    return max(resolved, key=_strictness)

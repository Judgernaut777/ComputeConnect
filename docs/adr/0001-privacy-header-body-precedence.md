# ADR 0001 — Header/body privacy precedence takes the more restrictive tier

**Status:** Accepted (2026-07-12, production-hardening pass); **Amended** (2026-07-12, Wave-E
hardening) to apply the precedence on the control plane as well — see "Amendment" below.

## Context

The OpenAI layer accepts a privacy tier from two channels: an `X-Privacy-Tier` header and a body
`privacy_tier`. The earlier implementation used `headers.get("x-privacy-tier", body.get(...))` —
header-if-present-else-body. A Wave-earlier LOW finding: a **permissive header could widen a
more-restrictive body**, e.g. a proxy-injected `X-Privacy-Tier: public` overriding a body
`local_only`, silently permitting cloud for data the caller marked local-only.

## Decision

When both are present, enforce the **more restrictive** of the two (AgentConnect's `PRIVACY_STRICTNESS`
order). A header can only narrow a body tier, never widen it. Rules: none present → most restrictive
(default deny); one present → that one; both → the stricter. A present-but-garbage value resolves to
most-restrictive and wins (fail closed). An empty/whitespace header is treated as absent so it does
not fail-close a valid body tier.

Implemented in `privacy.resolve_privacy_precedence`; `PRIVACY_STRICTNESS` byte-mirrors AgentConnect's
and a test asserts they stay equal.

## Consequences

* Fail-closed on conflict: a malformed or conflicting signal denies cloud, never widens it. This can
  make a legitimate `public` request fall back to local when a bad second signal is present — an
  availability cost accepted in exchange for never leaking under a privacy invariant.
* Consumers that set exactly one channel (AgentConnect sets only the body/subtask tier) are
  unaffected. Documented as binding in `docs/CONTRACT.md`.

## Amendment (2026-07-12, Wave-E hardening)

The original decision scoped `resolve_privacy_precedence` (header ⊕ body) to the **OpenAI layer
only**; the control-plane routes `/route/estimate` and `/generate` still read the body `privacy_tier`
alone (`resolve_privacy_tier`) and **ignored** the `X-Privacy-Tier` header. A Wave-E LOW finding:
a gateway that stamps a *more-restrictive* `X-Privacy-Tier` while an agent calls `/generate` directly
had that restriction silently dropped (body `public` ⇒ cloud permitted). That is the same
fail-open shape the OpenAI-layer fix closed, on a different door.

**Amended decision:** `/route/estimate` and `/generate` now resolve the header and body with the
**same** `resolve_privacy_precedence` (more-restrictive-wins). The header can only narrow, never
widen, the body tier on every tier-bearing route. Absent-header behavior is unchanged (body-only),
so existing single-channel consumers (AgentConnect) are unaffected. This **supersedes** the earlier
"header scoped to the OpenAI layer only" boundary; the precedence is now stated in `docs/CONTRACT.md`
as binding for **both** layers. Regression tests live in `tests/test_routes.py` (restrictive header
narrows a permissive body ⇒ `sim-cloud` `chat_requests == 0`; permissive header cannot widen a
restrictive body; absent header == prior behavior) alongside the existing OpenAI-layer tests.

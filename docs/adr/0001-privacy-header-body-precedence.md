# ADR 0001 — Header/body privacy precedence takes the more restrictive tier

**Status:** Accepted (2026-07-12, production-hardening pass)

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

# ADR 0004 — Fail-closed staleness ceiling + declarative config surface

**Status:** Accepted (2026-07-12, production-hardening pass)

## Context

Two loose ends. (1) Placement reads a TTL-cached snapshot; nothing bounded how stale a snapshot
could get before it was still trusted for a placement. (2) Registering a second engine required
editing code — the cross-repo LOW finding was that AgentConnect declares memory backends in
YAML/ENV but attaches compute providers programmatically.

## Decision

**Staleness ceiling.** `select_placement`/`estimate` accept `max_snapshot_age`; a candidate whose
snapshot is older than the ceiling is rejected at a new `stale` pipeline stage rather than trusted.
`AppConfig.effective_max_snapshot_age()` defaults it to a generous multiple of the snapshot TTL (a
backstop, not something that fires in normal operation). Because privacy filtering is structural (on
`placement_class`, not health), a stale snapshot can never cause a privacy-wrong cloud placement —
the ceiling only guards against trusting stale *capacity/health*.

**Config surface.** Add `computeconnect/config.py` (`--config` / `COMPUTECONNECT_CONFIG`): a JSON
(always) or YAML (if PyYAML installed) file declaring providers/engines, so a second engine needs no
code change. Separately, `docs/AGENTCONNECT_INTEGRATION.md` specifies the **AgentConnect-side**
env/yaml shape (`AGENTCONNECT_COMPUTE_URL` / a `compute:` block) that lets AgentConnect declare a
ComputeConnect URL the way it declares memory backends; that consumer change lives in the
AgentConnect repo and is noted for the lead.

## Consequences

* Grossly-stale capacity is fail-closed; the stale path is tested at unit and service level.
* Operators declare fleets in a file; the two-real-engine setup is reproducible without editing
  Python. PyYAML is an optional extra (`computeconnect[config]`); JSON needs nothing.
* The AgentConnect wiring is documented and agreed but intentionally **not** implemented here — it is
  a sibling-repo change.

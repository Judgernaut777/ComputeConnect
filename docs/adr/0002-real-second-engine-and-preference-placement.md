# ADR 0002 — Real second engine + preference-based placement (heterogeneity)

**Status:** Accepted (2026-07-12, production-hardening pass)

## Context

Through v0.1.0 the only second provider was a *simulated* cloud engine. STATUS.md's abandonment
re-eval (D2) correctly refused to claim heterogeneous-placement value from a simulation: "the
simulated provider must never be cited as evidence of heterogeneity." The open question was whether a
**real** heterogeneous placement decision is achievable on this single ARM box.

## Decision

Stand up a **second real llama.cpp engine** of materially different shape on a fresh port:
Qwen3-4B dense / 8k context on `:8091` alongside the reference Qwen3-30B-A3B MoE / 16k context on
`:8080` (`scripts/second_engine.sh`). Both are real CPU inference; nothing simulated.

Make placement able to choose between two same-class nodes: `WorkloadSpec` gains `latency_preference`
(fastest wins) and `quality_preference` (highest wins); hard constraints (capability, context-window
fit) always beat a soft preference. Declared `estimated_tokens_per_second` / `estimated_quality`
carry the operator's knowledge of each node. Default (no preference) ordering is unchanged
(local → loaded → queue), so no existing behavior regresses.

## Consequences

* A genuine heterogeneous decision is now demonstrable and demonstrated: the same workload class is
  placed on the 4B for latency, the 30B for quality, and the 30B for a >8k context that the 4B's
  window cannot fit — with real generations from both and a real failover when one is killed
  (`scratchpad/waveA/ComputeConnect/demo_two_real_engines.py`; real-engine tests in
  `tests/test_real_engine.py`, which skip when either engine is down and never fake it).
* `reason.considered` exposes the candidate set for auditability.
* The abandonment re-eval (D2) is updated: real single-box heterogeneity is now **demonstrated**
  for latency/quality/context-fit; the remaining unproven claim narrows to *cross-hardware-class*
  heterogeneity (an accelerator or a second physical node), which this box still cannot show.

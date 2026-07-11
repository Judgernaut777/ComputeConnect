# Contracts

The stable interface surface other products build against. This document is the **authority on what
does not change without a versioned amendment**. Design rationale lives in
[ARCHITECTURE.md](ARCHITECTURE.md); this file is the contract itself.

**No implementation exists.** These are specifications, not running endpoints.

---

## Two API layers

ComputeConnect exposes two distinct surfaces over **one execution backend**
([ARCHITECTURE §5](ARCHITECTURE.md#5-two-apis-one-backend)).

### Layer 1 — Control plane (consumed by AgentConnect)

The `LocalComputeProvider` surface AgentConnect already specifies and ships a client for.
ComputeConnect **conforms**; it does not redesign this.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Engine/provider liveness |
| GET | `/models` | Inventory |
| GET | `/models/loaded` | Resident-model state |
| POST | `/route/estimate` | Admission + selection; **carries `privacy_tier`** |
| POST | `/generate` | Thin streaming proxy (see below) |
| POST | `/runs/{run_id}/cancel` | Best-effort cancellation |

`/route/estimate` input: `{task_type, privacy_tier, required_capabilities, context_tokens,
max_output_tokens, latency_preference, quality_preference}`.
Output: `{eligible, selected_model, runtime, loaded, estimated_queue_seconds,
estimated_tokens_per_second, estimated_quality, reason}`.

### Layer 2 — Inference API (consumed by BrainConnect and direct applications)

A standard **OpenAI-compatible** endpoint (`/v1/chat/completions` and friends). It is the standard
inference interface every engine below already speaks — **not** a second routing layer. It carries
no placement semantics of its own.

---

## Binding invariants

These hold regardless of implementation. Breaking one is a breaking change.

1. **Direction is one-way.** AgentConnect → ComputeConnect. ComputeConnect never calls AgentConnect.
2. **An outage is a refusal, not an exception.** An unreachable ComputeConnect surfaces as
   `health.available = False`; it degrades a consumer, never crashes it.
3. **`/generate` is a thin streaming proxy.** ComputeConnect stays in the request path (AgentConnect
   reads output inline), streams without large in-memory buffering, and propagates cancellation and
   backpressure. It is as transparent as possible.
4. **Cloud execution is default-denied** (the structural privacy invariant,
   [ARCHITECTURE §6](ARCHITECTURE.md#6-structural-privacy-enforcement)). Unknown, missing, or
   local-only privacy ⇒ no cloud candidates, filtered **before** placement. No compliant provider ⇒
   a structured refusal. Never a silent downgrade.
5. **Both API layers reach one backend.** A fact true of one (model resident, node at capacity) is
   true of the other.

---

## Future amendments

Recorded, not required before implementation. Each is defense-in-depth or ergonomics, and the system
is specified to be correct and safe **without** them.

### CA-1 — Carry `privacy_tier` into `LocalRunRequest`

**Status:** proposed. **Owner of the change:** AgentConnect (it defines `LocalRunRequest`).

`LocalEstimateRequest` carries a required `privacy_tier`; `LocalRunRequest` does not. Execution
therefore cannot independently re-verify the privacy decision made at estimate time. Adding
`privacy_tier` to `LocalRunRequest` lets `/generate` **positively re-check** that the chosen provider
is permitted, rather than relying solely on the default-deny candidate filter.

**Not a prerequisite.** The structural invariant (§6) already makes the system safe, because the safe
state is the default and filtering precedes placement. CA-1 strengthens that to defense in depth; it
does not unblock it. Until CA-1 lands, `/generate` must assume the most restrictive tier when none is
present.

### CA-2 — Dispatch-by-reference for `/generate`

**Status:** proposed. **Owner of the change:** AgentConnect (its client reads output inline).

Today `/generate` must proxy because AgentConnect's client expects the generated output in the
response body. If a future client can accept a provider reference (`{endpoint, model, run_id}`) and
stream directly from the engine, ComputeConnect could leave the token hot path entirely and act as a
pure control plane. This also supplies the `run_id` that `/runs/{run_id}/cancel` requires but
`/generate` does not currently return.

**Not a prerequisite.** The thin streaming proxy (invariant 3) is the ratified design for now.

---

## Provisional / not yet contracted

* **ToolConnect.** Has a validated Phase 1 runtime but no execution/invoke surface, so there is no
  compute-facing contract to conform to. The boundary is drawn in
  [ARCHITECTURE §7.3](ARCHITECTURE.md#73-toolconnect-provisional); nothing may be built against it
  until ToolConnect exposes a compute-relevant surface.

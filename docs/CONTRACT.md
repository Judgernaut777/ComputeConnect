# Contracts

The stable interface surface other products build against. This document is the **authority on what
does not change without a versioned amendment**. Design rationale lives in
[ARCHITECTURE.md](ARCHITECTURE.md); this file is the contract itself.

**Implemented as of v0.1.0** by the `computeconnect` package in this repository
(`computeconnect serve`, default port **8090** — on the reference host 8080 is the external
llama.cpp engine and 8787 is reserved for BrainConnect). Two amendments are implemented and
documented below (CA-1, CA-3); CA-2 remains proposed.

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

v0.1.0 defines the previously-underdefined `estimated_quality` as an operator-declared heuristic
in `[0, 1]`, comparable only within one ComputeConnect deployment (ambiguity (4), ARCHITECTURE
§7.1). `/route/estimate` is served from a TTL-cached provider snapshot: it is cheap,
side-effect-free, and never touches a generation path. When ineligible, `reason` is the structured
refusal `{code, detail, privacy, rejected[]}` — every filtered provider appears in `rejected` with
the pipeline stage (`privacy | health | capability | model | context`) that removed it.

### Layer 2 — Inference API (consumed by BrainConnect and direct applications)

A standard **OpenAI-compatible** endpoint (`/v1/chat/completions` and friends). It is the standard
inference interface every engine below already speaks — **not** a second routing layer. It carries
no placement semantics of its own.

Implemented in v0.1.0: `GET /v1/models` and `POST /v1/chat/completions` (streaming SSE and
non-streaming). The layer reaches the same registry, run tracking, and generation path as Layer 1.
The structural privacy default applies here too: with no tier supplied, the most restrictive tier
is assumed and cloud-class providers are not candidates. A caller may supply an explicit tier via
the `X-Privacy-Tier` request header (or a `privacy_tier` body extension); an impermissible model
yields a structured `403` refusal (`error.type = "privacy_refusal"`), never a silent downgrade.
Responses carry an `X-Run-Id` header usable with `POST /runs/{run_id}/cancel`.

Model-resolution errors distinguish three cases so an OpenAI client can react correctly:

* **`404` `model_not_found`** — no provider is currently serving the model *and* it was not in the
  last healthy inventory of any (privacy-permitted) provider that is currently down.
* **`503` `model_temporarily_unavailable`** (`error.type = "service_unavailable"`) — the model was
  present in a provider's last *healthy* inventory and that provider is currently unhealthy:
  temporarily-down, not never-existed. Retry later. Providers the effective privacy tier filters
  out are ignored here, so this signal cannot leak the existence of a model the caller could never
  use.
* **`403` `privacy_refusal`** — the model exists on a healthy provider but the effective tier
  forbids that provider.

**Known limitation:** the 503 distinction is best-effort and process-local. The last-healthy
inventory lives in memory; after a ComputeConnect restart while the provider is still down, a
genuinely-known model answers `404` until its provider is next seen healthy. Persisting inventory
across restarts is out of scope for v0.1.0.

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

## Amendments

### CA-1 — Carry `privacy_tier` into `LocalRunRequest`

**Status: IMPLEMENTED (server side) in v0.1.0, ratified by the release lead 2026-07-12.**
**Owner of the wire-format change:** AgentConnect (it defines `LocalRunRequest`).

As implemented: `POST /generate` accepts an **optional** `privacy_tier` field in the request body.
The candidate set is rebuilt from that tier at execution time and the chosen provider is
**positively re-verified** against it — a second, independent evaluation of the same structural
default-deny filter that gated the estimate. When the field is **absent** (which is what
AgentConnect's shipped `LocalRunRequest` sends today), the **most restrictive tier is assumed**:
cloud-class providers are not candidates, and a request that names a cloud-resident model is
answered with `status: "refused"` and a machine-readable `refusal` object
(`{code, detail, privacy, rejected[]}`). AgentConnect may adopt the field whenever it amends
`LocalRunRequest`; until then the default-closed behavior applies.

`LocalEstimateRequest` carries a required `privacy_tier`; `LocalRunRequest` does not. Execution
therefore cannot independently re-verify the privacy decision made at estimate time. Adding
`privacy_tier` to `LocalRunRequest` lets `/generate` **positively re-check** that the chosen provider
is permitted, rather than relying solely on the default-deny candidate filter.

**Not a prerequisite.** The structural invariant (§6) already makes the system safe, because the safe
state is the default and filtering precedes placement. CA-1 strengthens that to defense in depth; it
does not unblock it. Until CA-1 lands, `/generate` must assume the most restrictive tier when none is
present.

### CA-2 — Dispatch-by-reference for `/generate`

**Status:** proposed — still. **Owner of the change:** AgentConnect (its client reads output
inline). The `run_id` half of its motivation is now satisfied more cheaply by CA-3; the
leave-the-token-hot-path half remains future work.

Today `/generate` must proxy because AgentConnect's client expects the generated output in the
response body. If a future client can accept a provider reference (`{endpoint, model, run_id}`) and
stream directly from the engine, ComputeConnect could leave the token hot path entirely and act as a
pure control plane. This also supplies the `run_id` that `/runs/{run_id}/cancel` requires but
`/generate` does not currently return.

**Not a prerequisite.** The thin streaming proxy (invariant 3) is the ratified design for now.

### CA-3 — `/generate` returns a run identifier

**Status: IMPLEMENTED in v0.1.0, ratified by the release lead 2026-07-12.**

This closes contract ambiguity (1) (ARCHITECTURE §7.1): `POST /runs/{run_id}/cancel` required a
`run_id` that `/generate` never returned. As implemented, `/generate` returns the identifier
twice, additively:

* an **`X-Run-Id` response header**, available as soon as the response starts streaming — this is
  what a cancelling caller should use, since it arrives before generation finishes;
* a **`run_id` field** in the response JSON (also echoed inside `metrics`), so a buffered reader
  such as AgentConnect's shipped client sees it after the fact.

`POST /runs/{run_id}/cancel` answers `{run_id, status}` with status ∈
`cancelling | already_finished` (HTTP 200) or `not_found` (HTTP 404); cancellation remains
best-effort. A supplementary native route, `GET /runs/{run_id}`, exposes run metadata
(provider, model, surface, state, timings). Existing clients that ignore unknown fields and
headers are unaffected.

### How `/generate` streams (implementation note, binding invariant 3)

The response is a **single JSON document emitted incrementally**: the prefix (`run_id`, `model`,
`runtime`, and the opening of `output`) is sent immediately, each upstream token is appended to
the `output` string as it is produced (JSON-escaped, nothing buffered beyond one delta), and the
document closes with `status`, `metrics`, and `warnings` — decided only when the generation ends.
A buffered client (AgentConnect's `HttpLocalComputeProvider.run()` calls `response.json()`) parses
it as a plain `LocalRunResult`; a streaming reader sees tokens live. Terminal `status` values:
`succeeded | failed | cancelled | refused`. Backpressure is propagated by consuming the upstream
one delta at a time; cancellation (via CA-3 or client disconnect) closes the upstream connection
mid-generation.

---

## Provisional / not yet contracted

* **ToolConnect.** Has a validated Phase 1 runtime but no execution/invoke surface, so there is no
  compute-facing contract to conform to. The boundary is drawn in
  [ARCHITECTURE §7.3](ARCHITECTURE.md#73-toolconnect-provisional); nothing may be built against it
  until ToolConnect exposes a compute-relevant surface.

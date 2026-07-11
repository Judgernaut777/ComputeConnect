# Status

**Last updated:** 2026-07-12

Read this before proposing work. It is the honest version.

---

## What exists

A minimal but real runtime: the `computeconnect` Python package, **v0.1.0**.

| Thing | State |
|---|---|
| `computeconnect` package (`pyproject.toml`, `src/computeconnect/`) | **Implemented.** Python 3.11, Starlette + uvicorn + httpx. |
| CLI | `computeconnect serve`, default port **8090** (8080 is the external llama.cpp engine, 8787 is reserved for BrainConnect). |
| Layer 1 control plane | All six `LocalComputeProvider` routes: `GET /health`, `GET /models`, `GET /models/loaded`, `POST /route/estimate`, `POST /generate`, `POST /runs/{run_id}/cancel`. Plus additive `GET /runs/{run_id}` metadata. |
| Layer 2 inference API | `GET /v1/models`, `POST /v1/chat/completions` (SSE streaming and non-streaming), same backend as Layer 1. |
| `/generate` streaming (D3) | Implemented: incremental single-JSON-document stream, one-delta backpressure, cancellation propagates to the upstream engine (verified: the upstream sees the disconnect mid-generation). |
| Structural privacy (D5) | Implemented as a staged pipeline: `resolve_privacy_tier` defaults closed, `filter_candidates` is the only constructor of the `CandidateSet` type, and placement/estimate accept only a `CandidateSet`. Cloud is filtered **before** placement; empty set ⇒ structured refusal. |
| Amendments | **CA-1** (optional `privacy_tier` on `/generate`, absent ⇒ most restrictive, positive re-verify) and **CA-3** (`run_id` in body + `X-Run-Id` header) implemented; see [CONTRACT.md](CONTRACT.md). CA-2 remains proposed. |
| Providers registered by default | Two: the local llama.cpp host (read-only upstream on `:8080`; ComputeConnect never manages its lifecycle) and a **simulated** cloud provider (in-process, distinct placement class `cloud`, distinct capabilities and capacity). |
| Tests | 64 pytest tests: privacy property tests, placement, all six routes over real HTTP (uvicorn on ephemeral ports, not an in-process shim), streaming/cancellation/disconnect, OpenAI layer, fault injection (upstream down, mid-stream engine failure), conformance driven by AgentConnect's **shipped** `HttpLocalComputeProvider` imported from the sibling checkout, and 6 tests against the **real** llama.cpp engine (skip when `:8080` is unreachable). |
| Gate | `cd ComputeConnect && .venv/bin/python -m pytest` — 64 passed on 2026-07-12 with the real engine reachable. |

## What was verified, on this host, 2026-07-12

* `computeconnect serve --port 8090` answered `/health` (`ok`, both providers), `/models`
  (`qwen3-30b-a3b` + `sim-cloud-large`), and a real `/v1/chat/completions` against the live
  engine (output `"OK."`). Manually, not only under pytest.
* A real `/generate` through the streaming proxy against `:8080` succeeded, and a real 1024-token
  generation was **cancelled mid-stream** and stopped early.
* AgentConnect's shipped client (`HttpLocalComputeProvider`) ran `health/inventory/loaded/estimate`
  against ComputeConnect backed by the real engine (ROADMAP Phase 1 gate), and
  `run()/cancel()` against the fake-upstream stack.
* The `wiki-llama` systemd unit was consumed **read-only**. Nothing here starts, stops, loads,
  unloads, or reconfigures it — lifecycle delegation (ARCHITECTURE §2.1) is **not implemented**.

## What is *not* implemented or verified

Stated so nobody builds on it by accident:

* **No lifecycle management.** No load/unload delegation, no llama.cpp router-mode verbs (still
  unprobed on this build), no llama-swap-style start/stop. The "loaded-model state" is read, not
  managed.
* **No capability normalizer.** No `lscpu`/NVML/CDI/NFD reading. Capabilities are operator-declared
  tags on the provider registration. The ARCHITECTURE §4 `Capability` schema is unbuilt, and on
  this accelerator-less host most of it would be untestable anyway.
* **No multi-node anything.** One process, providers configured in-process. No remote node agent,
  no mTLS (AgentConnect's checklist item 4 — the local plane here is loopback HTTP), no
  LiteLLM feeding.
* **The cloud provider is simulated.** It exists to exercise the candidate pipeline and the
  privacy invariant, and it does — but it proves contract behavior, not product value.
* `estimated_queue_seconds` beyond capacity is a declared heuristic (30 s per backlogged run), not
  a measurement. `usage` token counts on the OpenAI layer are length-based approximations.

---

## Abandonment re-evaluation (D2), 2026-07-12

The ratified condition: *abandon if only a single homogeneous host is demonstrated and maintained
single-node software already solves the use case.*

**What v0.1.0 demonstrates:** the *contract* claim of ARCHITECTURE §9 — registry, candidate
filtering, placement intent, structural default-deny privacy, streaming proxy, cancellation — all
validated with two logically distinct providers, exactly as the validation plan allowed. The
simulated second provider does change placement decisions (a cloud-only capability routes to it
when the tier permits, and is structurally refused when it does not), so the placement policy has
content.

**What v0.1.0 does not demonstrate:** the *product-value* claim. There is still **exactly one real
node** — this ARM box, no accelerator — and the second provider is a simulation written by this
repository. A simulated counterparty cannot demonstrate heterogeneous placement value, and we do
not claim it does. On today's demonstrated hardware, the D2 comparison stands: **llama-swap plus
LiteLLM, or LocalAI, would serve a single-host user as well or better** than ComputeConnect's
current runtime, with more maturity.

**Why this is not yet the abandonment case:** the condition has two clauses. The second —
"maintained single-node software already solves the use case" — is not fully true for *this*
use case: no maintained single-node system serves AgentConnect's six-route control-plane contract
(estimate/refusal semantics, structural privacy tiers, run cancellation) while also exposing the
standard inference surface; that adapter layer is precisely what v0.1.0 is. The honest reading:
ComputeConnect currently earns its keep **as the conformance implementation of the Connect compute
contract**, not as a heterogeneous placement engine.

**What would settle it, either way:**

* **Falsifies the premise:** a real second node of a different shape (an accelerator, or a Mac
  where `gpu_requires_host_process` is true) arrives, and placement across it delivers no decision
  a static config could not — or the node never arrives and the contract-adapter role gets
  absorbed into AgentConnect itself. Then D2 says stop, and this document must say so.
* **Validates the premise:** a real heterogeneous placement decision — same workload, different
  nodes chosen for capability/capacity reasons a request router cannot see — observed on real
  hardware.

Until one of those happens, the multi-node placement value claim remains **unmade, not disproven**,
and must not be asserted. The simulated provider must never be cited as evidence of heterogeneity.

---

## Decisions — all ratified

Full text in [ARCHITECTURE.md §8](ARCHITECTURE.md#8-decisions). D1–D2 on 2026-07-10; D3–D6 on
2026-07-11. Implementation status of each, as of v0.1.0:

| # | Decision | v0.1.0 |
|---|---|---|
| **D1** | Provider = compute environment, not LLM-API provider | Held: providers are a local host and a (simulated) cloud environment; no LiteLLM replacement built. |
| **D2** | Design-validation rule | Re-evaluated above, honestly. |
| **D3** | `/generate` thin streaming proxy | Implemented and tested (streaming, backpressure via one-delta pull, cancellation, disconnect). |
| **D4** | Two APIs, one backend | Implemented: both layers share one registry, one run tracker, one generation path per engine. |
| **D5** | Structural default-deny privacy | Implemented as pipeline structure, tested including local-down/cloud-capable hard cases. |
| **D6** | Apache-2.0 | Unchanged. |

## Contract ambiguities — disposition

Of the five ambiguities recorded in ARCHITECTURE §7.1: (1) `run_id` — **closed by CA-3**;
(2) streaming — **closed by D3's implementation** (documented in CONTRACT.md); (3) `privacy_tier`
at execution — **closed server-side by CA-1** (AgentConnect's `LocalRunRequest` still does not
carry it; default-closed covers that); (4) `estimated_quality` scale — **defined** as deployment-
local `[0,1]`; (5) topology hidden in `runtime`/`reason` — unchanged, `reason` now carries the
full placement rationale and rejection list.

## Known inconsistencies outside this repository

* `Connect/README.md` still lists ComputeConnect as design-phase/scope-undefined; as of v0.1.0
  there is a runtime. Owned by the Connect repo.
* The BrainConnect rename remains in progress; its librarian still targets an OpenAI-compatible
  `base_url` and can point at `:8090/v1` unchanged — but nothing has been rewired, and `:8787`
  remains reserved for BrainConnect's own serve.

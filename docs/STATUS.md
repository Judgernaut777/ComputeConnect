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
| Providers registered by default | Two: the local llama.cpp host (read-only upstream on `:8080`; ComputeConnect never manages its lifecycle) and a **simulated** cloud provider (in-process, distinct placement class `cloud`, distinct capabilities and capacity). A **second real engine** (Qwen3-4B / 8k ctx on `:8091`, `scripts/second_engine.sh`) can be registered via the config surface for real heterogeneous placement — see the D2 re-eval. |
| Placement preference | `latency_preference` (fastest) / `quality_preference` (highest) select between same-class nodes; hard constraints (capability, context-window fit) always win. `reason.considered` lists the candidate set. Default order (local → loaded → queue) unchanged. |
| Privacy header/body precedence | When both `X-Privacy-Tier` and body `privacy_tier` are present, the **more restrictive** wins; a header can never widen a more-restrictive body. `PRIVACY_STRICTNESS` byte-mirrors AgentConnect's (test-asserted). |
| Run persistence / restart | Optional SQLite run journal (`--run-journal`): in-flight runs orphaned by a crash are reconciled to terminal `interrupted` on restart, never lost/dangling; queryable via `GET /runs/{id}`. `/health` reports `persistence`. Default is in-memory. |
| Staleness | Fail-closed `max_snapshot_age` ceiling: a snapshot older than the bound is rejected at a `stale` stage, never trusted for placement; a stale snapshot can never cause a privacy-wrong cloud placement (privacy is structural on `placement_class`). |
| Config surface | `--config` / `COMPUTECONNECT_CONFIG` (JSON always; YAML with the `config` extra) declares providers without code. `docs/AGENTCONNECT_INTEGRATION.md` specifies the AgentConnect-side `AGENTCONNECT_COMPUTE_URL` / `compute:` yaml shape (consumer change is a sibling-repo task). |
| Tests | 109 pytest tests: privacy property + precedence tests, placement (incl. preference + staleness), all six routes over real HTTP (uvicorn on ephemeral ports, not an in-process shim), streaming/backpressure/cancellation/disconnect, OpenAI layer, fault injection + failover, run-journal restart reconciliation, config surface, conformance driven by AgentConnect's **shipped** `HttpLocalComputeProvider`, and real-engine tests including **two real engines** (`:8080` 30B + `:8091` 4B) that skip — never fake — when an engine is unreachable. |
| Gate | `cd ComputeConnect && .venv/bin/python -m pytest` — **109 passed** on 2026-07-12 with both real engines reachable. |

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
* **No multi-*host* anything.** One physical box. There is now a **second real engine process**
  (`:8091`) and placement chooses between the two for real (latency/quality/context), but both are
  CPU llama.cpp on the same host — no remote node agent, no mTLS (AgentConnect's checklist item 4 —
  the local plane here is loopback HTTP), no cross-hardware-class routing, no LiteLLM feeding.
* **The cloud provider is still simulated.** It exists to exercise the candidate pipeline and the
  cloud default-deny privacy invariant, and it does — but it proves contract behavior, not product
  value, and is never cited as evidence of heterogeneity. Real heterogeneity is demonstrated by the
  two real engines instead (D2 re-eval).
* `estimated_queue_seconds` beyond capacity is a declared heuristic (30 s per backlogged run), not
  a measurement. `usage` token counts on the OpenAI layer are length-based approximations.

---

## Abandonment re-evaluation (D2), 2026-07-12 — re-run after a REAL second engine

The ratified condition: *abandon if only a single homogeneous host is demonstrated and maintained
single-node software already solves the use case.*

**What changed since the last re-eval:** a **second real engine** now runs on this box —
Qwen3-4B dense / 8k context on `:8091`, alongside the reference Qwen3-30B-A3B MoE / 16k context on
`:8080` (`scripts/second_engine.sh`). Both are real CPU inference. This lets us re-run D2 against
*real* heterogeneity instead of a simulation.

**What is now demonstrated (real, not simulated):** a genuine heterogeneous placement decision.
With both real engines registered, the SAME workload class is placed on:

* the 4B node under a `latency_preference` (it is ~7-18× faster to first token here),
* the 30B node under a `quality_preference` (higher declared quality),
* the 30B node for a 12k-token context the 4B's 8k window cannot fit (capacity/capability),

with **real generations from both engines** and a **real failover** (kill the 4B, the latency job
moves to the 30B and still generates). Evidence:
`scratchpad/waveA/ComputeConnect/demo_two_real_engines.py` (end-to-end, real HTTP) and
`tests/test_real_engine.py::test_*two_real_engines*` / `*real_generation_from_BOTH*` (skip, never
fake, when either engine is down). So the placement policy is not merely non-trivial — it makes a
decision **on real hardware that a static request router cannot make**, because the choice depends on
live per-node latency/quality/context-fit. That is the "validates the premise" bar from the previous
re-eval, and it is now met **for the single-box, multi-engine case**.

**What is still NOT demonstrated:** *cross-hardware-class* heterogeneity. There is still exactly one
physical node and no accelerator. Both engines are CPU llama.cpp on the same box, so the demo does
not prove value for the case ComputeConnect was most ambitiously pitched at — routing between, say, a
local GPU and a rented one, or a Mac where `gpu_requires_host_process` holds. A determined operator
could approximate the single-box, two-engine value today with **llama-swap + LiteLLM** or
**LocalAI**; where those fall short is the six-route control-plane contract (estimate/refusal
semantics, structural default-deny privacy tiers, run cancellation, run persistence) exposed
*alongside* the OpenAI surface — which remains ComputeConnect's distinct value.

**Verdict:** not the abandonment case, and now for a stronger reason than last time. Previously the
multi-node value claim was "unmade, not disproven"; it is now **partially made** — real single-box
heterogeneous placement is demonstrated and reproducible — while the cross-hardware-class claim
stays honestly unmade. ComputeConnect earns its keep as (a) the conformance implementation of the
Connect compute contract and (b) a real, if modest, heterogeneous placement engine within one host.
The simulated cloud provider remains for exercising the cloud default-deny path and **must still
never be cited as evidence of heterogeneity** — that evidence now comes from the two real engines.

**What would still settle the remaining claim:** a real node of a *different hardware class* arrives
and placement across it delivers a decision a static config could not — validating the full premise —
or it never arrives and the cross-class ambition is formally dropped, leaving the (now demonstrated)
single-box role plus the contract-adapter role.

---

## Decisions — all ratified

Full text in [ARCHITECTURE.md §8](ARCHITECTURE.md#8-decisions). D1–D2 on 2026-07-10; D3–D6 on
2026-07-11. Implementation status of each, as of v0.1.0:

| # | Decision | v0.1.0 |
|---|---|---|
| **D1** | Provider = compute environment, not LLM-API provider | Held: providers are compute environments (two real llama.cpp engines of different shape + a simulated cloud); no LiteLLM replacement built. |
| **D2** | Design-validation rule | Re-evaluated above after a real second engine: single-box heterogeneous placement now **demonstrated**; cross-hardware-class value still unmade. |
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
* The BrainConnect rename is complete (package/CLI `brainconnect`, service string `brainconnect`,
  `brainconnect serve` on `:8787`); its librarian still targets an OpenAI-compatible `base_url`
  and can point at `:8090/v1` unchanged — but nothing has been rewired.

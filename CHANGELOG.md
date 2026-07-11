# Changelog

## 0.1.0 — 2026-07-12

First runtime. Everything before this release was documentation (Phase 0).

### Added

* `computeconnect` Python package (3.11+, Starlette/uvicorn/httpx, Apache-2.0) with
  `computeconnect serve` CLI, default port **8090**.
* **Layer 1 control plane** — the six `LocalComputeProvider` routes AgentConnect's shipped
  client calls: `GET /health`, `GET /models`, `GET /models/loaded`, `POST /route/estimate`,
  `POST /generate`, `POST /runs/{run_id}/cancel`; plus additive `GET /runs/{run_id}` run
  metadata.
* **Layer 2 inference API** — OpenAI-compatible `GET /v1/models` and
  `POST /v1/chat/completions` (SSE streaming and non-streaming), served by the same registry,
  run tracker, and generation path as Layer 1 (D4: two APIs, one backend).
* **Streaming `/generate`** (D3): a single JSON document emitted incrementally — parseable by
  AgentConnect's buffered client, live for streaming readers; one-delta backpressure; upstream
  cancellation on `POST /runs/{id}/cancel` and on client disconnect.
* **Structural default-deny privacy** (D5): `resolve_privacy_tier` clamps missing/unknown tiers
  to the most restrictive; `filter_candidates` removes cloud-class providers before placement
  and is the only constructor of the `CandidateSet` type placement accepts; an empty candidate
  set is a structured refusal (`{code, detail, privacy, rejected[]}`), never a downgrade.
* **Two providers**: the local llama.cpp engine as a read-only upstream (never lifecycle-managed),
  and a simulated cloud provider (distinct placement class, capabilities, capacity) for contract
  validation — explicitly not a heterogeneity claim (see docs/STATUS.md, D2 re-evaluation).
* **Implemented contract amendments** (docs/CONTRACT.md): **CA-1** — optional `privacy_tier` on
  `/generate` with positive re-verification, absent ⇒ most restrictive tier assumed; **CA-3** —
  `/generate` returns `run_id` in the response body and an `X-Run-Id` header, closing the
  cancel-without-an-id ambiguity.
* Test suite (64 tests): privacy property tests, placement policy, all six routes over real HTTP,
  streaming/cancellation/disconnect propagation, OpenAI layer, fault injection (upstream down,
  mid-stream engine failure), conformance via AgentConnect's shipped `HttpLocalComputeProvider`,
  and real-engine tests against llama.cpp on `:8080` that skip when it is unreachable.

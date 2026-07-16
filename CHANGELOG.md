# Changelog

## Unreleased — production hardening (2026-07-12)

### Added

* **Real heterogeneous placement across two REAL engines.** `scripts/second_engine.sh` stands up a
  second real llama.cpp engine of a different shape (Qwen3-4B / 8k ctx on `:8091` vs the 30B MoE /
  16k ctx on `:8080`). Placement now honors `latency_preference` (fastest node) and
  `quality_preference` (highest node), with capability/context-window fit always winning over a soft
  preference; `reason.considered` lists the candidate set. Demonstrated with real generations from
  both engines and a real failover (see `docs/STATUS.md` D2 re-eval).
* **Header/body privacy precedence** — when both `X-Privacy-Tier` and body `privacy_tier` are present
  the **more restrictive** wins (a header can never widen a more-restrictive body). `PRIVACY_STRICTNESS`
  byte-mirrors AgentConnect's, test-asserted. Fixes the Wave-earlier LOW finding.
* **Durable run journal + restart reconciliation** (`--run-journal`): in-flight runs orphaned by a
  crash are reconciled to terminal `interrupted` on restart, never lost or left `running`, and stay
  queryable. `/health` reports `persistence`.
* **Fail-closed staleness ceiling** (`max_snapshot_age`): a snapshot older than the bound is rejected
  at a new `stale` stage rather than trusted; a stale snapshot can never cause a privacy-wrong cloud
  placement.
* **Declarative config surface** (`--config` / `COMPUTECONNECT_CONFIG`, JSON always, YAML via the
  `config` extra) to declare providers without code; `docs/AGENTCONNECT_INTEGRATION.md` specifies the
  AgentConnect-side `AGENTCONNECT_COMPUTE_URL` / `compute:` yaml shape (sibling-repo consumer change).

### Tests

* 66 → **109** passing: privacy precedence property tests (incl. conflicting header-vs-body),
  preference + staleness placement, backpressure under a slow consumer, provider failover, run-journal
  restart reconciliation over real HTTP, config surface, and two-real-engine placement/generation.
* **Update (2026-07-17):** the suite has grown to **140 tests** (129 offline, always green, plus 11
  against a live `:8080` real engine) after the bearer-auth hardening landed
  (`tests/test_auth.py`, 25 tests, not yet its own entry above). 9 of the 11 real-engine tests
  currently **fail, not skip** — this is **environmental, not a product regression**: they assert
  the hardcoded model id `qwen3-30b-a3b`, but the host's `:8080` engine was renamed to
  `qwen3.6-35b-a3b` on 2026-07-13. See [docs/STATUS.md](docs/STATUS.md) for the current gate result.

### Docs

* ADRs `docs/adr/0001`–`0004`; CONTRACT.md privacy-precedence + operational (persistence/staleness)
  sections; STATUS.md D2 abandonment re-evaluation re-run against a real second engine.

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
* **503 for known-but-unhealthy models** on `/v1/chat/completions`: the provider registry retains
  each provider's last *healthy* model inventory, so a model whose provider is temporarily down
  answers `503` (`error.code = "model_temporarily_unavailable"`) instead of `404` — an OpenAI
  client can distinguish temporarily-down from never-existed. Privacy-filtered providers are
  ignored by the check, so it leaks nothing the caller's tier forbids. Best-effort and
  process-local (in-memory; documented in docs/CONTRACT.md Layer 2).
* `NOTICE` file (Apache-2.0 attribution) and PEP 639 license metadata (`license = "Apache-2.0"`,
  `license-files = ["LICENSE"]`, setuptools>=77), matching the other Connect repos.
* Test suite (66 tests): privacy property tests, placement policy, all six routes over real HTTP,
  streaming/cancellation/disconnect propagation, OpenAI layer, fault injection (upstream down,
  mid-stream engine failure, dead-provider 503 vs 404 vs privacy-clamped 404), conformance via
  AgentConnect's shipped `HttpLocalComputeProvider`, and real-engine tests against llama.cpp on
  `:8080` that skip when it is unreachable.

# Roadmap

**Architecture first.** No phase below begins until the phase above it has passed its gate.

**All of D1–D6 are ratified** ([ARCHITECTURE.md §8](ARCHITECTURE.md#8-decisions)). The architecture
and contracts are locked; the phases below no longer wait on a decision.

Each phase states a **gate**: an observable condition, not a feeling of doneness. A phase with no
falsifiable gate is not on this roadmap.

**No hardware purchase is required before Phase 1.** Contract validation uses two logically distinct
providers, one of which may be simulated, containerized, remote, or CPU-only. See
[ARCHITECTURE §9](ARCHITECTURE.md#9-validation-contract-versus-product-value) for why that is
sufficient for contracts and insufficient for product value.

---

## Phase 0 — Architecture and contracts *(current)*

Define what ComputeConnect is, what it refuses to be, and what it must conform to.

* Product boundaries, with prior-art collisions resolved by name.
* Integration contracts for AgentConnect, BrainConnect, ToolConnect ([CONTRACT.md](CONTRACT.md)).
* **All of D1–D6 ratified.** Dual-API architecture (D4), thin streaming proxy (D3), structural
  default-deny privacy (D5), and Apache-2.0 (D6) are locked.
* Ambiguities in AgentConnect's published contract documented, not silently resolved, and carried
  as future amendments **CA-1** and **CA-2**.
* `LICENSE` (Apache-2.0) added.

**Gate:** the documents and license committed and pushed, contracts locked. **Met.**

**Not in this phase:** any code, any dependency choice, any language choice.

---

## Phase 1 — Read-only observability

The smallest thing that is honestly useful and cannot become a scheduler by accident: **describe
the compute that exists.** No placement, no lifecycle, no mutation of any kind.

* Compute-provider registry with exactly one provider: the local ARM host.
* Capability normalizer reading `lscpu` and `/proc/cpuinfo`. On this node there is no accelerator,
  so the NVML and CDI paths are **stubs with no live coverage** — do not pretend otherwise.
* Three-level health rollup (node / engine / model) against the running `llama.cpp` server.
* Serve `GET /health`, `GET /models`, `GET /models/loaded` from real engine state.

**Gate:** AgentConnect's `HttpLocalComputeProvider` can call those three endpoints against a live
ComputeConnect and get back the model that `llama.cpp` is actually serving. Verified against the
real service, not a mock.

**Why read-only first:** it forces the registry and capability schema to be right before anything
depends on them, and it is the cheapest possible test of whether the capability record contains
anything `ollama ps` does not already give away — the falsification test in ARCHITECTURE §9.

---

## Phase 2 — Conform to the AgentConnect contract

Complete the surface AgentConnect's client already calls.

* `POST /route/estimate` — admission. Given `{task_type, privacy_tier, required_capabilities,
  context_tokens, ...}`, answer eligible / not, with a **reason**. This is the first real
  ComputeConnect logic: model-fit against a `Capability` record.
* `POST /generate` — per **D3**, a **thin streaming proxy**: stream without buffering, propagate
  cancellation and backpressure, stay as transparent as possible.
* `POST /runs/{run_id}/cancel` — best-effort, per the contract.
* Privacy: the structural default-deny filter (**D5**, ARCHITECTURE §6) is enforced at admission.
  Because `LocalRunRequest` carries no `privacy_tier`, `/generate` assumes the most restrictive tier
  when none is present — the safe default — pending amendment **CA-1**.

**Gate:** AgentConnect's existing local-manager tests pass against a live ComputeConnect with no
changes to AgentConnect. If they cannot, the contract was misread and Phase 2 reopens
ARCHITECTURE §7.1.

**Watch for:** an outage of ComputeConnect must surface in AgentConnect as
`health.available = False` and a routed-elsewhere task — never as an exception. Test the outage,
not just the happy path.

---

## Phase 3 — Model lifecycle, only where it is missing

Delegate to engines that already do this; implement only for engines that do not.

| Engine | Action |
|---|---|
| llama.cpp | Delegate — call its router-mode `load`/`unload` **once verified on this build** |
| Ollama | Delegate — `keep_alive`, `/api/ps` |
| vLLM | Delegate — sleep/wake, LoRA hot-swap; one base model per process is a constraint to model, not to fix |
| MLX | **Implement** — it has no load/unload, no TTL. This is the motivating case. |

Idle-eviction policy lives here: *when* to unload, decided by ComputeConnect; *how*, by the engine.

**Gate:** a model can be brought resident and evicted on the MLX path, and on the llama.cpp path
ComputeConnect provably issues the engine's own calls rather than its own logic. Reviewed by
diff, since "did we delegate" is a code-shape question, not a runtime one.

**Design check (D2):** if by the end of this phase ComputeConnect is a single-host model swapper
with no placement problem in sight, `llama-swap` is the maintained answer and should be preferred.

---

## Phase 4 — The second provider

The cross-host premise is currently **untested**, because exactly one provider exists. Until a
second one does, every claim about heterogeneity in ARCHITECTURE is theory.

Testing it does **not** require buying hardware. The requirement is **two logically distinct
providers differing in capability or policy**, where one may be simulated, containerized, remote, or
CPU-only:

* A simulated provider that advertises an accelerator and refuses CPU-only workloads, alongside the
  real CPU-only llama.cpp host — enough to make placement produce two different answers.
* A simulated provider marked cloud-resident, to exercise the **fail-closed** privacy path: a
  local-only workload must be refused with a reason, never placed there. Depends on **D5**.
* `Placement` as an emitted intent with an auditable rationale.
* Execution delegated to the native runtime (`podman run`, a Quadlet, `systemd`).

**Gate:** a placement decision that *changes* when the second provider's capabilities or policy
change, with a rationale a human can read and disagree with, and **zero scheduling code** in the
diff. If a bin-packing loop appears, the charter has been violated.

**What this gate does not prove:** that heterogeneous placement is worth doing. That claim needs a
real second node of a different shape, and it may not be made before then.

---

## Phase 5 — Providers beyond the local host

Only now does "compute provider registry" (per **D1**) earn its name.

* Provider adapters: Kubernetes (via DRA ResourceClaims), Ray (via placement groups), rented cloud GPU.
* Each adapter **translates a ComputeConnect placement intent into that provider's native
  scheduling primitive.** None reimplements one.
* The privacy-tier refusal path (**D5**) must be enforced before any cloud provider is reachable.
  A local-only workload that can physically reach a rented GPU is a bug, and the order of these
  two items is not negotiable.

**Gate:** a placement intent satisfied by Ray's scheduler, with ComputeConnect contributing the
*choice of cluster* and nothing below it.

---

## Explicitly not on this roadmap

Restated, because roadmaps are where scope creep enters:

* A scheduler. Ever.
* An inference engine. Ever.
* A request-level LLM gateway — that is LiteLLM, and ComputeConnect should feed it.
* Device detection from scratch — NVML, CDI, NFD, and DCGM are read, not replaced.
* Cost tracking and spend metadata — AgentConnect's ledger and LiteLLM both do this already.
* A tool authorization model — ToolConnect's, and not reachable from the compute plane.
* Secrets storage — referenced, never held.

---

## Sequencing note

Phases 1–3 are all single-provider and therefore all survivable by `llama-swap` plus LiteLLM.
**Phase 4 is where ComputeConnect first does something no existing tool does.** A reasonable reading
of this roadmap is that Phase 4 should be pulled earlier; the counter-argument is that a placement
layer built before a correct capability schema will place things wrong with great confidence.

Because Phase 4 can now be entered with a *simulated* second provider, that tension is cheaper than
it first appeared: pulling it earlier costs a fake, not a purchase. The maintainer should decide the
ordering, but hardware acquisition is no longer on the critical path — only the product-value claim
in [ARCHITECTURE §9](ARCHITECTURE.md#9-validation-contract-versus-product-value) is.

---

## Future contract amendments

Tracked in [CONTRACT.md](CONTRACT.md#future-amendments). Neither is required before implementation;
both are AgentConnect's to make.

* **CA-1 — Carry `privacy_tier` into `LocalRunRequest`.** Lets `/generate` positively re-verify the
  privacy decision instead of relying solely on the default-deny candidate filter. Defense in depth;
  the structural invariant (§6) already makes the system safe without it.
* **CA-2 — Dispatch-by-reference for `/generate`.** Would let ComputeConnect leave the token hot
  path and supply the `run_id` that `/runs/{run_id}/cancel` needs. Deferred; the thin streaming proxy
  is the ratified design for now.

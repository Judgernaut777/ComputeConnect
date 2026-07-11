# Status

**Last updated:** 2026-07-11

Read this before proposing work. It is the honest version.

---

## What exists

Documentation and a license. That is the whole repository.

| Thing | State |
|---|---|
| `README.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `docs/STATUS.md`, `docs/CONTRACT.md` | Drafted |
| `LICENSE` | **Apache-2.0** (D6, 2026-07-11) |
| Server, CLI, library, package, API | **None** |
| Tests | **None** — there is nothing to test |
| Language, dependencies | **Undecided** |
| Compute nodes under management | **Zero.** One node is *described*; nothing manages it. |

Nothing in ComputeConnect runs, and nothing depends on ComputeConnect.

**Licensing decision (D6):** the repository is **Apache-2.0**. Chosen for the explicit patent grant
(more appropriate to a compute control plane than MIT), for ecosystem consistency with the
infrastructure it integrates — Kubernetes, Ray, vLLM, containerd — and to match BrainConnect, the
one sibling that already carries a license.

---

## What was verified, on this host, today

Measured directly on 2026-07-10, not assumed:

| Fact | Value | How |
|---|---|---|
| Architecture | `aarch64`, Cortex-A720, 12 CPUs | `lscpu` |
| Accelerator | **None** | no GPU on this box |
| Inference engine | `llama.cpp`, reachable on `:8080` | `curl /health` → `{"status":"ok"}` |
| Engine supervisor | `wiki-llama`, a **systemd user unit**, active | `systemctl --user is-active` |
| Model resident | `qwen3-30b-a3b`, GGUF | `GET /v1/models` |
| `GET /models` | returns `200` | `curl` |

Two consequences that shape the roadmap:

* The one real node is **bare systemd, not a container**. No CDI, no Podman, no Kubernetes is in
  the loop. The container-runtime paths in ARCHITECTURE are, on this host, theory.
* The one real node has **no accelerator**. The NVML/CDI/DCGM capability paths have **no live
  coverage** and cannot be tested here. Any code written against them is unverified by
  construction until a second node exists.

### Verified from sibling repositories

* **AgentConnect already defines this product's primary contract.**
  `mcp-agentconnect/packages/agentconnect-core/src/agentconnect/core/local_compute.py` contains the
  `LocalComputeProvider` ABC, an `HttpLocalComputeProvider` client, and a `LocalModelManagerWorkerAdapter`.
  The six endpoints in ARCHITECTURE §7.1 are copied from that client, not invented here.
* **BrainConnect is WikiBrain, renamed**, per `Connect/README.md`. The rename is incomplete; the
  code still says `wiki`.
* **BrainConnect's librarian is a compute consumer.** `WikiBrain/cli/librarian/client.py` targets a
  configurable OpenAI-compatible `base_url`, defaulting to Ollama's `:11434`. The `wiki-llama`
  service on `:8080` is a hand-managed instance of what ComputeConnect exists to manage.
* **ToolConnect now has a validated Phase 1 runtime** but no execution/invoke surface — so there is
  still no compute-facing contract to conform to.

---

## What is *not* verified

Stated so nobody builds on it by accident:

* **llama.cpp router-mode `POST /models/load` and `POST /models/unload`.** Reported by research
  against upstream source (router mode, ~Dec 2025) and used in ARCHITECTURE §3. **Not probed on
  this build.** `GET /models` returns 200 here, which is necessary but not sufficient. Verify
  before Phase 3 delegates to it.
* Every claim about **vLLM, Ray, MLX, Ollama, Kubernetes DRA, CDI, LocalAI, llama-swap, and
  LiteLLM** comes from web research conducted 2026-07-10, not from running any of them. Two
  precision notes carried forward from that research:
  * DRA reached **GA in Kubernetes v1.34** (2025-08-27). One intermediate summary said v1.35; that
    is wrong.
  * **CDI is a CNCF *TAG Runtime working-group specification*, not a CNCF Sandbox/Incubating/
    Graduated project.** Do not write "the CNCF CDI project."
* LiteLLM's `enterprise/` license carve-out was verified verbatim from its root `LICENSE`. Its
  pricing, and any security-incident history, were **not** verified against primary sources and are
  deliberately absent from these documents.

---

## Decisions — all ratified

Full text in [ARCHITECTURE.md §8](ARCHITECTURE.md#8-decisions). D1–D2 on 2026-07-10; **D3–D6 on
2026-07-11**.

| # | Decision |
|---|---|
| **D1** | Provider registry means **compute environments and execution planes** — a local host, a remote host, a Kubernetes cluster, a rented GPU node, a runtime service tied to compute capacity. **Not** generic LLM API providers. Cloud model-provider routing stays delegated to LiteLLM or an equivalent gateway, which ComputeConnect may *integrate with* when placement requires it. |
| **D2** | Design-validation rule: if the demonstrated use case remains a single local host with no heterogeneous placement problem, prefer maintained single-node systems (LocalAI, llama-swap, Ollama, LiteLLM) over building ComputeConnect. Discipline, not a foregone conclusion to delete the repository. |
| **D3** | `/generate` is a **thin streaming proxy**: ComputeConnect stays in the request path (AgentConnect reads output inline), streams without large buffering, and propagates cancellation and backpressure. Dispatch-by-reference deferred to CA-2. |
| **D4** | **Two APIs, one backend.** Layer 1 control plane (`LocalComputeProvider`) for AgentConnect; Layer 2 OpenAI-compatible inference for BrainConnect and direct apps. Not one surface with an alias. |
| **D5** | **Structural default-deny privacy.** Cloud providers filtered from the candidate set before placement; unknown/missing/local-only ⇒ no cloud; empty set ⇒ structured refusal; never a silent downgrade. |
| **D6** | **Apache-2.0.** |

## Contract ambiguities in AgentConnect's surface

Documented in [ARCHITECTURE §7.1](ARCHITECTURE.md#71-agentconnect-conformance). Neither blocks
safety or Phase 1 any longer; both are recorded as future amendments in
[CONTRACT.md](CONTRACT.md#future-amendments):

* **`privacy_tier` is absent from `LocalRunRequest`** (present on `LocalEstimateRequest`). No longer
  a blocker: the structural default-deny invariant (§6) makes the system safe without it. The
  positive re-check amendment is **CA-1**.
* **`POST /generate` returns no `run_id`**, which `POST /runs/{run_id}/cancel` requires. Addressed by
  the dispatch-by-reference amendment **CA-2**. Both amendments are AgentConnect's to make; neither
  is required before ComputeConnect implementation begins.

## Hardware validation plan

No hardware purchase is required before Phase 1.

| Claim | What validates it | Available today? |
|---|---|---|
| **Contract validity** — registry, capability schema, admission, placement intent, fail-closed privacy | **Two logically distinct providers** differing in capability or policy. One may be **simulated, containerized, remote, or CPU-only**. | **Yes.** One real CPU-only llama.cpp host plus a simulated provider. |
| **Product value** — heterogeneous placement is worth doing | A **real** second node of a different shape: an accelerator, or a Mac where `gpu_requires_host_process` is true. | **No.** |

Until the second row is satisfied, the production multi-node placement claim is **unmade, not
disproven**, and must not be asserted.

---

## Known inconsistencies outside this repository

Recorded, not acted on. **This repository's scope is ComputeConnect only**; the fixes below belong
to whoever owns those repos.

* `Connect/README.md` lists ComputeConnect as *"Reserved. Name claimed. Scope undefined."* That is
  now stale — this document set defines the scope. The ecosystem table needs updating, and the
  status table's `—` in ComputeConnect's repository column needs a link.
* `ToolConnect/README.md` links to `docs/STATUS.md`, which **does not exist** in that repository.
* Per `Connect/README.md`, the BrainConnect rename is in progress: packages, the MCP server, and
  the `brain_*` tools all still carry `wiki` names.

---

## The honest summary

ComputeConnect has a **real but narrow** justification: nothing surveyed owns cross-host,
cross-runtime compute capability and lifecycle for a heterogeneous, Kubernetes-optional fleet. It
also has a **real risk of being redundant**: on one box, `llama-swap` plus LiteLLM covers Phases
1–3, and LocalAI covers most of the idea outright.

The premise that distinguishes it — heterogeneity — is **currently undemonstrated, because there is
exactly one provider and it has no accelerator.** Contracts and policy can be validated now against
a simulated second provider. The *value* claim cannot, and per **D2** it should not be asserted
until a genuine heterogeneous placement problem exists.

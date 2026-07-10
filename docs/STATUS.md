# Status

**Last updated:** 2026-07-10

Read this before proposing work. It is the honest version.

---

## What exists

Four documents. That is the whole repository.

| Thing | State |
|---|---|
| `README.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `docs/STATUS.md` | Drafted |
| Server, CLI, library, package, API | **None** |
| Tests | **None** — there is nothing to test |
| Language, dependencies, license | **Undecided** (D6) |
| Compute nodes under management | **Zero.** One node is *described*; nothing manages it. |

Nothing in ComputeConnect runs, and nothing depends on ComputeConnect.

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
  The six endpoints in ARCHITECTURE §5.1 are copied from that client, not invented here.
* **BrainConnect is WikiBrain, renamed**, per `Connect/README.md`. The rename is incomplete; the
  code still says `wiki`.
* **BrainConnect's librarian is a compute consumer.** `WikiBrain/cli/librarian/client.py` targets a
  configurable OpenAI-compatible `base_url`, defaulting to Ollama's `:11434`. The `wiki-llama`
  service on `:8080` is a hand-managed instance of what ComputeConnect exists to manage.
* **ToolConnect has no runtime either** — its repository is a single `README.md`.

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

## Decisions

Full text in [ARCHITECTURE.md §6](ARCHITECTURE.md#6-decisions).

**Ratified 2026-07-10:**

| # | Decision |
|---|---|
| **D1** | Provider registry means **compute environments and execution planes** — a local host, a remote host, a Kubernetes cluster, a rented GPU node, a runtime service tied to compute capacity. **Not** generic LLM API providers. ComputeConnect is not a LiteLLM replacement and not a cloud-LLM API proxy; cloud model-provider routing stays delegated to LiteLLM or an equivalent gateway, which ComputeConnect may *integrate with* when placement requires it. |
| **D2** | Design-validation rule: if the demonstrated use case remains a single local host with no heterogeneous placement problem, prefer maintained single-node systems (LocalAI, llama-swap, Ollama, LiteLLM) over building ComputeConnect. **This is discipline, not a foregone conclusion to delete the repository.** |

**Open:**

| # | Question | Blocking |
|---|---|---|
| **D3** | Is `/generate` a proxy (data path) or a reference (control plane only)? | Phase 2 |
| **D4** | One consumer surface, or two? | Phase 2, BrainConnect integration |
| **D5** | How is local-only privacy *structurally enforced*, not merely honored? | **Phase 5 — hard blocker.** No cloud provider may be reachable before this is answered. Requires settling contract ambiguity (3) with AgentConnect. |
| **D6** | License | First external contribution |

## Contract ambiguities awaiting AgentConnect

Documented in [ARCHITECTURE §5.1](ARCHITECTURE.md#51-agentconnect-conformance), **not silently
resolved.** The load-bearing two:

* **`POST /runs/{run_id}/cancel` requires a `run_id` that `POST /generate` never returns.** The
  shipped `LocalRunResult` carries no identifier, so cancellation is unreachable through the shipped
  client path.
* **`privacy_tier` is an input to `/route/estimate` but absent from `LocalRunRequest`.** The
  enforcement point for a local-only workload is therefore unspecified — which is why D5 cannot be
  answered by ComputeConnect alone.

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

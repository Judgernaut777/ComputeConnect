# Architecture

**Status:** draft. No code exists. **D1 and D2 are ratified** (2026-07-10) and are stated as
settled below. D3–D6 remain open and are called out inline.

This document draws ComputeConnect's boundaries, names its objects, confronts the projects whose
scope it overlaps, and defines its integration contracts. It deliberately stops short of
implementation.

---

## 1. Position

ComputeConnect is the **compute plane**. It sits below the request plane and above the runtime
substrate.

```
  ┌──────────────────────────────────────────────────────────────┐
  │  AgentConnect (tasks)   BrainConnect (memory)   ToolConnect  │   consumers of compute
  └───────────────┬──────────────────┬───────────────────────────┘
                  │                  │
                  │  LocalComputeProvider contract (already specified by AgentConnect)
                  ▼                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │                       ComputeConnect                          │
  │                                                               │
  │   compute registry · runtime registry · compute-provider      │
  │   registry · capability normalizer · admission (will it fit)  │
  │   · model lifecycle · health rollup · placement policy        │
  └───────────────┬──────────────────────────────────────────────┘
                  │  placement intent  (never a scheduler)
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Engines:   llama.cpp   vLLM   Ollama   MLX                   │
  │  Runtimes:  systemd   Podman   Docker   Kubernetes   Ray      │
  │  Detection: NVML   CDI   NFD   DCGM   /proc/cpuinfo           │
  └──────────────────────────────────────────────────────────────┘
```

The single sentence that constrains every design choice below:

> **ComputeConnect knows what compute exists and what it can do. It does not know how to do
> inference, and it does not schedule.**

### The distinction that makes this layer real

Everything in the request plane — LiteLLM most of all — models the world as *a set of live HTTP
endpoints an operator asserted are running*. Health means "did an inference request to this URL
succeed." Capability means "what the operator typed into a YAML file."

ComputeConnect models **infrastructure state that exists independently of any inference request**:

* Is that box powered on and reachable, before anyone sends a request?
* How much memory is free on it right now, and **will this specific model artifact fit**?
* Should an engine process be **started or stopped** to meet demand?
* What can this hardware actually do — i8mm? Metal? CUDA? Vulkan? — **discovered, not asserted**?

No project in the survey below owns that question for a *heterogeneous, non-Kubernetes-mandatory,
local-plus-cloud* fleet. That gap is the entire justification for this repository. It is narrow.
If ComputeConnect does not stay inside it, it should not exist.

---

## 2. Boundaries

### 2.1 ComputeConnect owns

| Responsibility | What it concretely means |
|---|---|
| **Compute-provider registry** | *Where compute is obtained from* — a compute environment or execution plane: a local host, a remote host, a Kubernetes cluster, a rented GPU node, a runtime service tied to compute capacity. **Per D1 this is not an LLM-API provider registry.** |
| **Compute registry** | Every addressable machine that can execute a workload, under one schema, spanning a bare-metal ARM box, a Mac running a host process, a Podman host, and a Kubernetes node. |
| **Runtime & model inventory** | *How* a workload is executed on a node — `systemd`, `podman`, `docker`, `k8s-pod`, `ray-actor`, `host-process` — and which model artifacts each runtime can serve. |
| **Loaded-model state** | Which models are resident, where, and since when. |
| **Hardware capabilities** | A normalized, inference-relevant capability record, derived by reading NVML/CDI/NFD/`lscpu` — never by reimplementing detection. |
| **Capacity & health** | A three-level state machine — node, engine, model — rolled up across backends into one signal, plus whether a node can take more load. |
| **Admission & execution estimates** | Will this model artifact fit on this node, given free memory, quantization, and context length? Can it take another concurrent request? What will it cost in queue time and throughput? |
| **Placement policy** | *Which* node/runtime/engine should serve this workload, expressed as an **intent**. Execution of that intent is delegated. |
| **Lifecycle delegation** | Load, unload, keep-alive, idle-evict — decided here, **executed by the engine wherever the engine already does it** (see §4). |
| **Run tracking & cancellation** | The identity and status of an in-flight run, and the ability to cancel it. |
| **Execution metadata** | What ran where, on what hardware, at what throughput. Infrastructure telemetry — **not** a task ledger, and **not** trusted memory. |

### 2.2 ComputeConnect does not own

Stated in the handoff, and confirmed by the survey:

* **Tasks** → AgentConnect. ComputeConnect has no concept of a task, a subtask, or a handoff.
* **Memory** → BrainConnect. ComputeConnect stores no facts and promotes nothing. See §5.2.
* **Tools** → ToolConnect. ComputeConnect does not decide who may call what.
* **Workflow engines** → out of scope entirely.
* **Secrets managers** → out of scope. Credentials are referenced, never held.

Delegated, with the delegate named:

| Concern | Delegated to |
|---|---|
| **Model inference** | llama.cpp, vLLM, Ollama, MLX, LocalAI |
| **Cloud API normalization** | LiteLLM, or an equivalent maintained provider gateway |
| **Cluster scheduling** | Kubernetes, Ray, Slurm, Nomad |
| **Container execution** | Docker, Podman, containerd |
| **Secrets** | Existing secret managers |

Added by this document, because the survey showed the risk is real:

* **A scheduler.** Ray's placement groups and Kubernetes' scheduler already do bin-packing.
  ComputeConnect emits an intent; the native runtime binds it.
* **A request-level LLM gateway.** LiteLLM owns this, per D1. ComputeConnect is not a replacement
  for LiteLLM and is not a generic cloud-LLM API proxy. It **may integrate with** such a gateway
  when workload placement requires reaching a cloud model provider.
* **Device detection.** NVML, CDI, NFD, and DCGM already enumerate hardware. ComputeConnect is a
  *normalizer* over them.
* **The inference data path**, if avoidable. See D3.

---

## 3. Prior art, and why this is not that

This section exists because the initial scope collided with several mature projects. Each is
named, and the collision is resolved. All version and status claims were verified in July 2026;
each is marked with the confidence it deserves.

### LiteLLM — *the most serious collision*

MIT-licensed, ~53k stars, releasing several times a week. Its Proxy already provides: a
provider/deployment registry as data, per-request routing across deployments using live latency,
in-flight count and cost signals, failure-driven cooldowns, durable spend and token metadata,
budgets, virtual keys, and rate limits.

**Roughly 60–70% of ComputeConnect's originally-stated responsibility list is nominally covered
by LiteLLM.** Building a parallel "provider registry + routing + health + cost tracking" stack
means rebuilding LiteLLM's Router with far less maturity.

The resolution is that **LiteLLM's entire model is a request router over already-running
endpoints**. It never starts a process, never reads VRAM, never detects hardware, has no concept
of "not loaded vs ready," and treats `localhost:11434` exactly like `api.openai.com`. Production
local stacks insert a *separate* tool (`llama-swap`) between LiteLLM and the engines precisely
because LiteLLM will not manage lifecycle.

> **Therefore:** ComputeConnect occupies the compute plane *below* LiteLLM and **should feed it** —
> registering and deregistering deployments as it brings engines up and down. It must not
> reimplement the Router. **D1** ratifies this.

*Licensing caveat, verified verbatim from the root `LICENSE`:* LiteLLM is MIT **except** the
`enterprise/` directory, which carries a proprietary BerriAI Enterprise License requiring a paid
subscription for production use. The official `ghcr.io/berriai/litellm` image bundles that
directory and gates it at runtime. "The proxy is MIT" is only mostly true. If ComputeConnect ever
*depends* on LiteLLM, depend on the MIT core, not the published image.

### LocalAI — *the closest analog to the whole idea*

`mudler/LocalAI` is an actively-maintained, multi-engine local inference server with a model
registry, hardware detection, health, and idle-unload. On a single box it is very nearly what an
unconstrained ComputeConnect would become.

> **Therefore:** the difference must be stated or the project is redundant. LocalAI **performs
> inference** and is **single-host**. ComputeConnect performs none and is **multi-host,
> multi-runtime**. This is the motivating case for the design-validation rule in **D2**: absent a
> genuine heterogeneous placement problem, LocalAI is the better answer and should be preferred.

### llama.cpp and Ollama — *they already own their own lifecycle*

* **llama.cpp** (MIT, near-daily releases) shipped a native multi-model **router mode** around
  December 2025, exposing `GET /models`, `POST /models/load`, `POST /models/unload`, plus
  `/health`, `/slots`, `/metrics` and both OpenAI- and Anthropic-compatible surfaces. Its
  queueing has no backpressure or max depth — requests defer indefinitely. *(Endpoint list is
  from source inspection by a research agent; the live node here answers `/health` and
  `/v1/models`, and `GET /models` returns 200. The `load`/`unload` verbs are **unverified against
  this build** — see STATUS.)*
* **Ollama** (MIT) exposes `/api/tags`, `/api/ps` (loaded models with `expires_at`, `size_vram`),
  a `keep_alive` parameter (default 5m, `0` unloads now), and `OLLAMA_MAX_QUEUE` with genuine
  **503 backpressure** — more production-hardened than llama.cpp on that axis.

> **Therefore:** for these two engines, ComputeConnect's "local model lifecycle" is a **delegation**,
> not an implementation. It calls their endpoints and records the resulting state.

### MLX — *the one engine where lifecycle is genuinely missing*

`mlx-lm`'s server exposes only `/v1/chat/completions`, `/v1/completions`, `/v1/models`, and
`/health`. **No load/unload endpoint, no TTL, no embeddings.** Its own documentation says it is
not recommended for production. It gained continuous batching in late 2025 and a partial CUDA
backend — it is no longer Apple-only.

> **Therefore:** MLX is where ComputeConnect adds net-new lifecycle value, and it is the
> motivating case for the lifecycle abstraction existing at all.

### llama-swap

A mature, engine-agnostic point solution for load-on-demand and TTL-based unload on **one host**.
It is what people reach for today to fill the gap LiteLLM leaves.

> **Therefore:** ComputeConnect's local-lifecycle slice is "llama-swap, but cross-host, capability-
> aware, and contract-bound to AgentConnect." Where a single host suffices, llama-swap is the
> cheaper answer and should be recommended. See **D2**.

### Ray

Apache-2.0. Within one cluster, Ray already *is* a compute registry (the GCS tracks every node's
CPU/GPU/memory/custom resources and labels, live), a placement engine (placement groups with
`PACK`/`SPREAD`/`STRICT_*`), an autoscaler, and a health tracker. `ray.serve.llm` even does
multi-model serving with per-model autoscaling.

Its scope stops hard at the cluster boundary: both the Ray autoscaler and KubeRay are explicitly
single-cluster, and the KubeRay federation proposal is unshipped.

> **Therefore:** *never* reimplement intra-cluster bin-packing, node heartbeats, or replica load
> balancing. ComputeConnect chooses **which cluster or host**, translates its request into Ray's
> resource spec, and hands off. Ray is a *compute provider*, not a competitor.

### Kubernetes, Docker, Podman, and CDI

* **Dynamic Resource Allocation (DRA)** reached **GA in Kubernetes v1.34** (released 2025-08-27),
  via KEP-4381 "structured parameters." Vendor and working-group sources are explicit that it
  *replaces* the device-plugin model, though no EOL date exists for device plugins — expect
  dual-track coexistence.
* **CDI (Container Device Interface)** is the convergence point for device injection: Docker
  (native, default since Engine 28.2.0), Podman, containerd, CRI-O, and DRA itself all terminate
  in CDI. NVIDIA's Container Toolkit generates CDI specs by default since v1.18.0.
  *Precision:* CDI is a specification owned by the Container Orchestrated Device working group
  under **CNCF TAG Runtime** — it is **not** a CNCF Sandbox/Incubating/Graduated project. Do not
  describe it as "the CNCF CDI project."
* For a **single-node rig** — one ARM box, one Mac, one workstation — Kubernetes and DRA are
  overkill. Bare `systemd`, or Podman **Quadlets**, is the appropriate substrate.

> **Therefore:** hardware capability detection is a **thin normalizer over NVML + CDI specs + NFD
> labels + `lscpu`**, never a new probing layer. Parsing the CDI spec at `/etc/cdi/*.yaml` is the
> highest-leverage single move: it is the one artifact every container runtime already agrees on.

### SkyPilot, llm-d, KubeAI

SkyPilot is the nearest cross-cloud placement layer but is VM/batch-job oriented. `llm-d` (CNCF
Sandbox, 2026) and KubeAI do GPU-aware inference scheduling **only inside Kubernetes**. None has
a story for a laptop, an edge box, or a bare-metal ARM host running llama.cpp outside k8s.

> **Therefore:** the unowned niche is precisely *heterogeneous, local-first, k8s-optional*.

---

## 4. Objects

Non-normative sketches. Field names are illustrative; this is a schema argument, not an API.

### ComputeNode

An addressable machine. The registry's primary object.

```
ComputeNode
  id                 stable identifier
  provider_id        → ComputeProvider
  runtimes           [systemd | podman | docker | k8s-pod | ray-actor | host-process]
  capabilities       → Capability
  availability       → Availability
  address            how to reach it
```

### Capability — *the normalizer's output*

The field that matters, and that no surveyed tool models: **inference-relevant** capability, not
device inventory.

```
Capability
  arch                    aarch64 | x86_64 | arm64-darwin
  cpu_features            [i8mm, dotprod, sve, avx512_vnni, ...]     ← from lscpu / NFD
  accelerators            [{kind: cuda|rocm|metal|vulkan|none,
                            vram_bytes, uuid, driver, cdi_device}]   ← from NVML / CDI / rocm-smi
  ram_bytes               total and currently free
  containerizable         bool
  gpu_requires_host_process   bool     ← TRUE on macOS: no GPU passthrough exists
```

`gpu_requires_host_process` is load-bearing. On macOS the GPU is reachable only from a native host
process — Hypervisor.framework has no vGPU, and no CDI or DRA concept covers a non-containerized
host inference process. A compute abstraction that cannot say *"GPU-accelerated ≠ containerized"*
will mis-place every workload it sends to a Mac.

### ModelArtifact

```
ModelArtifact
  id, format (gguf|safetensors|mlx), quantization, size_bytes,
  context_tokens, required_capabilities
```

### Admission — *"will this fit?"*

The question LiteLLM structurally cannot ask. A pure function of `ModelArtifact × Capability ×
current load`, returning fit / no-fit **with a reason**. The reason is the product: an unhelpful
refusal is worse than none.

### Placement — *an intent, never an action*

```
Placement
  workload → (node_id, runtime, engine)
  rationale     why this node, in terms a human can audit
```

ComputeConnect emits this. `podman run` / a Quadlet / a Ray placement group / a DRA ResourceClaim
executes it. The moment ComputeConnect binds a process to a core, it has become a scheduler and
violated its charter.

### Health — three levels, rolled up

The distinction the request plane collapses and should not:

| Level | Question | Source |
|---|---|---|
| **Node** | Is the box powered and reachable? | ICMP/SSH/agent heartbeat, DCGM |
| **Engine** | Is the inference server answering? | `GET /health` |
| **Model** | Is the model resident and ready? | `/models/loaded`, `/api/ps` |

An endpoint that answers `/health` while the requested model is not loaded is *available* at the
engine level and *not ready* at the model level. LiteLLM sees one bit here; ComputeConnect must
see three, because the admission and lifecycle decisions depend on which level failed.

---

## 5. Integration contracts

> No implementation. These define the boundary and the direction of the call.

### 5.1 AgentConnect conformance

**Already specified. Conform, do not invent.**

This is not a greenfield contract. AgentConnect's `agentconnect.core.local_compute` module already
defines the `LocalComputeProvider` ABC and ships `HttpLocalComputeProvider`, a client that speaks a
named HTTP surface. Its module docstring states the boundary better than this document could:

> *"AgentConnect defines the contract; it does not own the engine. VRAM admission, model loading,
> runtime selection, and queueing are somebody else's problem… AgentConnect asks one question —
> can local compute handle this? — and gets back a model/runtime/queue estimate or a refusal."*

**ComputeConnect is that somebody else.** The surface AgentConnect's client already calls:

```
GET  /health
GET  /models
GET  /models/loaded
POST /route/estimate
POST /generate
POST /runs/{run_id}/cancel
```

`POST /route/estimate` takes `{task_type, privacy_tier, required_capabilities, context_tokens,
max_output_tokens, latency_preference, quality_preference}` and returns `{eligible, selected_model,
runtime, loaded, estimated_queue_seconds, estimated_tokens_per_second, estimated_quality, reason}`.

Three properties of this contract are binding:

1. **Direction is one-way.** AgentConnect calls ComputeConnect. ComputeConnect must never call
   AgentConnect — that would create a cycle between the task and compute planes.
2. **An outage is a refusal, not an exception.** AgentConnect's adapter turns an unreachable
   provider into `health.available = False` and routes elsewhere. ComputeConnect being down must
   degrade AgentConnect, never crash it.
3. **Topology is hidden.** AgentConnect has no concept of a node. `/route/estimate` answers for the
   whole fleet; ComputeConnect resolves *which node* internally and reports the choice as
   `runtime` and `reason`.

The division of routing authority, which the two products must not both claim:

* **AgentConnect decides** whether work should use local versus cloud or other policy. That is
  *task* routing.
* **ComputeConnect decides** placement, runtime, and model **within the compute resources it
  manages**, once that policy selects it.

This is why "no routing implementation" and `POST /route/estimate` are not in conflict.

#### Ambiguities in the contract as written

Recorded, **not silently resolved**. ComputeConnect conforms to the contract as published; where the
contract underdetermines behavior, the gap is named here and must be settled *with* AgentConnect
before either side implements. Changing this surface unilaterally is not in scope.

1. **`cancel(run_id)` has no documented source of `run_id`.** `HttpLocalComputeProvider.run()` calls
   `POST /generate` and parses a `LocalRunResult` whose fields are `status`, `output`, `model`,
   `runtime`, `metrics`, `warnings`. **No run identifier is returned**, so a caller using the shipped
   client can never obtain the `run_id` that `POST /runs/{run_id}/cancel` requires. Either
   `/generate` must return an id, or the caller must supply one. This needs an AgentConnect-side
   decision.
2. **No streaming.** `/generate` is request/response. A long generation is therefore uncancellable
   in practice by the same client that started it, which compounds (1). If ComputeConnect proxies
   the data path (**D3**), streaming and backpressure become its problem and the contract is silent
   on both.
3. **`privacy_tier` is an input to `/route/estimate` but not to `/generate`.** `LocalRunRequest`
   carries `model`, `task_type`, `prompt`, `context`, `max_output_tokens`, `temperature`,
   `metadata` — no tier. So the *enforcement point* for a local-only workload is unspecified: a
   caller may estimate with one tier and generate with none. This is the contract-level face of
   **D5**, and fail-closed enforcement cannot be built until it is resolved.
4. **`estimated_quality` has no defined scale, units, or comparability** across models.
5. **Topology is intentionally hidden**, so `runtime` and `reason` are the only channel through
   which ComputeConnect can explain *which* node it chose. Anything an operator needs to audit must
   fit there.

### 5.2 BrainConnect — a compute consumer, not a peer

BrainConnect is WikiBrain, renamed; the rename is in progress and the code still says `wiki`.

BrainConnect's librarian is model-bearing: `cli/librarian/client.py` targets a configurable
OpenAI-compatible `base_url` (defaulting to Ollama's `:11434`). On this host, the endpoint it is
meant to use is a `llama.cpp` server on `:8080`, run by a `wiki-llama` systemd unit. **That
service is a hand-managed instance of exactly what ComputeConnect exists to manage.**

The contract is therefore the *same shape* as AgentConnect's, and the direction is the same:

```
BrainConnect ──(OpenAI-compatible inference)──▶ ComputeConnect ──▶ llama.cpp
```

BrainConnect is a **compute consumer, not a peer scheduler.** It calls an OpenAI-compatible
inference endpoint, and it may record durable lessons about model and runtime behavior. It does not
place workloads.

Three constraints:

1. **ComputeConnect never writes trusted memory.** It has no BrainConnect write client. Trust and
   promotion are human-gated inside BrainConnect and must not be reachable from the compute plane.
2. **Operational telemetry is not memory.** Throughput, queue depth, evictions, and health
   transitions belong in metrics and logging systems. Later, *selected* observations — "this
   quantization never fits this node," "this runtime degrades above N concurrent runs" — may be
   **proposed** to BrainConnect as memory candidates. Proposed, and gated by a human, like every
   other candidate. A compute plane that writes its own facts into a trusted ledger has laundered
   the gate.
3. **Privacy tier is an input, not an inference.** If a request requires local-only execution,
   ComputeConnect must **fail closed**: refuse with a reason rather than place it on a rented cloud
   GPU. Cloud providers are not considered at all for such workloads. Admission never downgrades
   privacy to satisfy a placement. This must eventually be **structural policy, not a prompt
   convention or a code comment**. **D5** covers the mechanism; there is no implementation in this
   phase.

*Open:* whether BrainConnect consumes the OpenAI-compatible surface directly or the
`LocalComputeProvider` surface. Reusing one surface for both consumers is simpler; the librarian
already speaks OpenAI. **D4.**

### 5.3 ToolConnect — **provisional**

ToolConnect is a tool-governance layer: which tools exist, who may call them, whether they are
healthy, what happened when they were called. It has **architecture documents but no runtime**, so
this contract is drafted against a document, not a system. **It is provisional and nothing may be
built against it.** Do not build against an unimplemented API.

The plausible relationship, stated so it can be argued with later: ToolConnect governs access to
compute-management tools, or exposes compute capabilities as governed tools.

Two boundaries are worth fixing now, because both products use the word "health":

* **ToolConnect owns health *of tools*. ComputeConnect owns health *of compute*.** A tool can be
  perfectly healthy on a box that is on fire, and vice versa. Neither should infer the other's.
* **Authorization is never ComputeConnect's.** If a tool needs a sandbox to execute in,
  ToolConnect decides *whether it may run*; ComputeConnect answers *where it can run*.

The plausible shape is that a tool requiring execution presents a sandbox requirement, and
ComputeConnect returns a `Placement`. AgentConnect already models this need — `SandboxSpec`
(`filesystem`, `network`, `shell`, with a `satisfied_by` check) exists in its core models today and
is the obvious vocabulary to reuse rather than mint a third one.

**This contract should not be built until ToolConnect has a runtime.** Designing against a
counterparty that cannot yet say "no" produces a contract that only one side has tested.

---

## 6. Decisions

D1 and D2 are **ratified** (2026-07-10). D3–D6 remain open; work should not start on the phases
that depend on them. Listed in [STATUS.md](STATUS.md) with the same numbering.

### D1 — Provider means *compute provider* — **RATIFIED**

ComputeConnect's provider registry represents **compute environments and execution planes**:

* a local host,
* a remote host,
* a Kubernetes cluster,
* a rented GPU node,
* a runtime service tied to compute capacity.

It does **not** represent generic LLM API providers. ComputeConnect is not positioned as a
replacement for LiteLLM and is not a generic cloud-LLM API proxy. Cloud model-provider routing
remains delegated to LiteLLM or another maintained provider gateway. ComputeConnect **may integrate
with** those systems when workload placement requires them.

### D2 — Design-validation rule — **RATIFIED**

> If the product's demonstrated use case remains a single local host with no heterogeneous
> placement problem, prefer maintained single-node systems such as LocalAI, llama-swap, Ollama, or
> LiteLLM rather than building ComputeConnect.

This is engineering discipline, and it is a rule to test the design against — **not** a foregone
conclusion that the repository should be deleted. The obligation it creates is to demonstrate a
heterogeneous placement problem, not to pre-emptively abandon.

### Open

| # | Decision | Recommendation |
|---|---|---|
| **D3** | Does `POST /generate` proxy inference through ComputeConnect (data path), or return an endpoint reference for the caller to hit directly (control plane only)? | **Proxy, reluctantly** — AgentConnect's client already expects it. But design for dispatch-by-reference, because a control plane in the token hot path must then own streaming, backpressure, and cancellation. Interacts with ambiguities (1) and (2) in §5.1. |
| **D4** | Do BrainConnect and AgentConnect consume one surface or two? | **One.** Serve the `LocalComputeProvider` surface, and expose an OpenAI-compatible alias for BrainConnect's existing librarian client. |
| **D5** | How is a local-only privacy tier *structurally enforced*, rather than merely honored? | Unresolved, and a hard blocker on any cloud provider. Admission must fail closed. Needs a mechanism, not a promise — and §5.1 ambiguity (3) must be settled with AgentConnect first. |
| **D6** | License for this repository. | Unresolved. The ecosystem's engines are MIT/Apache-2.0; no constraint forces a choice yet. |

---

## 7. Validation: contract versus product value

These are different claims, they fail differently, and conflating them is how a design doc gets to
feel validated without being validated.

### Contract validation — **does not require new hardware**

Provable with **two logically distinct providers** differing in capability or policy, where **one
may be simulated, containerized, remote, or CPU-only**. A fake provider that refuses GPU workloads
and a real llama.cpp host that accepts CPU ones is a sufficient two-provider fleet to exercise the
registry, the capability schema, admission, placement intent, and the fail-closed privacy path.

This is what Phases 1–3 test, and it is testable today on one box.

### Product-value validation — **eventually requires real heterogeneous compute**

No number of simulated providers demonstrates that heterogeneous placement is *worth doing*. Before
ComputeConnect may claim production multi-node placement value, it needs a real second node of a
different shape — an accelerator, or a Mac where `gpu_requires_host_process` is true.

Until then the claim is unmade, not disproven. **Today there is exactly one node and it has no
accelerator.**

### What would falsify the design

* If AgentConnect's existing local-manager tests cannot be made to pass against a real
  ComputeConnect serving the six endpoints, the contract in §5.1 was misread.
* If, after building the capability normalizer, every value in it is already available from a single
  `nvidia-smi` or `ollama ps` call, then §4 is ceremony and llama-swap is the answer.
* If a simulated second provider cannot be made to change a placement decision, the placement policy
  has no content.
* If no heterogeneous placement problem ever materializes, **D2** applies.

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
   AgentConnect            BrainConnect · direct applications
        │                            │
   control-plane API         OpenAI-compatible inference API
   (LocalComputeProvider)    (/v1/chat/completions)
        ▼                            ▼
  ┌──────────────────────────────────────────────────────────────┐
  │                        ComputeConnect                         │
  │  ┌─ Control plane ──────────────┐ ┌─ Inference API ────────┐  │
  │  │ placement · routing ·        │ │ standard OpenAI-        │  │
  │  │ scheduling · admission ·     │ │ compatible surface;     │  │
  │  │ capacity · health ·          │ │ NOT another routing     │  │
  │  │ cancellation · selection     │ │ layer                   │  │
  │  └──────────────┬───────────────┘ └───────────┬────────────┘  │
  │                 └───── same execution backend ─┘              │
  └────────────────────────────┬─────────────────────────────────┘
                               │  placement intent  (never a scheduler)
                               ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Engines:   llama.cpp   vLLM   Ollama   MLX   LocalAI         │
  │  Runtimes:  systemd   Podman   Docker   Kubernetes   Ray      │
  │  Detection: NVML   CDI   NFD   DCGM   /proc/cpuinfo           │
  └──────────────────────────────────────────────────────────────┘
```

Two consumers, two API surfaces, **one execution backend** — detailed in §5. They are not competing
systems: AgentConnect drives the control plane, BrainConnect and direct applications use the
inference API, and both ultimately reach the same engines.

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
* **Memory** → BrainConnect. ComputeConnect stores no facts and promotes nothing. See §7.2.
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

## 5. Two APIs, one backend

**Ratified — D4.** ComputeConnect exposes **two distinct API layers**. They are not two routers for
the same job, and they must not be collapsed into one. They are different *layers* with different
callers, and they terminate at the **same execution backend**.

### Layer 1 — Control plane

**Owned by ComputeConnect. Used by AgentConnect.** This is the layer that makes ComputeConnect a
control plane rather than an inference server. Its responsibilities:

* placement
* routing (of compute, not of LLM-API providers — see D1)
* scheduling *policy* (never a scheduler — see §4, `Placement`)
* admission ("will it fit?")
* health and capacity
* cancellation
* provider selection

The existing `LocalComputeProvider` endpoints belong here: `GET /health`, `GET /models`,
`GET /models/loaded`, `POST /route/estimate`, `POST /runs/{run_id}/cancel`. This is a *richer*
surface than inference — no engine speaks it — and it is detailed as a conformance target in §7.1.

### Layer 2 — Inference API

**A standard OpenAI-compatible inference endpoint** (`/v1/chat/completions` and friends). Its
purpose is compatibility, not orchestration:

* BrainConnect's librarian, which already speaks this dialect
* direct applications that want inference without knowing about the control plane
* provider compatibility — every engine below (llama.cpp, vLLM, Ollama, MLX, LocalAI) already
  serves this exact surface, so it is the lingua franca of the layer

> **This is not another routing layer.** It is simply the standard inference interface. It carries
> no placement or admission semantics of its own; it is the inference *verb* over whatever backend
> the control plane has already made ready.

### Both reach the same backend

`POST /generate` on the control plane and `/v1/chat/completions` on the inference API are two doors
into **one execution path**. The control plane adds estimate/admission/selection *around* a
generation; the inference API is the bare generation. Neither owns a second copy of the engine, the
model, or the runtime. A fact true of one — a model is resident, a node is at capacity — is true of
the other, because there is only one backend underneath.

This is why the split is layers, not duplication: remove the control plane and the inference API
still works against a manually-chosen backend; remove the inference API and the control plane has no
verb to execute what it placed.

---

## 6. Structural privacy enforcement

**Ratified — D5. This is a hard architectural invariant, not a policy knob, a prompt convention, or
a code comment.**

### The problem it corrects

`LocalEstimateRequest` carries a **required** `privacy_tier` field. `LocalRunRequest` does **not** —
it carries only `model`, `task_type`, `prompt`, `context`, `max_output_tokens`, `temperature`, and a
`metadata` dict (verified in AgentConnect's `local_compute.py`). So the *estimate* stage knows the
privacy tier and the *execution* stage is blind to it. Any enforcement that lives only at execution
time is therefore built on a value the execution stage cannot see.

### The invariant

**Cloud execution is default-denied.** Cloud providers are removed from the candidate set *before*
placement runs, and only an explicit, cloud-permitting tier can put them back:

```
privacy tier is unknown    ──▶  no cloud candidates
privacy tier is local-only ──▶  no cloud candidates
privacy tier is missing    ──▶  no cloud candidates
privacy tier explicitly
  permits cloud             ──▶  cloud candidates eligible
```

Three properties make this structural rather than conventional:

1. **The safe state is the default.** A caller who sets nothing gets local-only. Reaching a cloud
   provider requires an affirmative, explicit permit — forgetting to set a field can never *widen*
   exposure, only narrow it.
2. **Filtering happens before placement, not after.** The candidate set handed to the placement
   policy already excludes every non-compliant provider. Placement cannot select what it never saw;
   there is no "downgrade" path to accidentally take.
3. **No compliant provider ⇒ a structured refusal.** If filtering empties the candidate set, the
   request is refused with a machine-readable reason. ComputeConnect **never silently downgrades**
   privacy to make a placement succeed.

### Why not just trust the tier at execution time

Because the execution request does not carry the tier. Relying on the caller to re-send it in
`metadata` would be exactly the "prompt convention" this invariant forbids: safety would depend on
the caller remembering. The default-deny candidate filter holds even when the caller sends nothing,
which is the whole point. A future contract amendment (§7.1, and CONTRACT.md **CA-1**) adds
`privacy_tier` to `LocalRunRequest` so execution can *positively re-verify* the placement decision —
defense in depth — but the invariant above must already make the system safe without it.

---

## 7. Integration contracts

> No implementation. These define the boundary and the direction of the call.

### 7.1 AgentConnect conformance

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
   `metadata` — no tier. This is the contract-level face of **D5**. It does **not** block safety:
   the structural default-deny invariant in §6 makes the system safe regardless, because the safe
   state is the default. The amendment that adds `privacy_tier` here (CONTRACT.md **CA-1**) is
   defense in depth — a positive re-check at execution time — not a prerequisite.
4. **`estimated_quality` has no defined scale, units, or comparability** across models.
5. **Topology is intentionally hidden**, so `runtime` and `reason` are the only channel through
   which ComputeConnect can explain *which* node it chose. Anything an operator needs to audit must
   fit there.

### 7.2 BrainConnect — a compute consumer, not a peer

BrainConnect is WikiBrain, renamed; the rename is complete — the package and CLI are
`brainconnect`, the service identifies itself as `brainconnect`, and `brainconnect serve` exposes
the memory link on `127.0.0.1:8787`.

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
   ComputeConnect **fails closed** per the structural invariant in §6: cloud providers are filtered
   out of the candidate set before placement, and an empty set yields a structured refusal.
   Admission never downgrades privacy to satisfy a placement. There is no implementation in this
   phase; the invariant is the architecture.

**Which surface (D4, ratified):** BrainConnect consumes the **Layer 2 inference API**
(OpenAI-compatible), which its librarian already speaks. AgentConnect consumes the **Layer 1 control
plane**. These are the two layers of §5, reaching the same backend — not one surface with an alias.

### 7.3 ToolConnect (provisional)

ToolConnect is a tool-governance layer: which tools exist, who may call them, whether they are
healthy, what happened when they were called. It now has a **validated Phase 1 runtime**, but **no
execution/invoke surface** — governance decisions are made, not carried out through it. So there is
no compute-facing contract to conform to yet, and this section stays **provisional**: the boundary
is drawn, but **nothing may be built against it** until ToolConnect exposes a compute-relevant
surface.

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

## 8. Decisions

**All of D1–D6 are ratified.** D1–D2 on 2026-07-10; D3–D6 on 2026-07-11. Listed in
[STATUS.md](STATUS.md) with the same numbering.

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

### D3 — `/generate` is a thin streaming proxy — **RATIFIED**

ComputeConnect stays in the request path, because AgentConnect's shipped client reads generated
output inline and cannot be given a bare provider URL. But it stays there **as thinly as possible**:

```
Client ──▶ ComputeConnect ──▶ Provider ──▶ streaming response back through
```

* **Stream, do not buffer.** No large in-memory buffering of a generation; tokens pass through as
  they are produced.
* **Cancellation propagates** from client through ComputeConnect to the provider.
* **Backpressure propagates** the same way.
* ComputeConnect is **as transparent as possible** — a pass-through, not a staging buffer.

Dispatch-by-reference is recorded as a possible future amendment (CONTRACT.md **CA-2**), not built
now. This is the intended implementation; there is no streaming code in this phase.

### D4 — Two APIs, separated by role — **RATIFIED**

Not one surface with an alias. Two layers — control plane for AgentConnect, OpenAI-compatible
inference for BrainConnect and direct applications — reaching one backend. Fully specified in §5.

### D5 — Structural default-deny privacy enforcement — **RATIFIED**

A hard architectural invariant: cloud providers are filtered out of the candidate set before
placement, unknown/missing/local-only tiers yield no cloud candidates, and an empty set is a
structured refusal — never a silent downgrade. Fully specified in §6. The `LocalRunRequest`
amendment (CA-1) strengthens it but is not a prerequisite.

### D6 — Apache-2.0 — **RATIFIED**

The repository is licensed Apache-2.0 (`LICENSE`). Rationale: the explicit patent grant matters more
for a compute control plane than for a memory ledger; it matches every infrastructure system
ComputeConnect integrates with (Kubernetes, Ray, vLLM, containerd) and BrainConnect; and it is more
appropriate for infrastructure than MIT.

---

## 9. Validation: contract versus product value

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
  ComputeConnect serving the six endpoints, the contract in §7.1 was misread.
* If, after building the capability normalizer, every value in it is already available from a single
  `nvidia-smi` or `ollama ps` call, then §4 is ceremony and llama-swap is the answer.
* If a simulated second provider cannot be made to change a placement decision, the placement policy
  has no content.
* If no heterogeneous placement problem ever materializes, **D2** applies.

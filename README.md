# ComputeConnect

**A compute-resource control plane.** It is the authority on *what compute exists*, *what it
is capable of*, *whether it is healthy*, *whether a given model will fit on it*, and *where a
workload should run*.

ComputeConnect is **architecture and interfaces only**. There is no runtime, no server, and no
code in this repository. See [docs/STATUS.md](docs/STATUS.md) before proposing work.

---

## The one-sentence version

Every layer of the local-AI stack today assumes compute already exists and is already running:
gateways route requests to endpoints somebody else started, engines load models onto hardware
somebody else described, schedulers place pods on nodes somebody else registered. ComputeConnect
is the layer underneath that answers *what is actually out there, right now, and will this fit* —
and it is deliberately not an inference engine.

## What it does not do

ComputeConnect **does not perform inference**. It never loads a tensor, never picks a
quantization, never implements an attention kernel. It knows that `llama.cpp` on the ARM box can
run a 30B MoE, and it knows how to ask it to; it does not know how.

It also does not own tasks, memory, tools, workflow engines, inference engines, or secrets
managers. Those belong elsewhere, and the boundaries are drawn in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Where it sits

ComputeConnect is the **compute plane** of the [Connect ecosystem](https://github.com/Judgernaut777/Connect).

| Plane | Product | Owns |
|---|---|---|
| Task | **AgentConnect** | tasks, artifacts, decisions, reviews, handoffs, worker routing |
| Memory | **BrainConnect** | the human-gated trusted memory ledger |
| Tool | **ToolConnect** | which tools exist, who may call them, what happened |
| **Compute** | **ComputeConnect** | **what compute exists, what it can do, is it healthy, will this fit, where should this run** |

AgentConnect and BrainConnect are both **consumers** of compute. AgentConnect already ships a
`LocalComputeProvider` contract and an HTTP client for it; BrainConnect's librarian already talks
to an OpenAI-compatible local endpoint. ComputeConnect is the thing that should be on the other
end of both — and one of those contracts is already written, so ComputeConnect conforms to it
rather than inventing a new one.

## Status at a glance

Nothing runs. One compute node exists, and today it is described, not managed.

| Deliverable | State |
|---|---|
| Product boundaries | Drafted — [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| AgentConnect contract | **Already specified by AgentConnect**; ComputeConnect must conform. Two ambiguities documented, not patched. |
| BrainConnect contract | Drafted — a compute consumer, not a peer scheduler |
| ToolConnect contract | **Provisional** — ToolConnect has no runtime; nothing may be built against it |
| Code | None. Intentionally. |
| Decisions | **D1, D2 ratified.** D3–D6 open — see [STATUS.md](docs/STATUS.md) |

## The honest risk

A large fraction of ComputeConnect's originally-stated scope is **already owned by mature,
actively-maintained, permissively-licensed projects**. LiteLLM covers the request plane. Ray and
Kubernetes cover in-cluster placement. llama.cpp and Ollama already manage their own model
lifecycles. LocalAI is a close analog of the whole idea on a single box.

[ARCHITECTURE.md](docs/ARCHITECTURE.md#3-prior-art-and-why-this-is-not-that) confronts each of these
by name and narrows the charter accordingly. The defensible slice is real, but it is **much
smaller than the initial scope suggested**, and the roadmap reflects the smaller slice. A
ComputeConnect that drifts into request-level routing collapses into a worse LiteLLM.

The charter is held honest by a ratified design-validation rule (**D2**): *if the demonstrated use
case remains a single local host with no heterogeneous placement problem, prefer maintained
single-node systems — LocalAI, llama-swap, Ollama, LiteLLM — over building ComputeConnect.*

## Documents

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — boundaries, objects, prior art, integration contracts
* [docs/ROADMAP.md](docs/ROADMAP.md) — phases and the gate for each
* [docs/STATUS.md](docs/STATUS.md) — what is true today, and the decisions that block work

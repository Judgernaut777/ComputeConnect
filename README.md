# ComputeConnect

**A compute-resource control plane.** It is the authority on *what compute exists*, *what it
is capable of*, *whether it is healthy*, *whether a given model will fit on it*, and *where a
workload should run*.

As of **v0.1.0** there is a minimal runtime: the `computeconnect` package serves both API layers
from one backend. Licensed **Apache-2.0** ([LICENSE](LICENSE)). See
[docs/STATUS.md](docs/STATUS.md) — including its honest D2 re-evaluation — before proposing work.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -e .   # or: uv pip install -e .
.venv/bin/computeconnect serve --port 8090 --upstream http://127.0.0.1:8080
```

* `--upstream` is an existing OpenAI-compatible llama.cpp server, consumed **read-only** —
  ComputeConnect never starts, stops, loads, or unloads it.
* Port 8090 by default (on the reference host, 8080 is the engine and 8787 is BrainConnect's).
* Layer 1 (AgentConnect control plane): `GET /health`, `GET /models`, `GET /models/loaded`,
  `POST /route/estimate`, `POST /generate` (streams; returns `X-Run-Id`),
  `POST /runs/{run_id}/cancel`.
* Layer 2 (OpenAI-compatible): `GET /v1/models`, `POST /v1/chat/completions`.
* Privacy is structural: no `privacy_tier` means the most restrictive tier — cloud-class
  providers are filtered before placement and refusals are structured, never silent downgrades.

Tests: `.venv/bin/python -m pytest` (installs `pytest` via `pip install -e .[dev]`; real-engine
tests skip when no llama.cpp is reachable on `:8080`). 140 tests total, 129 of them fully offline;
see [docs/STATUS.md](docs/STATUS.md) for the current pass count and the real-engine caveat.

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

## Standalone by design

**ComputeConnect can be used on its own.** It depends on no sibling project. AgentConnect uses it
for orchestration, BrainConnect uses it for inference, and any other application may use it
directly:

* Applications and AgentConnect drive the **control-plane API** (`LocalComputeProvider`) — placement,
  admission, health, cancellation.
* BrainConnect and direct applications use the **OpenAI-compatible inference API** — the same dialect
  every engine already speaks.

Both surfaces reach one execution backend. The two-API split is specified in
[ARCHITECTURE.md §5](docs/ARCHITECTURE.md#5-two-apis-one-backend); the stable interface itself lives
in [docs/CONTRACT.md](docs/CONTRACT.md). No sibling product is required to run or build against
ComputeConnect.

## Status at a glance

A minimal runtime exists (v0.1.0). One real compute node exists; the second provider is
simulated, and the heterogeneity premise is therefore still **unproven** — stated plainly in
[STATUS.md](docs/STATUS.md).

| Deliverable | State |
|---|---|
| Product boundaries | Drafted — [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Contracts | Locked — [CONTRACT.md](docs/CONTRACT.md): two APIs, five binding invariants; **CA-1 and CA-3 implemented**, CA-2 proposed |
| AgentConnect contract | **Conformed to and tested with AgentConnect's shipped client**, including against the real local engine |
| BrainConnect contract | Drafted — a compute consumer on the inference API, not a peer scheduler; nothing rewired yet |
| ToolConnect contract | **Provisional** — validated runtime but no compute-facing surface yet |
| Code | `computeconnect` 0.1.0: both API layers, structural privacy, streaming + cancellation, 140 tests (129 offline + 11 real-engine) |
| Decisions | **D1–D6 all ratified** — implementation status per decision in [STATUS.md](docs/STATUS.md) |
| License | **Apache-2.0** |

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

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — boundaries, objects, prior art, the two APIs, the privacy invariant, integration contracts
* [docs/CONTRACT.md](docs/CONTRACT.md) — the stable interface surface and its future amendments
* [docs/ROADMAP.md](docs/ROADMAP.md) — phases and the gate for each
* [docs/STATUS.md](docs/STATUS.md) — what is true today, and the ratified decisions
* [LICENSE](LICENSE) — Apache-2.0

# Heterogeneity validation — 2026-07-27

**Status: single-host, two-engine heterogeneous placement PROVEN, live, on real hardware.**
Cross-machine heterogeneity is a separate, still-open claim — see Honest scope below.

This supersedes the earlier D2 re-evaluation's caveat ("skip, never fake, when either engine
is down"): on this date both engines were up, the full gate was run, and every two-engine test
**passed** rather than skipping. This document is the record of that run.

---

## What was proven

With two real, independently-running llama.cpp engines on the same host — materially different
in model family, parameter count, and context window — ComputeConnect's placement logic:

1. **Sees both engines as distinct nodes**, correctly reporting their model ids, sizes, and
   context windows.
2. **Selects the faster engine under a `latency_preference`** (the 4B dense model).
3. **Selects the higher-declared-quality engine under a `quality_preference`** (the 35B MoE model).
4. **Is forced onto the large-context engine when the workload does not fit the small one**
   (a 12k-token request cannot fit the 4B's 8k window, so it is placed on the 16k-window 35B
   engine regardless of preference) — a capability/capacity constraint, not a preference.
5. **Produces real generations from both engines**, not simulated or mocked output.

This is a genuine placement decision driven by live, per-node facts (latency, declared quality,
context-window fit) that a static router could not make without re-deriving those facts itself.

### The two engines

| Engine | Port | Model id | Params | Context |
|---|---|---|---|---|
| Reference | `:8080` | `qwen3.6-35b-a3b` | ~35B (MoE) | 16384 |
| Second | `:8091` | `qwen3-4b` | ~4B (dense) | 8192 |

Different model family (MoE vs. dense), different size (35B vs. 4B), and different context
window (16k vs. 8k): a real second node of a materially different shape, not a relabeled clone
of the first.

Pre-check performed before the run (both engines live):

```
GET :8080/v1/models -> {"id":"qwen3.6-35b-a3b", n_ctx:16384, n_params:34660610688 (~35B MoE)}
GET :8091/v1/models -> {"id":"qwen3-4b", n_ctx:8192, n_params:4022468096 (~4B dense)}
```

## Exact test names and results

Run: `cd /home/mini/ComputeConnect && .venv/bin/python -m pytest tests/test_real_engine.py -v -o addopts=""`

(`-o addopts=""` overrides the `-q` in `pyproject.toml`'s default addopts, which otherwise
suppresses per-test verbose names.)

```
tests/test_real_engine.py::test_real_models_inventory PASSED             [  9%]
tests/test_real_engine.py::test_real_estimate_eligible PASSED            [ 18%]
tests/test_real_engine.py::test_real_generate_small PASSED               [ 27%]
tests/test_real_engine.py::test_real_generate_cancellation PASSED        [ 36%]
tests/test_real_engine.py::test_real_shipped_agentconnect_client_phase1_gate PASSED [ 45%]
tests/test_real_engine.py::test_real_openai_layer_small PASSED           [ 54%]
tests/test_real_engine.py::test_two_real_engines_both_visible PASSED     [ 63%]
tests/test_real_engine.py::test_latency_preference_selects_the_fast_real_engine PASSED [ 72%]
tests/test_real_engine.py::test_quality_preference_selects_the_accurate_real_engine PASSED [ 81%]
tests/test_real_engine.py::test_large_context_only_fits_the_big_window_engine PASSED [ 90%]
tests/test_real_engine.py::test_real_generation_from_BOTH_engines PASSED [100%]

============================== 11 passed in 5.67s ==============================
```

The five tests specific to two-engine heterogeneity — all **PASSED**, not skipped:

* `test_two_real_engines_both_visible` — both `:8080` and `:8091` enumerate as distinct nodes.
* `test_latency_preference_selects_the_fast_real_engine` — `latency_preference` routes to the 4B.
* `test_quality_preference_selects_the_accurate_real_engine` — `quality_preference` routes to the
  35B-A3B.
* `test_large_context_only_fits_the_big_window_engine` — a 12k-token request is placed on the
  16k-window engine because the 8k-window engine cannot fit it.
* `test_real_generation_from_BOTH_engines` — a real, non-mocked generation is completed against
  each engine in the same run.

These tests live behind the `hetero_stack` fixture and a `_two_engines` marker in
`tests/test_real_engine.py`; they **skip** (never fake a pass) whenever `:8091` is not reachable.
On 2026-07-27 they did not skip — both engines were live and every one of the five passed for
real.

## Full-suite gate

```
cd /home/mini/ComputeConnect && .venv/bin/python -m pytest
........................................................................ [ 46%]
........................................................................ [ 93%]
..........                                                               [100%]
154 passed in 56.74s
```

154 passed, 0 skipped, 0 failed — every real-engine test (including all five two-engine tests)
ran against live hardware rather than skipping.

## How to reproduce

1. Confirm the reference engine is up (it runs as the `qwen36-msr1` systemd unit; do not
   restart it): `curl -s http://127.0.0.1:8080/v1/models`.
2. Start the second engine: `scripts/second_engine.sh` (defaults to `qwen3-4b` on `:8091`,
   8k context; do not kill an instance you did not start yourself if one is already running).
3. Confirm it is up: `curl -s http://127.0.0.1:8091/v1/models`.
4. Run the gate: `cd /home/mini/ComputeConnect && .venv/bin/python -m pytest`. All two-engine
   tests in `tests/test_real_engine.py` should **PASS** (not skip) whenever both engines answer.
   Model ids are read from `CC_REAL_MODEL` / `CC_REAL_MODEL_B` env vars if the defaults ever
   drift from what is actually loaded on `:8080` / `:8091`.

## Honest scope

**What this proves:** real, non-simulated heterogeneous placement across two engines that differ
in model family, size, and context window, with preference-driven selection, capacity-forced
placement, and real generation from both — reproducible on demand via the steps above.

**What this does NOT prove:** cross-machine heterogeneity. Both engines in this validation run
on **one host**. There is still exactly one physical box in this proof; a remote node of a
genuinely different hardware class (starting with the Radeon R9700 box, which is reachable today
only from the `192.168.34.55` agent host via a reverse SSH tunnel into ComputeConnect's upstream,
not from ComputeConnect's own host directly) has not been registered as a ComputeConnect provider
and has not had a placement decision made across it. Proving that — a real decision made across
two different physical machines, one of them GPU-class — remains the next, still-open step
toward the full heterogeneity premise in `docs/ARCHITECTURE.md`. Nothing in this document should
be read as claiming that step is done.

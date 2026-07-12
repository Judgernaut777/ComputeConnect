"""Against the real llama.cpp engine on :8080 (the wiki-llama systemd unit).

Read-only consumption: these tests never load, unload, or reconfigure
anything. They skip — not fail — when the engine is unreachable.

The generation tests are real CPU inference on a 30B MoE model: they are kept
tiny (a handful of output tokens) but still take seconds.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from computeconnect.app import AppConfig, create_app
from computeconnect.engines import LlamaCppEngine
from computeconnect.providers import ProviderSpec

from conftest import ServerHandle

REAL_UPSTREAM = "http://127.0.0.1:8080"
#: A SECOND real engine of a materially different shape: a 4B dense model with
#: an 8k context window, vs the 30B MoE / 16k window on :8080. Stand it up with
#:   scripts/second_engine.sh   (or the Wave-A demo runner). Tests that need it
#: skip when it is absent — they never fake heterogeneity.
REAL_UPSTREAM_B = "http://127.0.0.1:8091"


def _up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=3).json().get("status") == "ok"
    except Exception:
        return False


def _engine_up() -> bool:
    return _up(REAL_UPSTREAM)


pytestmark = pytest.mark.skipif(
    not _engine_up(), reason=f"real llama.cpp engine not reachable at {REAL_UPSTREAM}"
)


@pytest.fixture(scope="module")
def real_stack():
    config = AppConfig(
        providers=[
            ProviderSpec(
                id="local-llamacpp",
                placement_class="local",
                engine=LlamaCppEngine(REAL_UPSTREAM, stream_timeout=300.0),
                capabilities=("completion", "chat", "generate", "code", "summarize"),
                max_concurrency=1,
            )
        ],
        snapshot_ttl=5.0,
    )
    handle = ServerHandle(create_app(config)).start()
    try:
        yield handle.base_url
    finally:
        handle.stop()


def test_real_models_inventory(real_stack):
    body = httpx.get(f"{real_stack}/models", timeout=15).json()
    ids = {m["id"] for m in body["models"]}
    assert "qwen3-30b-a3b" in ids
    model = next(m for m in body["models"] if m["id"] == "qwen3-30b-a3b")
    assert model["context_tokens"] > 0
    assert model["loaded"] is True
    assert model["runtime"] == "llama.cpp"


def test_real_estimate_eligible(real_stack):
    body = httpx.post(
        f"{real_stack}/route/estimate",
        json={
            "task_type": "general",
            "privacy_tier": "local_only",
            "required_capabilities": ["generate"],
            "context_tokens": 128,
            "max_output_tokens": 64,
        },
        timeout=15,
    ).json()
    assert body["eligible"] is True
    assert body["selected_model"] == "qwen3-30b-a3b"


def test_real_generate_small(real_stack):
    """A real (tiny) generation through the streaming proxy."""
    resp = httpx.post(
        f"{real_stack}/generate",
        json={
            "task_type": "general",
            "prompt": "Reply with the single word OK and nothing else.",
            "max_output_tokens": 40,
            "temperature": 0.0,
        },
        timeout=240,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["model"] == "qwen3-30b-a3b"
    assert body["metrics"]["chunks"] > 0
    assert body["output"].strip()


def test_real_generate_cancellation(real_stack):
    """Cancel a longer real generation mid-stream; it must stop early."""
    started = time.time()
    with httpx.Client(timeout=240) as client:
        with client.stream(
            "POST",
            f"{real_stack}/generate",
            json={
                "task_type": "general",
                "prompt": "Count slowly from 1 to 500, one number per line.",
                "max_output_tokens": 1024,
                "temperature": 0.0,
            },
        ) as resp:
            run_id = resp.headers["X-Run-Id"]
            collected = []
            cancelled = False
            for chunk in resp.iter_text():
                collected.append(chunk)
                text = "".join(collected)
                marker = text.find('"output": "')
                # Cancel only once real generated output is flowing.
                if not cancelled and marker != -1 and len(text) > marker + 40:
                    client.post(f"{real_stack}/runs/{run_id}/cancel")
                    cancelled = True
    elapsed = time.time() - started
    document = json.loads("".join(collected))
    assert cancelled, "generation finished before cancellation could be exercised"
    assert document["status"] == "cancelled"
    assert elapsed < 120, f"cancel did not shorten a 1024-token generation ({elapsed:.0f}s)"


def test_real_shipped_agentconnect_client_phase1_gate(real_stack):
    """ROADMAP Phase 1 gate: AgentConnect's shipped HttpLocalComputeProvider,
    against a live ComputeConnect, gets back the model llama.cpp is actually
    serving — real service, not a mock."""
    import sys
    from pathlib import Path

    src = Path("/home/mini/mcp-agentconnect/packages/agentconnect-core/src")
    if not src.is_dir():
        pytest.skip("mcp-agentconnect checkout not available")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    local_compute = pytest.importorskip("agentconnect.core.local_compute")

    provider = local_compute.HttpLocalComputeProvider(real_stack, timeout=30.0)
    assert provider.health()["status"] == "ok"
    inventory = provider.inventory()
    assert "qwen3-30b-a3b" in {m.id for m in inventory}
    assert {m.id for m in provider.loaded()} == {m.id for m in inventory}
    estimate = provider.estimate(
        local_compute.LocalEstimateRequest(
            task_type="general",
            privacy_tier="local_only",
            required_capabilities=["generate"],
            context_tokens=64,
            max_output_tokens=32,
        )
    )
    assert estimate.eligible is True
    assert estimate.selected_model == "qwen3-30b-a3b"


def test_real_openai_layer_small(real_stack):
    resp = httpx.post(
        f"{real_stack}/v1/chat/completions",
        json={
            "model": "qwen3-30b-a3b",
            "messages": [
                {"role": "user", "content": "Reply with the single word OK."}
            ],
            "max_tokens": 40,
        },
        timeout=240,
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"].strip()


# --------------------------------------------------------------------------
# REAL heterogeneous placement across TWO real engines of different shape.
# Skips (never fakes) when the second engine on :8091 is not running.
# --------------------------------------------------------------------------

_two_engines = pytest.mark.skipif(
    not (_up(REAL_UPSTREAM) and _up(REAL_UPSTREAM_B)),
    reason=f"needs BOTH real engines ({REAL_UPSTREAM} 30B/16k and "
    f"{REAL_UPSTREAM_B} 4B/8k); start the second with scripts/second_engine.sh",
)


@pytest.fixture(scope="module")
def hetero_stack():
    """ComputeConnect fronting two REAL, materially-different local engines:
    the 30B MoE (higher quality, slower, 16k ctx) and the 4B dense (faster,
    lower quality, 8k ctx). Declared quality/tps reflect that reality."""
    config = AppConfig(
        providers=[
            ProviderSpec(
                id="local-30b",
                placement_class="local",
                engine=LlamaCppEngine(REAL_UPSTREAM, stream_timeout=300.0),
                capabilities=("completion", "chat", "generate", "code", "summarize"),
                max_concurrency=1,
                estimated_tokens_per_second=12.0,
                estimated_quality=0.9,
            ),
            ProviderSpec(
                id="local-4b",
                placement_class="local",
                engine=LlamaCppEngine(REAL_UPSTREAM_B, stream_timeout=300.0),
                capabilities=("completion", "chat", "generate"),
                max_concurrency=2,
                estimated_tokens_per_second=90.0,
                estimated_quality=0.55,
            ),
        ],
        snapshot_ttl=2.0,
    )
    handle = ServerHandle(create_app(config)).start()
    try:
        yield handle.base_url
    finally:
        handle.stop()


@_two_engines
def test_two_real_engines_both_visible(hetero_stack):
    ids = {m["id"] for m in httpx.get(f"{hetero_stack}/models", timeout=15).json()["models"]}
    assert {"qwen3-30b-a3b", "qwen3-4b"} <= ids


@_two_engines
def test_latency_preference_selects_the_fast_real_engine(hetero_stack):
    body = httpx.post(
        f"{hetero_stack}/route/estimate",
        json={
            "task_type": "general",
            "privacy_tier": "local_only",
            "required_capabilities": ["generate"],
            "context_tokens": 128,
            "max_output_tokens": 64,
            "latency_preference": "low_latency",
        },
        timeout=15,
    ).json()
    assert body["eligible"] is True
    assert body["selected_model"] == "qwen3-4b"  # the fast 4B node
    assert body["reason"]["provider_id"] == "local-4b"


@_two_engines
def test_quality_preference_selects_the_accurate_real_engine(hetero_stack):
    body = httpx.post(
        f"{hetero_stack}/route/estimate",
        json={
            "task_type": "general",
            "privacy_tier": "local_only",
            "required_capabilities": ["generate"],
            "context_tokens": 128,
            "max_output_tokens": 64,
            "quality_preference": "high",
        },
        timeout=15,
    ).json()
    assert body["eligible"] is True
    assert body["selected_model"] == "qwen3-30b-a3b"  # the accurate 30B node
    assert body["reason"]["provider_id"] == "local-30b"


@_two_engines
def test_large_context_only_fits_the_big_window_engine(hetero_stack):
    """A context beyond the 4B node's 8k window must place on the 30B/16k node
    even under a latency preference — real capacity/capability placement."""
    body = httpx.post(
        f"{hetero_stack}/route/estimate",
        json={
            "task_type": "general",
            "privacy_tier": "local_only",
            "required_capabilities": ["generate"],
            "context_tokens": 12000,  # > 8192, <= 16384
            "max_output_tokens": 512,
            "latency_preference": "low_latency",
        },
        timeout=15,
    ).json()
    assert body["eligible"] is True
    assert body["selected_model"] == "qwen3-30b-a3b"


@_two_engines
def test_real_generation_from_BOTH_engines(hetero_stack):
    """Real CPU inference from each of the two real engines, selected by
    pinning the model — proof that both actually run, not one plus a sim."""
    for model in ("qwen3-4b", "qwen3-30b-a3b"):
        resp = httpx.post(
            f"{hetero_stack}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with just: OK"}],
                "max_tokens": 24,
            },
            timeout=240,
        )
        assert resp.status_code == 200, model
        assert resp.json()["choices"][0]["message"]["content"].strip(), model

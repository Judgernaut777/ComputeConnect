"""Conformance of the six control-plane routes, over real HTTP."""

from __future__ import annotations

import httpx


def test_health_ok_with_provider_rollup(stack):
    resp = httpx.get(f"{stack.base_url}/health", timeout=10)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body["providers"]) == {"local-llamacpp", "sim-cloud"}
    assert body["providers"]["sim-cloud"]["placement_class"] == "cloud"


def test_models_aggregates_both_providers_in_wire_shape(stack):
    body = httpx.get(f"{stack.base_url}/models", timeout=10).json()
    ids = {m["id"] for m in body["models"]}
    assert ids == {"fake-llama-7b", "sim-cloud-large"}
    for model in body["models"]:
        # Exactly the fields HttpLocalComputeProvider._model parses.
        assert {"id", "runtime", "capabilities", "context_tokens", "loaded", "metadata"} <= set(
            model
        )
    local = next(m for m in body["models"] if m["id"] == "fake-llama-7b")
    assert local["context_tokens"] == 4096
    assert local["metadata"]["provider_id"] == "local-llamacpp"


def test_models_loaded_returns_resident_models(stack):
    body = httpx.get(f"{stack.base_url}/models/loaded", timeout=10).json()
    assert {m["id"] for m in body["models"]} == {"fake-llama-7b", "sim-cloud-large"}
    assert all(m["loaded"] for m in body["models"])


def test_route_estimate_local_happy_path(stack):
    resp = httpx.post(
        f"{stack.base_url}/route/estimate",
        json={
            "task_type": "general",
            "privacy_tier": "repo_sensitive",
            "required_capabilities": ["code"],
            "context_tokens": 512,
            "max_output_tokens": 256,
            "latency_preference": "normal",
            "quality_preference": "good_enough",
        },
        timeout=10,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible"] is True
    assert body["selected_model"] == "fake-llama-7b"
    assert body["runtime"] == "llama.cpp"
    assert body["loaded"] is True
    assert body["reason"]["placement_class"] == "local"


def test_route_estimate_is_side_effect_free(stack):
    before_chats = stack.upstream.chat_requests
    for _ in range(3):
        httpx.post(
            f"{stack.base_url}/route/estimate",
            json={"task_type": "general", "privacy_tier": "local_only"},
            timeout=10,
        )
    assert stack.upstream.chat_requests == before_chats  # no generation happened
    assert stack.sim.chat_requests == 0
    # No runs were created by estimation.
    assert stack.api.runs.active_count("local-llamacpp") == 0
    assert stack.api.runs.active_count("sim-cloud") == 0


def test_cancel_unknown_run_returns_404_not_crash(stack):
    resp = httpx.post(f"{stack.base_url}/runs/does-not-exist/cancel", timeout=10)
    assert resp.status_code == 404
    assert resp.json()["status"] == "not_found"


def test_run_metadata_after_generate(stack):
    resp = httpx.post(
        f"{stack.base_url}/generate",
        json={"prompt": "hi", "task_type": "general", "max_output_tokens": 4},
        timeout=30,
    )
    run_id = resp.headers["X-Run-Id"]
    body = resp.json()
    assert body["run_id"] == run_id
    meta = httpx.get(f"{stack.base_url}/runs/{run_id}", timeout=10).json()
    assert meta["state"] == "succeeded"
    assert meta["provider_id"] == "local-llamacpp"
    assert meta["surface"] == "generate"
    assert meta["metrics"]["chunks"] > 0


# --------------------------------------------------------------------------
# Header/body privacy precedence on the CONTROL PLANE (/generate,
# /route/estimate). These routes previously read ONLY the body privacy_tier
# and ignored the X-Privacy-Tier header, so a gateway that stamped a
# more-restrictive header while an agent hit /generate directly had that
# restriction silently dropped (body public ⇒ cloud permitted). They now use
# the same more-restrictive-wins precedence as the OpenAI layer: a header can
# NARROW but never WIDEN the body tier. `sim-cloud-large` is served only by the
# cloud provider, so its selection is an exact proxy for "cloud was permitted".


def test_generate_permissive_body_no_header_reaches_cloud(stack):
    """Baseline / today's behavior: absent header ⇒ body-only. A public body
    for a cloud-only model reaches the cloud engine."""
    resp = httpx.post(
        f"{stack.base_url}/generate",
        json={
            "prompt": "hi",
            "model": "sim-cloud-large",
            "privacy_tier": "public",
            "max_output_tokens": 4,
        },
        timeout=30,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"
    assert stack.sim.chat_requests == 1


def test_generate_restrictive_header_narrows_permissive_body_denies_cloud(stack):
    """The finding: a restrictive X-Privacy-Tier over a permissive body must
    deny cloud — the header narrows the body tier."""
    resp = httpx.post(
        f"{stack.base_url}/generate",
        json={
            "prompt": "hi",
            "model": "sim-cloud-large",
            "privacy_tier": "public",
            "max_output_tokens": 4,
        },
        headers={"X-Privacy-Tier": "local_only"},
        timeout=30,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "refused"
    assert stack.sim.chat_requests == 0  # cloud never touched


def test_generate_permissive_header_cannot_widen_restrictive_body(stack):
    """Symmetric guarantee: a permissive header over a restrictive body still
    denies cloud — a header can never WIDEN the body tier."""
    resp = httpx.post(
        f"{stack.base_url}/generate",
        json={
            "prompt": "hi",
            "model": "sim-cloud-large",
            "privacy_tier": "local_only",
            "max_output_tokens": 4,
        },
        headers={"X-Privacy-Tier": "public"},
        timeout=30,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "refused"
    assert stack.sim.chat_requests == 0


def test_route_estimate_permissive_body_no_header_admits_cloud(stack):
    """Absent header ⇒ body-only (today's behavior): a public body admits the
    cloud-only capability."""
    resp = httpx.post(
        f"{stack.base_url}/route/estimate",
        json={"privacy_tier": "public", "required_capabilities": ["cloud-batch"]},
        timeout=10,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible"] is True
    assert body["reason"]["placement_class"] == "cloud"


def test_route_estimate_restrictive_header_narrows_permissive_body(stack):
    """Restrictive header over permissive body ⇒ cloud denied on the estimate
    route too."""
    resp = httpx.post(
        f"{stack.base_url}/route/estimate",
        json={"privacy_tier": "public", "required_capabilities": ["cloud-batch"]},
        headers={"X-Privacy-Tier": "local_only"},
        timeout=10,
    )
    assert resp.status_code == 200
    assert resp.json()["eligible"] is False


def test_route_estimate_permissive_header_cannot_widen_restrictive_body(stack):
    """Permissive header cannot widen a restrictive body on the estimate route."""
    resp = httpx.post(
        f"{stack.base_url}/route/estimate",
        json={"privacy_tier": "local_only", "required_capabilities": ["cloud-batch"]},
        headers={"X-Privacy-Tier": "public"},
        timeout=10,
    )
    assert resp.status_code == 200
    assert resp.json()["eligible"] is False

"""Fault behavior: an outage is a refusal, never a crash (binding invariant 2)."""

from __future__ import annotations

import time

import httpx

from computeconnect.app import AppConfig, create_app
from computeconnect.engines import LlamaCppEngine
from computeconnect.providers import ProviderSpec

from conftest import ServerHandle


def test_service_level_stale_snapshot_refuses(upstream_server):
    """Deliverable 5, over real HTTP: with a long TTL (so the cache is not
    refreshed) and a short staleness ceiling, a placement made after the
    ceiling elapses is fail-closed — refused at the 'stale' stage rather than
    served from grossly-stale capacity."""
    _, upstream_handle = upstream_server
    cfg = AppConfig(
        providers=[
            ProviderSpec(
                id="local-llamacpp",
                placement_class="local",
                engine=LlamaCppEngine(upstream_handle.base_url),
                capabilities=("generate",),
            )
        ],
        snapshot_ttl=3600.0,  # effectively never auto-refresh during the test
        max_snapshot_age=0.5,  # but trust a snapshot for only 0.5s
    )
    handle = ServerHandle(create_app(cfg)).start()
    try:
        body = {"task_type": "general", "privacy_tier": "local_only"}
        fresh = httpx.post(
            f"{handle.base_url}/route/estimate", json=body, timeout=10
        ).json()
        assert fresh["eligible"] is True  # primed, snapshot is fresh
        time.sleep(0.7)  # exceed the 0.5s ceiling; TTL keeps the cache stale
        stale = httpx.post(
            f"{handle.base_url}/route/estimate", json=body, timeout=10
        ).json()
        assert stale["eligible"] is False
        assert any(r["stage"] == "stale" for r in stale["reason"]["rejected"])
        # And the service is still perfectly alive.
        assert httpx.get(f"{handle.base_url}/health", timeout=10).status_code == 200
    finally:
        handle.stop()


def test_upstream_down_health_is_degraded_not_error(stack_upstream_down):
    stack = stack_upstream_down
    resp = httpx.get(f"{stack.base_url}/health", timeout=15)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"  # sim cloud still up
    assert body["providers"]["local-llamacpp"]["healthy"] is False
    assert "unreachable" in body["providers"]["local-llamacpp"]["detail"]


def test_health_plain_text_200_body_is_treated_as_healthy(stack):
    """A proxy like llama-swap answers /health with a plain-text "OK" body,
    not JSON. resp.json() on that must not make ComputeConnect mark the
    provider unreachable/degraded — a 200 is a 200."""
    stack.upstream.health_plain_text = "OK"
    resp = httpx.get(f"{stack.base_url}/health", timeout=15)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["providers"]["local-llamacpp"]["healthy"] is True


def test_health_non_200_is_still_unhealthy(stack):
    """A non-200 /health must still count as unhealthy, plain-text body or
    not — only the *body-shape* tolerance changed, not the status check."""
    stack.upstream.health_status = 500
    resp = httpx.get(f"{stack.base_url}/health", timeout=15)
    assert resp.status_code == 200  # ComputeConnect's own /health always 200s
    body = resp.json()
    assert body["providers"]["local-llamacpp"]["healthy"] is False
    assert body["status"] == "degraded"  # sim-cloud still up


def test_upstream_down_models_omits_dead_provider(stack_upstream_down):
    body = httpx.get(f"{stack_upstream_down.base_url}/models", timeout=15).json()
    assert {m["id"] for m in body["models"]} == {"sim-cloud-large"}


def test_upstream_down_estimate_refuses_with_reason(stack_upstream_down):
    resp = httpx.post(
        f"{stack_upstream_down.base_url}/route/estimate",
        json={"task_type": "general", "privacy_tier": "local_only"},
        timeout=15,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible"] is False
    assert body["reason"]["code"] == "no_compliant_provider"
    stages = {r["stage"] for r in body["reason"]["rejected"]}
    assert stages == {"privacy", "health"}  # cloud filtered, local dead


def test_upstream_down_generate_is_refusal_not_500(stack_upstream_down):
    resp = httpx.post(
        f"{stack_upstream_down.base_url}/generate",
        json={"task_type": "general", "prompt": "hi"},
        timeout=15,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "refused"
    assert body["refusal"]["code"] == "no_compliant_provider"
    # Service is still alive.
    assert httpx.get(f"{stack_upstream_down.base_url}/health", timeout=15).status_code == 200


def test_all_providers_down_health_is_down(stack_upstream_down):
    stack = stack_upstream_down
    stack.sim.fail_health = True
    try:
        body = httpx.get(f"{stack.base_url}/health", timeout=15).json()
        assert body["status"] == "down"
        est = httpx.post(
            f"{stack.base_url}/route/estimate",
            json={"task_type": "general", "privacy_tier": "public"},
            timeout=15,
        ).json()
        assert est["eligible"] is False
    finally:
        stack.sim.fail_health = False


def test_openai_layer_upstream_down_is_clean_error(stack_upstream_down):
    resp = httpx.post(
        f"{stack_upstream_down.base_url}/v1/chat/completions",
        json={"model": "fake-llama-7b", "messages": [{"role": "user", "content": "x"}]},
        timeout=15,
    )
    assert resp.status_code == 404  # model not visible while its provider is dead
    assert "error" in resp.json()


def test_openai_known_but_unhealthy_model_is_503(stack, upstream_server):
    """A model seen in a provider's last healthy inventory answers 503, not
    404, while that provider is down — an OpenAI client can distinguish
    temporarily-down from never-existed."""
    _, upstream_handle = upstream_server
    # Learn the inventory while the upstream is healthy.
    body = httpx.get(f"{stack.base_url}/models", timeout=15).json()
    assert "fake-llama-7b" in {m["id"] for m in body["models"]}
    # Kill the upstream engine.
    upstream_handle.stop()
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        json={"model": "fake-llama-7b", "messages": [{"role": "user", "content": "x"}]},
        timeout=15,
    )
    assert resp.status_code == 503
    err = resp.json()["error"]
    assert err["code"] == "model_temporarily_unavailable"
    assert err["type"] == "service_unavailable"
    # A model that never existed anywhere is still a plain 404.
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        json={"model": "never-existed", "messages": [{"role": "user", "content": "x"}]},
        timeout=15,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


def test_openai_dead_cloud_model_not_leaked_under_restrictive_tier(stack):
    """The 503 distinction must not leak models the effective privacy tier
    forbids: a cloud-only model stays 404 under the default (most
    restrictive) tier even though the registry remembers it."""
    # Learn the inventory (sim-cloud healthy), then take the cloud down.
    httpx.get(f"{stack.base_url}/models", timeout=15)
    stack.sim.fail_health = True
    try:
        resp = httpx.post(
            f"{stack.base_url}/v1/chat/completions",
            json={"model": "sim-cloud-large", "messages": [{"role": "user", "content": "x"}]},
            timeout=15,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "model_not_found"
        # With an explicitly cloud-permitting tier, the same model is 503.
        resp = httpx.post(
            f"{stack.base_url}/v1/chat/completions",
            json={
                "model": "sim-cloud-large",
                "messages": [{"role": "user", "content": "x"}],
                "privacy_tier": "public",
            },
            timeout=15,
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "model_temporarily_unavailable"
    finally:
        stack.sim.fail_health = False


def test_openai_header_cannot_widen_more_restrictive_body(stack):
    """Deliverable 3, end to end: a permissive X-Privacy-Tier header must NOT
    override a more-restrictive body privacy_tier on a cloud model."""
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        headers={"X-Privacy-Tier": "public"},
        json={
            "model": "sim-cloud-large",
            "messages": [{"role": "user", "content": "x"}],
            "privacy_tier": "local_only",
        },
        timeout=15,
    )
    assert resp.status_code == 403  # body wins: cloud forbidden
    assert resp.json()["error"]["type"] == "privacy_refusal"
    assert stack.sim.chat_requests == 0  # never reached the cloud engine


def test_openai_body_cannot_widen_more_restrictive_header(stack):
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        headers={"X-Privacy-Tier": "secret_sensitive"},
        json={
            "model": "sim-cloud-large",
            "messages": [{"role": "user", "content": "x"}],
            "privacy_tier": "public",
        },
        timeout=15,
    )
    assert resp.status_code == 403
    assert stack.sim.chat_requests == 0


def test_openai_both_permissive_reaches_cloud(stack):
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        headers={"X-Privacy-Tier": "public"},
        json={
            "model": "sim-cloud-large",
            "messages": [{"role": "user", "content": "x"}],
            "privacy_tier": "public_redacted",
            "max_tokens": 4,
        },
        timeout=15,
    )
    assert resp.status_code == 200
    assert stack.sim.chat_requests == 1


def test_openai_empty_header_does_not_clobber_permissive_body(stack):
    """An empty X-Privacy-Tier header must not fail-close a valid body tier."""
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        headers={"X-Privacy-Tier": ""},
        json={
            "model": "sim-cloud-large",
            "messages": [{"role": "user", "content": "x"}],
            "privacy_tier": "public",
            "max_tokens": 4,
        },
        timeout=15,
    )
    assert resp.status_code == 200
    assert stack.sim.chat_requests == 1


def test_failover_to_surviving_provider_when_one_dies(stack, upstream_server):
    """Deliverable 4 (failover): kill provider A (local), and a placement moves
    to the surviving provider B when the tier permits it."""
    _, upstream_handle = upstream_server
    httpx.get(f"{stack.base_url}/models", timeout=10)  # prime both healthy
    upstream_handle.stop()  # provider A goes down
    resp = httpx.post(
        f"{stack.base_url}/generate",
        json={
            "prompt": "hi",
            "task_type": "general",
            "privacy_tier": "public",
            "max_output_tokens": 4,
        },
        timeout=30,
    )
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["runtime"] == "simulated-cloud"  # failed over from local to B
    assert stack.sim.chat_requests == 1


def test_failover_refuses_when_survivor_is_privacy_forbidden(stack, upstream_server):
    """The other half of the deliverable: when the only survivor is a cloud
    provider the tier forbids, placement REFUSES — never a silent downgrade."""
    _, upstream_handle = upstream_server
    httpx.get(f"{stack.base_url}/models", timeout=10)
    upstream_handle.stop()  # only the cloud provider remains
    resp = httpx.post(
        f"{stack.base_url}/generate",
        json={"prompt": "hi", "task_type": "general", "privacy_tier": "local_only"},
        timeout=30,
    )
    body = resp.json()
    assert body["status"] == "refused"
    assert body["refusal"]["code"] == "no_compliant_provider"
    assert stack.sim.chat_requests == 0  # cloud never silently used


def test_midflight_provider_failure_then_failover_on_retry(stack):
    """Provider B (cloud) fails mid-generation → reported 'failed', not a crash;
    a retry places on the surviving provider A (local)."""
    stack.sim.fail_after_tokens = 2
    r1 = httpx.post(
        f"{stack.base_url}/generate",
        json={
            "model": "sim-cloud-large",
            "task_type": "general",
            "prompt": "hi",
            "privacy_tier": "public",
        },
        timeout=30,
    )
    assert r1.json()["status"] == "failed"
    # Retry unpinned under the default tier: the local engine serves it.
    r2 = httpx.post(
        f"{stack.base_url}/generate",
        json={"prompt": "hi", "task_type": "general", "max_output_tokens": 4},
        timeout=30,
    )
    b2 = r2.json()
    assert b2["status"] == "succeeded"
    assert b2["runtime"] == "llama.cpp"
    # Service is fully healthy throughout.
    assert httpx.get(f"{stack.base_url}/health", timeout=10).status_code == 200


def test_estimate_never_500s_on_malformed_body(stack):
    resp = httpx.post(
        f"{stack.base_url}/route/estimate",
        content=b"this is not json",
        headers={"content-type": "application/json"},
        timeout=10,
    )
    assert resp.status_code == 400


def test_generate_requires_prompt(stack):
    resp = httpx.post(f"{stack.base_url}/generate", json={"task_type": "general"}, timeout=10)
    assert resp.status_code == 400

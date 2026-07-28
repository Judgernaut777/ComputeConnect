"""The shared ecosystem event-bus publisher (buspublish.py).

Proves, over real HTTP against real uvicorn servers (this suite's house
style — see conftest.py):

* configured: the right wire ``type``/``source_product``/payload lands on
  the bus for each emit point (provider health edge transitions, a placed
  generation, a refused generation);
* unconfigured or dead: ComputeConnect's own behavior (status code, body,
  latency) is unaffected and nothing raises;
* never: a prompt, a chat message, or generated output text reaches the bus
  — checked at the raw-bytes level, not just the parsed JSON shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest

from computeconnect.app import AppConfig, create_app
from computeconnect.engines import LlamaCppEngine, SimulatedCloudEngine
from computeconnect.providers import ProviderSpec

from conftest import ServerHandle, free_port


class FakeBusUpstream:
    """Minimal stand-in for AgentConnect's ``POST /events`` ingress.

    Records every request it receives (parsed JSON, the raw body bytes, and
    the presented Authorization header) so tests can assert on the wire
    shape and scan for leaked content at the byte level.
    """

    def __init__(self, *, token: str = "cc-bus-test-token", status: int = 201) -> None:
        self.token = token
        self.status = status
        self.requests: list[dict] = []
        self.raw_bodies: list[bytes] = []
        self.auth_headers: list[str] = []

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope["method"] == "POST" and scope["path"] == "/events":
            raw = b""
            while True:
                msg = await receive()
                raw += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            self.raw_bodies.append(raw)
            self.auth_headers.append(headers.get("authorization", ""))
            try:
                self.requests.append(json.loads(raw or b"{}"))
            except ValueError:
                self.requests.append({})
            body = json.dumps({"seq": len(self.requests), "event_id": "evt"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
        else:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within the timeout"


@pytest.fixture()
def bus_server():
    bus = FakeBusUpstream()
    handle = ServerHandle(bus).start()
    try:
        yield bus, handle
    finally:
        handle.stop()


def _cfg(
    upstream_url: str,
    *,
    bus_url: str | None,
    bus_token: str | None,
    snapshot_ttl: float = 0.0,
) -> AppConfig:
    return AppConfig(
        providers=[
            ProviderSpec(
                id="local-llamacpp",
                placement_class="local",
                engine=LlamaCppEngine(upstream_url),
                capabilities=("completion", "chat", "generate", "code", "summarize"),
                max_concurrency=2,
                estimated_quality=0.6,
                estimated_tokens_per_second=12.0,
            ),
            ProviderSpec(
                id="sim-cloud",
                placement_class="cloud",
                engine=SimulatedCloudEngine(token_delay=0.005, response_tokens=4),
                capabilities=("completion", "chat", "generate", "cloud-batch"),
                max_concurrency=8,
                estimated_quality=0.9,
                estimated_tokens_per_second=80.0,
            ),
        ],
        snapshot_ttl=snapshot_ttl,
        bus_url=bus_url,
        bus_token=bus_token,
    )


# --------------------------------------------------------------- configured


def test_provider_health_edge_transitions_publish(upstream_server, bus_server):
    """Edge-triggered per-provider health: a silent baseline while healthy,
    an immediate provider.offline on the transition down, a
    provider.recovered on the transition back — and no repeat emission for
    an unchanged classification."""
    upstream, upstream_handle = upstream_server
    bus, bus_handle = bus_server
    cfg = _cfg(upstream_handle.base_url, bus_url=bus_handle.base_url, bus_token=bus.token)
    handle = ServerHandle(create_app(cfg)).start()
    try:
        # First-ever probe, already healthy: silent baseline.
        httpx.get(f"{handle.base_url}/health", timeout=10)
        time.sleep(0.2)
        assert bus.requests == []

        # Still healthy: still nothing.
        httpx.get(f"{handle.base_url}/health", timeout=10)
        time.sleep(0.2)
        assert bus.requests == []

        # Transition to offline (upstream unreachable at the network level).
        upstream.health_status = 500
        httpx.get(f"{handle.base_url}/health", timeout=10)
        wait_until(lambda: any(r.get("type") == "provider.offline" for r in bus.requests))
        offline_events = [r for r in bus.requests if r.get("type") == "provider.offline"]
        assert len(offline_events) == 1
        ev = offline_events[0]
        assert ev["source_product"] == "computeconnect"
        assert ev["payload"]["provider_id"] == "local-llamacpp"
        assert ev["payload"]["placement_class"] == "local"

        # Repeated offline probe: no second emission.
        httpx.get(f"{handle.base_url}/health", timeout=10)
        time.sleep(0.2)
        assert len([r for r in bus.requests if r.get("type") == "provider.offline"]) == 1

        # Transition back to healthy.
        upstream.health_status = 200
        httpx.get(f"{handle.base_url}/health", timeout=10)
        wait_until(lambda: any(r.get("type") == "provider.recovered" for r in bus.requests))
        recovered = [r for r in bus.requests if r.get("type") == "provider.recovered"]
        assert len(recovered) == 1
        assert recovered[0]["payload"]["provider_id"] == "local-llamacpp"
    finally:
        handle.stop()


def test_provider_health_degraded_vs_offline(upstream_server, bus_server):
    """A 200 response whose body reports an unhealthy status (we reached the
    engine, it says it's down) is classified 'degraded', distinct from a
    connection-level failure ('offline')."""
    upstream, upstream_handle = upstream_server
    bus, bus_handle = bus_server
    cfg = _cfg(upstream_handle.base_url, bus_url=bus_handle.base_url, bus_token=bus.token)
    handle = ServerHandle(create_app(cfg)).start()
    try:
        httpx.get(f"{handle.base_url}/health", timeout=10)  # silent healthy baseline
        time.sleep(0.2)

        upstream.health_body_status = "down"  # HTTP 200, but body says down
        httpx.get(f"{handle.base_url}/health", timeout=10)
        wait_until(lambda: any(r.get("type") == "provider.degraded" for r in bus.requests))
        assert not any(r.get("type") == "provider.offline" for r in bus.requests)
    finally:
        handle.stop()


def test_generate_placed_emits_bus_event(upstream_server, bus_server):
    upstream, upstream_handle = upstream_server
    bus, bus_handle = bus_server
    cfg = _cfg(upstream_handle.base_url, bus_url=bus_handle.base_url, bus_token=bus.token)
    handle = ServerHandle(create_app(cfg)).start()
    try:
        resp = httpx.post(
            f"{handle.base_url}/generate",
            json={
                "prompt": "correct horse battery staple",
                "privacy_tier": "local_only",
                "max_output_tokens": 4,
            },
            timeout=30,
        )
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        wait_until(lambda: any(r.get("type") == "compute.generation.placed" for r in bus.requests))
        placed = [r for r in bus.requests if r.get("type") == "compute.generation.placed"]
        assert len(placed) == 1
        ev = placed[0]
        assert ev["source_product"] == "computeconnect"
        assert ev["outcome"] == "succeeded"
        assert ev["entity_id"] == run_id
        assert ev["payload"]["provider_id"] == "local-llamacpp"
        assert ev["payload"]["placement_class"] == "local"
        assert ev["payload"]["selected_model"] == "fake-llama-7b"
        assert ev["payload"]["privacy_tier"] == "local_only"
        assert "elapsed_seconds" in ev["payload"]
    finally:
        handle.stop()


def test_generate_refused_emits_bus_event(upstream_server, bus_server):
    upstream, upstream_handle = upstream_server
    bus, bus_handle = bus_server
    cfg = _cfg(upstream_handle.base_url, bus_url=bus_handle.base_url, bus_token=bus.token)
    handle = ServerHandle(create_app(cfg)).start()
    try:
        resp = httpx.post(
            f"{handle.base_url}/generate",
            json={
                "prompt": "top secret plan",
                "privacy_tier": "local_only",
                "model": "no-such-model-anywhere",
            },
            timeout=30,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "refused"

        wait_until(lambda: any(r.get("type") == "compute.generation.refused" for r in bus.requests))
        refused = [r for r in bus.requests if r.get("type") == "compute.generation.refused"]
        assert len(refused) == 1
        ev = refused[0]
        assert ev["source_product"] == "computeconnect"
        assert ev["outcome"] == "denied"
        assert ev["privacy_tier"] == "local_only"
        assert ev["payload"]["reason"]  # the refusal code, non-empty
    finally:
        handle.stop()


def test_openai_chat_completions_placed_and_refused(upstream_server, bus_server):
    upstream, upstream_handle = upstream_server
    bus, bus_handle = bus_server
    cfg = _cfg(upstream_handle.base_url, bus_url=bus_handle.base_url, bus_token=bus.token)
    handle = ServerHandle(create_app(cfg)).start()
    try:
        # Placed (non-streaming).
        resp = httpx.post(
            f"{handle.base_url}/v1/chat/completions",
            json={
                "model": "fake-llama-7b",
                "messages": [{"role": "user", "content": "hello there"}],
                "privacy_tier": "local_only",
            },
            timeout=30,
        )
        assert resp.status_code == 200
        wait_until(lambda: any(r.get("type") == "compute.generation.placed" for r in bus.requests))
        placed = [r for r in bus.requests if r.get("type") == "compute.generation.placed"]
        assert len(placed) == 1
        assert placed[0]["outcome"] == "succeeded"
        assert "prompt_tokens" in placed[0]["payload"]

        # Placed (streaming/SSE).
        with httpx.stream(
            "POST",
            f"{handle.base_url}/v1/chat/completions",
            json={
                "model": "fake-llama-7b",
                "messages": [{"role": "user", "content": "hello again"}],
                "privacy_tier": "local_only",
                "stream": True,
            },
            timeout=30,
        ) as stream_resp:
            for _ in stream_resp.iter_lines():
                pass
        assert stream_resp.status_code == 200
        wait_until(
            lambda: len([r for r in bus.requests if r.get("type") == "compute.generation.placed"])
            == 2
        )

        # Refused: privacy tier forbids every candidate provider.
        resp = httpx.post(
            f"{handle.base_url}/v1/chat/completions",
            json={
                "model": "sim-cloud-large",
                "messages": [{"role": "user", "content": "classified content"}],
                "privacy_tier": "local_only",
            },
            timeout=30,
        )
        assert resp.status_code in (403, 404, 503)
        wait_until(lambda: any(r.get("type") == "compute.generation.refused" for r in bus.requests))
    finally:
        handle.stop()


def test_no_prompt_or_output_text_ever_reaches_the_bus(upstream_server, bus_server):
    """Bytes-level check: neither the request prompt nor the model's
    generated tokens appear anywhere in what actually went out over HTTP."""
    upstream, upstream_handle = upstream_server
    bus, bus_handle = bus_server
    cfg = _cfg(upstream_handle.base_url, bus_url=bus_handle.base_url, bus_token=bus.token)
    handle = ServerHandle(create_app(cfg)).start()
    secret_prompt = "MARKER_9f1c2e_do_not_leak_this_prompt_text"
    try:
        resp = httpx.post(
            f"{handle.base_url}/generate",
            json={
                "prompt": secret_prompt,
                "privacy_tier": "local_only",
                "max_output_tokens": 4,
            },
            timeout=30,
        )
        assert resp.status_code == 200
        body = resp.json()
        generated_output = body["output"]
        assert generated_output  # the fake upstream really did produce tokens

        wait_until(lambda: any(r.get("type") == "compute.generation.placed" for r in bus.requests))
        time.sleep(0.1)  # let any further in-flight sends land too

        for raw in bus.raw_bodies:
            assert secret_prompt.encode() not in raw
            assert generated_output.encode() not in raw
            assert b"messages" not in raw
            assert b"reasoning_content" not in raw
    finally:
        handle.stop()


def test_auth_header_carries_the_scoped_token(upstream_server, bus_server):
    upstream, upstream_handle = upstream_server
    bus, bus_handle = bus_server
    cfg = _cfg(upstream_handle.base_url, bus_url=bus_handle.base_url, bus_token=bus.token)
    handle = ServerHandle(create_app(cfg)).start()
    try:
        httpx.post(
            f"{handle.base_url}/generate",
            json={"prompt": "hi", "privacy_tier": "local_only", "max_output_tokens": 4},
            timeout=30,
        )
        wait_until(lambda: len(bus.auth_headers) >= 1)
        assert bus.auth_headers[0] == f"Bearer {bus.token}"
    finally:
        handle.stop()


# ----------------------------------------------------- unconfigured / dead


def test_unconfigured_bus_is_a_transparent_noop(upstream_server):
    """No bus_url/bus_token at all: every route behaves exactly as it does
    without this module in the picture -- no attempted network call, no
    delay, no error."""
    upstream, upstream_handle = upstream_server
    cfg = _cfg(upstream_handle.base_url, bus_url=None, bus_token=None)
    handle = ServerHandle(create_app(cfg)).start()
    try:
        resp = httpx.post(
            f"{handle.base_url}/generate",
            json={"prompt": "hi", "privacy_tier": "local_only", "max_output_tokens": 4},
            timeout=30,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "succeeded"
    finally:
        handle.stop()


def test_dead_bus_leaves_generation_behavior_unaffected(upstream_server, caplog):
    """bus_url configured but nothing is listening there: /generate still
    succeeds, with the same shape and without added latency, and the
    failure is swallowed and logged rather than raised anywhere the caller
    could observe."""
    upstream, upstream_handle = upstream_server
    dead_bus_url = f"http://127.0.0.1:{free_port()}"
    cfg = _cfg(upstream_handle.base_url, bus_url=dead_bus_url, bus_token="whatever-token")
    handle = ServerHandle(create_app(cfg)).start()
    try:
        with caplog.at_level(logging.WARNING, logger="computeconnect.buspublish"):
            started = time.time()
            resp = httpx.post(
                f"{handle.base_url}/generate",
                json={"prompt": "hi", "privacy_tier": "local_only", "max_output_tokens": 4},
                timeout=30,
            )
            elapsed = time.time() - started
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "succeeded"
        assert body["output"].startswith("tok0")
        # Nowhere near the background publisher's own 1s connect / 2s read
        # timeout budget -- the request path never waited on it.
        assert elapsed < 3.0

        # Also unaffected on the OpenAI layer.
        resp2 = httpx.post(
            f"{handle.base_url}/v1/chat/completions",
            json={
                "model": "fake-llama-7b",
                "messages": [{"role": "user", "content": "hello"}],
                "privacy_tier": "local_only",
            },
            timeout=30,
        )
        assert resp2.status_code == 200

        wait_until(
            lambda: any(
                "computeconnect bus publish failed" in r.message for r in caplog.records
            ),
            timeout=5.0,
        )
    finally:
        handle.stop()


def test_publisher_disabled_when_only_one_of_url_or_token_is_set(upstream_server):
    """Both bus_url and bus_token are required; either alone leaves the
    publisher disabled -- a half-configured deployment fails safe to
    'no publishing', not to a broken/partial publish attempt."""
    from computeconnect.buspublish import BusPublisher

    assert BusPublisher(bus_url="http://127.0.0.1:1", token=None).enabled is False
    assert BusPublisher(bus_url=None, token="tkn").enabled is False
    assert BusPublisher(bus_url=None, token=None).enabled is False
    assert BusPublisher(bus_url="http://127.0.0.1:1", token="tkn").enabled is True


def test_publish_with_no_running_loop_is_a_noop() -> None:
    """publish() called from a synchronous context (no running event loop)
    never raises -- it degrades to a no-op, the same as an unreachable bus."""
    from computeconnect.buspublish import BusPublisher

    publisher = BusPublisher(bus_url="http://127.0.0.1:1", token="tkn")
    publisher.publish("compute.generation.placed", payload={"provider_id": "x"})  # no raise


def test_truncate_reason_bounds_and_never_raises() -> None:
    from computeconnect.buspublish import truncate_reason

    assert truncate_reason(None) == ""
    assert truncate_reason(12345) == "12345"
    long = "x" * 500
    out = truncate_reason(long, limit=50)
    assert len(out) == 50
    assert out.endswith("…")

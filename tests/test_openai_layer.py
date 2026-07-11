"""Layer 2: the OpenAI-compatible inference API, and the one-backend invariant."""

from __future__ import annotations

import json

import httpx


def test_v1_models_lists_the_same_inventory_as_the_control_plane(stack):
    v1 = httpx.get(f"{stack.base_url}/v1/models", timeout=10).json()
    control = httpx.get(f"{stack.base_url}/models", timeout=10).json()
    assert v1["object"] == "list"
    assert {m["id"] for m in v1["data"]} == {m["id"] for m in control["models"]}


def test_chat_completion_non_streaming(stack):
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        json={
            "model": "fake-llama-7b",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 4,
        },
        timeout=30,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "fake-llama-7b"
    assert body["choices"][0]["message"]["content"].startswith("tok0")
    assert body["choices"][0]["finish_reason"] == "stop"
    assert resp.headers["X-Run-Id"]


def test_chat_completion_streaming_sse(stack):
    deltas = []
    finish = None
    with httpx.Client(timeout=30) as client:
        with client.stream(
            "POST",
            f"{stack.base_url}/v1/chat/completions",
            json={
                "model": "fake-llama-7b",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 5,
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            saw_done = False
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    saw_done = True
                    continue
                chunk = json.loads(data)
                assert chunk["object"] == "chat.completion.chunk"
                choice = chunk["choices"][0]
                if choice["delta"].get("content"):
                    deltas.append(choice["delta"]["content"])
                if choice["finish_reason"]:
                    finish = choice["finish_reason"]
    assert saw_done
    assert finish == "stop"
    assert "".join(deltas) == "tok0 tok1 tok2 tok3 tok4 "


def test_openai_layer_applies_the_same_default_deny(stack):
    """No tier ⇒ most restrictive ⇒ the cloud-only model is refused with a
    structured 403, not silently served or downgraded."""
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        json={
            "model": "sim-cloud-large",
            "messages": [{"role": "user", "content": "hi"}],
        },
        timeout=30,
    )
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["type"] == "privacy_refusal"
    assert err["code"] == "no_compliant_provider"
    assert stack.sim.chat_requests == 0


def test_openai_layer_explicit_tier_header_permits_cloud(stack):
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        json={
            "model": "sim-cloud-large",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4,
        },
        headers={"X-Privacy-Tier": "public"},
        timeout=30,
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"].startswith("sim-token-0")
    assert stack.sim.chat_requests == 1


def test_openai_unknown_model_is_404(stack):
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        json={"model": "no-such", "messages": [{"role": "user", "content": "hi"}]},
        timeout=30,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


def test_openai_run_visible_to_control_plane_cancel_surface(stack):
    """One backend: an OpenAI-layer run is a control-plane run — same registry,
    same metadata, same cancellation surface."""
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions",
        json={
            "model": "fake-llama-7b",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4,
        },
        timeout=30,
    )
    run_id = resp.headers["X-Run-Id"]
    meta = httpx.get(f"{stack.base_url}/runs/{run_id}", timeout=10).json()
    assert meta["surface"] == "openai"
    assert meta["state"] == "succeeded"
    # Cancelling a finished run is acknowledged, not an error.
    cancel = httpx.post(f"{stack.base_url}/runs/{run_id}/cancel", timeout=10)
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "already_finished"


def test_openai_invalid_body_is_400(stack):
    resp = httpx.post(
        f"{stack.base_url}/v1/chat/completions", json={"model": "x"}, timeout=10
    )
    assert resp.status_code == 400

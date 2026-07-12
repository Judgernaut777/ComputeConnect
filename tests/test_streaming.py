"""/generate streaming behavior: incremental delivery, cancellation, disconnect,
and the CA-1 privacy re-verification at execution time."""

from __future__ import annotations

import json
import time

import httpx


def _read_stream(client_stream):
    """Collect (arrival_time, chunk) pairs from a streaming response."""
    arrivals = []
    for chunk in client_stream.iter_text():
        arrivals.append((time.time(), chunk))
    return arrivals


def test_generate_streams_incrementally_not_buffered(stack):
    """Tokens must arrive while the upstream is still generating.

    Upstream: 10 tokens x 80ms = ~0.8s total. If /generate buffered the whole
    response, the first byte would arrive at ~0.8s; streaming delivers it
    within the first token or two.
    """
    stack.upstream.response_tokens = 10
    stack.upstream.token_delay = 0.08
    started = time.time()
    with httpx.Client(timeout=30) as client:
        with client.stream(
            "POST",
            f"{stack.base_url}/generate",
            json={"prompt": "stream please", "task_type": "general"},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["X-Run-Id"]
            arrivals = _read_stream(resp)
    first_body_byte_at = arrivals[0][0] - started
    last_byte_at = arrivals[-1][0] - started
    assert len(arrivals) > 3, "expected multiple chunks, got a single buffered body"
    assert first_body_byte_at < last_byte_at - 0.4, (
        f"first chunk at {first_body_byte_at:.2f}s vs last at {last_byte_at:.2f}s: "
        "response was not incremental"
    )
    document = json.loads("".join(text for _, text in arrivals))
    assert document["status"] == "succeeded"
    assert document["output"] == "".join(f"tok{i} " for i in range(10))
    assert document["model"] == "fake-llama-7b"
    assert document["runtime"] == "llama.cpp"
    assert document["metrics"]["chunks"] == 10
    assert document["warnings"] == []


def test_generate_backpressure_paces_upstream_to_a_slow_consumer(stack):
    """Deliverable 4: a slow /generate consumer must pace the upstream engine.

    The upstream is asked for 400 tokens of 128 KB each (~51 MB) with no delay,
    far more than the pipeline's in-flight buffers hold (~18 MB, measured). We
    read the first couple of tokens, then stop reading for a second. If
    ComputeConnect propagated backpressure (one-delta pull + TCP), the upstream
    cannot have sent all 400 tokens while we were not reading — it blocks
    partway. Then we drain the rest and the document still completes cleanly.
    """
    stack.upstream.response_tokens = 400
    stack.upstream.token_bytes = 128 * 1024
    stack.upstream.token_delay = 0.0
    with httpx.Client(timeout=60) as client:
        with client.stream(
            "POST",
            f"{stack.base_url}/generate",
            json={"prompt": "backpressure", "task_type": "general"},
        ) as resp:
            assert resp.status_code == 200
            it = resp.iter_raw(chunk_size=16 * 1024)
            # Read a little — enough to get past the JSON prefix and a token.
            for _ in range(3):
                next(it)
            # Now stop reading and let the pipeline fill and block.
            time.sleep(1.0)
            paced = stack.upstream.sent_tokens
            assert 1 <= paced < stack.upstream.response_tokens, (
                f"upstream sent {paced}/{stack.upstream.response_tokens} tokens "
                "with a stalled consumer: backpressure was NOT propagated"
            )
            # Re-check after another idle beat: it must stay blocked, not creep on.
            time.sleep(0.5)
            assert stack.upstream.sent_tokens == paced, (
                "upstream kept producing while the consumer was idle: "
                "backpressure did not hold"
            )
            # Drain the rest; the response still completes.
            for _ in it:
                pass
    # Upstream eventually delivered everything once we resumed reading.
    assert stack.upstream.sent_tokens == stack.upstream.response_tokens
    assert stack.upstream.completed == 1


def test_generate_response_parses_as_local_run_result(stack):
    """A non-streaming reader (AgentConnect's shipped client) sees valid JSON
    with the LocalRunResult fields plus the run-id amendment."""
    resp = httpx.post(
        f"{stack.base_url}/generate",
        json={
            "model": "fake-llama-7b",
            "task_type": "general",
            "prompt": "hello",
            "context": "you are a test",
            "max_output_tokens": 4,
            "temperature": 0.0,
        },
        timeout=30,
    )
    body = resp.json()
    assert {"status", "output", "model", "runtime", "metrics", "warnings", "run_id"} <= set(body)
    assert body["status"] == "succeeded"
    assert body["run_id"] == resp.headers["X-Run-Id"]
    assert body["output"].startswith("tok0")


def test_cancellation_mid_stream_propagates_to_upstream(stack):
    """Cancel a long generation partway; the run ends 'cancelled' promptly and
    the upstream engine sees its client disconnect."""
    stack.upstream.response_tokens = 200
    stack.upstream.token_delay = 0.05  # ~10s if left alone
    started = time.time()
    with httpx.Client(timeout=30) as client:
        with client.stream(
            "POST",
            f"{stack.base_url}/generate",
            json={"prompt": "long one", "task_type": "general"},
        ) as resp:
            run_id = resp.headers["X-Run-Id"]
            collected = []
            cancelled = False
            for chunk in resp.iter_text():
                collected.append(chunk)
                # Wait for real generated tokens (not just the JSON prefix)
                # so the upstream generation is demonstrably in flight.
                if not cancelled and "tok3 " in "".join(collected):
                    cancel = client.post(f"{stack.base_url}/runs/{run_id}/cancel")
                    assert cancel.status_code == 200
                    assert cancel.json()["status"] == "cancelling"
                    cancelled = True
            assert cancelled, "never saw mid-stream tokens to cancel after"
    elapsed = time.time() - started
    assert elapsed < 5, f"cancellation did not interrupt the stream (took {elapsed:.1f}s)"
    document = json.loads("".join(collected))
    assert document["status"] == "cancelled"
    assert document["run_id"] == run_id
    assert any("cancelled" in w for w in document["warnings"])
    # Upstream saw the disconnect (poll: it notices on its next token tick).
    deadline = time.time() + 3
    while stack.upstream.disconnects == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert stack.upstream.disconnects == 1
    assert stack.upstream.completed == 0
    meta = httpx.get(f"{stack.base_url}/runs/{run_id}", timeout=10).json()
    assert meta["state"] == "cancelled"


def test_client_disconnect_propagates_to_upstream(stack):
    """Dropping the /generate connection mid-stream cancels the upstream run."""
    stack.upstream.response_tokens = 200
    stack.upstream.token_delay = 0.05
    with httpx.Client(timeout=30) as client:
        with client.stream(
            "POST",
            f"{stack.base_url}/generate",
            json={"prompt": "abandon me", "task_type": "general"},
        ) as resp:
            run_id = resp.headers["X-Run-Id"]
            seen = ""
            for chunk in resp.iter_text():
                seen += chunk
                if "tok1 " in seen:  # generation is demonstrably in flight
                    break  # abandon the response mid-generation
    deadline = time.time() + 5
    while stack.upstream.disconnects == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert stack.upstream.disconnects == 1
    assert stack.upstream.completed == 0
    deadline = time.time() + 5
    while time.time() < deadline:
        meta = httpx.get(f"{stack.base_url}/runs/{run_id}", timeout=10).json()
        if meta["state"] != "running":
            break
        time.sleep(0.05)
    assert meta["state"] == "disconnected"


class TestCA1PrivacyReverification:
    """CA-1 (implemented amendment): /generate accepts an optional privacy_tier
    and positively re-verifies the provider; absent means most restrictive."""

    def test_missing_tier_refuses_cloud_model_structurally(self, stack):
        resp = httpx.post(
            f"{stack.base_url}/generate",
            json={"model": "sim-cloud-large", "task_type": "general", "prompt": "hi"},
            timeout=30,
        )
        assert resp.status_code == 200  # a refusal, not a transport error
        body = resp.json()
        assert body["status"] == "refused"
        assert body["output"] == ""
        assert body["refusal"]["code"] == "no_compliant_provider"
        assert body["refusal"]["privacy"]["assumed"] is True
        assert body["refusal"]["privacy"]["cloud_permitted"] is False
        stages = {r["stage"] for r in body["refusal"]["rejected"]}
        assert "privacy" in stages
        assert stack.sim.chat_requests == 0  # never reached the cloud engine

    def test_local_only_tier_refuses_cloud_model(self, stack):
        resp = httpx.post(
            f"{stack.base_url}/generate",
            json={
                "model": "sim-cloud-large",
                "task_type": "general",
                "prompt": "hi",
                "privacy_tier": "local_only",
            },
            timeout=30,
        )
        assert resp.json()["status"] == "refused"
        assert stack.sim.chat_requests == 0

    def test_garbage_tier_treated_as_most_restrictive(self, stack):
        resp = httpx.post(
            f"{stack.base_url}/generate",
            json={
                "model": "sim-cloud-large",
                "task_type": "general",
                "prompt": "hi",
                "privacy_tier": "definitely-not-a-tier",
            },
            timeout=30,
        )
        body = resp.json()
        assert body["status"] == "refused"
        assert body["refusal"]["privacy"]["assumed"] is True

    def test_explicit_public_tier_reaches_the_cloud_provider(self, stack):
        resp = httpx.post(
            f"{stack.base_url}/generate",
            json={
                "model": "sim-cloud-large",
                "task_type": "general",
                "prompt": "hi",
                "privacy_tier": "public",
                "max_output_tokens": 4,
            },
            timeout=30,
        )
        body = resp.json()
        assert body["status"] == "succeeded"
        assert body["runtime"] == "simulated-cloud"
        assert stack.sim.chat_requests == 1

    def test_missing_tier_still_serves_local(self, stack):
        resp = httpx.post(
            f"{stack.base_url}/generate",
            json={"task_type": "general", "prompt": "hi", "max_output_tokens": 4},
            timeout=30,
        )
        body = resp.json()
        assert body["status"] == "succeeded"
        assert body["runtime"] == "llama.cpp"


def test_generate_unknown_model_is_structured_refusal(stack):
    resp = httpx.post(
        f"{stack.base_url}/generate",
        json={"model": "no-such-model", "task_type": "general", "prompt": "hi"},
        timeout=30,
    )
    body = resp.json()
    assert body["status"] == "refused"
    assert any(r["stage"] == "model" for r in body["refusal"]["rejected"])


def test_generate_mid_stream_engine_failure_is_failed_not_crash(stack):
    stack.sim.fail_after_tokens = 3
    resp = httpx.post(
        f"{stack.base_url}/generate",
        json={
            "model": "sim-cloud-large",
            "task_type": "general",
            "prompt": "hi",
            "privacy_tier": "public",
        },
        timeout=30,
    )
    body = resp.json()
    assert body["status"] == "failed"
    assert any("upstream engine error" in w for w in body["warnings"])
    assert body["output"].startswith("sim-token-0")  # partial output preserved
    # Service still alive afterwards.
    assert httpx.get(f"{stack.base_url}/health", timeout=10).status_code == 200

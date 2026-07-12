"""Test harness: a fake llama.cpp upstream and a live ComputeConnect server.

Server-level tests run against real uvicorn servers on ephemeral ports (real
HTTP, real streaming, real disconnects) — not an in-process ASGI shim. The
*upstream engine* in most tests is a fake llama.cpp lookalike defined here;
tests that talk to the real :8080 engine live in test_real_engine.py and skip
when it is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from dataclasses import dataclass

import pytest
import uvicorn

from computeconnect.app import AppConfig, create_app
from computeconnect.engines import LlamaCppEngine, SimulatedCloudEngine
from computeconnect.providers import ProviderSpec


class FakeLlamaUpstream:
    """Minimal llama.cpp-shaped ASGI app: /health, /v1/models, /v1/chat/completions.

    Streams SSE tokens with a configurable delay and records whether the
    client disconnected mid-generation — which is how we observe cancellation
    propagating through ComputeConnect to the engine.
    """

    def __init__(self) -> None:
        self.token_delay = 0.02
        self.response_tokens = 8
        #: Bytes of padding appended to each token — used to force real TCP
        #: backpressure (a slow consumer can only be paced if the in-flight
        #: bytes exceed the socket/uvicorn buffers).
        self.token_bytes = 0
        #: Count of token bodies whose ``send`` has RETURNED. If the downstream
        #: is paced, this cannot reach ``response_tokens`` while the client is
        #: not reading.
        self.sent_tokens = 0
        self.chat_requests = 0
        self.health_requests = 0
        self.models_requests = 0
        self.completed = 0
        self.disconnects = 0

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
        method, path = scope["method"], scope["path"]
        if method == "GET" and path == "/health":
            self.health_requests += 1
            await self._json(send, {"status": "ok"})
        elif method == "GET" and path == "/v1/models":
            self.models_requests += 1
            await self._json(
                send,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "fake-llama-7b",
                            "object": "model",
                            "owned_by": "fake-upstream",
                            "meta": {"n_ctx": 4096},
                        }
                    ],
                },
            )
        elif method == "POST" and path == "/v1/chat/completions":
            await self._chat(receive, send)
        else:
            await self._json(send, {"error": "not found"}, status=404)

    @staticmethod
    async def _json(send, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _chat(self, receive, send) -> None:
        raw = b""
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            raw += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        request = json.loads(raw or b"{}")
        self.chat_requests += 1
        limit = int(request.get("max_tokens") or 10**9)
        n = min(limit, self.response_tokens)

        disconnected = asyncio.Event()

        async def watch() -> None:
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    disconnected.set()
                    return

        watcher = asyncio.create_task(watch())
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            for i in range(n):
                await asyncio.sleep(self.token_delay)
                if disconnected.is_set():
                    self.disconnects += 1
                    return
                content = f"tok{i} " + ("x" * self.token_bytes)
                chunk = {"choices": [{"index": 0, "delta": {"content": content}}]}
                await send(
                    {
                        "type": "http.response.body",
                        "body": f"data: {json.dumps(chunk)}\n\n".encode(),
                        "more_body": True,
                    }
                )
                # This send has returned: the byte left our buffer only because
                # the downstream made room. Under backpressure it blocks here.
                self.sent_tokens += 1
            await send(
                {"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False}
            )
            self.completed += 1
        except Exception:
            self.disconnects += 1
        finally:
            watcher.cancel()


class ServerHandle:
    """A uvicorn server on an ephemeral port, run in a daemon thread."""

    def __init__(self, app) -> None:
        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
        )
        self.server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)
        self.base_url = ""

    def start(self) -> "ServerHandle":
        self._thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start within 15s")
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=10)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Stack:
    base_url: str
    upstream: FakeLlamaUpstream
    sim: SimulatedCloudEngine
    api: object  # ComputeConnectAPI


def build_stack_config(upstream_url: str, sim: SimulatedCloudEngine) -> AppConfig:
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
                engine=sim,
                capabilities=("completion", "chat", "generate", "cloud-batch"),
                max_concurrency=8,
                estimated_quality=0.9,
                estimated_tokens_per_second=80.0,
            ),
        ],
        snapshot_ttl=0.0,  # always re-probe: deterministic fault tests
    )


@pytest.fixture()
def upstream_server():
    upstream = FakeLlamaUpstream()
    handle = ServerHandle(upstream).start()
    try:
        yield upstream, handle
    finally:
        handle.stop()


@pytest.fixture()
def stack(upstream_server):
    upstream, upstream_handle = upstream_server
    sim = SimulatedCloudEngine(token_delay=0.01, response_tokens=8)
    app = create_app(build_stack_config(upstream_handle.base_url, sim))
    handle = ServerHandle(app).start()
    try:
        yield Stack(
            base_url=handle.base_url, upstream=upstream, sim=sim, api=app.state.api
        )
    finally:
        handle.stop()


@pytest.fixture()
def stack_upstream_down():
    """ComputeConnect whose local engine points at a port nobody listens on."""
    sim = SimulatedCloudEngine(token_delay=0.01, response_tokens=8)
    app = create_app(build_stack_config(f"http://127.0.0.1:{free_port()}", sim))
    handle = ServerHandle(app).start()
    try:
        yield Stack(base_url=handle.base_url, upstream=None, sim=sim, api=app.state.api)
    finally:
        handle.stop()

"""Engine clients — the things that actually do inference.

ComputeConnect performs no inference (charter). An engine is an upstream that
does. Two are shipped:

* :class:`LlamaCppEngine` — a read-only client of an existing, externally
  managed llama.cpp server (on this host: the ``wiki-llama`` systemd unit on
  :8080). ComputeConnect never starts, stops, or reconfigures it.
* :class:`SimulatedCloudEngine` — an in-process stand-in for a remote cloud
  provider. Its purpose is contract validation (ARCHITECTURE §9): a logically
  distinct second provider that exercises candidate filtering and the
  default-deny privacy path. It does not demonstrate product value.

Both speak the same minimal interface: ``health``, ``list_models``, and
``stream_chat`` (an async iterator of text deltas). Streaming is the only
generation verb — non-streaming callers are assembled from the stream, so
there is exactly one generation path per engine.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx


@dataclass(frozen=True)
class ModelInfo:
    id: str
    context_tokens: int
    capabilities: tuple[str, ...]
    loaded: bool
    metadata: dict = field(default_factory=dict)

    def wire(self) -> dict:
        """The /models entry shape AgentConnect's client parses."""
        return {
            "id": self.id,
            "runtime": self.metadata.get("runtime", "unknown"),
            "capabilities": list(self.capabilities),
            "context_tokens": self.context_tokens,
            "loaded": self.loaded,
            "metadata": dict(self.metadata),
        }


class EngineError(Exception):
    """An upstream engine failed or is unreachable."""


class LlamaCppEngine:
    """Read-only client of an OpenAI-compatible llama.cpp server."""

    name = "llama.cpp"

    def __init__(
        self,
        base_url: str,
        *,
        capability_tags: tuple[str, ...] = ("completion", "chat", "generate"),
        timeout: float = 10.0,
        stream_timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._capability_tags = capability_tags
        self._timeout = timeout
        self._stream_timeout = stream_timeout

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.base_url}/health")
            resp.raise_for_status()
            return resp.json()

    async def list_models(self) -> list[ModelInfo]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.base_url}/v1/models")
            resp.raise_for_status()
            body = resp.json()
        models: list[ModelInfo] = []
        for entry in body.get("data", []):
            meta = entry.get("meta") or {}
            models.append(
                ModelInfo(
                    id=str(entry.get("id", "")),
                    context_tokens=int(meta.get("n_ctx", 0) or 0),
                    capabilities=self._capability_tags,
                    # llama.cpp lists a model in /v1/models because it is
                    # serving it; treat listed as resident.
                    loaded=True,
                    metadata={"runtime": self.name, "owned_by": entry.get("owned_by", "")},
                )
            )
        return models

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """Proxy a streaming chat completion, yielding text deltas.

        The httpx stream context is closed when this generator is closed,
        which drops the upstream connection — that is how cancellation
        propagates to the engine.
        """
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        timeout = httpx.Timeout(self._timeout, read=self._stream_timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/v1/chat/completions", json=payload
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise EngineError(f"upstream returned {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    for choice in chunk.get("choices", []):
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield delta


class SimulatedCloudEngine:
    """An in-process simulated remote/cloud provider.

    Deterministic output, configurable latency, injectable failure — enough to
    prove candidate filtering, default-deny privacy, cancellation, and
    mid-stream fault handling without any network or GPU. It is *simulated*:
    nothing about it demonstrates real heterogeneous compute (see STATUS.md).
    """

    name = "simulated-cloud"

    def __init__(
        self,
        *,
        models: list[ModelInfo] | None = None,
        token_delay: float = 0.005,
        response_tokens: int = 32,
        fail_health: bool = False,
        fail_after_tokens: int | None = None,
    ) -> None:
        self.token_delay = token_delay
        self.response_tokens = response_tokens
        self.fail_health = fail_health
        self.fail_after_tokens = fail_after_tokens
        self.chat_requests = 0
        self._models = models or [
            ModelInfo(
                id="sim-cloud-large",
                context_tokens=131072,
                capabilities=("completion", "chat", "generate", "cloud-batch"),
                loaded=True,
                metadata={"runtime": self.name, "region": "sim-earth-1"},
            )
        ]

    async def health(self) -> dict:
        if self.fail_health:
            raise EngineError("simulated cloud outage")
        return {"status": "ok"}

    async def list_models(self) -> list[ModelInfo]:
        if self.fail_health:
            raise EngineError("simulated cloud outage")
        return list(self._models)

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        if self.fail_health:
            raise EngineError("simulated cloud outage")
        self.chat_requests += 1
        n = min(self.response_tokens, max_tokens) if max_tokens > 0 else self.response_tokens
        for i in range(n):
            if self.fail_after_tokens is not None and i >= self.fail_after_tokens:
                raise EngineError("simulated mid-stream provider failure")
            await asyncio.sleep(self.token_delay)
            yield f"sim-token-{i} "

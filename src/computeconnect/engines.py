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
            # A non-200 is still unhealthy (raise_for_status above already
            # covers that). But a *200* is a live signal even when the body
            # isn't JSON: llama-swap and some proxies answer /health with a
            # plain-text "OK" rather than a JSON document. Treat any non-JSON
            # 200 body as a healthy status-shaped dict, mirroring the
            # {"status": "ok"} shape ProviderRegistry._refresh() expects (it
            # only treats "unreachable"/"down"/"error" as unhealthy — anything
            # else, including this fallback, reads as up).
            try:
                return resp.json()
            except ValueError:
                text = resp.text.strip()
                return {"status": "ok", "raw": text[:200]}

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
        usage_sink: dict | None = None,
        reasoning_sink: dict | None = None,
    ) -> AsyncIterator[str]:
        """Proxy a streaming chat completion, yielding text deltas.

        The httpx stream context is closed when this generator is closed,
        which drops the upstream connection — that is how cancellation
        propagates to the engine.

        ``usage_sink``, when given, is a caller-owned dict mutated in place
        with the upstream's real ``usage`` block (prompt/completion/total
        tokens) if and when it arrives. OpenAI-compatible servers only emit
        that block for a streamed request when asked via
        ``stream_options.include_usage``, so this always sets that flag; a
        fresh dict per call keeps concurrent runs from clobbering each
        other's counts. Callers that don't care pass nothing and the field is
        never read.

        ``reasoning_sink`` mirrors ``usage_sink`` for reasoning models
        (glm-4.7, qwen3.6, gemma-4, gpt-oss on this deployment's upstream):
        they stream their chain-of-thought as ``delta.reasoning_content`` and
        only emit ``delta.content`` once thinking finishes. Reasoning text
        (never yielded as content — it is a separate channel) is accumulated
        into ``reasoning_sink["text"]``. The sink also carries
        ``reasoning_sink["finish_reason"]``, the upstream's terminal
        ``choice.finish_reason`` — callers need this to detect the case where
        the token budget ran out *while the model was still reasoning*
        (``finish_reason == "length"`` with empty ``content``), which would
        otherwise silently look like an empty response. A fresh dict per call
        avoids cross-request clobbering; callers that don't care pass nothing.
        """
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
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
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield content
                        # Reasoning text is a distinct channel — accumulated,
                        # never yielded as content.
                        reasoning = delta.get("reasoning_content")
                        if reasoning and reasoning_sink is not None:
                            reasoning_sink["text"] = (
                                reasoning_sink.get("text", "") + reasoning
                            )
                        finish_reason = choice.get("finish_reason")
                        if finish_reason and reasoning_sink is not None:
                            reasoning_sink["finish_reason"] = finish_reason
                    # The real-usage chunk (OpenAI convention) typically has
                    # empty/absent choices and carries only "usage" — check
                    # unconditionally, not just when choices was empty.
                    usage = chunk.get("usage")
                    if usage is not None and usage_sink is not None:
                        usage_sink.clear()
                        usage_sink.update(usage)


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
        usage_sink: dict | None = None,
        reasoning_sink: dict | None = None,
    ) -> AsyncIterator[str]:
        # Simulated: no real upstream usage block to report and no reasoning
        # channel. usage_sink/reasoning_sink are accepted (not populated)
        # purely so callers can treat every engine identically — duck-type
        # parity with LlamaCppEngine — and exercise the estimate-fallback
        # path against this engine deliberately.
        if self.fail_health:
            raise EngineError("simulated cloud outage")
        self.chat_requests += 1
        n = min(self.response_tokens, max_tokens) if max_tokens > 0 else self.response_tokens
        for i in range(n):
            if self.fail_after_tokens is not None and i >= self.fail_after_tokens:
                raise EngineError("simulated mid-stream provider failure")
            await asyncio.sleep(self.token_delay)
            yield f"sim-token-{i} "

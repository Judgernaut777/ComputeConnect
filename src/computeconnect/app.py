"""The ComputeConnect service: two API layers, one backend.

Layer 1 (control plane, consumed by AgentConnect's ``HttpLocalComputeProvider``):

    GET  /health          GET  /models        GET  /models/loaded
    POST /route/estimate  POST /generate      POST /runs/{run_id}/cancel

Layer 2 (inference, consumed by BrainConnect and direct applications):

    GET  /v1/models       POST /v1/chat/completions

Both layers resolve providers through the same registry, create runs in the
same run registry, and generate through the same per-engine streaming path —
one source of truth for residency and capacity (D4).

``/generate`` streams (D3): the response is a single JSON document whose
``output`` string is emitted incrementally as the upstream engine produces
tokens. AgentConnect's shipped client parses the complete body with
``response.json()`` and is oblivious; a streaming reader sees tokens as they
happen. Nothing is buffered server-side beyond one delta. Cancellation
propagates: ``POST /runs/{id}/cancel`` (or a client disconnect) closes the
upstream connection mid-generation.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__
from .engines import LlamaCppEngine, ModelInfo, SimulatedCloudEngine
from .placement import (
    CandidateSet,
    Placement,
    PlacementRefusal,
    WorkloadSpec,
    estimate,
    filter_candidates,
    select_placement,
)
from .privacy import ResolvedPrivacy, resolve_privacy_precedence, resolve_privacy_tier
from .providers import ProviderRegistry, ProviderSpec
from .runs import Run, RunJournal, RunRegistry

_CANCELLED = object()
_DONE = object()


@dataclass
class AppConfig:
    providers: list[ProviderSpec] = field(default_factory=list)
    snapshot_ttl: float = 5.0
    #: Fail-closed staleness ceiling (seconds) for placement. A cached snapshot
    #: older than this is treated as unavailable rather than trusted, so
    #: grossly-stale capacity/health never drives a placement. ``None`` disables
    #: the ceiling (a snapshot is only ever as old as ``snapshot_ttl`` anyway,
    #: since ``snapshot()`` refreshes past the TTL). The default is a generous
    #: multiple of the TTL: a real backstop, not something that fires normally.
    max_snapshot_age: float | None = None
    #: Optional path to a SQLite run journal. When set, runs are persisted and,
    #: on the next start, any run left ``running`` by a crash is reconciled to
    #: the terminal ``interrupted`` state (never lost, never dangling). ``None``
    #: keeps the historical pure in-memory registry.
    run_journal_path: str | None = None

    def effective_max_snapshot_age(self) -> float | None:
        if self.max_snapshot_age is not None:
            return self.max_snapshot_age
        if self.snapshot_ttl <= 0:
            return None  # TTL 0 = always re-probe; a ceiling would be meaningless
        return max(self.snapshot_ttl * 6.0, self.snapshot_ttl + 30.0)


def build_default_config(
    upstream_url: str = "http://127.0.0.1:8080",
    *,
    include_sim_cloud: bool = True,
    snapshot_ttl: float = 5.0,
    run_journal_path: str | None = None,
) -> AppConfig:
    """Local llama.cpp (read-only upstream) plus the simulated cloud provider."""
    providers = [
        ProviderSpec(
            id="local-llamacpp",
            placement_class="local",
            engine=LlamaCppEngine(upstream_url),
            capabilities=("completion", "chat", "generate", "code", "summarize"),
            max_concurrency=2,
            estimated_quality=0.6,
            estimated_tokens_per_second=12.0,
        )
    ]
    if include_sim_cloud:
        providers.append(
            ProviderSpec(
                id="sim-cloud",
                placement_class="cloud",
                engine=SimulatedCloudEngine(),
                capabilities=("completion", "chat", "generate", "cloud-batch"),
                max_concurrency=8,
                estimated_quality=0.9,
                estimated_tokens_per_second=80.0,
            )
        )
    return AppConfig(
        providers=providers,
        snapshot_ttl=snapshot_ttl,
        run_journal_path=run_journal_path,
    )


class UpstreamStream:
    """Consumes an engine stream in a dedicated task, so cancellation never
    tears at a running async generator from a second task.

    The queue is bounded at one item: the pump cannot run ahead of the client,
    which is the backpressure propagation D3 requires.
    """

    def __init__(self, agen) -> None:
        self._agen = agen
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._task: asyncio.Task | None = None

    def start(self) -> "UpstreamStream":
        self._task = asyncio.create_task(self._pump())
        return self

    async def _pump(self) -> None:
        try:
            async for item in self._agen:
                await self._queue.put(("delta", item))
            await self._queue.put(("done", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._queue.put(("error", exc))
        finally:
            await self._agen.aclose()

    async def next(self, cancel_event: asyncio.Event):
        """The next delta, or _DONE / _CANCELLED; raises on upstream error."""
        get_task = asyncio.ensure_future(self._queue.get())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {get_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if get_task not in done:
                get_task.cancel()
                return _CANCELLED
            kind, payload = get_task.result()
            if kind == "delta":
                return payload
            if kind == "done":
                return _DONE
            raise payload
        finally:
            cancel_task.cancel()

    async def aclose(self) -> None:
        """Stop the pump; cancelling it closes the upstream connection."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass


class ComputeConnectAPI:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.journal = (
            RunJournal(config.run_journal_path) if config.run_journal_path else None
        )
        self.runs = RunRegistry(journal=self.journal)
        self.registry = ProviderRegistry(
            config.providers, self.runs, snapshot_ttl=config.snapshot_ttl
        )
        self._max_snapshot_age = config.effective_max_snapshot_age()

    # ------------------------------------------------------------------ utils

    async def _candidates(self, raw_tier: object) -> CandidateSet:
        return await self._candidates_resolved(resolve_privacy_tier(raw_tier))

    async def _candidates_resolved(self, privacy: ResolvedPrivacy) -> CandidateSet:
        snapshots = await self.registry.snapshot()
        return filter_candidates(snapshots, privacy)

    @staticmethod
    def _messages_from_generate(body: dict) -> list[dict]:
        messages: list[dict] = []
        context = body.get("context")
        if context:
            messages.append({"role": "system", "content": str(context)})
        messages.append({"role": "user", "content": str(body.get("prompt", ""))})
        return messages

    # ------------------------------------------------------- Layer 1: control

    async def health(self, request: Request) -> Response:
        snapshots = await self.registry.snapshot()
        healthy = [s for s in snapshots if s.healthy]
        if len(healthy) == len(snapshots) and snapshots:
            status = "ok"
        elif healthy:
            status = "degraded"
        else:
            status = "down"
        return JSONResponse(
            {
                "status": status,
                "service": "computeconnect",
                "version": __version__,
                "persistence": {
                    "run_journal": bool(self.journal),
                    "reconciled_runs_on_start": len(self.runs.reconciled_run_ids),
                },
                "providers": {
                    s.spec.id: {
                        "healthy": s.healthy,
                        "detail": s.detail,
                        "placement_class": s.spec.placement_class,
                        "runtime": getattr(s.spec.engine, "name", "unknown"),
                        "models": len(s.models),
                        "active_runs": s.active_runs,
                    }
                    for s in snapshots
                },
            }
        )

    async def _models_payload(self, loaded_only: bool) -> dict:
        snapshots = await self.registry.snapshot()
        models = []
        for snap in snapshots:
            for model in snap.models:
                if loaded_only and not model.loaded:
                    continue
                wire = model.wire()
                wire["metadata"].update(
                    {
                        "provider_id": snap.spec.id,
                        "placement_class": snap.spec.placement_class,
                    }
                )
                models.append(wire)
        return {"models": models}

    async def models(self, request: Request) -> Response:
        return JSONResponse(await self._models_payload(loaded_only=False))

    async def models_loaded(self, request: Request) -> Response:
        return JSONResponse(await self._models_payload(loaded_only=True))

    async def route_estimate(self, request: Request) -> Response:
        """Cheap and side-effect-free: reads the cached snapshot, creates no run."""
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        candidates = await self._candidates(body.get("privacy_tier"))
        workload = WorkloadSpec(
            model=body.get("model") or None,
            required_capabilities=tuple(body.get("required_capabilities") or ()),
            context_tokens=int(body.get("context_tokens") or 0),
            max_output_tokens=int(body.get("max_output_tokens") or 0),
            latency_preference=str(body.get("latency_preference") or "normal"),
            quality_preference=str(body.get("quality_preference") or "good_enough"),
        )
        return JSONResponse(
            estimate(candidates, workload, max_snapshot_age=self._max_snapshot_age)
        )

    async def generate(self, request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not str(body.get("prompt") or "").strip():
            return JSONResponse({"error": "prompt is required"}, status_code=400)

        # CA-1: optional privacy_tier; absent means the most restrictive tier.
        # The candidate set is rebuilt here, so the chosen provider is
        # positively re-verified against privacy at execution time.
        candidates = await self._candidates(body.get("privacy_tier"))
        workload = WorkloadSpec(
            model=body.get("model") or None,
            max_output_tokens=int(body.get("max_output_tokens") or 0),
            context_tokens=int(body.get("context_tokens") or 0),
            latency_preference=str(body.get("latency_preference") or "normal"),
            quality_preference=str(body.get("quality_preference") or "good_enough"),
        )
        outcome = select_placement(
            candidates, workload, max_snapshot_age=self._max_snapshot_age
        )
        if isinstance(outcome, PlacementRefusal):
            return JSONResponse(
                {
                    "run_id": None,
                    "status": "refused",
                    "output": "",
                    "model": workload.model,
                    "runtime": None,
                    "metrics": {},
                    "warnings": [f"refused: {outcome.code}"],
                    "refusal": outcome.to_dict(),
                }
            )

        run = self.runs.create(
            provider_id=outcome.provider.spec.id, model=outcome.model.id, surface="generate"
        )
        stream = self._generate_stream(run, outcome, body)
        return StreamingResponse(
            stream,
            media_type="application/json",
            headers={"X-Run-Id": run.id},
        )

    async def _generate_stream(self, run: Run, placement: Placement, body: dict):
        """Emit one JSON document incrementally; final status decided at the end."""
        engine = placement.provider.spec.engine
        model = placement.model
        max_tokens = int(body.get("max_output_tokens") or 2048)
        temperature = float(body.get("temperature") or 0.0)

        yield (
            "{"
            f"\"run_id\": {json.dumps(run.id)}, "
            f"\"model\": {json.dumps(model.id)}, "
            f"\"runtime\": {json.dumps(getattr(engine, 'name', 'unknown'))}, "
            "\"output\": \""
        )

        status = "succeeded"
        warnings: list[str] = []
        chunks = 0
        chars = 0
        started = time.time()
        upstream = UpstreamStream(
            engine.stream_chat(
                model.id,
                self._messages_from_generate(body),
                max_tokens=max_tokens,
                temperature=temperature,
            )
        ).start()
        try:
            while True:
                item = await upstream.next(run.cancel_event)
                if item is _DONE:
                    break
                if item is _CANCELLED:
                    status = "cancelled"
                    warnings.append("run cancelled by request")
                    break
                chunks += 1
                chars += len(item)
                # json.dumps escapes; strip the surrounding quotes.
                yield json.dumps(item)[1:-1]
        except (GeneratorExit, asyncio.CancelledError):
            # Client of /generate went away (GeneratorExit at a yield,
            # CancelledError at an await): propagate cancellation upstream.
            status = "disconnected"
            raise
        except Exception as exc:
            status = "failed"
            warnings.append(f"upstream engine error: {exc}")
        finally:
            await upstream.aclose()  # closes the upstream connection: propagates cancel
            self.runs.finish(
                run,
                status,
                chunks=chunks,
                output_chars=chars,
                duration_seconds=round(time.time() - started, 4),
            )

        metrics = {
            "run_id": run.id,
            "provider_id": placement.provider.spec.id,
            "placement_class": placement.provider.spec.placement_class,
            "chunks": chunks,
            "output_chars": chars,
            "duration_seconds": round(time.time() - started, 4),
            "estimated_cost_usd": 0.0,
            "rationale": placement.rationale,
        }
        yield (
            "\", "
            f"\"status\": {json.dumps(status)}, "
            f"\"metrics\": {json.dumps(metrics)}, "
            f"\"warnings\": {json.dumps(warnings)}"
            "}"
        )

    async def cancel_run(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        result = self.runs.cancel(run_id)
        if result == "not_found":
            return JSONResponse({"run_id": run_id, "status": "not_found"}, status_code=404)
        return JSONResponse({"run_id": run_id, "status": result})

    async def run_metadata(self, request: Request) -> Response:
        run = self.runs.get(request.path_params["run_id"])
        if run is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        return JSONResponse(run.to_dict())

    # ----------------------------------------------------- Layer 2: inference

    async def v1_models(self, request: Request) -> Response:
        snapshots = await self.registry.snapshot()
        data = [
            {
                "id": model.id,
                "object": "model",
                "created": int(snap.taken_at),
                "owned_by": snap.spec.id,
            }
            for snap in snapshots
            for model in snap.models
        ]
        return JSONResponse({"object": "list", "data": data})

    async def v1_chat_completions(self, request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return self._openai_error("invalid JSON body", "invalid_request_error", 400)
        model_name = body.get("model")
        messages = body.get("messages")
        if not model_name or not isinstance(messages, list) or not messages:
            return self._openai_error(
                "'model' and non-empty 'messages' are required",
                "invalid_request_error",
                400,
            )

        # Same structural privacy path as the control plane: no tier means the
        # most restrictive tier; cloud is filtered before placement. When BOTH
        # the X-Privacy-Tier header and a body privacy_tier are present, the
        # MORE RESTRICTIVE of the two wins — a header can never widen a
        # more-restrictive body (CONTRACT.md "Privacy precedence").
        privacy = resolve_privacy_precedence(
            request.headers.get("x-privacy-tier"), body.get("privacy_tier")
        )
        candidates = await self._candidates_resolved(privacy)
        outcome = select_placement(
            candidates,
            WorkloadSpec(
                model=str(model_name),
                latency_preference=str(body.get("latency_preference") or "normal"),
                quality_preference=str(body.get("quality_preference") or "good_enough"),
            ),
            max_snapshot_age=self._max_snapshot_age,
        )
        if isinstance(outcome, PlacementRefusal):
            known_anywhere = any(
                model_name == m.id for s in await self.registry.snapshot() for m in s.models
            )
            if known_anywhere:
                return self._openai_error(
                    f"model '{model_name}' is not available under the effective privacy "
                    f"tier: {json.dumps(outcome.to_dict())}",
                    "privacy_refusal",
                    403,
                    code=outcome.code,
                )
            # Known-but-unhealthy beats never-existed: if the model was in a
            # provider's last healthy inventory and that provider is down,
            # an OpenAI client should see "retry later", not "no such model".
            if self.registry.known_but_unhealthy(
                str(model_name), cloud_permitted=candidates.privacy.cloud_permitted
            ):
                return self._openai_error(
                    f"model '{model_name}' is known but its provider is currently "
                    f"unhealthy; retry later: {json.dumps(outcome.to_dict())}",
                    "service_unavailable",
                    503,
                    code="model_temporarily_unavailable",
                )
            return self._openai_error(
                f"model '{model_name}' not found: {json.dumps(outcome.to_dict())}",
                "invalid_request_error",
                404,
                code="model_not_found",
            )

        run = self.runs.create(
            provider_id=outcome.provider.spec.id, model=outcome.model.id, surface="openai"
        )
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 2048)
        temperature = float(body.get("temperature") or 0.0)
        engine = outcome.provider.spec.engine
        upstream = UpstreamStream(
            engine.stream_chat(
                outcome.model.id, messages, max_tokens=max_tokens, temperature=temperature
            )
        )

        if body.get("stream"):
            return StreamingResponse(
                self._openai_sse(run, outcome, upstream),
                media_type="text/event-stream",
                headers={"X-Run-Id": run.id, "Cache-Control": "no-cache"},
            )

        # Non-streaming: assembling a complete JSON body necessarily collects
        # the output. The generation path is still the single streaming one.
        upstream.start()
        parts: list[str] = []
        status = "succeeded"
        try:
            while True:
                item = await upstream.next(run.cancel_event)
                if item is _DONE:
                    break
                if item is _CANCELLED:
                    status = "cancelled"
                    break
                parts.append(item)
        except Exception as exc:
            self.runs.finish(run, "failed", error=str(exc))
            return self._openai_error(f"upstream engine error: {exc}", "upstream_error", 502)
        finally:
            await upstream.aclose()
        self.runs.finish(run, status, chunks=len(parts))
        text = "".join(parts)
        return JSONResponse(
            {
                "id": f"chatcmpl-{run.id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": outcome.model.id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop" if status == "succeeded" else "cancelled",
                    }
                ],
                "usage": {
                    "prompt_tokens": sum(
                        len(str(m.get("content", ""))) // 4 for m in messages
                    ),
                    "completion_tokens": max(1, len(text) // 4),
                    "total_tokens": 0,
                },
            },
            headers={"X-Run-Id": run.id},
        )

    async def _openai_sse(self, run: Run, placement: Placement, upstream: "UpstreamStream"):
        created = int(time.time())
        model_id = placement.model.id
        status = "succeeded"

        def chunk(delta: dict, finish: str | None) -> str:
            payload = {
                "id": f"chatcmpl-{run.id}",
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload)}\n\n"

        yield chunk({"role": "assistant"}, None)
        upstream.start()
        n = 0
        try:
            while True:
                item = await upstream.next(run.cancel_event)
                if item is _DONE:
                    break
                if item is _CANCELLED:
                    status = "cancelled"
                    break
                n += 1
                yield chunk({"content": item}, None)
        except (GeneratorExit, asyncio.CancelledError):
            status = "disconnected"
            raise
        except Exception:
            status = "failed"
        finally:
            await upstream.aclose()
            self.runs.finish(run, status, chunks=n)
        finish = {"succeeded": "stop", "cancelled": "cancelled", "failed": "error"}[status]
        yield chunk({}, finish)
        yield "data: [DONE]\n\n"

    @staticmethod
    def _openai_error(
        message: str, err_type: str, status_code: int, code: str | None = None
    ) -> JSONResponse:
        return JSONResponse(
            {"error": {"message": message, "type": err_type, "param": None, "code": code}},
            status_code=status_code,
        )


def create_app(config: AppConfig | None = None) -> Starlette:
    api = ComputeConnectAPI(config or build_default_config())
    app = Starlette(
        routes=[
            Route("/health", api.health, methods=["GET"]),
            Route("/models", api.models, methods=["GET"]),
            Route("/models/loaded", api.models_loaded, methods=["GET"]),
            Route("/route/estimate", api.route_estimate, methods=["POST"]),
            Route("/generate", api.generate, methods=["POST"]),
            Route("/runs/{run_id}/cancel", api.cancel_run, methods=["POST"]),
            Route("/runs/{run_id}", api.run_metadata, methods=["GET"]),
            Route("/v1/models", api.v1_models, methods=["GET"]),
            Route("/v1/chat/completions", api.v1_chat_completions, methods=["POST"]),
        ]
    )
    app.state.api = api
    return app

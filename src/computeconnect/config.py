"""Declarative configuration surface — build providers from a file, not code.

There are two distinct config audiences, and this module is about the first:

* **Operators of ComputeConnect** declare their real engines here, so standing
  up a second (or third) engine needs no code change. A file is JSON or YAML
  (YAML only if PyYAML is installed — it is an optional extra, JSON always
  works). Point at it with ``--config PATH`` or ``COMPUTECONNECT_CONFIG``.

* **AgentConnect** declares *ComputeConnect's URL* on ITS own side, the way it
  declares memory backends — env ``AGENTCONNECT_COMPUTE_URL`` or a ``compute:``
  block in a YAML file AgentConnect reads. That surface is consumed by the
  AgentConnect process, not this one; it is specified in
  ``docs/AGENTCONNECT_INTEGRATION.md`` and implemented in the AgentConnect repo.

Schema (all keys optional; unknown keys ignored)::

    snapshot_ttl: 5.0            # seconds; provider snapshot cache TTL
    max_snapshot_age: 30.0       # seconds; fail-closed staleness ceiling
    run_journal: /path/runs.db   # enable durable runs + restart reconciliation
    include_sim_cloud: false     # append the simulated cloud provider too
    token: <bearer-token>        # require Authorization: Bearer <token> on all
                                 # routes but /health (env: COMPUTECONNECT_TOKEN)
    providers:
      local-llamacpp:
        engine: llamacpp         # llamacpp | simulated_cloud
        placement_class: local   # local | cloud
        base_url: http://127.0.0.1:8080
        capabilities: [completion, chat, generate, code, summarize]
        max_concurrency: 2
        estimated_tokens_per_second: 12.0
        estimated_quality: 0.9
      local-llamacpp-4b:
        engine: llamacpp
        base_url: http://127.0.0.1:8091
        capabilities: [completion, chat, generate]
        estimated_tokens_per_second: 90.0
        estimated_quality: 0.55
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .app import AppConfig, build_default_config
from .engines import LlamaCppEngine, SimulatedCloudEngine
from .providers import ProviderSpec

#: Environment variable naming a ComputeConnect config file.
CONFIG_ENV = "COMPUTECONNECT_CONFIG"

#: Environment variable holding the bearer token that authenticates every
#: route but ``/health`` (see ``app._BearerAuthMiddleware``). Resolved in
#: :func:`load_app_config` the same way ``CONFIG_ENV`` is: an explicit
#: ``token=`` argument (e.g. a CLI ``--token`` flag) wins, then the env var,
#: then unset (open, matching the historical loopback-only behavior). A config
#: file's own ``token`` key, if present, wins over both — same precedence as
#: ``run_journal``.
TOKEN_ENV = "COMPUTECONNECT_TOKEN"

_DEFAULT_CAPS = ("completion", "chat", "generate")


def _engine_from(spec: dict[str, Any]) -> object:
    kind = str(spec.get("engine", "llamacpp")).lower()
    if kind in ("llamacpp", "llama.cpp", "llama_cpp", "llama"):
        base_url = spec.get("base_url")
        if not base_url:
            raise ValueError("llamacpp provider requires a 'base_url'")
        return LlamaCppEngine(str(base_url))
    if kind in ("simulated_cloud", "sim_cloud", "simulated", "sim"):
        return SimulatedCloudEngine()
    raise ValueError(f"unknown engine type {kind!r} (want llamacpp | simulated_cloud)")


def _provider_from(pid: str, spec: dict[str, Any]) -> ProviderSpec:
    return ProviderSpec(
        id=pid,
        placement_class=str(spec.get("placement_class", "local")),
        engine=_engine_from(spec),
        capabilities=tuple(spec.get("capabilities") or _DEFAULT_CAPS),
        max_concurrency=int(spec.get("max_concurrency", 2)),
        estimated_quality=float(spec.get("estimated_quality", 0.5)),
        estimated_tokens_per_second=float(spec.get("estimated_tokens_per_second", 10.0)),
    )


def app_config_from_dict(
    raw: dict[str, Any],
    *,
    upstream_url: str = "http://127.0.0.1:8080",
    include_sim_cloud: bool = True,
    snapshot_ttl: float = 5.0,
    run_journal_path: str | None = None,
    token: str | None = None,
) -> AppConfig:
    """Build an :class:`AppConfig` from a parsed config mapping.

    Declared ``providers`` replace the built-in default fleet. If the file
    declares none, the default llama.cpp-upstream (+ optional sim cloud) fleet
    is used. CLI/env values are the fallback when the file omits a key.
    """
    providers_raw = raw.get("providers") or {}
    providers = [_provider_from(pid, s or {}) for pid, s in providers_raw.items()]

    if raw.get("include_sim_cloud"):
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

    if not providers:
        base = build_default_config(
            upstream_url,
            include_sim_cloud=include_sim_cloud,
            snapshot_ttl=float(raw.get("snapshot_ttl", snapshot_ttl)),
            run_journal_path=raw.get("run_journal") or run_journal_path,
        )
        if "max_snapshot_age" in raw:
            base.max_snapshot_age = raw.get("max_snapshot_age")
        base.token = raw.get("token") or token
        return base

    return AppConfig(
        providers=providers,
        snapshot_ttl=float(raw.get("snapshot_ttl", snapshot_ttl)),
        max_snapshot_age=raw.get("max_snapshot_age"),
        run_journal_path=raw.get("run_journal") or run_journal_path,
        token=raw.get("token") or token,
    )


def _read_file(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                f"{path} is YAML but PyYAML is not installed; "
                "install the 'config' extra (pip install computeconnect[config]) "
                "or use a .json config file"
            ) from exc
        return yaml.safe_load(text) or {}
    # JSON is always supported without any extra dependency.
    return json.loads(text) if text.strip() else {}


def load_app_config(
    path: str | None = None,
    *,
    upstream_url: str = "http://127.0.0.1:8080",
    include_sim_cloud: bool = True,
    snapshot_ttl: float = 5.0,
    run_journal_path: str | None = None,
    token: str | None = None,
) -> AppConfig:
    """Load config from ``path`` (or ``$COMPUTECONNECT_CONFIG``), or fall back
    to the built-in default fleet when neither is set.

    ``token``: an explicit bearer token (e.g. a CLI ``--token`` flag) wins;
    otherwise ``$COMPUTECONNECT_TOKEN`` is used; otherwise the app stays open,
    matching the historical loopback-only behavior. A config file's own
    ``token`` key wins over both — see :func:`app_config_from_dict`.
    """
    token = token if token is not None else os.environ.get(TOKEN_ENV)
    path = path or os.environ.get(CONFIG_ENV)
    if not path:
        config = build_default_config(
            upstream_url,
            include_sim_cloud=include_sim_cloud,
            snapshot_ttl=snapshot_ttl,
            run_journal_path=run_journal_path,
        )
        config.token = token
        return config
    return app_config_from_dict(
        _read_file(path),
        upstream_url=upstream_url,
        include_sim_cloud=include_sim_cloud,
        snapshot_ttl=snapshot_ttl,
        run_journal_path=run_journal_path,
        token=token,
    )

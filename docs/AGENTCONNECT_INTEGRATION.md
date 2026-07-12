# AgentConnect ⇄ ComputeConnect integration

How AgentConnect discovers and talks to a ComputeConnect deployment **without programmatic
wiring** — the cross-repo LOW finding from the earlier wave ("AgentConnect config requires code to
attach a `LocalComputeProvider`; memory backends, by contrast, are declared in YAML/ENV").

This document specifies the **declarative surface** so the two sides agree on its shape. The
consumer change lives in the **AgentConnect** repo (see "Consumer change" at the bottom); nothing in
this file is code ComputeConnect runs. ComputeConnect already ships the server side of the contract
(the six `LocalComputeProvider` routes, `docs/CONTRACT.md`).

## The surface AgentConnect should read

Mirror the memory-backend pattern in AgentConnect's `bootstrap.py` (`memory_from_env`,
`config/memory.yaml`): env overrides file, file overrides nothing, absence means the feature is off.

### Environment (highest precedence)

| Variable | Meaning |
|---|---|
| `AGENTCONNECT_COMPUTE_URL` | Base URL of a ComputeConnect deployment, e.g. `http://127.0.0.1:8090`. Presence enables the `local_model_manager` worker; absence leaves it unregistered (exactly today's "optional subsystem" semantics). |
| `AGENTCONNECT_COMPUTE_TIMEOUT` | Optional request timeout in seconds (default 30). |
| `AGENTCONNECT_COMPUTE_TOKEN` | Optional bearer token, sent as `Authorization`, never logged — mirrors `WIKIBRAIN_TOKEN`. ComputeConnect is unauthenticated on loopback today; this is forward-compat. |

### YAML (`config/compute.yaml`, or a `compute:` block alongside `memory:`)

```yaml
compute:
  enabled: true
  # Base URL of the ComputeConnect control plane (the six LocalComputeProvider routes).
  base_url: http://127.0.0.1:8090
  # Optional; env AGENTCONNECT_COMPUTE_TIMEOUT wins.
  timeout: 30
  # Optional bearer token; env AGENTCONNECT_COMPUTE_TOKEN wins. Prefer the env var.
  # token: Bearer <token>
  # Optional worker-registration knobs (all have safe defaults in the adapter):
  worker_id: local-manager
  task_type: general
  max_output_tokens: 2048
```

Precedence, byte-for-byte with memory: `AGENTCONNECT_COMPUTE_URL` (env) → `compute.base_url` (yaml) →
unset (subsystem off). A malformed `compute:` block should **degrade to off with a warning**, the
same way a bad `memory:` block does — a missing compute plane is a smaller problem than a wrong one.

## What AgentConnect builds from it

Exactly what it builds today, but from config instead of code:

```python
# sketch for AgentConnect bootstrap.py — belongs in the AgentConnect repo.
from agentconnect.core.local_compute import (
    HttpLocalComputeProvider, LocalModelManagerWorkerAdapter,
)

def compute_worker_from_env():
    url = os.environ.get("AGENTCONNECT_COMPUTE_URL") or (_load_compute_yaml().get("base_url"))
    if not url:
        return None                      # subsystem stays off, as today
    provider = HttpLocalComputeProvider(url, timeout=_timeout())
    return LocalModelManagerWorkerAdapter(provider)   # already exists, unchanged
```

`HttpLocalComputeProvider` and `LocalModelManagerWorkerAdapter` **already exist** in
`agentconnect/core/local_compute.py`; only the *wiring from config* is missing. No ComputeConnect
change is needed for any of this — its routes already conform.

## Privacy-tier vocabulary agreement

The two sides already share the tier vocabulary and its strictness order. ComputeConnect's
`privacy.PRIVACY_STRICTNESS` is a byte-mirror of AgentConnect's `models.PRIVACY_STRICTNESS`, asserted
by a test (`tests/test_privacy.py::TestPrivacyPrecedence::test_strictness_mirror_matches_agentconnect`)
so a drift on either side fails CI. The header/body **precedence** rule (more restrictive wins) is in
`docs/CONTRACT.md`; AgentConnect callers that set only `subtask.privacy_tier` are unaffected.

## Consumer change — for the lead (sibling repo, not this one)

The only change needed to close the finding lives in **`mcp-agentconnect`**:

1. Add `compute_worker_from_env()` (above) to `packages/agentconnect-core/src/agentconnect/core/bootstrap.py`.
2. Append its result (if not `None`) to the worker list in `service_from_env`.
3. Add `config/compute.yaml` (example above) and read it with the same `yaml.safe_load` +
   env-override pattern as `_load_memory_yaml`.
4. A degrade-to-off-on-malformed test, matching the memory backend's.

No wire-format or ComputeConnect-server change is implied. This file is the agreed shape.

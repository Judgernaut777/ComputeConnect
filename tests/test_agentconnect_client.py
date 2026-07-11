"""Conformance against AgentConnect's *shipped* client.

Imports ``HttpLocalComputeProvider`` straight out of the sibling
mcp-agentconnect checkout (read-only) and points it at a live ComputeConnect
server. If that client parses our wire, the contract is met by construction.
Skips if the sibling checkout or its dependencies are absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_AGENTCONNECT_SRC = Path("/home/mini/mcp-agentconnect/packages/agentconnect-core/src")

if not _AGENTCONNECT_SRC.is_dir():  # pragma: no cover
    pytest.skip("mcp-agentconnect checkout not available", allow_module_level=True)
if str(_AGENTCONNECT_SRC) not in sys.path:
    sys.path.insert(0, str(_AGENTCONNECT_SRC))

pytest.importorskip("pydantic", reason="agentconnect-core requires pydantic")
local_compute = pytest.importorskip(
    "agentconnect.core.local_compute", reason="agentconnect-core not importable"
)

HttpLocalComputeProvider = local_compute.HttpLocalComputeProvider
LocalEstimateRequest = local_compute.LocalEstimateRequest
LocalRunRequest = local_compute.LocalRunRequest


@pytest.fixture()
def provider(stack):
    return HttpLocalComputeProvider(stack.base_url, timeout=30.0)


def test_shipped_client_health(provider):
    body = provider.health()
    assert body["status"] == "ok"


def test_shipped_client_inventory_and_loaded(provider):
    inventory = provider.inventory()
    ids = {m.id for m in inventory}
    assert {"fake-llama-7b", "sim-cloud-large"} <= ids
    local = next(m for m in inventory if m.id == "fake-llama-7b")
    assert local.runtime == "llama.cpp"
    assert local.context_tokens == 4096
    assert local.loaded is True
    assert {m.id for m in provider.loaded()} == ids


def test_shipped_client_estimate(provider):
    estimate = provider.estimate(
        LocalEstimateRequest(
            task_type="general",
            privacy_tier="repo_sensitive",
            required_capabilities=["code"],
            context_tokens=256,
            max_output_tokens=128,
        )
    )
    assert estimate.eligible is True
    assert estimate.selected_model == "fake-llama-7b"
    assert estimate.runtime == "llama.cpp"
    assert estimate.loaded is True
    assert isinstance(estimate.reason, dict)


def test_shipped_client_run_parses_streamed_json(provider, stack):
    """The client buffers and json()s; our incrementally-streamed document must
    parse as a complete LocalRunResult."""
    result = provider.run(
        LocalRunRequest(
            model=None,
            task_type="general",
            prompt="hello from the shipped client",
            max_output_tokens=6,
        )
    )
    assert result.status == "succeeded"
    assert result.output.startswith("tok0")
    assert result.model == "fake-llama-7b"
    assert result.runtime == "llama.cpp"
    assert result.metrics["chunks"] > 0
    assert "run_id" in result.metrics  # the run-id amendment, visible to the client


def test_shipped_client_run_without_tier_never_reaches_cloud(provider, stack):
    """The shipped LocalRunRequest cannot carry a privacy tier — exactly the
    CA-1 gap. ComputeConnect must therefore assume most-restrictive: a run
    naming the cloud model is refused, and the refusal surfaces through the
    client as a non-succeeded status, not an exception."""
    result = provider.run(
        LocalRunRequest(model="sim-cloud-large", task_type="general", prompt="hi")
    )
    assert result.status == "refused"
    assert stack.sim.chat_requests == 0


def test_shipped_client_cancel_is_best_effort(provider):
    # Unknown run: the client logs and swallows; must not raise.
    provider.cancel("no-such-run")

"""Bearer-token auth and the non-loopback bind refusal.

Mirrors ToolConnect's tests/test_auth.py (same shape, same defense-in-depth
posture): a fail-closed default (no token, loopback-only) that stays open for
back-compat, but that MUST NOT be reachable, unauthenticated, on any interface
other than loopback. Route-level checks run against a real uvicorn server (via
the ``conftest.ServerHandle`` every other test-file uses) so the middleware is
exercised over real HTTP, not an in-process ASGI shim. CLI-level checks call
``cli.main`` directly with ``uvicorn.run`` stubbed out, so a "this should be
allowed to start" assertion never actually binds a socket.
"""

from __future__ import annotations

import httpx
import pytest

from computeconnect.app import LOOPBACK_HOSTS, AppConfig, create_app
from computeconnect.cli import main as cli_main
from computeconnect.engines import LlamaCppEngine, SimulatedCloudEngine
from computeconnect.providers import ProviderSpec

from conftest import ServerHandle

TOKEN = "s3cr3t-bearer-token-value"


def _config(*, token: str | None, upstream_url: str) -> AppConfig:
    return AppConfig(
        providers=[
            ProviderSpec(
                id="local-llamacpp",
                placement_class="local",
                engine=LlamaCppEngine(upstream_url),
                capabilities=("completion", "chat", "generate"),
            ),
        ],
        snapshot_ttl=0.0,
        token=token,
    )


@pytest.fixture()
def auth_server(upstream_server):
    """A real ComputeConnect server with a bearer token configured."""
    _, upstream_handle = upstream_server
    cfg = _config(token=TOKEN, upstream_url=upstream_handle.base_url)
    handle = ServerHandle(create_app(cfg)).start()
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture()
def open_server(upstream_server):
    """A real ComputeConnect server with no token — the historical behavior."""
    _, upstream_handle = upstream_server
    cfg = _config(token=None, upstream_url=upstream_handle.base_url)
    handle = ServerHandle(create_app(cfg)).start()
    try:
        yield handle
    finally:
        handle.stop()


# --------------------------------------------------------------------- (c)
# back-compat: no token configured means every route stays open, exactly as
# before this change.


def test_no_token_configured_is_open(open_server):
    resp = httpx.get(f"{open_server.base_url}/health", timeout=10)
    assert resp.status_code == 200
    resp = httpx.get(f"{open_server.base_url}/models", timeout=10)
    assert resp.status_code == 200


# --------------------------------------------------------------------- (a)
# token configured: every non-exempt route requires it.


def test_missing_token_is_401_when_required(auth_server):
    resp = httpx.get(f"{auth_server.base_url}/models", timeout=10)
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_wrong_token_is_401(auth_server):
    resp = httpx.get(
        f"{auth_server.base_url}/models",
        headers={"Authorization": "Bearer not-the-token"},
        timeout=10,
    )
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "bad_header",
    ["token-without-scheme", "Basic abc", "Bearer", "bearer"],
)
def test_malformed_authorization_header_is_401(auth_server, bad_header):
    # ("bearer " with a trailing space is also malformed/empty-token, but
    # httpx's own header validation refuses to send a value with trailing
    # whitespace, so it cannot be exercised over a real HTTP client here.)
    resp = httpx.get(
        f"{auth_server.base_url}/models",
        headers={"Authorization": bad_header},
        timeout=10,
    )
    assert resp.status_code == 401, f"{bad_header!r} should be rejected"


def test_correct_token_authorizes_get(auth_server):
    resp = httpx.get(
        f"{auth_server.base_url}/models",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    assert resp.status_code == 200


def test_correct_token_authorizes_post(auth_server):
    resp = httpx.post(
        f"{auth_server.base_url}/route/estimate",
        json={"task_type": "general", "privacy_tier": "local_only"},
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    assert resp.status_code == 200


def test_wrong_token_on_post_is_401_and_body_never_reaches_the_handler(auth_server):
    # An invalid-JSON body would normally 400; auth must be checked first, so
    # this is a clean 401, not a 400 — mirrors ToolConnect's
    # test_auth_check_precedes_body_parse.
    resp = httpx.post(
        f"{auth_server.base_url}/route/estimate",
        content=b"{not valid json",
        headers={
            "Authorization": "Bearer wrong",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------- (d)
# /health stays reachable with no credential even when a token is configured,
# so orchestration healthchecks keep working.


def test_health_exempt_even_with_token_configured(auth_server):
    resp = httpx.get(f"{auth_server.base_url}/health", timeout=10)
    assert resp.status_code == 200
    assert resp.json()["status"] in ("ok", "degraded", "down")


# --------------------------------------------------------------------- (b)
# non-loopback bind refusal, at the create_app() layer.


def test_create_app_refuses_nonloopback_without_token():
    cfg = AppConfig(providers=[], token=None)
    with pytest.raises(ValueError, match="refusing to build"):
        create_app(cfg, host="0.0.0.0")


def test_create_app_refuses_nonloopback_ipv6_without_token():
    cfg = AppConfig(providers=[], token=None)
    with pytest.raises(ValueError, match="refusing to build"):
        create_app(cfg, host="::")


def test_create_app_allows_nonloopback_with_token():
    cfg = AppConfig(providers=[], token=TOKEN)
    app = create_app(cfg, host="0.0.0.0")
    assert app is not None


@pytest.mark.parametrize("loopback_host", sorted(LOOPBACK_HOSTS))
def test_create_app_allows_loopback_without_token(loopback_host):
    """Back-compat: a loopback bind with no token has never needed one."""
    cfg = AppConfig(providers=[], token=None)
    app = create_app(cfg, host=loopback_host)
    assert app is not None


def test_create_app_host_none_skips_the_check_for_untargeted_callers():
    """The historical ``create_app(config)`` call (no ``host=``) — every
    existing in-process/test caller — is unaffected, even with no token and a
    config that says nothing about where it will be bound."""
    cfg = AppConfig(providers=[], token=None)
    app = create_app(cfg)
    assert app is not None


# --------------------------------------------------------------------- (b)
# non-loopback bind refusal, at the CLI preflight (defense in depth: the same
# thing create_app() enforces, enforced again one layer up, before create_app
# is even called).


def test_cli_serve_refuses_nonloopback_without_token(monkeypatch):
    monkeypatch.delenv("COMPUTECONNECT_TOKEN", raising=False)
    monkeypatch.delenv("COMPUTECONNECT_CONFIG", raising=False)
    with pytest.raises(SystemExit, match="refusing to bind"):
        cli_main(["serve", "--host", "0.0.0.0", "--port", "0"])


def test_cli_serve_allows_nonloopback_with_token_flag(monkeypatch):
    """A token supplied via --token is enough to start on a non-loopback host.
    ``uvicorn.run`` is stubbed so this never actually binds a socket."""
    monkeypatch.delenv("COMPUTECONNECT_TOKEN", raising=False)
    monkeypatch.delenv("COMPUTECONNECT_CONFIG", raising=False)
    calls = []
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kw: calls.append((app, kw))
    )
    rc = cli_main(
        ["serve", "--host", "0.0.0.0", "--port", "0", "--token", TOKEN]
    )
    assert rc == 0
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["host"] == "0.0.0.0"


def test_cli_serve_allows_nonloopback_with_token_env(monkeypatch):
    monkeypatch.setenv("COMPUTECONNECT_TOKEN", TOKEN)
    monkeypatch.delenv("COMPUTECONNECT_CONFIG", raising=False)
    calls = []
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kw: calls.append((app, kw))
    )
    rc = cli_main(["serve", "--host", "0.0.0.0", "--port", "0"])
    assert rc == 0
    assert len(calls) == 1


def test_cli_serve_allows_loopback_without_token(monkeypatch):
    """Back-compat: the default host (127.0.0.1) never needed a token."""
    monkeypatch.delenv("COMPUTECONNECT_TOKEN", raising=False)
    monkeypatch.delenv("COMPUTECONNECT_CONFIG", raising=False)
    calls = []
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kw: calls.append((app, kw))
    )
    rc = cli_main(["serve", "--port", "0"])
    assert rc == 0
    assert len(calls) == 1


# --------------------------------------------------------------------- config
# COMPUTECONNECT_TOKEN plumbing through load_app_config (env / explicit arg /
# config-file precedence), exercised directly rather than through the CLI.


def test_load_app_config_reads_token_from_env(monkeypatch):
    from computeconnect.config import load_app_config

    monkeypatch.setenv("COMPUTECONNECT_TOKEN", TOKEN)
    monkeypatch.delenv("COMPUTECONNECT_CONFIG", raising=False)
    cfg = load_app_config()
    assert cfg.token == TOKEN


def test_load_app_config_explicit_token_wins_over_env(monkeypatch):
    from computeconnect.config import load_app_config

    monkeypatch.setenv("COMPUTECONNECT_TOKEN", "env-token")
    monkeypatch.delenv("COMPUTECONNECT_CONFIG", raising=False)
    cfg = load_app_config(token="explicit-token")
    assert cfg.token == "explicit-token"


def test_load_app_config_no_token_leaves_it_unset(monkeypatch):
    from computeconnect.config import load_app_config

    monkeypatch.delenv("COMPUTECONNECT_TOKEN", raising=False)
    monkeypatch.delenv("COMPUTECONNECT_CONFIG", raising=False)
    cfg = load_app_config()
    assert cfg.token is None

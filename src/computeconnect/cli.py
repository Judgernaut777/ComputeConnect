"""``computeconnect serve`` — run the service.

Default port is 8090: on this host 8080 is the externally managed llama.cpp
upstream (never touched) and 8787 is reserved for BrainConnect.
"""

from __future__ import annotations

import argparse
import os

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="computeconnect", description="ComputeConnect compute-plane service"
    )
    parser.add_argument("--version", action="version", version=f"computeconnect {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="serve both API layers")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.add_argument(
        "--upstream",
        default="http://127.0.0.1:8080",
        help="base URL of the local llama.cpp engine (consumed read-only)",
    )
    serve.add_argument(
        "--no-sim-cloud",
        action="store_true",
        help="do not register the simulated cloud provider",
    )
    serve.add_argument("--snapshot-ttl", type=float, default=5.0)
    serve.add_argument(
        "--run-journal",
        default=None,
        help="path to a SQLite run journal for restart recovery (default: "
        "in-memory only). In-flight runs are reconciled to 'interrupted' on the "
        "next start, never left dangling.",
    )
    serve.add_argument(
        "--config",
        default=None,
        help="path to a YAML config declaring extra providers/engines "
        "(env: COMPUTECONNECT_CONFIG). Additive to the default llama.cpp "
        "upstream + simulated cloud unless the file sets 'defaults: {...}'.",
    )
    serve.add_argument("--log-level", default="info")
    serve.add_argument(
        "--token",
        default=None,
        help="bearer token required (as 'Authorization: Bearer <token>') on every "
        "route but /health (env: COMPUTECONNECT_TOKEN). Never pass a real secret "
        "as a bare flag on a shared host (argv is visible in `ps`) — prefer the "
        "env var. Required to bind a non-loopback --host.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn

        from .app import LOOPBACK_HOSTS, create_app
        from .config import TOKEN_ENV, load_app_config

        token = args.token if args.token is not None else os.environ.get(TOKEN_ENV)
        if args.host not in LOOPBACK_HOSTS and not token:
            # A non-loopback bind with no token would put an unauthenticated
            # inference + cancellation surface on a reachable interface.
            # Refuse here — the same check create_app(host=...) makes below is
            # defense in depth, not a substitute for failing fast at the CLI.
            raise SystemExit(
                f"refusing to bind {args.host} without authentication: set "
                f"--token (or ${TOKEN_ENV}), or bind a loopback host "
                f"({', '.join(sorted(LOOPBACK_HOSTS))})."
            )

        app = create_app(
            load_app_config(
                args.config,
                upstream_url=args.upstream,
                include_sim_cloud=not args.no_sim_cloud,
                snapshot_ttl=args.snapshot_ttl,
                run_journal_path=args.run_journal,
                token=token,
            ),
            host=args.host,
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

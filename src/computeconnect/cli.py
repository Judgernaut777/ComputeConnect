"""``computeconnect serve`` — run the service.

Default port is 8090: on this host 8080 is the externally managed llama.cpp
upstream (never touched) and 8787 is reserved for BrainConnect.
"""

from __future__ import annotations

import argparse

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
    serve.add_argument("--log-level", default="info")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn

        from .app import build_default_config, create_app

        app = create_app(
            build_default_config(
                args.upstream,
                include_sim_cloud=not args.no_sim_cloud,
                snapshot_ttl=args.snapshot_ttl,
            )
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

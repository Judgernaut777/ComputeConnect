"""CLI surface: defaults that must not drift (8090; 8080/8787 are taken)."""

from __future__ import annotations

from computeconnect.cli import build_parser


def test_serve_defaults():
    args = build_parser().parse_args(["serve"])
    assert args.port == 8090
    assert args.host == "127.0.0.1"
    assert args.upstream == "http://127.0.0.1:8080"
    assert args.no_sim_cloud is False


def test_serve_flags_parse():
    args = build_parser().parse_args(
        ["serve", "--port", "9001", "--no-sim-cloud", "--upstream", "http://x:1"]
    )
    assert args.port == 9001
    assert args.no_sim_cloud is True
    assert args.upstream == "http://x:1"

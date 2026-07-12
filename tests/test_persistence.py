"""Deliverable 6: runs are durably tracked, and a restart reconciles in-flight
runs to a terminal state — never lost, never left dangling as 'running'.

Deliverable 7 (operator side): the declarative config surface builds providers
from a file so a second engine needs no code change.
"""

from __future__ import annotations

import json

import httpx

from computeconnect.app import AppConfig, create_app
from computeconnect.config import app_config_from_dict, load_app_config
from computeconnect.engines import LlamaCppEngine
from computeconnect.providers import ProviderSpec
from computeconnect.runs import RunJournal, RunRegistry

from conftest import ServerHandle


# --------------------------------------------------------------- run journal


def test_journal_reconciles_orphaned_running_run(tmp_path):
    """A run left 'running' by a crash becomes 'interrupted' on the next start."""
    db = str(tmp_path / "runs.db")
    # First process: create a run and leave it running (simulate a crash: no finish).
    reg1 = RunRegistry(journal=RunJournal(db))
    run = reg1.create(provider_id="local-llamacpp", model="m", surface="generate")
    assert run.state == "running"
    assert reg1.reconciled_run_ids == []  # nothing to reconcile yet

    # Second process over the SAME journal: reconciliation runs on construction.
    reg2 = RunRegistry(journal=RunJournal(db))
    assert run.id in reg2.reconciled_run_ids
    recovered = reg2.get(run.id)
    assert recovered is not None
    assert recovered.state == "interrupted"  # terminal, not 'running'
    assert recovered.finished_at is not None


def test_journal_does_not_touch_already_terminal_runs(tmp_path):
    db = str(tmp_path / "runs.db")
    reg1 = RunRegistry(journal=RunJournal(db))
    run = reg1.create(provider_id="p", model="m", surface="generate")
    reg1.finish(run, "succeeded", chunks=3)

    reg2 = RunRegistry(journal=RunJournal(db))
    assert reg2.reconciled_run_ids == []  # succeeded run left alone
    recovered = reg2.get(run.id)
    assert recovered.state == "succeeded"
    assert recovered.metrics.get("chunks") == 3


def test_reconcile_is_idempotent(tmp_path):
    db = str(tmp_path / "runs.db")
    reg1 = RunRegistry(journal=RunJournal(db))
    run = reg1.create(provider_id="p", model="m", surface="generate")
    ids_a = RunRegistry(journal=RunJournal(db)).reconciled_run_ids
    ids_b = RunRegistry(journal=RunJournal(db)).reconciled_run_ids
    assert run.id in ids_a
    assert ids_b == []  # already interrupted the first time


def test_cancel_of_reconciled_run_reports_already_finished(tmp_path):
    db = str(tmp_path / "runs.db")
    reg1 = RunRegistry(journal=RunJournal(db))
    run = reg1.create(provider_id="p", model="m", surface="generate")
    reg2 = RunRegistry(journal=RunJournal(db))
    assert reg2.cancel(run.id) == "already_finished"
    assert reg2.cancel("no-such-run") == "not_found"


def test_no_journal_is_pure_in_memory():
    reg = RunRegistry()
    assert reg.reconciled_run_ids == []
    run = reg.create(provider_id="p", model="m", surface="generate")
    assert reg.get(run.id) is run
    assert reg.get("missing") is None


def test_service_restart_reconciles_over_http(upstream_server, tmp_path):
    """End-to-end: a run recorded 'running' in the journal by one server is
    queryable as 'interrupted' from a freshly-started server on the same
    journal — a client can tell 'restart interrupted your run; retry' from
    'no such run'."""
    _, upstream_handle = upstream_server
    db = str(tmp_path / "runs.db")

    def make_app():
        cfg = AppConfig(
            providers=[
                ProviderSpec(
                    id="local-llamacpp",
                    placement_class="local",
                    engine=LlamaCppEngine(upstream_handle.base_url),
                    capabilities=("generate",),
                )
            ],
            snapshot_ttl=0.0,
            run_journal_path=db,
        )
        return create_app(cfg)

    # Server 1: inject an orphaned running run directly into its registry+journal.
    app1 = make_app()
    api1 = app1.state.api
    orphan = api1.runs.create(provider_id="local-llamacpp", model="m", surface="generate")

    # Server 2: a fresh process over the same journal.
    app2 = make_app()
    handle2 = ServerHandle(app2).start()
    try:
        meta = httpx.get(f"{handle2.base_url}/runs/{orphan.id}", timeout=10).json()
        assert meta["state"] == "interrupted"
        assert meta["run_id"] == orphan.id
        health = httpx.get(f"{handle2.base_url}/health", timeout=10).json()
        assert health["persistence"]["run_journal"] is True
        assert health["persistence"]["reconciled_runs_on_start"] >= 1
    finally:
        handle2.stop()


# --------------------------------------------------------------- config surface


def test_config_from_dict_builds_declared_providers():
    cfg = app_config_from_dict(
        {
            "snapshot_ttl": 3.0,
            "max_snapshot_age": 20.0,
            "providers": {
                "big": {
                    "engine": "llamacpp",
                    "base_url": "http://127.0.0.1:8080",
                    "capabilities": ["generate"],
                    "estimated_tokens_per_second": 12.0,
                    "estimated_quality": 0.9,
                },
                "small": {
                    "engine": "llamacpp",
                    "base_url": "http://127.0.0.1:8091",
                    "estimated_tokens_per_second": 90.0,
                    "estimated_quality": 0.55,
                },
            },
        }
    )
    ids = {p.id for p in cfg.providers}
    assert ids == {"big", "small"}
    assert cfg.snapshot_ttl == 3.0
    assert cfg.max_snapshot_age == 20.0
    small = next(p for p in cfg.providers if p.id == "small")
    assert small.estimated_tokens_per_second == 90.0
    assert small.engine.base_url == "http://127.0.0.1:8091"


def test_config_empty_falls_back_to_default_fleet():
    cfg = app_config_from_dict({}, include_sim_cloud=False)
    assert {p.id for p in cfg.providers} == {"local-llamacpp"}


def test_config_include_sim_cloud_appends_cloud_provider():
    cfg = app_config_from_dict(
        {"providers": {"local": {"engine": "llamacpp", "base_url": "http://x:1"}},
         "include_sim_cloud": True}
    )
    classes = {p.placement_class for p in cfg.providers}
    assert classes == {"local", "cloud"}


def test_load_app_config_reads_json_file(tmp_path):
    path = tmp_path / "cc.json"
    path.write_text(
        json.dumps(
            {"providers": {"n": {"engine": "llamacpp", "base_url": "http://h:1"}}}
        )
    )
    cfg = load_app_config(str(path))
    assert {p.id for p in cfg.providers} == {"n"}


def test_load_app_config_no_path_is_default():
    cfg = load_app_config(None, include_sim_cloud=False)
    assert {p.id for p in cfg.providers} == {"local-llamacpp"}


def test_config_rejects_unknown_engine():
    import pytest

    with pytest.raises(ValueError):
        app_config_from_dict({"providers": {"x": {"engine": "wat"}}})

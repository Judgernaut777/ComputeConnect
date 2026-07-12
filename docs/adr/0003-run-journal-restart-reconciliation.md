# ADR 0003 — Durable run journal with restart reconciliation

**Status:** Accepted (2026-07-12, production-hardening pass)

## Context

Runs lived only in memory. On restart, in-flight runs vanished: `GET /runs/{id}` returned 404 and a
client could not tell "your run was interrupted by a restart" from "no such run". Production wants
defined restart behavior for in-flight work.

## Decision

Add an **optional** SQLite `RunJournal` (`--run-journal PATH` / `AppConfig.run_journal_path`). Runs
are recorded at create and at finish. On the next start, `reconcile()` transitions any row still
`running` (orphaned by a crash) to the new terminal state **`interrupted`**; reconciled runs stay
queryable. ComputeConnect is a thin proxy holding no resumable generation state, so an interrupted
run is **accounted for, not resumed** — that is the deliberate contract. Default remains pure
in-memory (no behavior change, no new dependency: `sqlite3` is stdlib).

## Consequences

* Restart behavior is now defined and tested (`tests/test_persistence.py`, incl. an end-to-end
  two-server test over real HTTP). `/health` surfaces `persistence.reconciled_runs_on_start`.
* `interrupted` joins `TERMINAL_STATES`; cancelling a reconciled run returns `already_finished`.
* Not distributed and not a resume mechanism — a heavier design (resumable streams, external queue)
  is explicitly out of scope for a thin proxy.

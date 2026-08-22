# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI entry point (T7 / FR-1/4/6 surface).

Subcommands:
  poll    — single incremental fetch against a registry feed (FR-1)
  sweep   — periodic full-catalog sweep, PRIMARY control (FR-4)
  replay  — offline replay against an external-validity panel (FR-6)
  serve   — stub: reserved for future live watcher

Sink is pluggable via --sink: stdout (default) or file:<path>.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

from .core import calibrate
from .core.diff import compute_events
from .core.poller import PollConfig, Poller
from .core.state import FetchStatus, StateStore
from .core.sweep import run_sweep

log = logging.getLogger("mcp_drift_monitor.cli")


def _now_iso() -> str:
    """Real UTC timestamp for every fetch (NFR-1: 'when did the change happen').

    Injected into events by the entry point, NOT in the core engine — keeps the
    core deterministic/testable with fixture timestamps while the CLI produces
    the real wall-clock time.
    """
    return datetime.now(UTC).isoformat()

app = typer.Typer(
    name="mcp-drift-monitor",
    help="Continuous MCP Registry Drift Monitor (arXiv:2608.00997). "
    "content-binding revalidation + full-catalog sweep.",
)


def _emit(sink: str, payload: dict) -> None:
    """Pluggable sink: stdout or file:<path>.

    One JSON object per line (NDJSON). Keeps the CLI testable without
    inspecting stdout formatting.
    """
    line = json.dumps(payload, sort_keys=True)
    if sink == "stdout":
        print(line)
    elif sink.startswith("file:"):
        path = Path(sink.split(":", 1)[1])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    else:
        log.error("unknown sink: %r (use stdout or file:<path>)", sink)
        raise typer.BadParameter(f"unknown sink: {sink}")


@app.command()
def poll(
    feed_url: str = typer.Option(..., help="MCP registry feed URL"),
    db: str = typer.Option("drift.sqlite", help="state store path"),
    max_retries: int = typer.Option(5, help="HTTP retry budget for 429/network errors"),
    sink: str = typer.Option("stdout", "--sink", help="stdout | file:<path>"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="debug logging"),
) -> None:
    """Single incremental fetch against a registry feed (FR-1)."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    poller = Poller(PollConfig(feed_url=feed_url, max_retries=max_retries))
    store = StateStore(db)
    try:
        entries, status = poller.fetch_catalog()
        _emit(sink, {
            "event": "poll",
            "status": status.value,
            "entry_count": len(entries),
        })
        if status == FetchStatus.OK:
            cur = {e.server_id: e.desc_hash for e in entries}
            prior = store.get_all_hashes()
            # Real timestamps + monotonic obs_index (NFR-1, FR-5): injected at the
            # entry point, NOT hardcoded to 0/"" so events record when they happened.
            obs_index = store.get_next_obs_index()
            ts = _now_iso()
            drifts, arrivals, removals = compute_events(prior, cur, obs_index, ts)
            for d in drifts:
                _emit(sink, {
                    "event": "drift", "server_id": d.server_id,
                    "old_desc_hash": d.old_desc_hash,
                    "new_desc_hash": d.new_desc_hash,
                    "obs_index": obs_index, "ts": ts,
                })
            for a in arrivals:
                _emit(sink, {"event": "arrival", "server_id": a.server_id,
                             "obs_index": obs_index, "ts": ts})
            for r in removals:
                _emit(sink, {"event": "removal", "server_id": r.server_id,
                             "obs_index": obs_index, "ts": ts})
            # FR-5: persist the append-only audit trail so drift/arrival/removal
            # events survive restarts even if the sink output is lost.
            store.append_events(drifts + arrivals + removals, obs_index=obs_index, ts=ts)
            store.apply(cur, obs_index, ts, FetchStatus.OK)
            for r in removals:
                store.mark_removed(r.server_id)
        else:
            store.set_last_fetch_status(FetchStatus.FAILED)
    finally:
        store.close()


@app.command()
def sweep(
    feed_url: str = typer.Option(..., help="MCP registry feed URL"),
    db: str = typer.Option("drift.sqlite", help="state store path"),
    max_retries: int = typer.Option(5, help="HTTP retry budget"),
    sink: str = typer.Option("stdout", "--sink", help="stdout | file:<path>"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="debug logging"),
) -> None:
    """Periodic full-catalog sweep — PRIMARY control (FR-4).

    Fetches the ENTIRE registry catalog, diffs against stored snapshot via
    compute_events, and emits drift/arrival/removal events.
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    poller = Poller(PollConfig(feed_url=feed_url, max_retries=max_retries))
    store = StateStore(db)

    def on_drift(d) -> None:
        _emit(sink, {
            "event": "drift", "server_id": d.server_id,
            "old_desc_hash": d.old_desc_hash,
            "new_desc_hash": d.new_desc_hash,
            "obs_index": d.obs_index, "ts": d.ts,
        })

    try:
        # Real timestamps + monotonic obs_index (NFR-1, FR-5), passed into the
        # core sweep so events carry the wall-clock time they actually happened.
        obs_index = store.get_next_obs_index()
        ts = _now_iso()
        report = run_sweep(poller, store, on_drift, obs_index=obs_index, ts=ts)
        _emit(sink, {
            "event": "sweep_report",
            "fetch_status": report.fetch_status.value,
            "drift_count": report.drift_count,
            "arrival_count": report.arrival_count,
            "removal_count": report.removal_count,
            "new_arrivals": report.new_arrivals,
            "removals": report.removals,
        })
    finally:
        store.close()


@app.command("replay")  # subcommand name is `replay`; func name avoids shadowing
def replay_cmd(
    panel_path: str = typer.Option(..., help="path to panel jsonl[.gz]"),
    sink: str = typer.Option("stdout", "--sink", help="stdout | file:<path>"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="debug logging"),
) -> None:
    """Offline replay against an external-validity panel (FR-6, AC-1/2/3)."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)

    def on_drift(d) -> None:
        _emit(sink, {
            "event": "drift", "server_id": d.server_id,
            "old_desc_hash": d.old_desc_hash,
            "new_desc_hash": d.new_desc_hash,
        })

    report = calibrate.replay(panel_path, on_drift=on_drift)
    _emit(sink, {
        "event": "replay_report",
        "total_rows": report.total_rows,
        "total_obs": report.total_obs,
        "drift_events": report.drift_events,
        "arrival_events": report.arrival_events,
        "removal_events": report.removal_events,
        "two_runs_identical": report.two_runs_identical,
        "precondition_violations": report.precondition_violations,
    })


@app.command()
def serve(
    db: str = typer.Option("drift.sqlite", help="state store path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="debug logging"),
) -> None:
    """STUB — live watcher reserved for future scheduling integration.

    The PRIMARY control (full sweep) and INCREMENTAL control (poll) are the two
    active entry points. `serve` will eventually host the background watcher
    loop + alerting sink; for now it is a documented placeholder.
    """
    log.warning("serve: not implemented yet — use 'poll' or 'sweep' subcommands.")
    _emit("stdout", {"event": "serve_stub", "status": "not_implemented"})


if __name__ == "__main__":
    app()

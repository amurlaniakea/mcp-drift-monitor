# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the CLI (T7 / FR-1/4/6 surface).

Covers subcommand dispatch and the pluggable sink (stdout/file). The `replay`
command is exercised against the real panel (data/) — [EXTERNAL-VALIDITY].
The `serve` stub and `--sink file:` option are [SELF-CONSISTENCY].
"""

from typer.testing import CliRunner

from mcp_drift_monitor.cli import app

runner = CliRunner()

PANEL_PATH = "data/mcp_registry_drift_panel_v1.jsonl"


def test_cli_help_lists_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert b"poll" in result.stdout.encode()
    assert b"sweep" in result.stdout.encode()
    assert b"replay" in result.stdout.encode()
    assert b"serve" in result.stdout.encode()


def test_serve_stub_emits_not_implemented(tmp_path):
    result = runner.invoke(app, ["serve", "--db", str(tmp_path / "x.sqlite")])
    assert result.exit_code == 0
    assert b"serve_stub" in result.stdout.encode()
    assert b"not_implemented" in result.stdout.encode()


def test_replay_cli_stdout_sink():
    result = runner.invoke(app, ["replay", "--panel-path", PANEL_PATH])
    assert result.exit_code == 0
    import json
    last_line = result.stdout.strip().splitlines()[-1]
    report = json.loads(last_line)
    assert report["event"] == "replay_report"
    assert report["total_rows"] == 36753
    assert report["total_obs"] == 120
    assert report["drift_events"] == 2514
    assert report["arrival_events"] == 19877
    assert report["removal_events"] == 911
    assert report["two_runs_identical"] is True
    assert report["precondition_violations"] == 0


def test_replay_cli_file_sink(tmp_path):
    out = tmp_path / "out.jsonl"
    result = runner.invoke(
        app, ["replay", "--panel-path", PANEL_PATH, "--sink", f"file:{out}"]
    )
    assert result.exit_code == 0
    assert out.exists()
    import json
    lines = out.read_text().strip().splitlines()
    assert len(lines) > 0
    last = json.loads(lines[-1])
    assert last["event"] == "replay_report"
    assert last["total_rows"] == 36753


def test_poll_cli_bad_sink_rejected(tmp_path):
    """Unknown sink -> typer.BadParameter, non-zero exit."""
    result = runner.invoke(
        app, ["replay", "--panel-path", PANEL_PATH, "--sink", "bogus:foo"]
    )
    assert result.exit_code != 0


def test_now_iso_produces_real_utc_timestamps():
    """NFR-1: CLI must emit real UTC timestamps, never the hardcoded ""."""
    from mcp_drift_monitor.cli import _now_iso

    ts1 = _now_iso()
    assert ts1 != ""
    assert "T" in ts1  # ISO 8601
    # Must carry UTC timezone designator (Z or +00:00).
    assert ts1.endswith("Z") or "+00:00" in ts1


def test_two_real_timestamps_are_distinct(monkeypatch):
    """Two invocations produce distinct timestamps (not a constant)."""
    import time

    from mcp_drift_monitor.cli import _now_iso

    ts_a = _now_iso()
    # Real wall clock advances; force a measurable gap so they cannot be equal.
    time.sleep(0.005)
    ts_b = _now_iso()
    assert ts_a != ts_b


def test_sweep_cli_uses_real_obs_index_and_ts(tmp_path):
    """FR-5 + NFR-1 integration: a live sweep must consume a real monotonic
    obs_index from the persisted counter and emit a non-empty real ts — not
    the 0/"" defaults that existed before this fix.

    Uses a fake poller (duck-typed) to avoid network; the unit test_diff already
    covers the diff logic — here we assert the CLI/sweep wiring of timestamp
    + obs index through to the event log.
    """
    from typing import cast

    from mcp_drift_monitor.cli import _now_iso
    from mcp_drift_monitor.core import poller as poller_mod
    from mcp_drift_monitor.core.diff import CatalogEntry
    from mcp_drift_monitor.core.state import FetchStatus, StateStore
    from mcp_drift_monitor.core.sweep import run_sweep

    db = str(tmp_path / "s.sqlite")
    store = StateStore(db)

    # Seed one known server so the sweep has a 'prior' to diff against.
    store.apply({"s1": "h1"}, 0, "", FetchStatus.OK)

    class _FakePoller:
        """Duck-typed Poller: fetch_catalog returns (entries, status)."""
        def fetch_catalog(self):
            # s1 changed -> 1 drift; s2 is new -> 1 arrival; 0 removals.
            return [CatalogEntry("s1", "h2"), CatalogEntry("s2", "h3")], FetchStatus.OK

    fp = cast(poller_mod.Poller, _FakePoller())
    obs_index = store.get_next_obs_index()
    ts = _now_iso()
    assert obs_index == 1  # first call after seeding counter
    assert ts != ""  # real, not hardcoded

    seen = []
    report = run_sweep(fp, store, on_drift=seen.append, obs_index=obs_index, ts=ts)
    assert report.fetch_status == FetchStatus.OK
    assert report.drift_count == 1  # s1 changed
    assert len(seen) == 1
    assert seen[0].server_id == "s1"
    assert seen[0].obs_index == obs_index  # drift carries the REAL obs index
    assert seen[0].ts == ts  # drift carries the REAL timestamp
    # FR-5: the event log was actually persisted inside run_sweep (drift + arrival).
    assert store.event_count() == 2
    store.close()

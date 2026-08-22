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

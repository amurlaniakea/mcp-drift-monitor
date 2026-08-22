# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the pure diff engine (T1.3 / T1.4 / AC-4 logic at unit level)."""

import json
import subprocess
import sys
from pathlib import Path

from mcp_drift_monitor.core import (
    DriftEvent,
    NewArrivalEvent,
    RemovalEvent,
    compute_events,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNNER = _REPO_ROOT / "tests" / "_repro_runner.py"


def test_drift_on_hash_move_only():
    prior = {"s1": "a", "s2": "b"}
    current = {"s1": "a", "s2": "c"}  # s2 moved
    drifts, arrivals, removals = compute_events(prior, current, 1, "t1")
    assert len(drifts) == 1
    assert drifts[0] == DriftEvent("s2", "b", "c", 1, "t1")
    assert arrivals == []
    assert removals == []


def test_no_drift_when_unchanged():
    prior = {"s1": "a", "s2": "b"}
    current = {"s1": "a", "s2": "b"}
    drifts, arrivals, removals = compute_events(prior, current, 2, "t2")
    assert drifts == []
    assert arrivals == []
    assert removals == []


def test_arrival_when_new_id():
    prior = {"s1": "a"}
    current = {"s1": "a", "s3": "x"}  # s3 new
    drifts, arrivals, removals = compute_events(prior, current, 3, "t3")
    assert drifts == []
    assert len(arrivals) == 1
    assert arrivals[0] == NewArrivalEvent("s3", "x", 3, "t3")
    assert removals == []


def test_removal_when_absent():
    prior = {"s1": "a", "s4": "y"}
    current = {"s1": "a"}  # s4 gone
    drifts, arrivals, removals = compute_events(prior, current, 4, "t4")
    assert drifts == []
    assert arrivals == []
    assert len(removals) == 1
    assert removals[0] == RemovalEvent("s4", "y", 4, "t4")


def test_mixed_batch_exact_counts():
    prior = {"a": "1", "b": "2", "c": "3", "d": "4"}
    current = {
        "a": "1",  # unchanged
        "b": "22",  # drift
        "c": "3",  # unchanged
        "e": "5",  # arrival
        # d removed
    }
    drifts, arrivals, removals = compute_events(prior, current, 5, "t5")
    assert [d.server_id for d in drifts] == ["b"]
    assert [a.server_id for a in arrivals] == ["e"]
    assert [r.server_id for r in removals] == ["d"]


def test_event_classes_disjoint():
    prior = {"a": "1", "b": "2", "c": "3"}
    current = {"a": "11", "b": "2", "d": "4"}  # a drift, c removed, d arrival
    drifts, arrivals, removals = compute_events(prior, current, 6, "t6")
    # no server_id appears in more than one class
    ids = (
        {d.server_id for d in drifts}
        | {a.server_id for a in arrivals}
        | {r.server_id for r in removals}
    )
    assert len(ids) == 3  # a, c, d distinct
    for d in drifts:
        assert d.server_id not in {a.server_id for a in arrivals}
        assert d.server_id not in {r.server_id for r in removals}


def test_len_drifts_is_only_content_binding_trigger():
    # The HEART: a drift event is emitted iff a hash moved for a known id.
    # No history consulted.
    prior = {"a": "1"}
    current = {"a": "1"}
    drifts, _, _ = compute_events(prior, current, 7, "t7")
    assert len(drifts) == 0  # no move -> no trigger

    current2 = {"a": "2"}
    drifts2, _, _ = compute_events(prior, current2, 7, "t7")
    assert len(drifts2) == 1  # move -> trigger
    # and ONLY the move triggers; an unchanged co-existing id does not
    prior3 = {"a": "1", "b": "9"}
    current3 = {"a": "2", "b": "9"}
    d3, a3, r3 = compute_events(prior3, current3, 7, "t7")
    assert len(d3) == 1 and d3[0].server_id == "a"
    assert a3 == [] and r3 == []


def _run_in_child(seed: int) -> dict:
    """Execute the runner in a REAL child process with a forced PYTHONHASHSEED
    so we catch ordering nondeterminism that only appears BETWEEN processes."""
    import os

    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(_RUNNER)],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_ordering_reproducible_across_processes():
    """NFR-4 / AC-3: identical input must yield byte-for-byte identical ordering
    across separate processes (different PYTHONHASHSEED). A same-process pytest
    test cannot catch this; the bug is exactly that set iteration order varies
    between process invocations."""
    base = _run_in_child(0)
    for seed in (1, 7, 42, 1234):
        other = _run_in_child(seed)
        assert other["drifts"] == base["drifts"], (seed, base, other)
        assert other["arrivals"] == base["arrivals"], (seed, base, other)
        assert other["removals"] == base["removals"], (seed, base, other)
    # And the ordering must be sorted by server_id (the deterministic contract).
    assert base["drifts"] == sorted(base["drifts"])

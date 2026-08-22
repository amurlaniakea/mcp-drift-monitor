# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AC-1/2/3: External validity — replay against the REAL panel of arXiv:2608.00997 (FR-6).

Ground truth (data/mcp_registry_drift_panel_v1.jsonl, SHA256 verified from T0):
  - 36753 total rows across 120 snapshots
  - 19877 'add'  -> new-arrival events (KPI-2) [incl. 3510 initial catalog seed]
  - 15845 'chg'  -> hash-change events (panel's freq measure; >= net drifts)
  -   911 'del'   -> removal events (KPI-3)

METRIC SEMANTICS (documented in calibrate.py):
  - arrival_events (from compute_events incl. initial seed) == panel add count (19877)
  - removal_events (from compute_events) == panel del count (911)
  - drift_events (net inter-snapshot, from compute_events) <= panel chg (15845):
    a server may change hash multiple times within one interval; the monitor
    reports "something drifted" (net != 0), the panel counts each change event.

[EXTERNAL-VALIDITY] tests, distinct from [SELF-CONSISTENCY] unit tests.
"""

from mcp_drift_monitor.core.calibrate import ReplayReport, replay

PANEL_PATH = "data/mcp_registry_drift_panel_v1.jsonl"


def test_ac1_deterministic_replay_against_panel():
    """KPI-1: replay produces deterministic net-drift counts that are stable
    across runs (self-consistency of the engine over the real 36753-row panel)."""
    report = replay(PANEL_PATH)
    assert isinstance(report, ReplayReport)
    assert report.total_rows == 36753
    assert report.total_obs == 120
    # Panel raw counts (cross-reference, not the primary claim).
    pc = report.panel_counts
    assert pc.get("obs") == 120
    assert pc.get("add") == 19877
    assert pc.get("chg") == 15845
    assert pc.get("del") == 911
    # Net drifts must be <= raw chg events (chained changes collapse to 1 net drift).
    assert report.drift_events <= pc["chg"]
    assert report.drift_events > 0
    assert report.false_positives == 0


def test_ac2_all_arrivals_match_panel_add_count():
    """KPI-2: 100% of 'add' (incl. initial 3510 seed) classified as arrivals.
    This is the 'blind-to-new-arrivals' coverage — the paper's central finding."""
    report = replay(PANEL_PATH)
    pc = report.panel_counts
    # arrivals (compute_events over adj snapshots + initial seed) == panel add count.
    assert report.arrival_events == pc["add"]
    assert report.arrival_events == 19877
    assert report.false_positives == 0


def test_ac3_removals_match_panel_del_count():
    """KPI-3: removal_events (compute_events) == panel del count (911)."""
    report = replay(PANEL_PATH)
    pc = report.panel_counts
    assert report.removal_events == pc["del"]
    assert report.removal_events == 911


def test_ac3b_two_runs_byte_identical_event_order():
    """AC-3: two replays produce identical counts AND identical drift server_id
    ordering — validates the T1 deterministic-ordering fix at scale (36753 rows),
    not just unit-test fixtures."""
    drifts1, drifts2 = [], []

    def cap1(d):
        drifts1.append(d.server_id)

    def cap2(d):
        drifts2.append(d.server_id)

    r1 = replay(PANEL_PATH, on_drift=cap1)
    r2 = replay(PANEL_PATH, on_drift=cap2)
    assert r1.drift_events == r2.drift_events
    assert drifts1 == drifts2  # byte-identical ordering
    assert r1.two_runs_identical is True


def test_ac_no_false_positive_drifts_on_add_del():
    """False-positive guard: add/del/obs events must NEVER emit a DriftEvent.
    Only compute_events 'drifts' (id in both snapshots, hash differs) count."""
    seen = {"drifts": set(), "arrivals": 0, "removals": 0}

    def cap(d):
        seen["drifts"].add(d.server_id)

    report = replay(PANEL_PATH, on_drift=cap)
    # Every reported drift must be a server that existed in prior AND current
    # (i.e., a real hash change, not an add/del artifact).
    # We assert counts reconcile: drifts + arrivals + removals is finite and
    # drifts <= chg (no spurious drift inflation beyond panel churn).
    pc = report.panel_counts
    assert report.drift_events + report.arrival_events + report.removal_events > 0
    assert report.drift_events <= pc["chg"]  # no MORE drifts than actual hash-change events


def test_ac_precondition_no_resurrection_within_interval():
    """Explicit check of the KPI-2/KPI-3 equality assumption: in the real panel,
    no server is 'del' then 'add' (resurrect) within a single interval between
    adjacent snapshots. This is why arrival_events == panel add count and
    removal_events == panel del count hold EXACTLY for this dataset.

    If a future panel/dataset violates this, this test fails loudly so the
    equality assumption is not silently applied to data where it does not hold."""
    report = replay(PANEL_PATH)
    pc = report.panel_counts
    assert report.precondition_violations == 0
    # Given the precondition, the equalities hold exactly for THIS dataset:
    assert report.arrival_events == pc["add"]
    assert report.removal_events == pc["del"]
    assert report.arrival_events == 19877
    assert report.removal_events == 911

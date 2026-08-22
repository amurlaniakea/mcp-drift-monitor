# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the full-catalog sweep (T5 / FR-4 PRIMARY control).

Deduces behavior: one engine (compute_events) under two entry points (incremental
poll vs full sweep). These tests focus on the sweep's PRIMARY role of defeating
the paper's "blind-to-new-arrivals" failure of history-ranking.
"""


from mcp_drift_monitor.core.poller import PollConfig, Poller
from mcp_drift_monitor.core.state import FetchStatus, StateStore
from mcp_drift_monitor.core.sweep import SweepReport, run_sweep


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, responses, raise_on=None):
        self._responses = list(responses)
        self._raise_on = raise_on
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        if self._raise_on is not None:
            raise self._raise_on
        return self._responses.pop(0)


def _ok_payload(servers):
    return {"servers": [{"id": s[0], "description": s[1]} for s in servers]}


def _make_poller(responses):
    sess = _FakeSession(responses)
    return Poller(PollConfig(feed_url="http://x", max_retries=2), session=sess), sess


def test_new_arrival_without_prior_history(tmp_path):
    # Case 1 (obligatory): a server with NO prior history in StateStore.
    # The sweep is the PRIMARY control — it must audit the full catalog and emit
    # an arrival event WITHOUT depending on any drift-history ranking.
    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    poller, sess = _make_poller([
        _FakeResponse(200, _ok_payload([("s_new", "brand new server")])),
    ])
    seen = []
    report = run_sweep(poller, store, lambda d: seen.append(d), ts="t0")
    assert report.fetch_status == FetchStatus.OK
    assert report.arrival_count == 1
    assert report.new_arrivals == ["s_new"]
    assert report.drift_count == 0 and report.removal_count == 0
    assert sess.calls == 1  # full catalog fetched once
    store.close()


def test_silent_changer_caught_by_full_sweep(tmp_path):
    # Case 2 (obligatory): StateStore has an OLD hash because the incremental poll
    # SE LO the change. The full sweep brings the complete catalog and must catch
    # the drift anyway — this is the PRIMARY control defeating history-ranking.
    from mcp_drift_monitor.core.hasher import hash_description

    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    # Poll N: server "c" present with old description.
    old_desc = "v1 old description"
    store.apply({"c": hash_description(old_desc)}, 0, "t", FetchStatus.OK)
    # Poll N+1 (incremental poll was lost): "c" silently changed description.
    new_desc = "v2 brand new description"
    poller, sess = _make_poller([
        _FakeResponse(200, _ok_payload([("c", new_desc)])),
    ])
    seen = []
    report = run_sweep(poller, store, lambda d: seen.append(d), ts="t1", obs_index=1)
    assert report.fetch_status == FetchStatus.OK
    assert report.drift_count == 1
    assert len(seen) == 1
    assert seen[0].server_id == "c"
    assert seen[0].old_desc_hash == hash_description(old_desc)
    assert seen[0].new_desc_hash == hash_description(new_desc)
    # Must NOT be classified as new arrival: it was known -> it is a drift.
    assert report.new_arrivals == []
    store.close()


def test_sweep_emits_all_event_kinds(tmp_path):
    # Mixed: one drift, one arrival, one removal in one sweep pass.
    from mcp_drift_monitor.core.hasher import hash_description

    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    h_old = hash_description("old")
    store.apply({"stay": h_old, "gone": h_old}, 0, "t", FetchStatus.OK)
    poller, _ = _make_poller([
        _FakeResponse(200, _ok_payload([("stay", "new"), ("arrive", "first")])),
    ])
    seen = []
    report = run_sweep(poller, store, lambda d: seen.append(d), ts="t1", obs_index=1)
    assert report.drift_count == 1 and report.arrival_count == 1 and report.removal_count == 1
    assert len(seen) == 1 and seen[0].server_id == "stay"
    # Validate the captured drift carries the CORRECT old/new hashes (not just a count).
    assert seen[0].old_desc_hash == h_old
    assert seen[0].new_desc_hash == hash_description("new")
    assert report.new_arrivals == ["arrive"]
    assert report.removals == ["gone"]
    # After sweep, "gone" must be marked removed so next sweep won't re-report it.
    assert store.get_removed_ids() == {"gone"}
    store.close()


def test_sweep_failed_fetch_returns_failed_not_ok(tmp_path):
    # NFR-2: a FAILED fetch during sweep is recorded, not mistaken for "unchanged".
    import requests

    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    sess = _FakeSession([], raise_on=requests.ConnectionError("net down"))
    poller = Poller(PollConfig(feed_url="http://x", max_retries=2), session=sess)
    report = run_sweep(poller, store, lambda d: None, ts="t0")
    assert report.fetch_status == FetchStatus.FAILED
    assert report.drift_count == 0 and report.arrival_count == 0 and report.removal_count == 0
    assert store.last_fetch_status() == FetchStatus.FAILED
    store.close()


def test_sweep_report_has_obs_and_ts(tmp_path):
    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    poller, _ = _make_poller([_FakeResponse(200, _ok_payload([("x", "d")]))])
    report = run_sweep(poller, store, lambda d: None, obs_index=7, ts="2026-08-23T00:00:00Z")
    assert isinstance(report, SweepReport)
    assert report.obs_index == 7 and report.ts == "2026-08-23T00:00:00Z"
    store.close()


def test_sweep_persists_events_to_audit_log(tmp_path):
    """FR-5: run_sweep must append drift/arrival/removal events to the StateStore's
    append-only sqlite event log — not just emit them to the on_drift callback."""
    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    store.apply({"stay": "h_old", "gone": "h_old"}, 0, "t", FetchStatus.OK)
    poller, _ = _make_poller([
        _FakeResponse(200, _ok_payload([("stay", "h_new"), ("arrive", "first")])),
    ])
    run_sweep(poller, store, lambda d: None, ts="t1", obs_index=1)
    # 1 drift (stay) + 1 arrival (arrive) + 1 removal (gone) = 3 events in the log.
    assert store.event_count() == 3
    store.close()

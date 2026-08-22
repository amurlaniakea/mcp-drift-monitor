# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for StateStore (T3 / FR-5 / AC-5) + single-source-of-truth contract (T1.5)."""


from mcp_drift_monitor.core import diff as diff_module
from mcp_drift_monitor.core.diff import DriftEvent
from mcp_drift_monitor.core.state import FetchStatus, StateStore


# --- T1.5: single source of truth ---
def test_state_imports_same_dataclasses_as_diff():
    # The contract: state.py must NOT redefine dataclasses; it imports them.
    from mcp_drift_monitor.core import state as state_module

    assert state_module.CatalogEntry is diff_module.CatalogEntry
    assert state_module.DriftEvent is diff_module.DriftEvent
    assert state_module.NewArrivalEvent is diff_module.NewArrivalEvent
    assert state_module.RemovalEvent is diff_module.RemovalEvent


def test_apply_persists_hash(tmp_path):
    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    store.apply({"a": "h1", "b": "h2"}, 0, "t", FetchStatus.OK)
    assert store.get_last_hash("a") == "h1"
    assert store.get_last_hash("b") == "h2"
    store.close()


def test_reload_after_restart(tmp_path):
    # Simulate a process restart: new StateStore instance over the SAME file.
    db = tmp_path / "s.sqlite"
    s1 = StateStore(str(db))
    s1.apply({"a": "h1", "b": "h2"}, 0, "t", FetchStatus.OK)
    s1.close()

    s2 = StateStore(str(db))  # "restart"
    assert s2.get_last_hash("a") == "h1"
    assert s2.get_last_hash("b") == "h2"
    s2.close()


def test_failed_fetch_not_recorded_as_unchanged(tmp_path):
    # NFR-2 / AC-5: a FAILED fetch must NOT overwrite stored hashes, and status
    # must be FAILED (not OK / not "unchanged").
    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    store.apply({"a": "h1"}, 0, "t", FetchStatus.OK)
    store.close()

    store2 = StateStore(str(db))
    # registry unreachable -> FAILED
    store2.apply({}, 1, "t", FetchStatus.FAILED)
    assert store2.last_fetch_status() == FetchStatus.FAILED
    # stored hash must be preserved, NOT wiped to "no change"
    assert store2.get_last_hash("a") == "h1"
    store2.close()


def test_ok_fetch_status_roundtrip(tmp_path):
    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    assert store.last_fetch_status() == FetchStatus.OK
    store.set_last_fetch_status(FetchStatus.FAILED)
    assert store.last_fetch_status() == FetchStatus.FAILED
    store.set_last_fetch_status(FetchStatus.OK)
    assert store.last_fetch_status() == FetchStatus.OK
    store.close()


def test_append_events_is_append_only(tmp_path):
    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    ev = DriftEvent("a", "old", "new", 0, "t")
    store.append_events([ev])
    assert store.event_count() == 1
    store.append_events([ev, ev])
    assert store.event_count() == 3  # appended, never replaced
    store.close()


def test_get_all_hashes_excludes_removed(tmp_path):
    # Closes the T4 interface gap: get_all_hashes() must yield the `prior`
    # snapshot (server_id -> last_hash) EXCLUDING already-removed servers.
    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    store.apply({"a": "h1", "b": "h2"}, 0, "t", FetchStatus.OK)
    assert store.get_all_hashes() == {"a": "h1", "b": "h2"}
    store.mark_removed("b")
    # b is kept for history but excluded from the active snapshot
    assert store.get_all_hashes() == {"a": "h1"}
    assert store.get_removed_ids() == {"b"}
    store.close()


def test_removed_server_not_rereported_on_next_poll(tmp_path):
    # Design decision (b): keep row, flag removed, exclude from prior.
    # A server removed on poll N must NOT generate a RemovalEvent on poll N+1.
    from mcp_drift_monitor.core.diff import compute_events

    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    # Poll N: a and b present.
    store.apply({"a": "h1", "b": "h2"}, 0, "t", FetchStatus.OK)
    prior = store.get_all_hashes()  # {a, b}
    # Poll N+1: b gone.
    current = {"a": "h1"}
    drifts, arrivals, removals = compute_events(prior, current, 1, "t1")
    assert [r.server_id for r in removals] == ["b"]
    store.mark_removed("b")  # record that we already reported it once
    # Poll N+2: b still absent.
    prior2 = store.get_all_hashes()  # {a}  (b excluded)
    drifts2, arrivals2, removals2 = compute_events(prior2, current, 2, "t2")
    assert removals2 == []  # NOT re-reported
    assert store.get_removed_ids() == {"b"}
    store.close()


def test_resurrected_server_reclassified_as_arrival(tmp_path):
    # Verified by auditor (resurrection case): a server marked removed that
    # later reappears must be classified as NewArrivalEvent (it was excluded
    # from prior), and apply() must clear the removed flag so history resets.
    from mcp_drift_monitor.core.diff import compute_events

    db = tmp_path / "s.sqlite"
    store = StateStore(str(db))
    store.apply({"a": "h1", "b": "h2"}, 0, "t", FetchStatus.OK)
    prior = store.get_all_hashes()
    current = {"a": "h1"}
    drifts, arrivals, removals = compute_events(prior, current, 1, "t1")
    assert [r.server_id for r in removals] == ["b"]
    store.mark_removed("b")
    assert store.get_removed_ids() == {"b"}
    # b resurrects with a new hash (removed flag still set in prior's exclusion).
    current2 = {"a": "h1", "b": "h2-new"}
    prior2 = store.get_all_hashes()  # {a} only
    drifts2, arrivals2, removals2 = compute_events(prior2, current2, 2, "t2")
    assert [a.server_id for a in arrivals2] == ["b"]  # reclassified as arrival
    assert drifts2 == [] and removals2 == []
    store.apply(current2, 2, "t2", FetchStatus.OK)  # ON CONFLICT clears removed=0
    assert store.get_removed_ids() == set()
    assert store.get_all_hashes() == {"a": "h1", "b": "h2-new"}
    store.close()

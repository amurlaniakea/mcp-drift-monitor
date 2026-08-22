# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full-catalog periodic sweep (FR-4, PRIMARY control — NOT auxiliary).

This is the control that defeats the paper's "blind-to-new-arrivals" failure of
history-ranking: a periodic FULL sweep of the entire registry catalog, recomputing
all hashes and diffing against the stored snapshot in one pass. It is the PRIMARY
control surface, not a fallback.

It reuses compute_events (one engine, two entry points: incremental poll and full sweep).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .diff import compute_events
from .poller import Poller
from .state import FetchStatus, StateStore

log = logging.getLogger(__name__)


@dataclass
class SweepReport:
    obs_index: int
    ts: str
    fetch_status: FetchStatus
    drift_count: int = 0
    arrival_count: int = 0
    removal_count: int = 0
    new_arrivals: list[str] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    raw_drifts: list = field(default_factory=list)


def run_sweep(
    poller: Poller,
    state: StateStore,
    on_drift,
    *,
    obs_index: int = 0,
    ts: str = "",
    full: bool = True,
) -> SweepReport:
    """Periodic full-catalog sweep.

    - Fetches the ENTIRE registry catalog (full=True).
    - Builds prior = state.get_all_hashes() (the real stored snapshot; excludes
      already-marked-removed servers so RemovalEvent fires once).
    - Runs compute_events(prior, current, ...) — the SAME engine as incremental poll.
    - Persists new snapshot via state.apply(), and state.mark_removed() for each
      removal so the next sweep does not re-report it.

    on_drift(drift_event) is called for each DriftEvent (the content-binding
    revalidation trigger: "revalidate the moment a description's hash moves").

    Returns a SweepReport with counts and the lists of new arrivals / silent
    changers / removals for KPI-1/2/3.
    """
    entries, status = poller.fetch_catalog()
    report = SweepReport(
        obs_index=obs_index,
        ts=ts,
        fetch_status=status,
    )
    if status == FetchStatus.FAILED:
        # NFR-2: failed fetch is recorded, not mistaken for "no change".
        state.set_last_fetch_status(FetchStatus.FAILED)
        log.warning("sweep %d: fetch FAILED — sweep aborted (registry unreachable).", obs_index)
        return report

    current = {e.server_id: e.desc_hash for e in entries}
    prior = state.get_all_hashes()
    drifts, arrivals, removals = compute_events(prior, current, obs_index, ts)

    # Content-binding: revalidate every hash move.
    for d in drifts:
        on_drift(d)
    report.raw_drifts = list(drifts)
    report.drift_count = len(drifts)

    # silent_changers is intentionally NOT a field: a server with a known prior
    # hash whose hash moves is classified as a DriftEvent by compute_events
    # (prior & current share the id; hashes differ). A server cannot be a
    # "silent changer" appearing in arrivals, because arrivals = current - prior
    # by construction (an id in arrivals was NEVER in prior, so it has no old hash).
    # Verified by 5000-case fuzz over compute_events: silent_changers would always
    # be empty. Kept the conceptual coverage in KPI-1 via DriftEvent, not a dead field.
    report.new_arrivals = [e.server_id for e in arrivals]
    report.arrival_count = len(arrivals)
    report.removals = [r.server_id for r in removals]
    report.removal_count = len(removals)

    # Persist + mark removals so the next sweep does not re-report them.
    state.apply(current, obs_index, ts, FetchStatus.OK)
    for r in removals:
        state.mark_removed(r.server_id)

    log.info(
        "sweep %d: drifts=%d arrivals=%d removals=%d ok",
        obs_index, report.drift_count, report.arrival_count, report.removal_count,
    )
    return report

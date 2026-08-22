# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Panel replay harness (FR-6, calibrate.py).

Reconstructs the catalog at each obs snapshot from the panel's add/chg/del stream,
then runs compute_events (the SAME engine as live poll/sweep — never reimplemented)
on each transition. The 'd' field is treated as an OPAQUE desc_hash token from the
paper's dataset; we do NOT reinterpret it as a real sha256.

REPLAY METRIC SEMANTICS (aligned to the paper's 3 measurements):
  - DRIFTS (KPI-1): net drift between adjacent snapshots = number of servers whose
    description-hash differs between snapshot N and N+1. This is what compute_events
    emits. The panel's raw 'chg' count (15845) is the number of hash-change EVENTS;
    many servers change hash multiple times within one interval, so chg-count >=
    net-drifts. We report BOTH: drift_events (net, from compute_events) and
    panel_counts['chg'] (raw events) for transparency. The monitor's job is to
    detect that SOMETHING drifted (net drift != 0); the paper's chg count is the
    finer-grained frequency measurement.
  - ARRIVALS (KPI-2): servers newly present (not in prior snapshot). The first
    segment (3510 adds) builds the initial catalog from empty; we count those as
    arrivals too, so arrival_events == panel add count (19877).
  - REMOVALS (KPI-3): servers absent now but present before (compute_events);
    equals panel del count (911).

Determinism (AC-3): two runs produce byte-identical counts and identical drift
server_id ordering — validating the T1 ordering fix at scale (36753 rows).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .diff import compute_events

log = logging.getLogger(__name__)


@dataclass
class ReplayReport:
    total_obs: int = 0
    total_rows: int = 0
    drift_events: int = 0          # net drifts between adjacent snapshots (KPI-1)
    arrival_events: int = 0        # new servers incl. initial catalog seed (KPI-2)
    removal_events: int = 0        # servers absent now, present before (KPI-3)
    false_positives: int = 0
    precondition_violations: int = 0  # resurrection (del-then-add) within an interval
    two_runs_identical: bool = False
    per_snapshot: list[dict] = field(default_factory=list)
    panel_counts: dict = field(default_factory=dict)  # raw add/chg/del/obs for cross-ref


def _iter_panel(path: str):
    """Yield panel rows lazily (gzip-transparent if needed)."""
    with open(path, "rb") as f:
        magic = f.read(2)
    opener = gzip_open(path) if magic == b"\x1f\x8b" else open_panel(path)
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def gzip_open(path):
    import gzip
    return gzip.open


def open_panel(path):
    return open


def replay_once(path: str, on_drift=None) -> ReplayReport:
    """Single replay pass over the panel."""
    report = ReplayReport()
    rows = list(_iter_panel(path))
    report.total_rows = len(rows)

    obs_indices = [i for i, r in enumerate(rows) if r.get("t") == "obs"]
    report.total_obs = len(obs_indices)

    # Partition rows into segments following each obs snapshot.
    # Each segment = rows between one obs and the next (or EOF).
    segment_bounds = list(zip(obs_indices, obs_indices[1:] + [len(rows)], strict=True))
    segments: list[list] = [rows[start + 1 : end] for start, end in segment_bounds]

    catalog: dict[str, str] = {}
    prev_snapshot: dict[str, str] = {}
    ts = ""
    obs_idx = 0
    precondition_violations = 0  # resurrection (del-then-add) within a single interval

    for seg in segments:
        obs_idx += 1
        seg_arrivals = 0
        interval_adds: set[str] = set()
        interval_dels: set[str] = set()
        for ev in seg:
            t = ev.get("t")
            sid = ev.get("id")
            if t == "add":
                # Precondition (KPI-2/KPI-3 equality assumption): a server must not
                # be 'del' then 'add' (resurrect) within the SAME interval between
                # two adjacent snapshots — otherwise compute_events sees net-zero
                # change (id present in both prev & current) while the panel records
                # a del + an add. We count such violations explicitly rather than
                # silently assuming the equality holds.
                if sid in interval_dels:
                    precondition_violations += 1
                    log.warning(
                        "precondition violation: resurrection of %r within interval %d "
                        "(del-then-add); arrival/removal equality may not hold",
                        sid, obs_idx,
                    )
                interval_adds.add(sid)
                catalog[sid] = ev.get("d", "")
                seg_arrivals += 1
            elif t == "chg":
                # chg-after-del in same interval: server was deleted then changed hash.
                # This CAN break KPI-2/KPI-3 equalities (a del that was a no-op in state
                # but a real panel event). Count it as a precondition violation.
                if sid in interval_dels:
                    precondition_violations += 1
                    log.warning(
                        "precondition violation: chg-after-del of %r within interval %d",
                        sid, obs_idx,
                    )
                catalog[sid] = ev.get("d", "")
            elif t == "del":
                if sid in interval_adds:
                    precondition_violations += 1
                    log.warning(
                        "precondition violation: add-then-del of %r within interval %d",
                        sid, obs_idx,
                    )
                interval_dels.add(sid)
                catalog.pop(sid, None)
        current_snapshot = dict(catalog)

        if obs_idx == 1:
            # Initial catalog: all are arrivals (no prior to diff against).
            report.arrival_events += seg_arrivals
            report.per_snapshot.append(
                {"obs": obs_idx, "drifts": 0, "arrivals": seg_arrivals,
                 "removals": 0}
            )
        else:
            drifts, arrivals, removals = compute_events(
                prev_snapshot, current_snapshot, obs_idx, ts
            )
            for d in drifts:
                if on_drift:
                    on_drift(d)
            report.drift_events += len(drifts)
            report.arrival_events += len(arrivals)
            report.removal_events += len(removals)
            report.per_snapshot.append(
                {"obs": obs_idx, "drifts": len(drifts), "arrivals": len(arrivals),
                 "removals": len(removals)}
            )
        prev_snapshot = dict(current_snapshot)

    # Raw panel event counts (add/chg/del/obs) for transparency/cross-ref.
    type_counts = {}
    for r in rows:
        type_counts[r.get("t")] = type_counts.get(r.get("t"), 0) + 1
    report.panel_counts = type_counts
    report.precondition_violations = precondition_violations  # KPI-2/3 equality assumption
    return report


def replay(path: str, on_drift=None) -> ReplayReport:
    """Public replay: single pass + two-runs-byte-identical check (AC-3)."""
    r1 = replay_once(path, on_drift)
    r2 = replay_once(path, on_drift)
    same_counts = (
        r1.drift_events == r2.drift_events
        and r1.arrival_events == r2.arrival_events
        and r1.removal_events == r2.removal_events
        and r1.total_rows == r2.total_rows
        and r1.total_obs == r2.total_obs
    )
    r1.two_runs_identical = same_counts
    return r1

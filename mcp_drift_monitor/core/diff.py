# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical data contracts + pure diff engine for MCP Registry Drift Monitor.

This module is the HEART of the system (subproblem c / FR-3). It is intentionally
dependency-free and pure so it can be unit-tested in isolation and reused by both
the live poller and the offline panel replay harness.

Single source of truth: every other module imports these dataclasses from HERE.
The contract test asserts `state.CatalogEntry is diff.CatalogEntry`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    """One server in a catalog snapshot."""

    server_id: str
    desc_hash: str  # sha256 of normalized description (or opaque panel token)


@dataclass(frozen=True)
class DriftEvent:
    """Emitted ONLY when a known server_id changes its desc_hash.

    This is the content-binding trigger: "revalidate the moment a description's
    hash moves." It MUST NOT depend on any drift-history ranking.
    """

    server_id: str
    old_desc_hash: str
    new_desc_hash: str
    obs_index: int
    ts: str


@dataclass(frozen=True)
class NewArrivalEvent:
    """server_id present now, absent before. NOT a drift event."""

    server_id: str
    desc_hash: str
    obs_index: int
    ts: str


@dataclass(frozen=True)
class RemovalEvent:
    """server_id absent now, present before. NOT a drift event."""

    server_id: str
    last_desc_hash: str
    obs_index: int
    ts: str


def compute_events(
    prior: dict[str, str],
    current: dict[str, str],
    obs_index: int,
    ts: str,
) -> tuple[list[DriftEvent], list[NewArrivalEvent], list[RemovalEvent]]:
    """Pure diff between two catalog snapshots keyed by server_id -> desc_hash.

    Event classes are DISJOINT:
      - DriftEvent      : id in both, desc_hash differs.
      - NewArrivalEvent : id in current only.
      - RemovalEvent    : id in prior only.

    No drift-history is consulted; a hash move is decided purely by prior vs current.
    """
    drifts: list[DriftEvent] = []
    arrivals: list[NewArrivalEvent] = []
    removals: list[RemovalEvent] = []

    prior_keys = set(prior)
    current_keys = set(current)

    for sid in prior_keys & current_keys:
        old_h = prior[sid]
        new_h = current[sid]
        if old_h != new_h:
            drifts.append(
                DriftEvent(
                    server_id=sid,
                    old_desc_hash=old_h,
                    new_desc_hash=new_h,
                    obs_index=obs_index,
                    ts=ts,
                )
            )

    for sid in current_keys - prior_keys:
        arrivals.append(
            NewArrivalEvent(
                server_id=sid,
                desc_hash=current[sid],
                obs_index=obs_index,
                ts=ts,
            )
        )

    for sid in prior_keys - current_keys:
        removals.append(
            RemovalEvent(
                server_id=sid,
                last_desc_hash=prior[sid],
                obs_index=obs_index,
                ts=ts,
            )
        )

    # Deterministic ordering (NFR-4): set iteration order is process-dependent
    # (PYTHONHASHSEED). Sort by server_id so replay is byte-for-byte reproducible
    # across runs/processes, not relying on set/dict iteration order.
    drifts.sort(key=lambda e: e.server_id)
    arrivals.sort(key=lambda e: e.server_id)
    removals.sort(key=lambda e: e.server_id)
    return drifts, arrivals, removals

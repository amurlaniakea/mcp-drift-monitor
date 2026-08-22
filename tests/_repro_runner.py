# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Standalone runner used by test_diff_reproducibility to exercise compute_events
in a SEPARATE process (distinct PYTHONHASHSEED each call). Prints JSON so the
parent can compare ordering across processes.
"""
import json
import sys

from mcp_drift_monitor.core import compute_events

# 12 servers, all change hash -> large unordered set to expose nondeterminism.
prior = {f"s{i}": f"old{i}" for i in range(12)}
current = {f"s{i}": f"new{i}" for i in range(12)}

drifts, arrivals, removals = compute_events(prior, current, 0, "ts")
out = {
    "drifts": [e.server_id for e in drifts],
    "arrivals": [e.server_id for e in arrivals],
    "removals": [e.server_id for e in removals],
}
# We cannot use sys.stdout directly if pytest captured it; print to fd 1 raw.
sys.stdout.write(json.dumps(out))

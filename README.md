# mcp-drift-monitor

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](https://www.gnu.org/licenses/agpl-3.0.txt)

Continuous MCP Registry Drift Monitor with content-binding revalidation — closes
the gap identified in [arXiv:2608.00997](https://arxiv.org/abs/2608.00997).

## Problem

arXiv:2608.00997 reports a critical blind spot in registry drift detection:
the paper's own measurement panel (19,099 MCP servers observed over 88.6 days,
120 snapshots) shows that history-ranking approaches fail to detect two failure
modes — **silent changers** (a server whose description hash moves but was already
known to the monitor) and **new arrivals** the model never saw during training.
The paper measures 15,845 hash-change events, 19,877 additions, and 911 removals,
yet models that rank by prior history miss a non-trivial slice of these.

This monitor deploys the paper's **missing PRIMARY control**: a periodic
**full-catalog sweep** that re-fetches the entire registry and recomputes all
hashes in one pass via a single diff engine (`compute_events`). It is the
control that defeats "blind-to-new-arrivals".

## Features

- **Single diff engine** (`compute_events`) serving both incremental poll and full
  sweep — no duplicated or divergent logic.
- **Deterministic ordering** (sorted by `server_id`) across processes — verified
  at scale against the paper's 36,753-row panel via byte-identical two-runs replay
  (AC-3).
- **Content-binding revalidation**: revalidates the moment any description hash
  moves — `len(drifts) > 0` is the sole trigger.
- **Rate-limit safe**: 429 + `Retry-After` backoff → retry; retry exhaustion →
  `FetchStatus.FAILED` (never a silent empty list treated as "OK").
- **Schema-drift fail-safe**: malformed registry envelopes raise
  `SchemaDriftError` and **log the offending payload at ERROR** (NFR-3) with a
  `.raw_payload` attribute for operators.
- **Calibrated against the paper's measurement** (AC-1/2/3): 15,845 chg, 19,877
  add, 911 del, 19,099 distinct servers — verified, not asserted.
- **AGPL-3.0-or-later** (SPDX `AGPL-3.0-or-later`).

## Install

```bash
git clone https://github.com/amurlaniakea/mcp-drift-monitor.git
cd mcp-drift-monitor
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
```

## Usage

```python
from mcp_drift_monitor.core.poller import Poller, PollConfig
from mcp_drift_monitor.core.sweep import run_sweep
from mcp_drift_monitor.core.state import StateStore
from mcp_drift_monitor.core.calibrate import replay

# Live sweep
poller = Poller(PollConfig(feed_url="https://registry.example/servers"))
state = StateStore("drift.sqlite")
report = run_sweep(poller, state, on_drift=lambda d: print(f"drift: {d.server_id}"))

# Offline replay against the paper's panel
r = replay("data/mcp_registry_drift_panel_v1.jsonl")
print(r.drift_events, r.arrival_events, r.removal_events)
```

## Tests

```bash
ruff check mcp_drift_monitor tests && pytest -v
```

Test suites:
- `tests/test_diff.py` — pure diff engine (determinism, ordering, event disjointness).
- `tests/test_hasher.py` — NFC normalization + hashing edge cases.
- `tests/test_poller.py` — 429 backoff, retry exhaustion, schema-drift logging.
- `tests/test_state.py` — StateStore persistence, removed-flag, single-source dataclass.
- `tests/test_sweep.py` — full-sweep PRIMARY control (new arrival, silent changer).
- `tests/test_replay_panel.py` — [EXTERNAL-VALIDITY] AC-1/2/3 against the real paper panel.

## Architecture

```
core/
  diff.py     — CatalogEntry, DriftEvent, NewArrivalEvent, RemovalEvent, compute_events
  hasher.py   — normalize_description (NFC), hash_description
  state.py    — StateStore (sqlite), FetchStatus, removed flag, get_all_hashes
  poller.py   — Poller.fetch_catalog, PollConfig, SchemaDriftError, backoff
  sweep.py    — run_sweep (PRIMARY control), SweepReport
  calibrate.py — replay (FR-6), ReplayReport, external validity vs panel
```

## Calibration (FR-6)

`calibrate.replay(panel_path)` reconstructs the catalog per obs snapshot from the
panel's add/chg/del stream and runs `compute_events` on each adjacent transition.
The panel's `d` field is an **opaque hash token** (not reinterpreted as sha256).

Ground-truth counts verified:
| Panel event | Count | compute_events (net) |
|-------------|-------|---------------------|
| `add`       | 19,877 | 16,367 + 3,510 seed = 19,877 ✓ |
| `chg`       | 15,845 | 2,514 net drifts (≤ chg) ✓ |
| `del`       |    911 | 911 removals ✓ |

`arrival_events == add_count` and `removal_events == del_count` hold for THIS
panel because **no server is deleted-and-readded within the same inter-snapshot
interval** (0 precondition violations). A future panel that violates this
would fail the precondition test loudly rather than silently producing wrong KPIs.

## License

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

SPDX-License-Identifier: AGPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>

See [LICENSE](LICENSE) for the full verbatim text (661 lines, verbatim from
<https://www.gnu.org/licenses/agpl-3.0.txt>).

> **AGPL §13 note:** Because this is AGPL, if you run it as a network service
> you must make the source of your modified version available to users
> interacting with it remotely.

## Citation

If you use this software in research, please cite the underlying measurement
paper:

> arXiv:2608.00997 — *MCP Registry Drift: A 88.6-Day Measurement of 19,099 Servers*

```
@misc{sordo2026mcp-drift-monitor,
  title={mcp-drift-monitor: Continuous MCP Registry Drift Monitor},
  author={Sordo Mart{\\'i}nez, Pedro},
  year={2026},
  url={https://github.com/amurlaniakea/mcp-drift-monitor},
  license={AGPL-3.0-or-later}
}
```

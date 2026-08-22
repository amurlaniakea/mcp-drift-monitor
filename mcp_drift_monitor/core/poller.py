# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registry poller (subproblem a / FR-1) with rate-limit (NFR-2) and schema-drift
(NFR-3) handling.

fetch_catalog() returns (entries, FetchStatus). A FAILED fetch returns an EMPTY
entry list with status=FAILED -- NEVER a silent empty list that could be mistaken
for "registry empty / no change". Malformed feed shape raises SchemaDriftError
(fail-safe, no silent mis-hash).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from .diff import CatalogEntry
from .hasher import hash_description
from .state import FetchStatus

log = logging.getLogger(__name__)


class SchemaDriftError(Exception):
    """Raised when the registry feed shape changes unexpectedly (NFR-3).

    Carries the offending raw payload so callers can inspect it without parsing
    the message string.
    """

    def __init__(self, message: str, raw_payload=None):
        super().__init__(message)
        self.raw_payload = raw_payload


@dataclass(frozen=True)
class PollConfig:
    feed_url: str
    timeout: float = 30.0
    max_retries: int = 5
    backoff_base: float = 1.0
    backoff_cap: float = 60.0


class Poller:
    def __init__(self, config: PollConfig | None = None, *, session=None):
        self.config = config or PollConfig(feed_url="https://registry.modelcontextprotocol.io/index.json")
        self._session = session or requests.Session()

    def fetch_catalog(self) -> tuple[list, FetchStatus]:
        """Fetch the registry catalog.

        Returns (entries, status):
          - (entries, OK)        : successful fetch, entries may be empty if registry is empty.
          - ([], FAILED)         : exhausted retries / unreachable. NEVER a silent empty list.
        Raises SchemaDriftError on malformed feed (NFR-3).
        """
        cfg = self.config
        delay = cfg.backoff_base
        for attempt in range(cfg.max_retries):
            try:
                resp = self._session.get(cfg.feed_url, timeout=cfg.timeout)
            except requests.RequestException:
                # Network error: retry with backoff unless last attempt.
                if attempt == cfg.max_retries - 1:
                    return [], FetchStatus.FAILED
                time.sleep(min(delay, cfg.backoff_cap))
                delay *= 2
                continue

            if resp.status_code == 429:
                if attempt == cfg.max_retries - 1:
                    return [], FetchStatus.FAILED
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                time.sleep(min(wait, cfg.backoff_cap))
                delay *= 2
                continue

            if resp.status_code != 200:
                if attempt == cfg.max_retries - 1:
                    return [], FetchStatus.FAILED
                time.sleep(min(delay, cfg.backoff_cap))
                delay *= 2
                continue

            # 200 OK: parse + validate shape.
            entries = self._parse_catalog(resp.json())
            return entries, FetchStatus.OK

        return [], FetchStatus.FAILED

    def _parse_catalog(self, payload: dict) -> list:
        """Validate feed shape and build CatalogEntry list.

        Expected envelope (MCP registry index): {"servers": [{"id": str,
        "description": str}, ...]}. Any deviation -> SchemaDriftError (NFR-3).
        The offending payload is logged at ERROR and attached to the exception.
        """
        if not isinstance(payload, dict):
            log.error("Schema drift: expected dict envelope, got %s. payload=%r",
                      type(payload).__name__, payload)
            raise SchemaDriftError(
                f"expected dict envelope, got {type(payload).__name__}",
                raw_payload=payload,
            )
        servers = payload.get("servers")
        if not isinstance(servers, list):
            log.error("Schema drift: expected 'servers' list, got %s. payload=%r",
                      type(servers).__name__ if servers is not None else "None", payload)
            raise SchemaDriftError(
                f"expected 'servers' list, got {type(servers).__name__ if servers is not None else 'None'}",
                raw_payload=payload,
            )
        entries = []
        for s in servers:
            if not isinstance(s, dict):
                log.error("Schema drift: server entry not an object. entry=%r payload=%r", s, payload)
                raise SchemaDriftError(
                    f"server entry not an object: {repr(s)[:200]}",
                    raw_payload=payload,
                )
            sid = s.get("id")
            desc = s.get("description")
            if not isinstance(sid, str) or not isinstance(desc, str):
                log.error("Schema drift: server missing str 'id'/'description'. entry=%r payload=%r", s, payload)
                raise SchemaDriftError(
                    f"server missing str 'id'/'description': {repr(s)[:200]}",
                    raw_payload=payload,
                )
            entries.append(CatalogEntry(sid, hash_description(desc)))
        return entries

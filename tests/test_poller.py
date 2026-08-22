# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Poller (T4 / FR-1 / NFR-2 / NFR-3).

Network is simulated with a fake requests.Session so no real HTTP is hit.
"""

import pytest

from mcp_drift_monitor.core.diff import CatalogEntry
from mcp_drift_monitor.core.hasher import hash_description
from mcp_drift_monitor.core.poller import PollConfig, Poller, SchemaDriftError
from mcp_drift_monitor.core.state import FetchStatus


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeSession:
    """Replays a scripted list of responses; optionally raises on a call."""

    def __init__(self, responses, raise_on=None):
        self._responses = list(responses)
        self._raise_on = raise_on  # exception to raise on next .get
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        if self._raise_on is not None:
            raise self._raise_on
        return self._responses.pop(0)


def _ok_payload(servers):
    return {"servers": [{"id": s[0], "description": s[1]} for s in servers]}


def test_200_ok_parses_entries():
    sess = _FakeSession([_FakeResponse(200, _ok_payload([("a", "desc a"), ("b", "desc b")]))])
    poller = Poller(PollConfig(feed_url="http://x", max_retries=5), session=sess)
    entries, status = poller.fetch_catalog()
    assert status == FetchStatus.OK
    assert entries == [
        CatalogEntry("a", hash_description("desc a")),
        CatalogEntry("b", hash_description("desc b")),
    ]


def test_429_then_200_succeeds_with_backoff(monkeypatch):
    # First call 429 with Retry-After, second 200.
    sleeps = []
    monkeypatch.setattr("mcp_drift_monitor.core.poller.time.sleep", lambda s: sleeps.append(s))
    sess = _FakeSession([
        _FakeResponse(429, {}, {"Retry-After": "0"}),
        _FakeResponse(200, _ok_payload([("a", "d")])),
    ])
    poller = Poller(PollConfig(feed_url="http://x", max_retries=5, backoff_base=0.0), session=sess)
    entries, status = poller.fetch_catalog()
    assert status == FetchStatus.OK
    assert len(entries) == 1
    assert sess.calls == 2
    assert sleeps  # backoff was applied


def test_exhaustion_returns_failed_not_empty_silent(monkeypatch):
    sleeps = []
    monkeypatch.setattr("mcp_drift_monitor.core.poller.time.sleep", lambda s: sleeps.append(s))
    # All attempts 429 -> exhaust retries.
    sess = _FakeSession([_FakeResponse(429, {}, {"Retry-After": "0"})] * 5)
    poller = Poller(PollConfig(feed_url="http://x", max_retries=5, backoff_base=0.0), session=sess)
    entries, status = poller.fetch_catalog()
    assert status == FetchStatus.FAILED
    assert entries == []  # explicit empty, with FAILED status (not silent OK/empty)
    assert sess.calls == 5


def test_network_error_exhaustion_returns_failed(monkeypatch):
    import requests

    sleeps = []
    monkeypatch.setattr("mcp_drift_monitor.core.poller.time.sleep", lambda s: sleeps.append(s))
    sess = _FakeSession([], raise_on=requests.ConnectionError("down"))
    poller = Poller(PollConfig(feed_url="http://x", max_retries=3, backoff_base=0.0), session=sess)
    entries, status = poller.fetch_catalog()
    assert status == FetchStatus.FAILED
    assert entries == []


def test_malformed_envelope_raises_schema_drift(caplog):
    # Missing 'servers' key -> SchemaDriftError, zero entries, no silent mis-hash.
    payload = {"unexpected": "shape"}
    sess = _FakeSession([_FakeResponse(200, payload)])
    poller = Poller(PollConfig(feed_url="http://x", max_retries=2), session=sess)
    with pytest.raises(SchemaDriftError) as exc_info:
        poller.fetch_catalog()
    err = exc_info.value
    assert err.raw_payload == payload  # attached for caller diagnosis
    # NFR-3: the offending raw structure MUST be logged at ERROR level.
    assert any(
        r.levelname == "ERROR" and "shape" in r.message and "servers" in r.message
        for r in caplog.records
    ), "expected ERROR log of the offending payload"


def test_server_missing_description_field_raises_schema_drift(caplog):
    payload = {"servers": [{"id": "a"}]}
    sess = _FakeSession([_FakeResponse(200, payload)])
    poller = Poller(PollConfig(feed_url="http://x", max_retries=2), session=sess)
    with pytest.raises(SchemaDriftError) as exc_info:
        poller.fetch_catalog()
    assert exc_info.value.raw_payload == payload
    assert any(
        r.levelname == "ERROR" and "description" in r.message
        for r in caplog.records
    ), "expected ERROR log of the offending payload"

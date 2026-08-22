# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for description normalization + hashing (T2.3 / FR-2)."""


import pytest

from mcp_drift_monitor.core.hasher import hash_description, normalize_description


def test_normalize_strips_and_collapses_whitespace():
    raw = "   run   a   shell\t\nexec   "
    assert normalize_description(raw) == "run a shell exec"


def test_normalize_nfc_unicode():
    # 'café' composed (U+00E9) vs 'cafe' + combining acute (U+0301)
    composed = "caf\u00e9 server"
    decomposed = "cafe\u0301 server"
    assert normalize_description(composed) == normalize_description(decomposed)
    assert normalize_description(composed) == "caf\u00e9 server"


def test_hash_idempotent():
    desc = "Executes arbitrary shell commands on the host."
    assert hash_description(desc) == hash_description(desc)


def test_hash_deterministic_across_nfc_forms():
    composed = "caf\u00e9 server"
    decomposed = "cafe\u0301 server"
    assert hash_description(composed) == hash_description(decomposed)


def test_different_descriptions_different_hashes():
    a = hash_description("read-only file access")
    b = hash_description("write access to filesystem")
    assert a != b


def test_normalize_known_fixture():
    # Matches the AC-4 spirit: same semantic content, different spacing -> same hash.
    raw1 = "Monitor   MCP   servers"
    raw2 = "  Monitor MCP servers  "
    assert normalize_description(raw1) == normalize_description(raw2)
    assert hash_description(raw1) == hash_description(raw2)


def test_normalize_rejects_none():
    with pytest.raises(TypeError):
        normalize_description(None)


def test_normalize_unicode_class_whitespace():
    # Non-breaking space (U+00A0) and ideographic space (U+3000) must collapse.
    raw = "tool\u00a0call\u3000execute"
    assert normalize_description(raw) == "tool call execute"

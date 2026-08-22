# SPDX-FileCopyrightText: 2026 Pedro Sordo Martínez <amurlaniakea@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Description normalization + hashing (subproblem b / FR-2).

A description's drift is detected by comparing the sha256 of its NORMALIZED form.
Normalization MUST be deterministic and unicode-safe so that semantically identical
descriptions produce the same hash regardless of whitespace/canonical-form noise.
"""

from __future__ import annotations

import hashlib
import unicodedata


def normalize_description(raw: str) -> str:
    """Deterministic normalization of a registry description.

    - NFC unicode normalization (canonical composition): e.g. 'cafe\\u0301' and
      'caf\\u00e9' collapse to the same string.
    - Strip leading/trailing whitespace (all Unicode whitespace classes).
    - Collapse internal runs of whitespace to a single space.
    Pure function: same input -> same output, no external state.
    """
    if raw is None:
        raise TypeError("normalize_description requires a str, got None")
    text = unicodedata.normalize("NFC", raw)
    text = " ".join(text.split())  # splits on any Unicode whitespace, rejoins with ' '
    return text


def hash_description(raw: str) -> str:
    """sha256 hex digest of the normalized description."""
    normalized = normalize_description(raw)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

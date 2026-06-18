"""Polymarket Gamma returns several fields as JSON-encoded strings *inside* the
JSON body, e.g. ``outcomePrices: "[\\"0.51\\",\\"0.49\\"]"`` and
``clobTokenIds: "[\\"9802...\\",\\"5383...\\"]"``. Centralizing the double-parse
here prevents the most likely class of silent bug.
"""
from __future__ import annotations

import json
from typing import Any


def loads_maybe(value: Any) -> Any:
    """Decode a possibly-JSON-encoded string to Python. Idempotent: if ``value``
    is already a list/dict (or None) it is returned unchanged. Returns the raw
    string if it is not valid JSON."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return value
    return value


def parse_prices(value: Any) -> list[float]:
    """Parse an outcomePrices-style field into a list of floats. Non-list / un-decodable input → []
    (never float() each char of a raw string)."""
    parsed = loads_maybe(value)
    if not isinstance(parsed, list):
        return []
    try:
        return [float(x) for x in parsed]
    except (TypeError, ValueError):
        return []


def parse_str_list(value: Any) -> list[str]:
    """Parse an outcomes/clobTokenIds-style field into a list of strings.

    Token ids are huge integers; they MUST stay strings (never coerced to int/
    float, which would lose precision). A raw string from un-decodable JSON must
    NOT iterate into per-character fragments → require an actual list, else []."""
    parsed = loads_maybe(value)
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed]

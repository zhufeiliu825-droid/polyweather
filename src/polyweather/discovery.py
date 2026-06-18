"""Discover a city's daily-temperature event and parse it into an EventState.

The bucket *ladder* geometry (ordered comparators/thresholds) is what makes the
dead-bucket logic computable, so parsing the question text correctly is central.
"""
from __future__ import annotations

import re
from typing import Any

from .models import Bucket, Comparator
from .util.pmjson import parse_prices, parse_str_list

# "...be 19°C or below...", "...be 20°C...", "...be 29°C or higher..."  (every market is °C)
_NUM_UNIT = re.compile(r"(-?\d+)\s*°?\s*C", re.IGNORECASE)


def parse_bucket_question(question: str) -> tuple[Comparator, int, str]:
    """Extract (comparator, threshold, unit) from a bucket market question. Unit is always
    °C — every selected city resolves in Celsius."""
    m = _NUM_UNIT.search(question)
    if not m:
        raise ValueError(f"no °C temperature threshold in question: {question!r}")
    threshold = int(m.group(1))
    unit = "C"
    q = question.lower()
    if "or below" in q or "or lower" in q:
        comp = Comparator.LE
    elif "or higher" in q or "or above" in q:
        comp = Comparator.GE
    else:
        comp = Comparator.EQ
    return comp, threshold, unit


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bucket_from_market(m: dict[str, Any]) -> Bucket | None:
    q = m.get("question", "")
    try:
        comp, thr, unit = parse_bucket_question(q)
    except ValueError:
        return None
    tokens = parse_str_list(m.get("clobTokenIds"))
    prices = parse_prices(m.get("outcomePrices"))
    # Map the YES side by the `outcomes` array, NEVER a positional guess: Gamma's outcome ORDER is
    # data-driven (some markets are No-first), so token_yes=tokens[0] could silently be the NO token →
    # every auto-bet buys the OPPOSITE outcome with real money. Require a clean 2-way {yes,no} pair with
    # a matching token array; any other schema → skip the bucket (fail closed) rather than guess (audit r6).
    outcomes = [str(o).strip().lower() for o in parse_str_list(m.get("outcomes"))]
    if len(outcomes) != 2 or set(outcomes) != {"yes", "no"} or len(tokens) < 2:
        return None
    yi = outcomes.index("yes")
    ni = 1 - yi
    return Bucket(
        question=q,
        comparator=comp,
        threshold=thr,
        unit=unit,
        token_yes=tokens[yi],
        token_no=tokens[ni],
        condition_id=m.get("conditionId", ""),
        price_yes=(prices[yi] if len(prices) >= 2 else None),   # CLOB overwrites the live price; this is a fallback
        best_bid=_f(m.get("bestBid")),
        best_ask=_f(m.get("bestAsk")),
        last_trade=_f(m.get("lastTradePrice")),
        volume=_f(m.get("volume")),
    )



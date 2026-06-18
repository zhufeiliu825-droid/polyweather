"""Remaining-hours forecast peak — the max of today's forecast for the hours still ahead.

The realized running max is locked (monotonic); the only open question is whether the remaining hours
exceed it. This returns the forecast's remaining-hours max. The `bias` arg is kept for callers that want a
residual correction, but the live path passes 0 — no historical bias is applied (the agent judges any NWP
bias direction from the live atmosphere instead).
"""
from __future__ import annotations

from datetime import date, datetime


def remaining_corrected_max(
    curve: list[tuple[datetime, float]],
    now_local: datetime,
    target: date,
    bias: float = 0.0,
) -> float | None:
    """Max of the forecast for the time strictly after now, today, minus `bias`. Granularity-agnostic — the
    `curve` is now 15-min points (a just-passed sub-hour slot dropping out is harmless: the realized
    running_max floor already covers it, so we keep the clean 'remaining = future' rule)."""
    rem = [tp for dt, tp in curve if dt.date() == target and dt > now_local]
    if not rem:
        return None
    return max(rem) - bias

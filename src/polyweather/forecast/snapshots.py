"""Intraday forecast snapshots — one hourly append per (city, market-day) of the forecast peak as it stands
NOW. Accumulated over the day they form the convergence "fan": how the predicted peak VALUE + TIME settle
(or keep drifting) as the day approaches — a real-time revision/uncertainty signal the strike + decision LLMs
read like a meteorologist (today's bug: London revised 25.9 → 26.5 → realized 27, but we'd frozen on 25.9).

Stored compact (peak + met-at-peak only, NOT the full curve): the convergence of the PEAK is the signal; the
current full curve is already live in scan.hourly. JSONL, append-only, one file per (station, market-day).
"""
from __future__ import annotations

import json
from datetime import date

from ..realized.feature_store import DATA_DIR
from .regime import classify_regime

SNAP_DIR = DATA_DIR / "forecast_snapshots"


def _path(station: str, mkt_day: date):
    return SNAP_DIR / f"{station}_{mkt_day.isoformat()}.jsonl"


def append_snapshot(station: str, mkt_day: date, record: dict) -> None:
    """Append one snapshot line. Append-only (each line is independently parseable) → crash-safe, no rewrite."""
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    with _path(station, mkt_day).open("a") as f:
        f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")


def read_snapshots(station: str, mkt_day: date) -> list[dict]:
    """All snapshots for (station, market-day) in issue order; [] if none. A corrupt line is skipped, not fatal."""
    p = _path(station, mkt_day)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def fan_summary(station: str, mkt_day: date) -> dict | None:
    """Summarize the day's snapshot fan for the LLMs: the peak-VALUE series across successive forecast issues,
    its drift/convergence, and the LATEST met-at-peak (≤1h old → the current atmospheric picture, no extra
    fetch). None if no snapshots yet. `converged` = the last few issues agree within 0.3° (a confident peak)."""
    snaps = [s for s in read_snapshots(station, mkt_day) if s.get("peak_C") is not None]
    if not snaps:
        return None
    series = [{"issue": s.get("issue_utc"), "peak_C": s["peak_C"], "at": s.get("peak_local")} for s in snaps]
    pcs = [s["peak_C"] for s in series]
    recent = pcs[-3:]
    return {
        "n": len(series),
        "series": series,                                  # [{issue, peak_C, at}] — the convergence fan
        "peak_C_first": pcs[0], "peak_C_latest": pcs[-1],
        "peak_C_min": min(pcs), "peak_C_max": max(pcs),
        "trend_C": round(pcs[-1] - pcs[0], 1),             # net revision since the first issue (+ = revised up)
        "converged": (max(recent) - min(recent)) <= 0.3,   # last ≤3 issues agree → a settled peak
        "met_at_peak_latest": snaps[-1].get("met_at_peak"),
        "regime_latest": (snaps[-1].get("regime")
                          or classify_regime(snaps[-1].get("met_at_peak"))),
    }

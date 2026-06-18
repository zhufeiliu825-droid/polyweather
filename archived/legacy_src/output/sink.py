"""Append-only JSONL snapshot sink (greppable, tail-able, and the backtest's
comparison format)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..models import EventState, Signal

DATA_DIR = Path(__file__).resolve().parents[3] / "data"


def snapshot_record(state: EventState, regime=None, peak_passed=None) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "city": state.city,
        "local_date": state.local_date.isoformat(),
        "unit": state.unit,
        "running_max": state.running_max,
        "obs_count": state.obs_count,
        "last_wu_poll_gmt": state.last_wu_poll_gmt,
        "regime": getattr(regime, "value", regime),
        "peak_passed": peak_passed,
        "buckets": [
            {
                "comparator": b.comparator.value,
                "threshold": b.threshold,
                "implied_p": b.implied_p,
                "best_bid": b.best_bid,
                "best_ask": b.best_ask,
                "liquidity": b.liquidity,
                "dead": state.is_dead(b),
            }
            for b in state.buckets
        ],
    }


def write_snapshot(state: EventState, regime=None, peak_passed=None, path: Path | None = None) -> None:
    path = path or (DATA_DIR / "snapshots.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(snapshot_record(state, regime, peak_passed)) + "\n")


def write_signals(signals: list[Signal], path: Path | None = None) -> None:
    if not signals:
        return
    path = path or (DATA_DIR / "signals.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with path.open("a") as f:
        for s in signals:
            rec = s.model_dump(mode="json")
            rec["ts"] = ts
            f.write(json.dumps(rec) + "\n")

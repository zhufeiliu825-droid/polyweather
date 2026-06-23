"""Offline scorer for the real-time nowcast prober — does the NOWCAST beat the MARKET?

Joins data/rt_nowcast.jsonl (logged nowcast dist + de-vigged market dist per snapshot) to data/settle.jsonl
(realized daily max), and by LEAD-TO-PEAK reports: Brier(nowcast) vs Brier(market), each side's probability on
the eventual winning bucket, top-bucket hit rate, and the paper PnL of buying the nowcast's top bucket at the
logged market price. The decisive cut is the last ~60-90 min before peak (lead < ~1.5h) — that is the only
window this session measured any bucket-predictability, and the only place a real-time data edge can show up.

Needs SETTLED days (markets resolve next day), so it reports 0 scorable until the logged days settle + accumulate.

  uv run python maintain/rt_nowcast_score.py
"""
from __future__ import annotations

import json

from polyweather.realized.feature_store import DATA_DIR

ROWS = DATA_DIR / "rt_nowcast.jsonl"
SETTLE = DATA_DIR / "settle.jsonl"


def _bucket_of(label_keys: list[str], realized: int) -> str | None:
    """Which logged bucket label (==N / >=N / <=N) contains the realized integer max."""
    best = None
    for k in label_keys:
        comp, n = (k[:2], int(k[2:])) if k[:2] in (">=", "<=") else ("==", int(k[2:]))
        if (comp == "==" and realized == n) or (comp == ">=" and realized >= n) or (comp == "<=" and realized <= n):
            if comp == "==":
                return k
            best = best or k
    return best


def _brier(dist: dict, realized_label: str) -> float:
    return round(sum((p - (1.0 if k == realized_label else 0.0)) ** 2 for k, p in dist.items()), 4)


def main() -> None:
    if not ROWS.exists():
        print("no data/rt_nowcast.jsonl yet — run maintain/rt_nowcast.py first")
        return
    settle = {}
    if SETTLE.exists():
        for ln in SETTLE.open():
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("actual_max_awc") is not None:
                settle[(str(d.get("city", "")).lower(), d.get("market_date"))] = int(d["actual_max_awc"])

    segs = {">3h": [], "1.5-3h": [], "0.5-1.5h": [], "<0.5h": []}

    def seg(lead):
        return ">3h" if lead >= 3 else "1.5-3h" if lead >= 1.5 else "0.5-1.5h" if lead >= 0.5 else "<0.5h"

    n_rows = n_scored = 0
    for ln in ROWS.open():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        n_rows += 1
        nc, mk, raw = r.get("nowcast"), r.get("market_devig"), r.get("market_raw")
        lead = r.get("lead_h")
        if not (nc and mk and raw) or lead is None:
            continue
        R = settle.get((r["city"].lower(), r["date"]))
        if R is None:
            continue
        rl = _bucket_of(list(raw.keys()), R)
        if rl is None:
            continue
        n_scored += 1
        nc_top = max(nc, key=nc.get)
        mk_top = max(mk, key=mk.get)
        pnl = (1.0 - raw.get(nc_top, 1.0)) if nc_top == rl else -raw.get(nc_top, 0.0)  # buy nowcast pick @ market
        segs[seg(lead)].append({
            "nc_brier": _brier(nc, rl), "mk_brier": _brier(mk, rl),
            "nc_p": nc.get(rl, 0.0), "mk_p": mk.get(rl, 0.0),
            "nc_hit": nc_top == rl, "mk_hit": mk_top == rl, "pnl": pnl,
        })

    print(f"rows logged: {n_rows} | scorable (settled + joined): {n_scored}")
    if not n_scored:
        print("0 scorable yet — wait for logged days to settle, then re-run. (the prober must run through peak windows.)")
        return
    print(f"\n{'lead':9} {'n':>4} | {'nowcast Brier':>13} {'market Brier':>12} | "
          f"{'nc P(win)':>9} {'mk P(win)':>9} | {'nc top-hit':>10} {'mk top-hit':>10} | {'nowcast PnL/share':>16}")
    for k in [">3h", "1.5-3h", "0.5-1.5h", "<0.5h"]:
        s = segs[k]
        if not s:
            continue
        n = len(s)
        print(f"{k:9} {n:>4} | {sum(x['nc_brier'] for x in s)/n:>13.3f} {sum(x['mk_brier'] for x in s)/n:>12.3f} | "
              f"{sum(x['nc_p'] for x in s)/n:>9.2f} {sum(x['mk_p'] for x in s)/n:>9.2f} | "
              f"{100*sum(x['nc_hit'] for x in s)//n:>9}% {100*sum(x['mk_hit'] for x in s)//n:>9}% | "
              f"{sum(x['pnl'] for x in s)/n:>+16.3f}")
    print("\nEDGE = nowcast Brier < market Brier AND nowcast PnL/share > 0, concentrated in the <1.5h rows.")
    print("If the nowcast only ties/loses there too, prediction has no edge even with the real-time feed — stop.")


if __name__ == "__main__":
    main()

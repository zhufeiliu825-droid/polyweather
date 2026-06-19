"""Reward-farming runner (PILOT). `--scan-once` is read-only; the default loop manages the separate
reward farmer through the shared executor and remains DRY unless `POLY_DRY_RUN=0`.
Run: `uv run python run/run_rewards.py --scan-once`."""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

import httpx

from polyweather import clob_executor as ex
from polyweather.rewards import advice
from polyweather.rewards import farmer
from polyweather.rewards import scan as rscan
from polyweather.stations import load_stations

UTC = timezone.utc
HDR = {"User-Agent": "polyweather-rewards/1.0"}


_HDR = (f"  {'city':13} {'bucket':7} {'$/day':>5} {'mid':>5} {'comp':>6} "
        f"{'h2peak':>6} {'peak':>5} {'lean':>4} {'score':>7}")


def _row(t: dict) -> str:
    return (f"  {t['city']:13} {t['bucket']:7} {t['daily_rate']:5.0f} {(t['mid'] or 0):5.2f} "
            f"{t['competition']:6.0f} {(t['hours_to_peak'] or 0):6.1f} {(t['forecast_peak'] or 0):5.1f} "
            f"{(t['our_yes_lean'] or 0):4.2f} {t['score']:7.4f}")


async def scan_once(top: int = 16) -> None:
    stations = list(load_stations().values())
    now = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=40, headers=HDR) as client:
        targets = await rscan.scan(client, stations, now)
    rscan.log_targets(targets, now.strftime("%Y-%m-%dT%H:%MZ"))
    edge = [t for t in targets if t["our_yes_lean"]]
    print(f"[{now:%H:%MZ}] {len(targets)} reward targets ({len(edge)} with a forecast lean) · 仅明天+ event. "
          f"h2peak = 距峰值小时数(越大=市场越静、越不易被吃)", flush=True)

    print(f"\n▶ 双边/高奖励 — top {top}  (score = $/day × YES-lean ÷ competition):")
    print(_HDR)
    for t in targets[:top]:
        print(_row(t), flush=True)

    # 单边/低价 (推友 Fig2): 便宜 + 我们看好(=低估)+ 低竞争 → 小资金单边买
    cheap = sorted((t for t in targets if t["mid"] is not None and t["mid"] <= 0.15
                    and (t["our_yes_lean"] or 0) >= 0.3), key=lambda t: t["mid"])
    print(f"\n▶ 单边/低价 — 便宜+我们看好+低竞争 (mid≤0.15, lean≥0.3)  [{len(cheap)} 个]:")
    print(_HDR)
    for t in cheap[:top]:
        print(_row(t), flush=True)


async def farm_dry(capital_cap: float, max_targets: int) -> None:
    stations = list(load_stations().values())
    now = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=40, headers=HDR) as client:
        targets = await rscan.scan(client, stations, now)
        cache, advice_log = await advice.hourly_advice(client, targets, now)
    targets = advice.apply_advice(targets, cache, now)
    orders = farmer.select(targets, capital_cap=capital_cap, max_targets=max_targets)
    farmer.log_orders(orders, now.strftime("%Y-%m-%dT%H:%MZ"), dry=True)
    tot_cap = sum(o.capital for o in orders)
    tot_rew = sum(o.est_daily_reward for o in orders)
    print(f"[{now:%H:%MZ}] FARM PLAN (DRY) — cap ${capital_cap:.0f}, ≤{max_targets} buckets · 选中 {len(orders)} 个 "
          f"(仅明天+ · fair−bid≥{farmer.MIN_MODEL_EDGE:.2f} · 健康闸 · 每event top-{farmer.MAX_PER_EVENT} · "
          f"lean≥{farmer.MIN_LEAN} · mid∈[{farmer.MID_LO},{farmer.MID_HI}]):", flush=True)
    if advice_log:
        print("  " + advice_log, flush=True)
    print(f"  {'city':13} {'bucket':7} {'bid':>5} {'sh':>4} {'cap$':>6} {'pool/d':>6} {'comp':>6} {'our%':>5} {'~$/d':>5} {'lean':>4} {'h2pk':>5}")
    for o in orders:
        print(f"  {o.city:13} {o.bucket:7} {o.price:5.2f} {o.size:4.0f} {o.capital:6.2f} {o.daily_rate:6.0f} "
              f"{o.competition:6.0f} {o.est_share * 100:4.1f}% {o.est_daily_reward:5.1f} {o.lean:4.2f} {o.hours_to_peak:5.1f}",
              flush=True)
    print(f"  合计:占用 ${tot_cap:.2f} → 估算 ~${tot_rew:.2f}/天 奖励  (DRY,写入 data/reward_orders.jsonl,未下任何单)", flush=True)


async def farm_place(capital_cap: float, max_targets: int) -> None:
    stations = list(load_stations().values())
    now = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=40, headers=HDR) as client:
        targets = await rscan.scan(client, stations, now)
        cache, advice_log = await advice.hourly_advice(client, targets, now)
    targets = advice.apply_advice(targets, cache, now)
    orders = farmer.select(targets, capital_cap=capital_cap, max_targets=max_targets)
    live = os.environ.get("POLY_DRY_RUN", "1") == "0"
    farmer.log_orders(orders, now.strftime("%Y-%m-%dT%H:%MZ"), dry=not live)
    results = farmer.place_orders(orders, now_iso=now.strftime("%Y-%m-%dT%H:%MZ"))
    print(f"[{now:%H:%MZ}] FARM PLACE ({'LIVE' if live else 'DRY'}) — {len(orders)} reward bids "
          f"(GTC, no expiry — loop reprices on drift):", flush=True)
    if advice_log:
        print("  " + advice_log, flush=True)
    for o, res in results:
        print(f"  {o.city:13} {o.bucket:7} bid {o.price:.2f} ×{o.size:.0f}sh ${o.capital:.2f} → "
              f"{'OK' if res.ok else 'FAIL'} {res.detail}", flush=True)


async def farm_loop(capital_cap: float, max_targets: int, *, once: bool, interval_min: int) -> None:
    stations = list(load_stations().values())
    while True:
        now = datetime.now(UTC)
        async with httpx.AsyncClient(timeout=40, headers=HDR) as client:
            state, log = await farmer.run_cycle(client, stations, now, capital_cap=capital_cap, max_targets=max_targets)
        live = os.environ.get("POLY_DRY_RUN", "1") == "0"
        resting = sum(1 for e in state.values() if e["status"] == "RESTING")
        filled = sum(1 for e in state.values() if e["status"] == "FILLED")
        print(f"[{now:%H:%MZ}] farm cycle ({'LIVE' if live else 'DRY'}): {len(log)} actions · "
              f"resting {resting} · filled {filled}", flush=True)
        for ln in log:
            print("  " + ln, flush=True)
        if once:
            break
        await asyncio.sleep(interval_min * 60)


async def cancel_all() -> None:
    """Cancel ALL the farmer's resting orders (from reward_state.json) — NOT the v2 cancel_all() (that nukes the
    directional bot's orders too). Keeps FILLED positions in state. Use after a code fix to clear stale bids."""
    state = farmer.load_state()
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    live = os.environ.get("POLY_DRY_RUN", "1") == "0"
    resting = [(t, e) for t, e in state.items() if e.get("status") == "RESTING" and e.get("order_id")]
    print(f"[{datetime.now(UTC):%H:%MZ}] CANCEL-ALL ({'LIVE' if live else 'DRY'}) — {len(resting)} resting farmer orders:", flush=True)
    new_state = {t: e for t, e in state.items() if e.get("status") == "FILLED"}      # keep positions for Stage 2 / B3b
    cancelled = 0
    for tok, e in resting:
        res = ex.cancel_order(e["order_id"], now_iso=now_iso, reason="cancel-all")
        if res.ok:
            cancelled += 1
        else:
            new_state[tok] = e                                                       # still live → keep to retry
        print(f"  {e['city']:12} {e['bucket']:7} {str(e['order_id'])[:14]}… → {'OK' if res.ok else 'FAIL: ' + res.detail}", flush=True)
    farmer.save_state(new_state)
    print(f"  → cancelled {cancelled}/{len(resting)}; kept {len(new_state)} in state (FILLED positions + any failed cancels)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-once", action="store_true",
                    help="print + log the top reward targets, then exit (read-only, no orders)")
    ap.add_argument("--farm-dry", action="store_true",
                    help="select edge-aligned targets + print/log the order plan (no placement)")
    ap.add_argument("--farm-place", action="store_true",
                    help="place the selected reward bids once via the executor (DRY unless POLY_DRY_RUN=0)")
    ap.add_argument("--farm-loop", action="store_true",
                    help="continuous buy-side farm: poll fills + (re-)place bids tracking the reward band (DRY unless POLY_DRY_RUN=0)")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel ALL the farmer's resting orders (keeps FILLED positions) — use after a code fix to clear stale bids")
    ap.add_argument("--once", action="store_true", help="with --farm-loop: run a single cycle then exit")
    ap.add_argument("--interval", type=int, default=5, help="with --farm-loop: minutes between cycles")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--cap", type=float, default=200.0, help="capital cap (USDC) — matches the live launcher")
    ap.add_argument("--max", type=int, default=20, dest="max_targets", help="max buckets to farm — matches the live launcher")
    a = ap.parse_args()
    if a.cancel_all:
        asyncio.run(cancel_all())
    elif a.farm_loop:
        asyncio.run(farm_loop(a.cap, a.max_targets, once=a.once, interval_min=a.interval))
    elif a.farm_place:
        asyncio.run(farm_place(a.cap, a.max_targets))
    elif a.farm_dry:
        asyncio.run(farm_dry(a.cap, a.max_targets))
    elif a.scan_once:
        asyncio.run(scan_once(a.top))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

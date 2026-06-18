"""Polyweather monitor entrypoint (London, poll-only, read-only).

    uv run python -m polyweather --city london --once
    uv run python -m polyweather --city london --interval 30
"""
from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import load_config
from .discovery import apply_odds, discover_city_event
from .engine import SignalTracker, evaluate, evaluate_upside
from .forecast.conditions import classify_regime, peak_passed
from .forecast.open_meteo import fetch_conditions
from .forecast.upside import build_forecast_model, remaining_corrected_max
from .odds.gamma_feed import fetch_event
from .output.dashboard import console, render_conditions, render_signals, render_state
from .output.sink import write_signals, write_snapshot
from .realized.daily_max import update_running_max


async def run(city_name: str, once: bool, interval: float) -> None:
    cfg = load_config()
    if city_name not in cfg.cities:
        console.print(f"[red]unknown city {city_name!r}; known: {list(cfg.cities)}")
        return
    city = cfg.cities[city_name]
    tracker = SignalTracker()

    async with httpx.AsyncClient(
        timeout=20.0, headers={"User-Agent": "polyweather/0.1 (read-only monitor)"}
    ) as client:
        state = await discover_city_event(client, city, cfg.defaults.temp_tag_id)
        if state is None:
            console.print(f"[yellow]no live event found for {city_name}")
            return
        console.print(f"[dim]tracking event {state.event_id} ({state.slug})[/dim]")

        try:
            fc_model = await build_forecast_model(
                client, city, cfg.defaults.wu_api_key, cfg.thresholds
            )
            console.print(
                f"[dim]forecast model: bias={fc_model.bias:+.2f} sigma={fc_model.sigma:.2f}[/dim]"
            )
        except Exception as e:
            fc_model = None
            console.print(f"[yellow]forecast model unavailable: {e!r}")

        while True:
            try:
                raw = await fetch_event(client, state.event_id)
                apply_odds(state, raw)
                await update_running_max(client, state, city, cfg.defaults.wu_api_key)
            except Exception as e:  # keep the monitor alive across transient errors
                console.print(f"[red]refresh error: {e!r}")

            # conditions + forecast (one fetch supplies timing, regime, and upside)
            ps = None
            regime = None
            fc_probs = None
            if fc_model is not None:
                try:
                    bundle = await fetch_conditions(
                        client, city.latitude, city.longitude, city.timezone
                    )
                    now_local = datetime.now(ZoneInfo(city.timezone)).replace(tzinfo=None)
                    if cfg.conditions.use_conditions:
                        ps = peak_passed(bundle, state.local_date, now_local, cfg.conditions)
                        regime = classify_regime(bundle, state.local_date, cfg.conditions)
                    hourly = [(h.time, h.temp_c) for h in bundle.hourly]
                    rem = remaining_corrected_max(
                        hourly, now_local, state.local_date, fc_model.bias
                    )
                    fc_probs = fc_model.bucket_probs(state, rem, regime=regime)
                except Exception as e:
                    console.print(f"[red]forecast error: {e!r}")

            local_hour = datetime.now(ZoneInfo(city.timezone)).hour
            age = (
                time.time() - state.last_wu_fetch_ts
                if state.last_wu_fetch_ts is not None
                else None
            )
            signals = evaluate(
                state, cfg.thresholds, local_hour=local_hour, wu_poll_age_s=age,
                peak_passed=(ps.peak_passed if ps is not None else None),
            )
            if fc_probs is not None:
                signals = signals + evaluate_upside(
                    state, fc_probs, cfg.thresholds, regime=regime
                )
            new, cleared = tracker.diff(signals)

            render_state(state, city.timezone)
            render_conditions(ps, regime)
            render_signals(signals)
            write_snapshot(state, regime=regime, peak_passed=(ps.peak_passed if ps else None))
            write_signals(new)  # persist only newly-fired signals

            if once:
                break
            await asyncio.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(prog="polyweather")
    ap.add_argument("--city", default="london")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--interval", type=float, default=30.0, help="poll seconds")
    args = ap.parse_args()
    asyncio.run(run(args.city, args.once, args.interval))


if __name__ == "__main__":
    main()

"""Show today's forecast-implied bucket distribution vs the live market for a
city, and any UPSIDE edges. Read-only.

    uv run python scripts/forecast_check.py --city london
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from rich.console import Console
from rich.table import Table

from polyweather.config import load_config
from polyweather.discovery import discover_city_event
from polyweather.engine import devig, evaluate_upside
from polyweather.forecast.open_meteo import fetch_hourly
from polyweather.forecast.upside import build_forecast_model, remaining_corrected_max
from polyweather.realized.daily_max import update_running_max

console = Console()


async def main(city_name: str) -> None:
    cfg = load_config()
    city = cfg.cities[city_name]
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "polyweather-fc/0.1"}) as client:
        state = await discover_city_event(client, city, cfg.defaults.temp_tag_id)
        if state is None:
            console.print(f"[yellow]no live event for {city_name}")
            return
        await update_running_max(client, state, city, cfg.defaults.wu_api_key)
        model = await build_forecast_model(client, city, cfg.defaults.wu_api_key, cfg.thresholds)
        hourly = await fetch_hourly(client, city.latitude, city.longitude, city.timezone)
        now_local = datetime.now(ZoneInfo(city.timezone)).replace(tzinfo=None)
        rem = remaining_corrected_max(hourly, now_local, state.local_date, model.bias)
        fc_probs = model.bucket_probs(state, rem)
        market, raw_sum = devig(state)

        console.print(
            f"[bold]{state.title}[/bold]  running max [cyan]{state.running_max:g}°{state.unit}[/cyan]  "
            f"local {now_local:%H:%M}  bias={model.bias:+.2f} sigma={model.sigma:.2f}  "
            f"forecast remaining-max≈{rem:.1f}°" if rem is not None else
            f"[bold]{state.title}[/bold]  running max {state.running_max}  (no remaining forecast)"
        )
        t = Table()
        t.add_column("bucket"); t.add_column("market", justify="right")
        t.add_column("forecast", justify="right"); t.add_column("edge", justify="right")
        for b in state.buckets:
            key = (b.comparator, b.threshold)
            mk = market.get(key, 0.0); fc = fc_probs.get(key, 0.0)
            diff = fc - mk
            style = "bold green" if diff >= cfg.thresholds.upside_edge_min else (
                "red" if diff <= -cfg.thresholds.upside_edge_min else None)
            t.add_row(f"{b.comparator.value}{b.threshold}°{b.unit}",
                      f"{mk:.0%}", f"{fc:.0%}", f"{diff:+.0%}", style=style)
        console.print(t)

        sigs = evaluate_upside(state, fc_probs, cfg.thresholds)
        if not sigs:
            console.print("[dim]no UPSIDE edges over threshold[/dim]")
        for s in sigs:
            console.print(f"[bold]● UPSIDE {s.label}: {s.action}[/bold] — forecast {s.fair:.0%} vs market {s.market_price:.0%} (edge {s.edge:+.0%})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="london")
    a = ap.parse_args()
    asyncio.run(main(a.city))

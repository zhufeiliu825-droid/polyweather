"""Compute the running daily max over a city's LOCAL clock day. Recomputed from
the full observation list each poll, so a corrected/late METAR (downward
revision) propagates and the running max can move down."""
from __future__ import annotations

import time
from datetime import date

import httpx

from ..config import CityConfig
from ..models import EventState, Obs
from ..util.timewin import in_local_day
from .wu_client import fetch_observations


def compute_running_max(
    obs: list[Obs], tz: str, target: date
) -> tuple[float | None, int, int | None]:
    """Return (max_temp, obs_count, newest_valid_time_gmt) for the local day."""
    day = [o for o in obs if in_local_day(o.valid_time_gmt, tz, target)]
    if not day:
        return None, 0, None
    return (
        max(o.temp for o in day),
        len(day),
        max(o.valid_time_gmt for o in day),
    )


async def update_running_max(
    client: httpx.AsyncClient,
    state: EventState,
    city: CityConfig,
    api_key: str,
) -> None:
    obs = await fetch_observations(
        client,
        city.location_key,
        api_key,
        state.local_date.strftime("%Y%m%d"),
        city.unit,
    )
    mx, n, last = compute_running_max(obs, city.timezone, state.local_date)
    state.running_max = mx
    state.obs_count = n
    state.last_wu_poll_gmt = last
    state.last_wu_fetch_ts = time.time()

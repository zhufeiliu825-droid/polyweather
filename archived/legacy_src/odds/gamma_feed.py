"""Polymarket Gamma API HTTP (public, no auth). Returns raw event dicts; parsing
lives in discovery.py."""
from __future__ import annotations

from typing import Any

import httpx

GAMMA = "https://gamma-api.polymarket.com"


async def fetch_events_by_series(
    client: httpx.AsyncClient, series_id: int, *, closed: bool = False, limit: int = 20
) -> list[dict[str, Any]]:
    """Cheap per-city fetch: all events in one daily-weather series."""
    r = await client.get(
        f"{GAMMA}/events",
        params={"series_id": series_id, "closed": str(closed).lower(), "limit": limit},
    )
    r.raise_for_status()
    return r.json()


async def fetch_events_by_tag(
    client: httpx.AsyncClient, tag_id: int, *, closed: bool = False, page: int = 100
) -> list[dict[str, Any]]:
    """Breadth fetch: every live temperature event across all cities (paginated)."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = await client.get(
            f"{GAMMA}/events",
            params={
                "tag_id": tag_id,
                "closed": str(closed).lower(),
                "limit": page,
                "offset": offset,
            },
        )
        r.raise_for_status()
        batch = r.json()
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out


async def fetch_event(client: httpx.AsyncClient, event_id: str) -> dict[str, Any]:
    """Single event by id — the cheap odds-refresh path (~50KB vs the full universe)."""
    r = await client.get(f"{GAMMA}/events/{event_id}")
    r.raise_for_status()
    return r.json()

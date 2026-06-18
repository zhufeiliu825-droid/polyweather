"""Structured data-source failure log → data/source_failures.jsonl (append-only, queryable).

Every data-source failure (Open-Meteo / AWC / IEM / Gamma / CLOB / agent / Telegram / daemon) lands here
with WHAT failed AND what the system did about it (consequence), so a flaky source / dead station /
recurring CLOB outage is analyzable after the fact. PURE observability — it never changes control flow
and never raises into the caller (a logging failure must not break a tick)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .realized.feature_store import DATA_DIR

FAILURE_LOG = DATA_DIR / "source_failures.jsonl"

# controlled vocab (free-form tolerated, but prefer these for grep-ability):
#   source      : open_meteo | awc | iem | gamma | clob | agent | telegram | daemon
#   kind        : fetch_failed | http_error | stale | empty | no_event | parse_error | exception
#   consequence : skipped | escalated | fallback | stale_no_bet | retry_next_tick


def log_failure(source: str, *, kind: str, detail: str = "",
                station: str | None = None, city: str | None = None,
                consequence: str | None = None) -> None:
    """Append ONE failure record to data/source_failures.jsonl. Best-effort — swallows its own errors so
    observability can never break the caller's tick."""
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source, "kind": kind, "station": station, "city": city,
        "detail": str(detail)[:300], "consequence": consequence,
    }
    try:
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_failure_summary(within_min: int = 60, *, now: datetime | None = None) -> str:
    """One-line rollup of the last `within_min` minutes of failures, counted by source — '' if none.
    Appended to the hourly digest so a recurring source problem (or a silent Telegram outage backlog)
    is visible without grepping the log. Best-effort; never raises."""
    if not FAILURE_LOG.exists():
        return ""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=within_min)
    counts: dict[str, int] = {}
    try:
        for line in FAILURE_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ts >= cutoff:
                src = rec.get("source", "?")
                counts[src] = counts.get(src, 0) + 1
    except Exception:
        return ""
    if not counts:
        return ""
    parts = " ".join(f"{s}×{n}" for s, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return f"⚠ 上{within_min}min数据源失效: {parts}"

"""Terminal rendering of an EventState (per-bucket detail view)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.table import Table

from ..models import EventState, Signal

console = Console()

_CONF_STYLE = {"high": "bold red", "medium": "yellow", "low": "dim"}


def _fmt(x: float | None, nd: int = 3) -> str:
    return f"{x:.{nd}f}" if x is not None else "—"


def build_table(state: EventState, tz: str) -> Table:
    now_local = datetime.now(ZoneInfo(tz)).strftime("%H:%M %Z")
    mx = f"{state.running_max:g}°{state.unit}" if state.running_max is not None else "—"
    title = (
        f"{state.title}   |   local {now_local}   |   "
        f"running max {mx}   obs={state.obs_count}"
        f"   {'verified' if state.verified else 'UNVERIFIED'}"
    )
    t = Table(title=title, title_justify="left", expand=False)
    t.add_column("bucket")
    t.add_column("Yes", justify="right")
    t.add_column("bid", justify="right")
    t.add_column("ask", justify="right")
    t.add_column("liq", justify="right")
    t.add_column("state")

    cur = state.current_bucket()
    for b in state.buckets:
        dead = state.is_dead(b)
        if dead:
            tag, style = "DEAD", "dim red"
        elif b is cur:
            tag, style = "◀ max", "bold green"
        else:
            tag, style = "", None
        label = f"{b.comparator.value}{b.threshold}°{b.unit}".replace("==", " ")
        row = [
            label,
            _fmt(b.implied_p),
            _fmt(b.best_bid),
            _fmt(b.best_ask),
            _fmt(b.liquidity, 0),
            tag,
        ]
        t.add_row(*row, style=style)
    return t


def render_state(state: EventState, tz: str) -> None:
    console.print(build_table(state, tz))


def render_conditions(peak_state, regime) -> None:
    """One line: weather regime + peak/lock timing (from the conditions layer)."""
    if peak_state is None and regime is None:
        return
    parts = []
    if regime is not None:
        parts.append(f"regime={getattr(regime, 'value', regime)}")
    if peak_state is not None:
        ep = peak_state.expected_peak.strftime("%H:%M") if peak_state.expected_peak else "?"
        lock = "LOCKED" if peak_state.peak_passed else "open"
        parts.append(f"expected peak≈{ep}  max {lock} ({peak_state.reason})")
    console.print(f"[dim]· {'   '.join(parts)}[/dim]")


def render_signals(signals: list[Signal]) -> None:
    if not signals:
        console.print("[dim]no edge signals[/dim]")
        return
    for s in signals:
        style = _CONF_STYLE.get(s.confidence.value, "")
        console.print(
            f"[{style}]● {s.type.value}  {s.label}  {s.action} @ {s.market_price:g} "
            f"(fair {s.fair:g}, edge {s.edge:g}, {s.confidence.value}) — {s.note}[/{style}]"
        )

"""Core data models. The realized daily max is monotonic non-decreasing through
the day, which drives the dead-bucket logic implemented here."""
from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


class Comparator(str, Enum):
    LE = "<="   # "<X>°C or below"
    EQ = "=="   # "<X>°C"
    GE = ">="   # "<X>°C or higher"


class Bucket(BaseModel):
    """One mutually-exclusive outcome of a daily-temperature event."""
    question: str
    comparator: Comparator
    threshold: int             # in the event's resolution unit
    unit: str                  # "C" (every selected city resolves in Celsius)
    token_yes: str             # CLOB token id for the "Yes" outcome
    token_no: str
    condition_id: str
    # live market state (filled by the odds feed)
    price_yes: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade: float | None = None
    volume: float | None = None

    def contains(self, temp: float) -> bool:
        """Whether a (final) max temperature lands in this bucket."""
        if self.comparator is Comparator.LE:
            return temp <= self.threshold
        if self.comparator is Comparator.GE:
            return temp >= self.threshold
        return temp == self.threshold

    @property
    def implied_p(self) -> float | None:
        """Best available market-implied P(Yes): midpoint, else last trade, else
        the gamma price."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        if self.last_trade is not None:
            return self.last_trade
        return self.price_yes

    @property
    def sort_key(self) -> tuple[int, int]:
        # LE(0) < EQ(1) < GE(2) within equal thresholds; thresholds are distinct
        # in practice, so threshold ordering yields the low->high ladder.
        order = {Comparator.LE: 0, Comparator.EQ: 1, Comparator.GE: 2}
        return (self.threshold, order[self.comparator])


class EventState(BaseModel):
    """Live state for one city's daily-temperature event."""
    city: str
    local_date: date
    verified: bool = False
    buckets: list[Bucket] = Field(default_factory=list)   # ordered low -> high (each carries unit/tokens)
    running_max: float | None = None                      # realized max so far (resolution unit)

    def order_buckets(self) -> None:
        self.buckets.sort(key=lambda b: b.sort_key)


class Decision(BaseModel):
    """A per-city decision for the external executor (openclaw). The AGENT (or, when the LLM is down, a
    deterministic round-the-forecast-peak fallback) chooses the bucket the day's HIGH will land in; the
    deterministic gates only veto an UNSAFE bet (dead/over-ceiling/unhealthy/oversized), never pick the
    bucket. action=BET → openclaw posts a resting maker order; action=ESCALATE → push to Telegram,
    the user decides. No forced bets, no daily cap."""
    city: str
    station_id: str
    local_date: date
    label: str
    comparator: Comparator
    threshold: int
    side: str = "BUY_YES"
    order_type: str = "maker"        # always "maker" — a resting GTD bid at market−undercut (cheap buckets sized up)
    token_yes: str = ""              # CLOB token id of our_top's YES outcome (what the executor buys)
    action: str = "BET"              # "BET" (auto via openclaw) | "ESCALATE" (Telegram → user)
    confidence: float | None = None  # the chooser's confidence in the bucket (agent's 0-1; None on the deterministic fallback)
    market_yes: float | None = None  # fav's market price (health + 0.85 ceiling + maker level)
    maker_price: float | None = None  # the maker limit to post (market − undercut, tick-snapped at place time)
    size_shares: float = 10.0        # FIXED share count to buy (Polymarket sizes orders in shares); $ = shares×price
    momentum_5m: float | None = None # recent relative price drop of the fav over the momentum window (None = no signal)
    health_ok: bool = True           # market sanity (de-vig sum ~1, not absurd)
    alts: list = Field(default_factory=list)  # top-k buckets by market price [{label, mkt, alive}] — shown in the escalation
    reason: str = ""

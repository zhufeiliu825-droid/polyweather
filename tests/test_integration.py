"""End-to-end seam test (audit r9 SYS-2): a REAL decide() Decision must flow into the executor and
buy EXACTLY the bucket/token/price decide() chose — guards the producer→consumer contract that the
per-module unit tests never wired together."""
import json
from datetime import date, datetime, timezone

from polyweather import clob_executor as ex
from polyweather.decision import decide
from polyweather.models import Bucket, Comparator, EventState

_LADDER = [(Comparator.LE, 19)] + [(Comparator.EQ, t) for t in range(20, 29)] + [(Comparator.GE, 29)]
_NOW = datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc)
_PEAK = datetime(2026, 6, 15, 16, 30, tzinfo=timezone.utc)   # in the future → pre-peak


def _state(prices):
    bs = []
    for c, t in _LADDER:
        p = prices.get(t, 0.01)
        bs.append(Bucket(question=str(t), comparator=c, threshold=t, unit="C",
                         token_yes=f"TOK{t}", token_no="n", condition_id="x",
                         price_yes=p, best_bid=p, best_ask=p, last_trade=p))
    st = EventState(city="london", local_date=date(2026, 6, 15), buckets=bs, running_max=21)
    st.order_buckets()
    return st


def test_decide_to_executor_contract_dry(tmp_path, monkeypatch):
    monkeypatch.delenv("POLY_DRY_RUN", raising=False)          # default → DRY (no live submit)
    monkeypatch.setattr(ex, "ORDER_LOG", tmp_path / "orders.jsonl")

    st = _state({22: 0.50, 21: 0.40})                          # healthy book; forecast 21.9 → ==22
    d = decide(st, 21.9, "EGLC", now=_NOW, peak_utc=_PEAK)
    assert d is not None and d.action == "BET" and d.threshold == 22 and d.token_yes == "TOK22"

    r = ex.place_from_decision(d, now_iso="2026-06-15T15:00:00Z")
    assert r.ok and r.dry_run

    rec = json.loads((tmp_path / "orders.jsonl").read_text().strip().splitlines()[-1])
    assert rec["token"] == d.token_yes == "TOK22"              # executor buys EXACTLY the chosen bucket's YES token
    assert rec["status"] == "DRY_RUN"
    assert rec["shares"] == d.size_shares                      # the Decision's share count
    assert rec["price"] <= d.maker_price + 1e-9               # posts the Decision's maker price (snapped DOWN)


def test_decide_escalate_never_reaches_executor():
    # a boundary day (21.5) → decide() ESCALATEs; an ESCALATE carries no maker_price, so it is unplaceable.
    st = _state({21: 0.40, 22: 0.40})
    d = decide(st, 21.5, "EGLC", now=_NOW, peak_utc=_PEAK)
    assert d is not None and d.action == "ESCALATE" and d.maker_price is None
    assert ex._validate(d) is not None                         # executor would reject it (action != BET)

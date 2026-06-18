import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "maintain"))

import settle_event as se  # noqa: E402


def test_pnl_loss_uses_shares_times_price():
    # 10 shares @ 0.83 lost → lose the cost = -8.30 (NOT -0.0: the old code read a non-existent
    # `size_usd` field, so every real bet recorded P&L = 0 — audit r5 fatal).
    assert se.pnl_if_filled(False, 10.0, 0.83) == -8.3


def test_pnl_win_pays_one_minus_price():
    # 10 shares @ 0.83 won → each YES share pays $1, cost 0.83 → +1.70
    assert se.pnl_if_filled(True, 10.0, 0.83) == 1.7


def test_pnl_zero_when_no_shares():
    assert se.pnl_if_filled(False, 0.0, 0.5) == 0.0
    assert se.pnl_if_filled(True, 0.0, 0.5) == 0.0


def test_pnl_cheap_winner_uses_share_convention():
    # a $0.05 favorite that wins on 22.22 shares → +22.22×0.95, NOT the old notional (1-p)/p artifact
    assert se.pnl_if_filled(True, 22.22, 0.05) == round(22.22 * 0.95, 2)


def test_confirmed_fill_ignores_live_submission_and_farmer_orders():
    orders = [
        {"order_id": "expired", "station": "EGLC", "city": "London", "market_date": "2026-06-18",
         "status": "LIVE", "side": "BUY", "type": "GTD-maker", "bucket": "==25C", "price": 0.20},
        {"order_id": "farmer", "station": "EGLC", "city": "London", "market_date": "2026-06-18",
         "status": "LIVE", "side": "BUY", "type": "GTC-maker", "bucket": "==26C", "price": 0.30},
    ]
    fills = [{"strategy": "reward", "order_id": "farmer", "station": "EGLC",
              "market_date": "2026-06-18", "status": "FILLED", "filled_shares": 20.0}]
    assert se.confirmed_directional_fill(orders, fills, "EGLC", "London", "2026-06-18") == (None, None)


def test_confirmed_fill_joins_exact_directional_order():
    order = {"order_id": "main", "station": "EGLC", "city": "London", "market_date": "2026-06-18",
             "status": "LIVE", "side": "BUY", "type": "GTD-maker", "bucket": "==25C", "price": 0.20}
    fill = {"strategy": "directional", "order_id": "main", "station": "EGLC", "market_date": "2026-06-18",
            "status": "PARTIAL", "filled_shares": 4.25}
    assert se.confirmed_directional_fill([order], [fill], "EGLC", "London", "2026-06-18") == (fill, order)

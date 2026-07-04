import time
from datetime import date

import pytest

from polyweather import clob_executor as ex
from polyweather.models import Comparator, Decision


@pytest.fixture(autouse=True)
def _clear_tick_cache():
    ex._TICK_CACHE.clear()            # the per-token tick cache is module-global → reset around every test
    yield
    ex._TICK_CACHE.clear()


def _bet(**over) -> Decision:
    base = dict(city="london", station_id="EGLC", local_date=date(2026, 6, 15),
                label="==22C", comparator=Comparator.EQ, threshold=22,
                token_yes="0xabc", action="BET", confidence=0.6,
                market_yes=0.50, maker_price=0.49, size_shares=10.0, health_ok=True)
    base.update(over)
    return Decision(**base)


def test_validate_clean_bet_ok():
    assert ex._validate(_bet()) is None


def test_validate_rejects_high_price():
    assert "0.85" in (ex._validate(_bet(maker_price=0.86)) or "")


def test_validate_rejects_non_bet():
    assert "action" in (ex._validate(_bet(action="ESCALATE")) or "")


def test_validate_rejects_missing_token():
    assert "token" in (ex._validate(_bet(token_yes="")) or "")


def test_validate_rejects_oversize():
    # 999 sh × 0.49 ≈ $489 notional > $50 cap → rejected
    assert "notional" in (ex._validate(_bet(size_shares=999)) or "")


def test_validate_rejects_zero_shares():
    assert "size_shares" in (ex._validate(_bet(size_shares=0)) or "")


def test_validate_rejects_sub_dollar_notional():
    # 10 shares @ $0.05 = $0.50 < the $1 platform minimum → rejected (the real flow SIZES UP a cheap maker)
    assert "platform min" in (ex._validate(_bet(maker_price=0.05)) or "")


def test_validate_allows_sized_up_dollar_maker():
    # a cheap maker SIZED UP to clear the $1 minimum is accepted (40 sh × $0.025 = $1.00) — never a taker
    assert ex._validate(_bet(maker_price=0.025, size_shares=40)) is None


def test_validate_rejects_below_min_shares():
    # < 5 shares → below the exchange share minimum (clean local reject)
    assert "share" in (ex._validate(_bet(size_shares=3)) or "")


def test_place_dry_run_logs_no_submit(tmp_path, monkeypatch):
    monkeypatch.delenv("POLY_DRY_RUN", raising=False)          # default → DRY
    monkeypatch.setattr(ex, "ORDER_LOG", tmp_path / "orders.jsonl")
    r = ex.place_from_decision(_bet(), now_iso="2026-06-15T12:00:00Z")
    assert r.ok and r.dry_run and "DRY_RUN" in r.detail
    assert "DRY_RUN" in (tmp_path / "orders.jsonl").read_text()


def test_place_rejected_does_not_log_dry(tmp_path, monkeypatch):
    monkeypatch.delenv("POLY_DRY_RUN", raising=False)
    monkeypatch.setattr(ex, "ORDER_LOG", tmp_path / "orders.jsonl")
    r = ex.place_from_decision(_bet(maker_price=0.90), now_iso="2026-06-15T12:00:00Z")
    assert not r.ok and "rejected" in r.detail
    assert "REJECTED" in (tmp_path / "orders.jsonl").read_text()


def test_cancel_dry_run(tmp_path, monkeypatch):
    monkeypatch.delenv("POLY_DRY_RUN", raising=False)
    monkeypatch.setattr(ex, "ORDER_LOG", tmp_path / "orders.jsonl")
    r = ex.cancel_order("ord_1", now_iso="2026-06-15T12:30:00Z", reason="dead bucket")
    assert r.ok and r.dry_run and "cancel" in r.detail.lower()


# ---- live-path error handling (fake client; no py-clob-client needed) ----

class _Fake:
    def __init__(self, order=None, cancel=None):
        self._order, self._cancel = order, cancel

    def get_order(self, oid):
        if isinstance(self._order, Exception):
            raise self._order
        return self._order

    def cancel_order(self, oid):                       # v2 method name (v1's `cancel` was renamed)
        if isinstance(self._cancel, Exception):
            raise self._cancel
        return self._cancel


def _live(monkeypatch, tmp_path, fake):
    monkeypatch.setenv("POLY_DRY_RUN", "0")
    monkeypatch.setattr(ex, "ORDER_LOG", tmp_path / "orders.jsonl")
    monkeypatch.setattr(ex, "_live_client", lambda: (fake, None))


def test_order_id_key_variants():
    assert ex._order_id({"orderID": "a"}) == "a"
    assert ex._order_id({"order_id": "b"}) == "b"
    assert ex._order_id({"id": "c"}) == "c"
    assert ex._order_id({}) is None and ex._order_id("nope") is None


def test_order_status_authoritative(tmp_path, monkeypatch):
    _live(monkeypatch, tmp_path, _Fake(order={"status": "MATCHED"}))
    assert ex.order_status("x") == "FILLED"
    _live(monkeypatch, tmp_path, _Fake(order={"status": "LIVE", "size_matched": 0, "original_size": 5}))
    assert ex.order_status("x") == "OPEN"
    _live(monkeypatch, tmp_path, _Fake(order={"status": "CANCELED"}))
    assert ex.order_status("x") == "CANCELLED"
    # contradictory (matched>=size but status not terminal) → PARTIAL, never a faked FILLED
    _live(monkeypatch, tmp_path, _Fake(order={"status": "LIVE", "size_matched": 5, "original_size": 5}))
    assert ex.order_status("x") == "PARTIAL"


def test_order_status_gone_and_expired(tmp_path, monkeypatch):
    # a GTD that lapsed unfilled: empty response (order off the book) → GONE; explicit EXPIRED status → EXPIRED.
    # Both mean NOT-filled → the daemon may safely re-place (a FILL persists, so a filled order never reads gone).
    _live(monkeypatch, tmp_path, _Fake(order=None))
    assert ex.order_status("x") == "GONE"
    _live(monkeypatch, tmp_path, _Fake(order={"status": "EXPIRED"}))
    assert ex.order_status("x") == "EXPIRED"


def test_order_status_error_paths(tmp_path, monkeypatch):
    _live(monkeypatch, tmp_path, _Fake(order=RuntimeError("boom")))
    assert ex.order_status("x") == "ERROR"          # query raised → ERROR (daemon won't act)
    monkeypatch.setenv("POLY_DRY_RUN", "0")
    monkeypatch.setattr(ex, "_live_client", lambda: (None, "no creds"))
    assert ex.order_status("x") == "ERROR"
    monkeypatch.delenv("POLY_DRY_RUN", raising=False)
    assert ex.order_status("x") == "DRY"            # DRY default


def test_cancel_only_ok_when_confirmed(tmp_path, monkeypatch):
    _live(monkeypatch, tmp_path, _Fake(cancel={"canceled": ["x"]}))
    assert ex.cancel_order("x", now_iso="t").ok is True
    _live(monkeypatch, tmp_path, _Fake(cancel={"not_canceled": {"x": "err"}}))
    assert ex.cancel_order("x", now_iso="t").ok is False     # unconfirmed → ok=False (no double-bet)
    _live(monkeypatch, tmp_path, _Fake(cancel={"error": "nope"}))
    assert ex.cancel_order("x", now_iso="t").ok is False
    _live(monkeypatch, tmp_path, _Fake(cancel=RuntimeError("boom")))
    assert ex.cancel_order("x", now_iso="t").ok is False     # exception → ok=False


# ---- tick-snap (M2) + ambiguous-failure (H2) on the live place path ----

class _FakePlace:                                       # v2 surface: create_and_post_order + str tick
    def __init__(self, post=None, raise_exc=None, tick="0.01"):
        self._post, self._raise, self._tick = post, raise_exc, tick
        self.posted_price = None
        self.posted_side = None
        self.order_type = None
        self.post_only = None

    def get_tick_size(self, token):
        return self._tick                              # v2 returns the grid as a STRING ("0.01")

    def create_and_post_order(self, order_args, options=None, order_type=None, post_only=False):
        self.posted_price = order_args.price           # capture the price actually sent
        self.posted_side = getattr(order_args, "side", None)
        self.expiration = getattr(order_args, "expiration", None)   # GTD maker TTL (0 for a FAK taker)
        self.order_type, self.post_only = order_type, post_only
        if self._raise is not None:
            raise self._raise
        return self._post


def test_snap_maker_floors_to_grid():
    assert ex._snap_maker(0.835, 0.01) == 0.83          # half-tick → DOWN, never up to 0.84
    assert ex._snap_maker(0.84, 0.01) == 0.84           # already on grid → unchanged
    assert ex._snap_maker(0.495, 0.01) == 0.49
    assert abs(ex._snap_maker(0.834, 0.001) - 0.834) < 1e-9


def test_place_live_maker_is_gtd_with_expiration(tmp_path, monkeypatch):
    # the resting maker is a GTD that auto-expires after MAKER_TTL_MIN — exchange-side, never a manual cancel.
    from py_clob_client_v2 import OrderType
    fake = _FakePlace(post={"orderID": "m1"})
    _live(monkeypatch, tmp_path, fake)
    before = int(time.time())
    r = ex.place_from_decision(_bet(maker_price=0.49), now_iso="t")   # _bet defaults to a maker
    assert r.ok and r.order_id == "m1"
    assert fake.order_type == OrderType.GTD and fake.post_only is True            # resting bid, never crosses
    assert fake.expiration >= before + ex.MAKER_TTL_MIN * 60 - 5                  # ~15-min TTL set


def test_place_live_snaps_price_down(tmp_path, monkeypatch):
    fake = _FakePlace(post={"orderID": "o1"})
    _live(monkeypatch, tmp_path, fake)
    r = ex.place_from_decision(_bet(maker_price=0.835), now_iso="t")
    assert r.ok and not r.dry_run and r.order_id == "o1"
    assert abs(fake.posted_price - 0.83) < 1e-9          # posted DOWN to the grid, never rounded up
    assert '"price": 0.83' in (tmp_path / "orders.jsonl").read_text()


def test_place_live_ambiguous_on_exception(tmp_path, monkeypatch):
    _live(monkeypatch, tmp_path, _FakePlace(raise_exc=RuntimeError("connection dropped after submit")))
    r = ex.place_from_decision(_bet(), now_iso="t")
    assert not r.ok and r.ambiguous is True              # order MAY be live → caller must not re-place


def test_place_live_ambiguous_on_missing_order_id(tmp_path, monkeypatch):
    _live(monkeypatch, tmp_path, _FakePlace(post={}))    # accepted-but-no-id → ambiguous, not silent-fail
    r = ex.place_from_decision(_bet(), now_iso="t")
    assert not r.ok and r.ambiguous is True


def test_place_live_4xx_rejected_not_ambiguous(tmp_path, monkeypatch):
    # the exact live failure (status 400 "invalid order version") is a pre-acceptance reject → order NOT
    # created → REJECTED, ambiguous=False (so the daemon logs "rejected", NOT a false "may be live" UNRESOLVED).
    import httpx
    from py_clob_client_v2.exceptions import PolyApiException
    exc = PolyApiException(httpx.Response(400, json={"error": "invalid order version, please use the latest clob-client"}))
    _live(monkeypatch, tmp_path, _FakePlace(raise_exc=exc))
    r = ex.place_from_decision(_bet(), now_iso="t")
    assert not r.ok and r.ambiguous is False and "rejected" in r.detail.lower()
    assert "REJECTED" in (tmp_path / "orders.jsonl").read_text()


def test_place_live_5xx_stays_ambiguous(tmp_path, monkeypatch):
    # a 5xx MAY have landed before the error → stays ambiguous (UNRESOLVED), never re-placed.
    import httpx
    from py_clob_client_v2.exceptions import PolyApiException
    _live(monkeypatch, tmp_path, _FakePlace(raise_exc=PolyApiException(httpx.Response(503))))
    r = ex.place_from_decision(_bet(), now_iso="t")
    assert not r.ok and r.ambiguous is True


def test_place_dry_snaps_price(tmp_path, monkeypatch):
    monkeypatch.delenv("POLY_DRY_RUN", raising=False)
    monkeypatch.setattr(ex, "ORDER_LOG", tmp_path / "orders.jsonl")
    r = ex.place_from_decision(_bet(maker_price=0.835), now_iso="t")
    assert r.ok and r.dry_run and "0.83" in r.detail and not r.ambiguous


# ---- cheap-bucket MAKER (sized up to clear $1; never a taker, never crosses) ----

def test_place_live_cheap_bucket_is_gtd_maker(tmp_path, monkeypatch):
    # a cheap fav rests as a GTD maker (post_only, NOT a FAK taker), snapped DOWN to the grid — never crosses
    from py_clob_client_v2 import OrderType
    fake = _FakePlace(post={"orderID": "m9"}, tick="0.001")
    _live(monkeypatch, tmp_path, fake)
    r = ex.place_from_decision(_bet(maker_price=0.035, size_shares=30), now_iso="t")   # 30×0.035 = $1.05 ≥ $1
    assert r.ok and not r.dry_run and r.order_id == "m9"
    assert fake.order_type == OrderType.GTD and fake.post_only is True       # resting maker, never crosses
    assert abs(fake.posted_price - 0.035) < 1e-9                             # snapped DOWN to the 0.001 grid
    assert '"type": "GTD-maker"' in (tmp_path / "orders.jsonl").read_text()


# ---- GTC maker (the reward farmer's NO-EXPIRE order: rests until fill/cancel, reprices via cancel+replace) ----

def test_place_live_gtc_when_ttl_none(tmp_path, monkeypatch):
    from py_clob_client_v2 import OrderType
    fake = _FakePlace(post={"orderID": "g1"})
    _live(monkeypatch, tmp_path, fake)
    r = ex.place_from_decision(_bet(maker_price=0.49), now_iso="t", ttl_min=None)
    assert r.ok and r.order_id == "g1" and "GTC-maker" in r.detail
    assert fake.order_type == OrderType.GTC and fake.post_only is True       # rests until cancel/fill, never crosses
    assert fake.expiration == 0                                              # GTC → no expiry (vs GTD's future ts)
    assert '"type": "GTC-maker"' in (tmp_path / "orders.jsonl").read_text()


def test_place_dry_gtc_label(tmp_path, monkeypatch):
    monkeypatch.delenv("POLY_DRY_RUN", raising=False)
    monkeypatch.setattr(ex, "ORDER_LOG", tmp_path / "orders.jsonl")
    r = ex.place_from_decision(_bet(), now_iso="t", ttl_min=None)
    assert r.ok and r.dry_run and "GTC-maker" in r.detail


# ---- SELL path (Stage 2 flatten: sell held YES as a maker ask, snapped UP) ----

def test_validate_allows_sell_yes():
    assert ex._validate(_bet(side="SELL_YES", maker_price=0.55)) is None


def test_validate_sell_can_price_above_the_buy_ceiling():
    # a SELL ask may price high (we sell dear); the 0.85 ceiling is a BUY-only guard
    assert ex._validate(_bet(side="SELL_YES", maker_price=0.95)) is None
    assert "0.85" in (ex._validate(_bet(side="BUY_YES", maker_price=0.95)) or "")    # a BUY at 0.95 still rejected


def test_validate_rejects_unknown_side():
    assert "side" in (ex._validate(_bet(side="BUY_NO")) or "")


def test_snap_ask_ceils_to_grid():
    assert ex._snap_ask(0.835, 0.01) == 0.84            # half-tick → UP (mirror of _snap_maker's DOWN)
    assert ex._snap_ask(0.84, 0.01) == 0.84             # already on grid → unchanged
    assert ex._snap_ask(0.831, 0.01) == 0.84


def test_place_live_sell_is_side_sell_snapped_up(tmp_path, monkeypatch):
    from py_clob_client_v2 import Side
    fake = _FakePlace(post={"orderID": "s1"})
    _live(monkeypatch, tmp_path, fake)
    r = ex.place_from_decision(_bet(side="SELL_YES", maker_price=0.835), now_iso="t", ttl_min=None)
    assert r.ok and r.order_id == "s1"
    assert fake.posted_side == Side.SELL and abs(fake.posted_price - 0.84) < 1e-9   # ask snapped UP, never down
    assert fake.post_only is True                                                   # still a maker (never crosses)
    assert '"side": "SELL"' in (tmp_path / "orders.jsonl").read_text()


def test_place_dry_sell_label(tmp_path, monkeypatch):
    monkeypatch.delenv("POLY_DRY_RUN", raising=False)
    monkeypatch.setattr(ex, "ORDER_LOG", tmp_path / "orders.jsonl")
    r = ex.place_from_decision(_bet(side="SELL_YES", maker_price=0.55), now_iso="t")
    assert r.ok and r.dry_run and "SELL" in r.detail


# ---- TAKER: SELL = the farmer's 关门时间 force-flatten; BUY = the cluster/snipe FAK entry ----

def test_validate_allows_taker_both_sides():
    assert ex._validate(_bet(side="BUY_YES", order_type="taker")) is None            # cluster/snipe FAK entry
    assert ex._validate(_bet(side="SELL_YES", order_type="taker", maker_price=0.55)) is None     # farmer force-flatten
    assert "0.85" in (ex._validate(_bet(side="BUY_YES", order_type="taker", maker_price=0.86)) or "")  # ceiling still binds


def test_place_live_taker_buy_is_fak_snapped_up(tmp_path, monkeypatch):
    from py_clob_client_v2 import OrderType, Side
    fake = _FakePlace(post={"orderID": "b1", "takingAmount": "10", "makingAmount": "8.4"})
    _live(monkeypatch, tmp_path, fake)
    r = ex.place_from_decision(_bet(side="BUY_YES", order_type="taker", maker_price=0.831), now_iso="t", ttl_min=None)
    assert r.ok and r.order_id == "b1" and "FAK-taker" in r.detail
    assert fake.order_type == OrderType.FAK and fake.post_only is False              # crosses → NOT post_only
    assert fake.posted_side == Side.BUY and fake.expiration == 0
    assert abs(fake.posted_price - 0.84) < 1e-9                                      # taker BUY snaps UP (≥ ask → marketable)
    assert r.filled == 10.0                                                          # BUY fill = takingAmount (shares received)


def test_taker_filled_parses_by_side():
    resp = {"takingAmount": "10", "makingAmount": "8.4"}
    assert ex._taker_filled(resp, is_sell=False) == 10.0                             # BUY: shares = takingAmount
    assert ex._taker_filled(resp, is_sell=True) == 8.4                               # SELL: shares = makingAmount
    assert ex._taker_filled({"status": "matched"}, is_sell=False) is None            # no parsable amount → UNCONFIRMED
    assert ex._taker_filled({"takingAmount": "oops"}, is_sell=False) is None


def test_place_live_taker_is_fak_crosses(tmp_path, monkeypatch):
    from py_clob_client_v2 import OrderType, Side
    fake = _FakePlace(post={"orderID": "f1"})
    _live(monkeypatch, tmp_path, fake)
    r = ex.place_from_decision(_bet(side="SELL_YES", order_type="taker", maker_price=0.835), now_iso="t", ttl_min=None)
    assert r.ok and r.order_id == "f1" and "FAK-taker" in r.detail
    assert fake.order_type == OrderType.FAK and fake.post_only is False              # crosses → NOT post_only
    assert fake.posted_side == Side.SELL and fake.expiration == 0                    # SELL, immediate
    assert abs(fake.posted_price - 0.83) < 1e-9                                      # taker SELL snaps DOWN (≤ bid → marketable)


def test_place_live_taker_no_rest_id_still_ok(tmp_path, monkeypatch):
    _live(monkeypatch, tmp_path, _FakePlace(post={"status": "matched"}))             # FAK filled, no resting id
    r = ex.place_from_decision(_bet(side="SELL_YES", order_type="taker", maker_price=0.50), now_iso="t", ttl_min=None)
    assert r.ok and r.order_id is None                                               # accepted (filled), nothing rested


def test_place_live_taker_error_resp_fails(tmp_path, monkeypatch):
    _live(monkeypatch, tmp_path, _FakePlace(post={"error": "bad"}))
    r = ex.place_from_decision(_bet(side="SELL_YES", order_type="taker", maker_price=0.50), now_iso="t", ttl_min=None)
    assert not r.ok                                                                  # an error response is a real failure


# ---- per-token tick cache (no get_tick_size round-trip per order/reprice) ----

def test_tick_size_caches_per_token():
    calls = []
    class _C:
        def get_tick_size(self, token):
            calls.append(token)
            return "0.01"
    c = _C()
    assert ex._tick_size(c, "0xT")[1] == 0.01
    assert ex._tick_size(c, "0xT")[1] == 0.01            # served from cache
    assert calls == ["0xT"]                              # fetched only once


def test_tick_size_failure_not_cached():
    calls = []
    class _C:
        def get_tick_size(self, token):
            calls.append(token)
            raise RuntimeError("lookup down")
    c = _C()
    assert ex._tick_size(c, "0xT") == ("0.01", 0.01)     # safe default
    assert ex._tick_size(c, "0xT") == ("0.01", 0.01)
    assert calls == ["0xT", "0xT"]                       # retried (a transient failure is NOT cached)


# ---- book_bid_ask: one-sided book keeps the live side (not discarded as no-book); fetch failure → None ----

class _BookResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
    def json(self):
        return self._payload


def test_book_bid_ask_one_sided_keeps_bid(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _BookResp({"bids": [{"price": "0.30", "size": "9"}], "asks": []}))
    assert ex.book_bid_ask("0xT") == (0.30, None)        # bid kept, ask None — NOT discarded (one-sided book is forceable)


def test_book_bid_ask_both_sides(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _BookResp(
        {"bids": [{"price": "0.30", "size": "9"}, {"price": "0.29", "size": "5"}],
         "asks": [{"price": "0.34", "size": "9"}, {"price": "0.36", "size": "5"}]}))
    assert ex.book_bid_ask("0xT") == (0.30, 0.34)        # best bid = max, best ask = min


def test_book_bid_ask_failure_is_none(monkeypatch):
    import httpx
    def boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(httpx, "get", boom)
    assert ex.book_bid_ask("0xT") is None                # fetch failure → whole None (caller skips the token)


# ---- filled_size: order-level matched shares (co-hold-independent) for the maker-cancel ceiling decrement ----

def test_filled_size_dry_is_none(monkeypatch):
    monkeypatch.setenv("POLY_DRY_RUN", "1")
    assert ex.filled_size("o1") is None                  # DRY → None (caller keeps its cur-based clamp)


def test_filled_size_reads_size_matched(monkeypatch):
    monkeypatch.setenv("POLY_DRY_RUN", "0")
    class _C:
        def get_order(self, oid):
            return {"size_matched": "30", "original_size": "50", "status": "PARTIAL"}
    monkeypatch.setattr(ex, "_live_client", lambda: (_C(), None))
    assert ex.filled_size("o1") == 30.0


def test_filled_size_error_is_none(monkeypatch):
    monkeypatch.setenv("POLY_DRY_RUN", "0")
    class _C:
        def get_order(self, oid):
            raise RuntimeError("down")
    monkeypatch.setattr(ex, "_live_client", lambda: (_C(), None))
    assert ex.filled_size("o1") is None                  # error → None (fail open to the cur-based clamp)

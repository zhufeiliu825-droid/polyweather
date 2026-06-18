from datetime import datetime, timezone

import pytest

from polyweather.rewards import scan as rscan


def _mkt(q, rate, yes_first=True):
    toks = '["TOKy","TOKn"]' if yes_first else '["TOKn","TOKy"]'
    outs = '["Yes","No"]' if yes_first else '["No","Yes"]'
    return {"question": q, "clobRewards": ([{"rewardsDailyRate": rate}] if rate else None),
            "clobTokenIds": toks, "outcomes": outs, "outcomePrices": '["0.22","0.78"]',
            "conditionId": "0xabc", "rewardsMinSize": 50, "rewardsMaxSpread": 4.5,
            "bestBid": "0.22", "bestAsk": "0.23"}


def test_reward_buckets_parses_only_rewarded():
    ev = {"title": "Highest temperature in Seoul on June 18?", "endDate": "2026-06-18T12:00:00Z",
          "markets": [_mkt("Will the highest temperature in Seoul be 27°C on June 18?", 192),
                      _mkt("Will the highest temperature in Seoul be 30°C on June 18?", 0)]}   # 0 reward → skipped
    rbs = rscan.reward_buckets(ev)
    assert len(rbs) == 1
    rb = rbs[0]
    assert rb.city == "seoul" and rb.market_date == "2026-06-18"
    assert rb.comparator == "==" and rb.threshold == 27 and rb.daily_rate == 192
    assert rb.min_size == 50 and rb.max_spread == 4.5 and rb.token_yes == "TOKy"


def test_reward_buckets_keeps_expired_reward_for_held_token_context():
    ev = {"title": "Highest temperature in Seoul on June 18?", "endDate": "2026-06-18T12:00:00Z",
          "markets": [_mkt("Will the highest temperature in Seoul be 27°C on June 18?", 0)]}
    assert rscan.reward_buckets(ev) == []
    [rb] = rscan.reward_buckets(ev, include_tokens={"TOKy"})
    assert rb.token_yes == "TOKy" and rb.daily_rate == 0


def test_reward_buckets_maps_yes_token_when_outcomes_are_no_first():
    # Gamma outcome ORDER is data-driven — token_yes must come from the outcomes array, never positional
    ev = {"title": "Highest temperature in Paris on June 18?", "endDate": "2026-06-18T12:00:00Z",
          "markets": [_mkt("Will the highest temperature in Paris be 32°C or above on June 18?", 57, yes_first=False)]}
    rb = rscan.reward_buckets(ev)[0]
    assert rb.comparator == ">=" and rb.threshold == 32 and rb.token_yes == "TOKy"


def test_our_yes_lean_directions():
    assert rscan.our_yes_lean("==", 27, 27.0) == 1.0
    assert rscan.our_yes_lean("==", 27, 27.4) == pytest.approx(0.6)
    assert rscan.our_yes_lean("==", 27, 29.0) == 0.0
    assert rscan.our_yes_lean(">=", 30, 31.0) == 1.0 and rscan.our_yes_lean(">=", 30, 28.0) == 0.1
    assert rscan.our_yes_lean("<=", 20, 18.0) == 1.0 and rscan.our_yes_lean("<=", 20, 23.0) == 0.1
    assert rscan.our_yes_lean("==", 27, None) is None                       # no forecast → no lean


def test_book_mid_competition():
    book = {"bids": [{"price": "0.22", "size": "100"}, {"price": "0.20", "size": "500"}],
            "asks": [{"price": "0.23", "size": "80"}, {"price": "0.30", "size": "999"}]}
    mid, comp, spread = rscan.book_mid_competition(book, 4.5)                # mid 0.225, band ±0.045
    assert mid == pytest.approx(0.225)
    assert comp == 100 + 500 + 80                                           # 0.30 ask is 0.075 away → excluded
    assert spread == pytest.approx(0.01)                                    # 0.23 − 0.22 → sane, real book
    assert rscan.book_mid_competition({"bids": [], "asks": []}, 4.5) == (None, 0.0, None)


def test_score_rewards_edge_low_competition():
    assert rscan.score(200, 1.0, 100, 50) > rscan.score(200, 1.0, 4000, 50)   # less competition → higher
    assert rscan.score(200, 1.0, 100, 50) > rscan.score(200, 0.2, 100, 50)    # stronger lean → higher
    assert rscan.score(200, None, 100, 50) == 0.0                             # no forecast lean → 0


def test_reward_buckets_attaches_devig_sum():
    # de-vig sum = Σ of ALL the event's bucket Yes-prices (~1 in a real book); 2 toy buckets at 0.22 each → 0.44
    ev = {"title": "Highest temperature in Seoul on June 18?", "endDate": "2026-06-18T12:00:00Z",
          "markets": [_mkt("Will the highest temperature in Seoul be 27°C on June 18?", 192),
                      _mkt("Will the highest temperature in Seoul be 28°C on June 18?", 80)]}
    rbs = rscan.reward_buckets(ev)
    assert rbs and all(rb.devig_sum == pytest.approx(0.44) for rb in rbs)     # both markets counted in the sum


def test_is_future_event():
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)                   # Seoul (UTC+9) local date = 6-18
    assert rscan._is_future_event("2026-06-19", now, "Asia/Seoul") is True    # tomorrow → farmer's scope
    assert rscan._is_future_event("2026-06-18", now, "Asia/Seoul") is False   # today = directional bot's market
    assert rscan._is_future_event("2026-06-17", now, "Asia/Seoul") is False   # past


def test_current_or_future_event_includes_today_for_valuation():
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    assert rscan._is_current_or_future_event("2026-06-19", now, "Asia/Seoul") is True
    assert rscan._is_current_or_future_event("2026-06-18", now, "Asia/Seoul") is True
    assert rscan._is_current_or_future_event("2026-06-17", now, "Asia/Seoul") is False

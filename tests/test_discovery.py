import json

import pytest

from polyweather.discovery import _bucket_from_market, parse_bucket_question
from polyweather.models import Comparator


@pytest.mark.parametrize(
    "q,comp,thr,unit",
    [
        ("Will the highest temperature in London be 19°C or below on June 13?", Comparator.LE, 19, "C"),
        ("Will the highest temperature in London be 20°C on June 13?", Comparator.EQ, 20, "C"),
        ("Will the highest temperature in London be 29°C or higher on June 13?", Comparator.GE, 29, "C"),
    ],
)
def test_parse_bucket_question(q, comp, thr, unit):
    assert parse_bucket_question(q) == (comp, thr, unit)


def test_parse_bucket_question_no_threshold():
    with pytest.raises(ValueError):
        parse_bucket_question("Will it rain in London tomorrow?")


_Q = "Will the highest temperature in London be 25°C on June 16?"


def _mkt(outcomes, tokens, prices):
    return {"question": _Q, "outcomes": json.dumps(outcomes), "clobTokenIds": json.dumps(tokens),
            "outcomePrices": json.dumps(prices), "conditionId": "cond"}


def test_bucket_maps_yes_by_outcomes_yes_first():
    b = _bucket_from_market(_mkt(["Yes", "No"], ["TOK_YES", "TOK_NO"], ["0.3", "0.7"]))
    assert b is not None and b.token_yes == "TOK_YES" and b.token_no == "TOK_NO" and b.price_yes == 0.3


def test_bucket_maps_yes_by_outcomes_no_first():
    # No-first ordering: token_yes/price_yes must follow the 'Yes' INDEX, not position 0 (r6 wrong-token fix)
    b = _bucket_from_market(_mkt(["No", "Yes"], ["TOK_NO", "TOK_YES"], ["0.7", "0.3"]))
    assert b is not None and b.token_yes == "TOK_YES" and b.token_no == "TOK_NO" and b.price_yes == 0.3


def test_bucket_fail_closed_on_bad_outcomes():
    # missing outcomes → skip (never bet a positional guess)
    assert _bucket_from_market({"question": _Q, "clobTokenIds": '["A","B"]', "outcomePrices": '["0.3","0.7"]'}) is None
    # non-{yes,no} outcomes → skip
    assert _bucket_from_market(_mkt(["Maybe", "No"], ["A", "B"], ["0.3", "0.7"])) is None
    # fewer than 2 tokens → skip (no tradeable YES token)
    assert _bucket_from_market(_mkt(["Yes", "No"], ["A"], ["0.3", "0.7"])) is None

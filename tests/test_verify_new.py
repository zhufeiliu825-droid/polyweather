import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "maintain"))

import verify_new as vn  # noqa: E402

_Q21 = "Will the highest temperature in London be 21°C on June 15?"
_Q22 = "Will the highest temperature in London be 22°C on June 15?"


def _mkt(q, outcomes, prices):
    return {"question": q, "outcomes": json.dumps(outcomes), "outcomePrices": json.dumps(prices)}


def test_winner_yes_first():
    ev = {"markets": [_mkt(_Q21, ["Yes", "No"], ["1", "0"]),    # 21 won
                      _mkt(_Q22, ["Yes", "No"], ["0", "1"])]}   # 22 lost
    w = vn.winner(ev)
    assert w is not None and w.threshold == 21


def test_winner_no_first_uses_outcomes_not_position():
    # No-first ordering: the WINNING bucket has YES=1 at the 'yes' INDEX (pr[1]), not pr[0].
    # The old `pr[0]==1.0` would wrongly pick the LOSING ==22 bucket (whose NO=1 sits at pr[0]).
    ev = {"markets": [_mkt(_Q21, ["No", "Yes"], ["0", "1"]),    # 21 won: No=0, Yes=1
                      _mkt(_Q22, ["No", "Yes"], ["1", "0"])]}   # 22 lost: No=1, Yes=0
    w = vn.winner(ev)
    assert w is not None and w.threshold == 21                  # correct winner, not 22


def test_winner_skips_non_binary_market():
    ev = {"markets": [_mkt(_Q21, ["Maybe", "No"], ["1", "0"])]}
    assert vn.winner(ev) is None

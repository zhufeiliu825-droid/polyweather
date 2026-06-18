from polyweather import params as p


def test_defaults_match_strategy():
    assert p.PRICE_CEILING == 0.85
    assert p.SIZE_SHARES == 10.0
    assert p.MAX_SIZE_USD == 50.0
    assert p.MIN_ORDER_USD == 1.0              # platform $ minimum (the taker is sized to clear it)
    assert p.MIN_ORDER_SHARES == 5.0
    assert p.MAKER_UNDERCUT == 0.001           # −0.1¢ (snapped to the market's real tick at place time)
    assert p.MOMENTUM_DROP_PCT == 0.30         # ≥ this relative drop over the window → pass-1 defers
    assert p.MOMENTUM_WINDOW_MIN == 5          # momentum look-back (minutes)
    assert p.STRIKE_ENABLED == 1               # strike-timing stage on (0 → exact pre-strike behaviour)
    assert p.STRIKE_MIN_LAG_H == 0.0           # strike window lower bound = solar_noon + this
    assert p.STRIKE_CUTOFF_BUFFER_MIN == 45
    assert p.STRIKE_GRACE_MIN == 10
    assert p.ACTIVATE_WINDOW_MIN == 50          # pass-1 activates ≈ peak−50 (room for the +20min pass-2 to fill)
    assert p.AGENT_RETRY_MIN == 20              # gap before the pass-2 (gpt-5.4) retry
    assert p.SCAN_INTERVAL_MIN == 30           # scan every 30min, using the 15-min curve (sharper peak time)
    assert p.RECHECK_MIN == 30
    assert p.AWC_FRESH_MIN == 90


def test_float_helper_env_override(monkeypatch):
    monkeypatch.setenv("POLY_X", "0.80")
    assert p._f("POLY_X", 0.85) == 0.80          # env wins
    assert p._f("POLY_MISSING", 0.85) == 0.85    # default when unset


def test_helpers_fall_back_on_bad_value(monkeypatch):
    monkeypatch.setenv("POLY_BAD", "notanumber")
    assert p._f("POLY_BAD", 0.85) == 0.85        # garbage → default, never crash
    assert p._i("POLY_BAD", 90) == 90


def test_int_helper(monkeypatch):
    monkeypatch.setenv("POLY_N", "120")
    assert p._i("POLY_N", 90) == 120


def test_bounded_float_rejects_out_of_range(monkeypatch):
    # a fat-finger outside [lo,hi] → safe default, never silently disabling a gate (audit r5)
    monkeypatch.setenv("POLY_X", "1.5")
    assert p._fb("POLY_X", 0.85, 0.01, 0.99) == 0.85     # 1.5 > 0.99 → default (would auto-bet a 96¢ outcome)
    monkeypatch.setenv("POLY_X", "0.0")
    assert p._fb("POLY_X", 0.10, 0.01, 1.0) == 0.10      # 0 < 0.01 → default (would auto-bet a coin-flip)
    monkeypatch.setenv("POLY_X", "0.90")
    assert p._fb("POLY_X", 0.85, 0.01, 0.99) == 0.90     # in range → honored


def test_bounded_int_rejects_out_of_range(monkeypatch):
    monkeypatch.setenv("POLY_W", "600")
    assert p._ib("POLY_W", 30, 1, 120) == 30             # 600 > 120 → default (no 9h-pre-peak activation)
    monkeypatch.setenv("POLY_W", "45")
    assert p._ib("POLY_W", 30, 1, 120) == 45             # in range → honored


def test_float_rejects_non_finite(monkeypatch):
    # nan/inf must fall back — else `notional > nan` / `spread > nan` is False and the cap FAILS OPEN (r5)
    for bad in ("nan", "inf", "-inf", "Infinity"):
        monkeypatch.setenv("POLY_X", bad)
        assert p._f("POLY_X", 50.0) == 50.0


def test_safety_knobs_in_hard_range():
    # the live defaults must satisfy their own invariants
    assert 0 < p.PRICE_CEILING < 1
    assert p.SIZE_SHARES >= p.MIN_ORDER_SHARES >= 5
    assert 0 < p.MAX_SIZE_USD <= 500
    assert p.HEALTH_SUM_LO < p.HEALTH_SUM_HI
    assert p.MIN_ORDER_USD > 0

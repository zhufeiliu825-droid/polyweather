import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run"))

import telegram_digest as td  # noqa: E402


def _city(station, city, strike_utc, market_date):
    return {"station": station, "city": city, "strike_utc": strike_utc, "market_date": market_date}


def test_digest_shows_order_status():
    # the line shows the fine status (挂单中 = resting/dry maker, 已成交 = filled), not the old binary 已下注
    pool = {"cities": [_city("EFHK", "Helsinki", "2026-06-17 20:00Z", "2026-06-17")], "excluded": []}
    when = datetime(2026, 6, 17, 6, 0, tzinfo=timezone.utc)
    resting = td.render_digest(pool, {"EFHK": "Europe/Helsinki"},
                               {"EFHK": {"bucket": "16", "price": 0.463, "status": "挂单中"}}, when)
    assert "挂单中 @16°C @0.463" in resting and "已下注" not in resting
    filled = td.render_digest(pool, {"EFHK": "Europe/Helsinki"},
                              {"EFHK": {"bucket": "16", "price": 0.463, "status": "已成交"}}, when)
    assert "已成交 @16°C @0.463" in filled


def test_offset_correct_on_dst_transition_day():
    # r6/r7 TG-1: Wellington 2026-04-05 (NZDT→NZST fall-back). A 14:00-local peak is AFTER the transition
    # → utc+12, NOT the buggy utc+13 that z.utcoffset(aware-UTC) produced by misreading the fold.
    pool = {"cities": [_city("NZWN", "Wellington", "2026-04-05 01:00Z", "2026-04-05")], "excluded": []}
    out = td.render_digest(pool, {"NZWN": "Pacific/Auckland"}, {},
                           datetime(2026, 4, 5, 6, 0, tzinfo=timezone.utc))
    assert "utc+12" in out and "1400" in out and "utc+13" not in out


def test_sorted_by_utc_offset_west_to_east():
    # USER SPEC: sort by the UTC OFFSET (utc-12 → utc+14, west→east), so Buenos Aires (utc-3) comes
    # BEFORE London (utc+1) — regardless of the absolute UTC peak instant.
    pool = {"cities": [
        _city("EGLC", "London", "2026-06-15 11:00Z", "2026-06-15"),         # utc+1, peak 12:00Z
        _city("SAEZ", "Buenos Aires", "2026-06-15 19:00Z", "2026-06-15"),   # utc-3, peak 20:00Z
    ], "excluded": []}
    out = td.render_digest(pool, {"SAEZ": "America/Argentina/Buenos_Aires", "EGLC": "Europe/London"},
                           {}, datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc))
    body = out.splitlines()[1:]
    assert body[0].startswith("20260615buenosaires") and body[1].startswith("20260615london")  # utc-3 < utc+1
    # each line still shows the UTC peak time (…Z) + local (…当地)
    assert "2000Z" in body[0] and "(1700当地)" in body[0]           # BA: peak 20:00Z, local 17:00
    assert "1200Z" in body[1] and "(1300当地)" in body[1]           # London: peak 12:00Z, local 13:00

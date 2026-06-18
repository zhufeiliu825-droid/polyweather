from datetime import date, datetime

from polyweather.forecast.upside import remaining_corrected_max


def test_remaining_corrected_max_applies_bias():
    hourly = [
        (datetime(2026, 6, 13, 16, 0), 21.0),
        (datetime(2026, 6, 13, 17, 0), 21.4),  # after 16:30 -> counts
        (datetime(2026, 6, 13, 18, 0), 20.0),
    ]
    now = datetime(2026, 6, 13, 16, 30)
    rem = remaining_corrected_max(hourly, now, date(2026, 6, 13), bias=0.2)
    assert abs(rem - (21.4 - 0.2)) < 1e-9


def test_remaining_corrected_max_raw_when_no_bias():
    hourly = [(datetime(2026, 6, 13, 17, 0), 25.9), (datetime(2026, 6, 13, 18, 0), 24.0)]
    rem = remaining_corrected_max(hourly, datetime(2026, 6, 13, 16, 0), date(2026, 6, 13))
    assert rem == 25.9                              # default bias 0 → the raw forecast peak


def test_remaining_corrected_max_none_when_no_future_hours():
    hourly = [(datetime(2026, 6, 13, 12, 0), 20.0)]
    assert remaining_corrected_max(hourly, datetime(2026, 6, 13, 16, 0), date(2026, 6, 13)) is None

from datetime import datetime, timezone

from maintain import build_residuals as B


def test_local_today_prevents_unsettled_american_day_from_entering_residuals():
    now = datetime(2026, 6, 19, 1, 30, tzinfo=timezone.utc)
    assert str(B._local_today(now, "America/Toronto")) == "2026-06-18"
    assert str(B._local_today(now, "Asia/Seoul")) == "2026-06-19"

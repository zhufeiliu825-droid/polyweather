from datetime import date

from polyweather.util.timewin import epoch_to_local, in_local_day, local_date_of

# 1781133600 == 2026-06-10 23:20Z == 2026-06-11 00:20 BST (verified against the
# WU EGLC observations endpoint). The UTC date is the 10th but the LOCAL date is
# the 11th — the exact case the local-day window must get right.
BST_BOUNDARY = 1781133600


def test_bst_boundary_local_date():
    assert local_date_of(BST_BOUNDARY, "Europe/London") == date(2026, 6, 11)


def test_bst_offset_is_plus_one_in_june():
    dt = epoch_to_local(BST_BOUNDARY, "Europe/London")
    assert dt.hour == 0 and dt.minute == 20
    assert dt.utcoffset().total_seconds() == 3600


def test_in_local_day():
    assert in_local_day(BST_BOUNDARY, "Europe/London", date(2026, 6, 11))
    assert not in_local_day(BST_BOUNDARY, "Europe/London", date(2026, 6, 10))

from datetime import date

from polyweather.models import Obs
from polyweather.realized.daily_max import compute_running_max

TZ = "Europe/London"
TARGET = date(2026, 6, 11)

# epochs around the 2026-06-11 BST day (UTC 2026-06-10 23:20Z .. 2026-06-11 22:50Z)
E_2320_PREV = 1781133600   # 2026-06-10 23:20Z == 2026-06-11 00:20 BST  (belongs to TARGET)
E_NOON = 1781175600        # 2026-06-11 11:00Z == 12:00 BST              (TARGET)
E_NEXT_DAY = 1781220000    # 2026-06-11 23:20Z == 2026-06-12 00:20 BST   (NOT target)


def test_running_max_over_local_day():
    obs = [
        Obs(valid_time_gmt=E_2320_PREV, temp=11),
        Obs(valid_time_gmt=E_NOON, temp=17),
    ]
    mx, n, last = compute_running_max(obs, TZ, TARGET)
    assert mx == 17 and n == 2 and last == E_NOON


def test_excludes_other_local_day():
    obs = [
        Obs(valid_time_gmt=E_NOON, temp=17),
        Obs(valid_time_gmt=E_NEXT_DAY, temp=25),  # next BST day, must be ignored
    ]
    mx, n, _ = compute_running_max(obs, TZ, TARGET)
    assert mx == 17 and n == 1


def test_empty_day():
    assert compute_running_max([], TZ, TARGET) == (None, 0, None)


def test_downward_revision_lowers_max():
    # simulate a correction: the 17 reading was revised down to 15
    before = compute_running_max([Obs(valid_time_gmt=E_NOON, temp=17)], TZ, TARGET)
    after = compute_running_max([Obs(valid_time_gmt=E_NOON, temp=15)], TZ, TARGET)
    assert before[0] == 17 and after[0] == 15

from datetime import datetime

from email_date_range.model import DateStats
from email_date_range.timeline import add_months, compute_gaps, month_iter, months_between


def test_month_arithmetic():
    assert add_months((2020, 12), 1) == (2021, 1)
    assert add_months((2021, 1), -1) == (2020, 12)
    assert months_between((2020, 11), (2021, 2)) == 4
    assert list(month_iter((2020, 11), (2021, 2))) == [(2020, 11), (2020, 12), (2021, 1), (2021, 2)]
    assert list(month_iter((2021, 2), (2020, 11))) == []


def test_gaps_across_year_boundary():
    by_month = {(2020, 10): 5, (2021, 3): 2, (2021, 4): 1, (2021, 6): 9}
    assert compute_gaps(by_month) == [
        {"start": (2020, 11), "end": (2021, 2), "length": 4},
        {"start": (2021, 5), "end": (2021, 5), "length": 1},
    ]
    assert compute_gaps(by_month, min_gap_months=2) == [
        {"start": (2020, 11), "end": (2021, 2), "length": 4},
    ]


def test_no_gaps_for_contiguous_or_single_month():
    assert compute_gaps({(2020, 1): 1, (2020, 2): 1}) == []
    assert compute_gaps({(2020, 1): 1}) == []
    assert compute_gaps({}) == []


def test_datestats_merge_and_bounds():
    a, b = DateStats(), DateStats()
    a.add(datetime(2001, 5, 1), "delivery_time")
    a.add(None, None)
    b.add(datetime(1999, 1, 1), "creation_time")
    b.add(datetime(1601, 1, 1), "delivery_time")
    a.merge(b)
    assert (a.dated, a.no_date, a.implausible) == (2, 1, 1)
    assert a.earliest == datetime(1999, 1, 1) and a.latest == datetime(2001, 5, 1)
    assert a.fallback_dates == 1
    assert a.span_days == (datetime(2001, 5, 1) - datetime(1999, 1, 1)).days

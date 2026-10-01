"""Month arithmetic and gap analysis."""

from __future__ import annotations

from collections.abc import Iterator

YearMonth = tuple[int, int]


def add_months(ym: YearMonth, n: int) -> YearMonth:
    year, month0 = divmod(ym[0] * 12 + ym[1] - 1 + n, 12)
    return year, month0 + 1


def months_between(start: YearMonth, end: YearMonth) -> int:
    """Number of months from start to end inclusive."""
    return (end[0] - start[0]) * 12 + end[1] - start[1] + 1


def month_iter(start: YearMonth, end: YearMonth) -> Iterator[YearMonth]:
    """Yield (year, month) tuples from start to end inclusive."""
    for n in range(max(0, months_between(start, end))):
        yield add_months(start, n)


def ym_label(ym: YearMonth) -> str:
    return f"{ym[0]}-{ym[1]:02d}"


def compute_gaps(by_month, min_gap_months: int = 1) -> list[dict]:
    """Runs of consecutive months with no mail between the first and last active month.

    Returns [{"start": (y, m), "end": (y, m), "length": months}, ...] in
    chronological order. Long silent stretches inside an otherwise busy
    mailbox can mean deleted mail, an incomplete export, or spoliation.
    """
    active = sorted(ym for ym, count in by_month.items() if count)
    gaps = []
    for prev, cur in zip(active, active[1:], strict=False):
        start, end = add_months(prev, 1), add_months(cur, -1)
        length = months_between(start, end)
        if length >= max(1, min_gap_months):
            gaps.append({"start": start, "end": end, "length": length})
    return gaps

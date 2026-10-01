"""Result containers shared by the PST and MBOX scanners."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Dates outside this window are almost always artefacts: a zeroed FILETIME
# (1601-01-01), a broken Date header (1970-01-01), or a sender whose clock was
# decades off. They are counted and exported, but kept out of the range and
# the distributions so a single bad item cannot stretch the timeline.
EARLIEST_PLAUSIBLE = datetime(1980, 1, 1)
FUTURE_TOLERANCE = timedelta(days=2)

# Date sources that do not describe when the mail was sent or received.
# A high share of these means the timeline should be treated with suspicion.
FALLBACK_SOURCES = frozenset({"creation_time", "received_header", "envelope"})

_SAMPLE_LIMIT = 5


def utc_now() -> datetime:
    """Current UTC time as a naive datetime (all dates in this tool are naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def latest_plausible(now: datetime | None = None) -> datetime:
    return (now or utc_now()) + FUTURE_TOLERANCE


@dataclass
class DateStats:
    """Running date statistics for one mailbox (or several, after merge())."""

    not_after: datetime = field(default_factory=latest_plausible)
    by_year: Counter = field(default_factory=Counter)
    by_month: Counter = field(default_factory=Counter)  # (year, month) -> count
    sources: Counter = field(default_factory=Counter)  # date source -> count
    earliest: datetime | None = None
    latest: datetime | None = None
    dated: int = 0
    no_date: int = 0
    implausible: int = 0
    implausible_samples: list = field(default_factory=list)

    def is_plausible(self, dt: datetime) -> bool:
        return EARLIEST_PLAUSIBLE <= dt <= self.not_after

    def add(self, dt: datetime | None, source: str | None) -> bool:
        """Record one item. Returns False if the date was missing or implausible."""
        if dt is None:
            self.no_date += 1
            return False
        if not self.is_plausible(dt):
            self.implausible += 1
            if len(self.implausible_samples) < _SAMPLE_LIMIT:
                self.implausible_samples.append(dt)
            return False
        self.dated += 1
        self.by_year[dt.year] += 1
        self.by_month[(dt.year, dt.month)] += 1
        self.sources[source] += 1
        if self.earliest is None or dt < self.earliest:
            self.earliest = dt
        if self.latest is None or dt > self.latest:
            self.latest = dt
        return True

    def merge(self, other: DateStats) -> None:
        self.by_year.update(other.by_year)
        self.by_month.update(other.by_month)
        self.sources.update(other.sources)
        self.dated += other.dated
        self.no_date += other.no_date
        self.implausible += other.implausible
        room = _SAMPLE_LIMIT - len(self.implausible_samples)
        self.implausible_samples.extend(other.implausible_samples[:max(0, room)])
        for dt in (other.earliest, other.latest):
            if dt is None:
                continue
            if self.earliest is None or dt < self.earliest:
                self.earliest = dt
            if self.latest is None or dt > self.latest:
                self.latest = dt

    @property
    def fallback_dates(self) -> int:
        return sum(n for src, n in self.sources.items() if src in FALLBACK_SOURCES)

    @property
    def span_days(self) -> int | None:
        if self.earliest is None or self.latest is None:
            return None
        return (self.latest - self.earliest).days


@dataclass
class ScanResult:
    """Everything the reports need to know about one scanned file."""

    file: str
    size_mb: float
    method: str | None = None
    stats: DateStats = field(default_factory=DateStats)
    recovered: int = 0
    unreadable: int = 0
    skipped_non_mail: int = 0
    item_classes: Counter = field(default_factory=Counter)
    error: str | None = None
    elapsed: float = 0.0
    export_path: str | None = None
    exported_rows: int = 0

    @property
    def name(self) -> str:
        return Path(self.file).name

    @property
    def messages(self) -> int:
        return self.stats.dated

    @property
    def has_dates(self) -> bool:
        return self.error is None and self.stats.dated > 0

"""Console, CSV and JSON reports."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import datetime

from . import __version__
from .model import EARLIEST_PLAUSIBLE, FALLBACK_SOURCES, DateStats, ScanResult, utc_now
from .timeline import compute_gaps, month_iter, months_between, ym_label

BAR_WIDTH = 30
RULE = 70
_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _can_encode(chars: str) -> bool:
    try:
        chars.encode(getattr(sys.stdout, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class Glyphs:
    """Block characters for the histograms, with an ASCII fallback for legacy consoles."""

    def __init__(self):
        fancy = _can_encode("█░─✗")
        self.full = "█" if fancy else "#"
        self.empty = "░" if fancy else "-"
        self.rule = "─" if fancy else "-"
        self.cross = "✗" if fancy else "x"


def fmt_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "N/A"


def fmt_datetime(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "N/A"


def iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def last_error_line(error: str) -> str:
    lines = [ln.strip() for ln in error.strip().splitlines()]
    for line in reversed(lines):
        if line and not line.startswith(("File ", "Traceback", "^", "~")):
            return line
    return lines[0] if lines else "unknown error"


def combined_stats(results: list[ScanResult]) -> DateStats:
    total = DateStats()
    for r in results:
        if r.error is None:
            total.merge(r.stats)
    return total


# ── Console ───────────────────────────────────────────────────────────────────

class ConsoleReport:
    def __init__(self, show_monthly=False, show_gaps=False, min_gap=1, out=None):
        self.show_monthly = show_monthly
        self.show_gaps = show_gaps
        self.min_gap = min_gap
        self.out = out or sys.stdout
        self.g = Glyphs()

    def p(self, text: str = "") -> None:
        print(text, file=self.out)

    def _bar(self, count: int, max_count: int) -> str:
        filled = round(BAR_WIDTH * count / max_count) if max_count else 0
        return self.g.full * filled + self.g.empty * (BAR_WIDTH - filled)

    def year_distribution(self, by_year, indent="    "):
        if not by_year:
            return
        top = max(by_year.values())
        total = sum(by_year.values())
        for year in sorted(by_year):
            c = by_year[year]
            self.p(f"{indent}{year}  {self._bar(c, top)}  {c:>7,}  ({100 * c / total:4.1f}%)")

    def month_matrix(self, by_month, indent="  "):
        """Year x month table; empty months show as '.' so gaps stand out."""
        if not by_month:
            self.p(f"{indent}Monthly breakdown: no dated messages.")
            return
        years = sorted({y for (y, _m) in by_month})
        cw = max(5, len(f"{max(by_month.values()):,}"))
        width = 6 + cw * 12 + cw + 2
        self.p(f"{indent}Monthly breakdown (messages per month):")
        self.p(f"{indent}{'Year':<6}" + "".join(f"{m:>{cw}}" for m in _MONTH_ABBR)
               + f"{'Total':>{cw + 2}}")
        self.p(f"{indent}{'-' * width}")
        col_totals = [0] * 12
        for year in years:
            row = f"{indent}{year:<6}"
            for month in range(1, 13):
                c = by_month.get((year, month), 0)
                col_totals[month - 1] += c
                row += f"{c:>{cw},}" if c else f"{'.':>{cw}}"
            row += f"{sum(by_month.get((year, m), 0) for m in range(1, 13)):>{cw + 2},}"
            self.p(row)
        self.p(f"{indent}{'-' * width}")
        self.p(f"{indent}{'All':<6}"
               + "".join(f"{c:>{cw},}" if c else f"{'.':>{cw}}" for c in col_totals)
               + f"{sum(col_totals):>{cw + 2},}")

    def gap_analysis(self, by_month, indent="  "):
        if not by_month:
            self.p(f"{indent}Gap analysis: no dated messages.")
            return
        active = sorted(by_month)
        first, last = active[0], active[-1]
        span = months_between(first, last)
        gaps = compute_gaps(by_month, self.min_gap)
        self.p(f"{indent}Gap analysis (monthly):")
        self.p(f"{indent}  Active span   : {ym_label(first)} -> {ym_label(last)}  ({span} months)")
        self.p(f"{indent}  Months w/ mail: {len(active)}/{span}  ({100 * len(active) / span:.0f}%)")
        if not gaps:
            self.p(f"{indent}  Gaps          : none (>= {self.min_gap} mo)")
            return
        self.p(f"{indent}  Gaps found    : {len(gaps)}  "
               f"({sum(g['length'] for g in gaps)} empty months total)")
        for g in sorted(gaps, key=lambda g: (-g["length"], g["start"])):
            span_text = (ym_label(g["start"]) if g["length"] == 1
                         else f"{ym_label(g['start'])} -> {ym_label(g['end'])}")
            flag = "  <-- notable" if g["length"] >= 3 else ""
            self.p(f"{indent}    {span_text:<20} {g['length']:>3} mo{flag}")

    def _line(self, label: str, value: str) -> None:
        self.p(f"  {label:<11}: {value}")

    def file_block(self, r: ScanResult) -> None:
        s = r.stats
        self.p(f"\n  {self.g.rule * 66}")
        self._line("File", r.file)
        speed = f", {r.size_mb / r.elapsed:.0f} MB/s" if r.elapsed >= 0.1 else ""
        self._line("Size", f"{r.size_mb} MB  ({r.elapsed:.1f}s{speed})")
        if r.error:
            for line in r.error.strip().splitlines():
                self.p(f"  {self.g.cross}  {line}")
            return
        self._line("Method", r.method or "-")
        no_date = f"  (+{s.no_date:,} without date)" if s.no_date else ""
        self._line("Messages", f"{s.dated:,}{no_date}")
        if r.item_classes:
            top = Counter(r.item_classes).most_common(6)
            self._line("Item types", "  ".join(f"{cls} {n:,}" for cls, n in top))
        if r.skipped_non_mail:
            self._line("Skipped", f"{r.skipped_non_mail:,} non-mail items (--mail-only)")
        if r.recovered:
            self._line("Recovered", f"{r.recovered:,} orphaned/deleted item(s), included above")
        if r.unreadable:
            self._line("Unreadable", f"{r.unreadable:,} item(s) could not be opened")
        if s.sources:
            parts = []
            for src, n in s.sources.most_common():
                mark = " (fallback)" if src in FALLBACK_SOURCES else ""
                parts.append(f"{src} {n:,}{mark}")
            self._line("Date source", "  ".join(parts))
        if s.implausible:
            samples = ", ".join(fmt_date(d) for d in s.implausible_samples)
            self._line("Implausible", f"{s.implausible:,} date(s) before "
                       f"{fmt_date(EARLIEST_PLAUSIBLE)} or in the future, excluded "
                       f"from the range (e.g. {samples})")
        if r.export_path:
            self._line("Exported", f"{r.export_path}  ({r.exported_rows:,} rows)")
        self._line("Earliest", fmt_datetime(s.earliest))
        self._line("Latest", fmt_datetime(s.latest))
        if s.span_days is not None:
            self._line("Span", f"{s.span_days} days  (~{s.span_days / 365:.1f} years)")
        if s.by_year:
            self.p("\n  Distribution by year:")
            self.year_distribution(s.by_year)
        if self.show_monthly:
            self.p()
            self.month_matrix(s.by_month)
        if self.show_gaps:
            self.p()
            self.gap_analysis(s.by_month)

    def render(self, results: list[ScanResult], total_time: float) -> None:
        self.p("\n" + "=" * RULE)
        self.p("  Email Date Range Scanner -- Results")
        self.p("=" * RULE)
        for r in results:
            self.file_block(r)

        errors = [r for r in results if r.error]
        dated = [r for r in results if r.has_dates]
        empty = len(results) - len(errors) - len(dated)
        total = combined_stats(results)

        self.p(f"\n{'=' * RULE}")
        self.p("  SUMMARY")
        self.p(self.g.rule * RULE)
        self.p(f"  Files processed : {len(results)}  ({len(dated)} with dates, "
               f"{empty} without dated items, {len(errors)} errors)")
        self.p(f"  Total messages  : {total.dated:,}")
        if total.no_date:
            self.p(f"  Without date    : {total.no_date:,}")
        recovered = sum(r.recovered for r in results if not r.error)
        if recovered:
            self.p(f"  Recovered       : {recovered:,}")
        if total.fallback_dates:
            self.p(f"  Fallback dates  : {total.fallback_dates:,}  "
                   "(creation time / Received / mbox envelope - not a send date)")
        if total.implausible:
            self.p(f"  Implausible     : {total.implausible:,}  (excluded from the range)")
        if total.earliest is None:
            self.p("\n  No dated messages found.")
        else:
            self.p(f"  Overall range   : {fmt_date(total.earliest)}  ->  {fmt_date(total.latest)}")
            self.p(f"  Overall span    : {total.span_days} days  (~{total.span_days / 365:.1f} years)")
        self.p(f"  Total time      : {total_time:.1f}s")
        if total.by_year:
            self.p("\n  Distribution by year (all files combined):")
            self.year_distribution(total.by_year)
        if len(dated) > 1:
            if self.show_monthly:
                self.p()
                self.month_matrix(total.by_month)
            if self.show_gaps:
                self.p()
                self.gap_analysis(total.by_month)
        self.p()


# ── CSV summary ───────────────────────────────────────────────────────────────

SUMMARY_COLUMNS = [
    "file", "size_mb", "method", "messages", "no_date", "earliest_utc",
    "latest_utc", "span_days", "elapsed_sec", "error", "recovered",
    "implausible", "fallback_dates", "unreadable", "skipped_non_mail",
    "export_file",
]


def write_csv(results: list[ScanResult], path: str) -> None:
    years = sorted({y for r in results if not r.error for y in r.stats.by_year})
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["=== Per-file summary ==="])
        w.writerow(SUMMARY_COLUMNS)
        for r in results:
            s = r.stats
            w.writerow([
                r.file, r.size_mb, r.method or "", s.dated, s.no_date,
                s.earliest.strftime("%Y-%m-%d %H:%M:%S") if s.earliest else "",
                s.latest.strftime("%Y-%m-%d %H:%M:%S") if s.latest else "",
                s.span_days if s.span_days is not None else "",
                f"{r.elapsed:.1f}", (r.error or "").replace("\n", " | "),
                r.recovered, s.implausible, s.fallback_dates, r.unreadable,
                r.skipped_non_mail, r.export_path or "",
            ])
        w.writerow([])
        w.writerow(["=== Messages per year ==="])
        w.writerow(["file"] + [str(y) for y in years] + ["total"])
        for r in results:
            if r.error:
                continue
            w.writerow([r.file] + [r.stats.by_year.get(y, 0) for y in years] + [r.stats.dated])
        totals = [sum(r.stats.by_year.get(y, 0) for r in results if not r.error) for y in years]
        w.writerow(["TOTAL"] + totals + [sum(totals)])


# ── JSON ──────────────────────────────────────────────────────────────────────

def _stats_dict(s: DateStats, min_gap: int) -> dict:
    by_month = s.by_month
    filled = {}
    if by_month:
        for ym in month_iter(min(by_month), max(by_month)):
            filled[ym_label(ym)] = by_month.get(ym, 0)
    return {
        "messages": s.dated,
        "no_date": s.no_date,
        "implausible": s.implausible,
        "implausible_samples": [iso(d) for d in s.implausible_samples],
        "fallback_dates": s.fallback_dates,
        "earliest_utc": iso(s.earliest),
        "latest_utc": iso(s.latest),
        "span_days": s.span_days,
        "date_sources": dict(s.sources.most_common()),
        "by_year": {str(y): s.by_year[y] for y in sorted(s.by_year)},
        "by_month": filled,
        "gaps": [{"start": ym_label(g["start"]), "end": ym_label(g["end"]),
                  "months": g["length"]} for g in compute_gaps(by_month, min_gap)],
    }


def write_json(results: list[ScanResult], path: str, options: dict, min_gap: int = 1) -> None:
    files = []
    for r in results:
        entry = {
            "file": r.file,
            "size_mb": r.size_mb,
            "method": r.method,
            "error": r.error,
            "elapsed_sec": round(r.elapsed, 3),
            "recovered": r.recovered,
            "unreadable": r.unreadable,
            "skipped_non_mail": r.skipped_non_mail,
            "item_classes": dict(Counter(r.item_classes).most_common()),
            "export_file": r.export_path,
            "exported_rows": r.exported_rows,
        }
        entry.update(_stats_dict(r.stats, min_gap))
        files.append(entry)
    doc = {
        "tool": "email-date-range",
        "version": __version__,
        "generated_utc": iso(utc_now()),
        "options": options,
        "files": files,
        "summary": _stats_dict(combined_stats(results), min_gap),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)

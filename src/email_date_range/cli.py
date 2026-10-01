"""email-date-range: date range, distribution and gaps for PST/OST/MBOX files."""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import queue
import sys
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .export import ExportWriter
from .mbox import scan_mbox
from .model import ScanResult
from .pst import pypff, scan_pst
from .report import ConsoleReport, fmt_date, last_error_line, write_csv, write_json

PST_EXTENSIONS = {".pst", ".ost"}
MBOX_EXTENSIONS = {".mbox", ".mbx"}
SUPPORTED_EXTENSIONS = PST_EXTENSIONS | MBOX_EXTENSIONS
PST_MAGIC = b"!BDN"
PROGRESS_INTERVAL = 0.3  # seconds between progress redraws

EXIT_OK, EXIT_USAGE, EXIT_FILE_ERRORS = 0, 1, 2


def force_utf8_output() -> None:
    """Make Unicode bars and non-Latin subjects printable on cp866/cp1252 consoles."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ── Input discovery ───────────────────────────────────────────────────────────

def detect_format(path: Path) -> str | None:
    """'pst' or 'mbox' by file signature, falling back to the extension."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(5)
    except OSError:
        head = b""
    if head[:4] == PST_MAGIC:
        return "pst"
    if head == b"From ":
        return "mbox"
    ext = path.suffix.lower()
    if ext in PST_EXTENSIONS:
        return "pst"
    if ext in MBOX_EXTENSIONS:
        return "mbox"
    return None


def collect_files(paths: list[str], recursive: bool) -> tuple[list[Path], list[str]]:
    """Expand folders, keep explicitly named files. Returns (files, problems)."""
    files, problems, seen = [], [], set()

    def add(f: Path):
        key = os.path.normcase(str(f.resolve()))
        if key not in seen:
            seen.add(key)
            files.append(f)

    for raw in paths:
        p = Path(raw)
        if p.is_file():
            # an explicitly named file is scanned whatever its extension
            # (Thunderbird keeps mbox files without one)
            add(p)
        elif p.is_dir():
            found = sorted(f for f in (p.rglob("*") if recursive else p.glob("*"))
                           if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS)
            if not found:
                problems.append(f"no PST/OST/MBOX files in '{p}'"
                                + ("" if recursive else " (tip: add -r)"))
            for f in found:
                add(f)
        else:
            problems.append(f"not found: '{p}'")
    return files, problems


def plan_export_paths(base: str, files: list[Path]) -> dict[str, str]:
    """One CSV per input: <base_stem>_<input_stem>.csv, never two inputs on one name.

    If ``base`` is an existing folder the CSVs go there as <input_stem>.csv.
    """
    base_path = Path(base)
    if base_path.is_dir():
        folder, prefix = base_path, ""
    else:
        folder, prefix = base_path.parent, f"{base_path.stem}_"
    used, plan = set(), {}
    for f in files:
        candidates = [f"{prefix}{f.stem}.csv", f"{prefix}{f.stem}_{f.suffix.lstrip('.')}.csv"]
        candidates += [f"{prefix}{f.stem}_{n}.csv" for n in range(2, len(files) + 2)]
        for name in candidates:
            key = name.lower()
            if key not in used:
                used.add(key)
                plan[str(f)] = str(folder / name)
                break
    return plan


# ── Scanning ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScanOptions:
    recover: bool = False
    mail_only: bool = False


def scan_file(path: str, options: ScanOptions, export_path: str | None = None,
              progress_q=None) -> ScanResult:
    size_mb = round(os.path.getsize(path) / 1_048_576, 1)
    result = ScanResult(file=path, size_mb=size_mb)
    name = Path(path).name

    def progress(done, total):
        if progress_q is not None:
            progress_q.put({"name": name, "status": "running", "done": done, "total": total})

    started = time.perf_counter()
    export = None
    try:
        fmt = detect_format(Path(path))
        if export_path:
            export = ExportWriter(export_path, path)
        if fmt == "pst":
            scan_pst(path, result, recover=options.recover, mail_only=options.mail_only,
                     export=export, progress=progress)
        elif fmt == "mbox":
            scan_mbox(path, result, export=export, progress=progress)
        else:
            result.error = "unsupported file: neither a PST/OST nor an mbox"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
    finally:
        if export is not None:
            export.close()
            result.export_path, result.exported_rows = export_path, export.rows
    result.elapsed = time.perf_counter() - started
    if progress_q is not None:
        progress_q.put({"name": name, "status": "done"})
    return result


CRASH_ERROR = ("the scanner process crashed while parsing this file (libpff hit corrupt "
               "data it does not guard against). Work on a copy repaired with SCANPST "
               "(recover-pst --method scanpst) or try recover-pst --method deep")


def crashed_result(path: str) -> ScanResult:
    try:
        size_mb = round(os.path.getsize(path) / 1_048_576, 1)
    except OSError:
        size_mb = 0.0
    return ScanResult(file=path, size_mb=size_mb, method="libpff", error=CRASH_ERROR)


class ProgressPrinter:
    """Draws a live progress line on stderr from events sent by scanners/workers."""

    def __init__(self, events, enabled: bool):
        self.events = events
        self.enabled = enabled
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if self.enabled:
            self._thread.start()

    def stop(self):
        if self.enabled:
            self._stop.set()
            self._thread.join()
            self.clear()

    def clear(self):
        if self.enabled:
            with self._lock:
                sys.stderr.write("\r" + " " * 79 + "\r")
                sys.stderr.flush()

    def _drain(self):
        while True:
            try:
                event = self.events.get_nowait()
            except (queue.Empty, EOFError, OSError):
                return
            except Exception:
                return
            self._state[event["name"]] = event

    def _run(self):
        while not self._stop.is_set():
            self._drain()
            self._render()
            self._stop.wait(PROGRESS_INTERVAL)

    @staticmethod
    def _bar(done, total, width=20):
        if total <= 0:
            return f"{done:,}"
        filled = int(width * done / total)
        return f"[{'#' * filled}{'.' * (width - filled)}] {done:,}/{total:,} ({100 * done / total:.0f}%)"

    def _render(self):
        active = [(n, e) for n, e in self._state.items() if e.get("status") == "running"]
        if not active:
            return
        line = "  " + "   |   ".join(f"{n}: {self._bar(e['done'], e['total'])}" for n, e in active)
        if len(line) > 78:
            line = line[:75] + "..."
        with self._lock:
            sys.stderr.write("\r" + line.ljust(79)[:79])
            sys.stderr.flush()


def _status_line(r: ScanResult, n: int, idx: int) -> str:
    head = f"  [{idx}/{n}] {r.name} ... "
    if r.error:
        return head + "ERROR: " + last_error_line(r.error)
    return (head + f"{fmt_date(r.stats.earliest)} -> {fmt_date(r.stats.latest)}"
            f"  ({r.messages:,} msgs, {r.elapsed:.1f}s)")


def _scan_batch(paths: list[str], workers: int, scan, options, export_plan, events, on_result):
    """Scan paths in a process pool; return the paths whose worker process died.

    Failed paths come back in submission order. With a single worker the pool
    runs tasks first-in first-out, so the first failed path is the one that
    killed the process and the rest are innocent bystanders.
    """
    failed = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [(p, pool.submit(scan, p, options, export_plan.get(p), events)) for p in paths]
        for path, future in futures:
            try:
                on_result(future.result())
            except BrokenProcessPool:
                failed.append(path)
    return failed


def run_scan(files: list[Path], options: ScanOptions, workers: int,
             export_plan: dict[str, str], show_progress: bool,
             scan=scan_file) -> list[ScanResult]:
    """Scan every file in child processes, so a crash inside libpff costs one file, not the run."""
    n = len(files)
    index = {str(f): i + 1 for i, f in enumerate(files)}
    results: dict[str, ScanResult] = {}
    manager = mp.Manager()
    events = manager.Queue()
    printer = ProgressPrinter(events, show_progress)

    def report(r: ScanResult) -> None:
        results[r.file] = r
        printer.clear()
        print(_status_line(r, n, index[r.file]), flush=True)

    printer.start()
    try:
        pending = [str(f) for f in files]
        while pending:
            failed = _scan_batch(pending, min(workers, len(pending)), scan, options,
                                 export_plan, events, report)
            if not failed:
                break
            if workers == 1:
                report(crashed_result(failed[0]))
                pending = failed[1:]
            else:
                workers, pending = 1, failed  # several were in flight: isolate one at a time
    finally:
        printer.stop()
        manager.shutdown()
    return [results[str(f)] for f in files]


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="email-date-range",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Report the date range, per-year/per-month distribution and silent\n"
            "gaps of PST, OST and MBOX mail archives."),
        epilog=r"""
examples:
  email-date-range "C:\Cases\Matter001" -r --csv report.csv
  email-date-range mailbox.pst --recover --gaps --monthly
  email-date-range "C:\Cases\Matter001" -r --workers 4 --json report.json
  email-date-range archive.mbox --export items.csv

supported formats: .pst .ost (via libpff-python), .mbox .mbx
(files named explicitly are detected by signature, whatever the extension)

exit status: 0 = all files scanned, 1 = bad arguments / nothing to scan,
             2 = one or more files could not be scanned
""")
    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="PST/OST/MBOX file(s) and/or folder(s) to scan")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="scan folders recursively")
    parser.add_argument("--csv", metavar="FILE",
                        help="save the summary to CSV (UTF-8 with BOM, opens in Excel)")
    parser.add_argument("--json", metavar="FILE",
                        help="save full results (incl. monthly counts and gaps) to JSON")
    parser.add_argument("--export", metavar="FILE",
                        help="write one CSV row per item (date, source, folder, subject, "
                             "sender, Message-ID). One CSV per input, named "
                             "<FILE_stem>_<input_stem>.csv; if FILE is an existing "
                             "folder, <input_stem>.csv inside it")
    parser.add_argument("--log", metavar="FILE",
                        help="error log path (default: email_date_range_errors.log, "
                             "written only if a file fails)")
    parser.add_argument("--workers", metavar="N", type=int, default=1,
                        help="files scanned in parallel (default: 1). 2-4 helps with "
                             "several large files on SSD; keep 1 on HDD")
    parser.add_argument("--recover", action="store_true",
                        help="PST/OST: also count orphaned items - nodes cut off from "
                             "the folder tree, typically hard-deleted mail")
    parser.add_argument("--mail-only", action="store_true",
                        help="PST/OST: count only e-mail, meeting requests and delivery "
                             "reports; skip calendar, contacts, tasks and notes")
    parser.add_argument("--monthly", action="store_true",
                        help="print a year x month table ('.' marks empty months)")
    parser.add_argument("--gaps", action="store_true",
                        help="report runs of consecutive months without mail (possible "
                             "deletion, incomplete export or spoliation)")
    parser.add_argument("--min-gap", metavar="MONTHS", type=int, default=1,
                        help="smallest gap to report, in months (default: 1)")
    parser.add_argument("--no-progress", action="store_true",
                        help="do not draw the live progress line")
    parser.add_argument("--fast", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()
    args = build_parser().parse_args(argv)
    if args.workers < 1 or args.min_gap < 1:
        print("error: --workers and --min-gap must be >= 1", file=sys.stderr)
        return EXIT_USAGE
    if args.fast:
        print("note: --fast is obsolete and ignored - libpff exposes no folder "
              "contents table, so every message is opened anyway", file=sys.stderr)

    files, problems = collect_files(args.paths, args.recursive)
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    if not files:
        print("Nothing to scan.", file=sys.stderr)
        return EXIT_USAGE

    if pypff is None and any(detect_format(f) == "pst" for f in files):
        print("WARNING: pypff not installed - PST/OST files will fail. "
              "Install with: pip install libpff-python\n", file=sys.stderr)

    workers = min(args.workers, len(files))
    print(f"\nFound {len(files)} file(s)"
          + (" [recursive]" if args.recursive else "")
          + (f" [workers={workers}]" if workers > 1 else "") + ". Scanning...\n")

    export_plan = plan_export_paths(args.export, files) if args.export else {}
    show_progress = not args.no_progress and sys.stderr.isatty()
    options = ScanOptions(recover=args.recover, mail_only=args.mail_only)

    started = time.perf_counter()
    results = run_scan(files, options, workers, export_plan, show_progress)
    elapsed = time.perf_counter() - started

    ConsoleReport(args.monthly, args.gaps, args.min_gap).render(results, elapsed)
    if args.csv:
        write_csv(results, args.csv)
        print(f"  CSV saved : {args.csv}")
    if args.json:
        write_json(results, args.json, options={
            "recover": args.recover, "mail_only": args.mail_only,
            "recursive": args.recursive, "min_gap": args.min_gap,
        }, min_gap=args.min_gap)
        print(f"  JSON saved: {args.json}")

    errors = [r for r in results if r.error]
    if errors:
        log_path = args.log or "email_date_range_errors.log"
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("Email Date Range Scanner -- Error Log\n" + "=" * 60 + "\n")
            for r in errors:
                fh.write(f"\n[FILE] {r.file}\n{r.error}\n{'-' * 60}\n")
        print(f"  {len(errors)} error(s) -- details: {log_path}")
        return EXIT_FILE_ERRORS
    return EXIT_OK


def entry_point() -> None:
    mp.freeze_support()
    sys.exit(main())

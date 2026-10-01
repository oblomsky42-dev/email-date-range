r"""recover-pst: forensic recovery of deleted items from a PST/OST.

Chain of custody:
  * The ORIGINAL file is never modified - all work happens on a copy.
  * The original is SHA-256 hashed before any operation and re-verified after.
  * Every step goes to a timestamped log plus a JSON manifest.

Methods (--method):
  orphan   nodes detached from the folder tree - typically mail deleted past
           Deleted Items. Pure Python via libpff-python. Default.
  deep     orphan + recovered-block scan with the pffexport CLI from libpff
           (pff-tools). Finds more, needs pffexport on PATH.
  scanpst  prepares a truncated copy for Microsoft's Inbox Repair Tool
           (SCANPST.EXE). Only the safe part is automated (copy, hash,
           truncate); scanpst itself is a GUI and is run by hand.

Output in --workdir:
  recovery_report.json   manifest: hashes, method, counts, recovered items
  recovered_items.csv    one row per recovered item (orphan/deep)
  recovery_log.txt       full operation log
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import multiprocessing
import os
import platform
import re
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .pst import iter_orphan_messages, lock_error, message_date, open_pst, pypff

EXIT_OK, EXIT_FAILED, EXIT_SOURCE_CHANGED = 0, 1, 2


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Log:
    def __init__(self, path: Path):
        self.path = path
        self._fh = open(path, "w", encoding="utf-8")
        self.line(f"recover-pst {__version__} log - started")

    def line(self, msg: str) -> None:
        stamped = f"[{_now()}] {msg}"
        print(stamped, flush=True)
        self._fh.write(stamped + "\n")
        self._fh.flush()

    def close(self) -> None:
        self.line("log closed")
        self._fh.close()


def sha256_file(path, chunk: int = 8 * 1024 * 1024, show_progress: bool = True) -> str:
    digest = hashlib.sha256()
    size = os.path.getsize(path)
    done = 0
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
            done += len(block)
            if show_progress and size:
                print(f"\r    hashing... {100 * done / size:.0f}%", end="", flush=True)
    if show_progress:
        print("\r" + " " * 30 + "\r", end="", flush=True)
    return digest.hexdigest()


# ── orphan ────────────────────────────────────────────────────────────────────

def orphan_items(path: str) -> tuple[int, list[dict], str | None]:
    """(orphan node count, recovered messages, error). Runs in a child process."""
    items, nodes = [], 0
    try:
        pff = open_pst(path)
        try:
            nodes = pff.number_of_orphan_items
            for msg, folder in iter_orphan_messages(pff):
                if msg is None:
                    items.append({"date_utc": "", "date_source": "", "folder": folder,
                                  "subject": "", "sender": "", "identifier": "",
                                  "note": "unreadable"})
                    continue
                dt, source = message_date(msg)
                items.append({
                    "date_utc": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
                    "date_source": source or "",
                    "folder": folder,
                    "subject": _attr(msg, "subject"),
                    "sender": _attr(msg, "sender_name"),
                    "identifier": _attr(msg, "identifier"),
                    "note": "",
                })
        finally:
            pff.close()
    except Exception as exc:
        return nodes, items, str(exc)
    return nodes, items, None


def recover_orphan(work_copy: Path, log: Log, worker=orphan_items) -> dict:
    if pypff is None:
        log.line("ERROR: pypff not installed - orphan mode unavailable "
                 "(pip install libpff-python).")
        return {"available": False, "items": []}
    log.line("Opening the working copy with libpff for the orphan scan...")
    # libpff can crash on corrupt structures; a child process keeps this
    # process alive so the source re-verification and the report still happen
    try:
        with ProcessPoolExecutor(max_workers=1) as pool:
            nodes, items, error = pool.submit(worker, str(work_copy)).result()
    except BrokenProcessPool:
        log.line("ERROR: libpff crashed during the orphan scan (corrupt structures); "
                 "no items recovered.")
        return {"available": True, "items": [], "crashed": True}
    log.line(f"Found {nodes} orphan node(s).")
    if error:
        log.line(f"ERROR during orphan scan: {error}")
    dated = sum(1 for i in items if i["date_utc"])
    log.line(f"Orphan recovery: {len(items)} message(s), {dated} with a date.")
    return {"available": True, "items": items}


def _attr(item, name: str) -> str:
    try:
        value = getattr(item, name)
    except Exception:
        return ""
    return "" if value is None else str(value)


# ── deep (pffexport) ──────────────────────────────────────────────────────────

def find_pffexport(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for name in ("pffexport", "pffexport.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


# pffexport writes FILETIMEs in OutlookHeaders.txt as
#   "Delivery time:\t\t\t\tJan 01, 2001 00:00:00.000000000 UTC"
_PFF_TIME_RE = re.compile(
    r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?: UTC)?")
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
_HEADER_FIELDS = {
    "delivery time": "delivery_time",
    "client submit time": "client_submit_time",
    "creation time": "creation_time",
    "subject": "subject",
    "sender name": "sender",
}


def parse_pff_time(text: str) -> str | None:
    m = _PFF_TIME_RE.search(text)
    if not m or m.group(1) not in _MONTHS:
        return None
    mon, day, year, hh, mm, ss = m.groups()
    try:
        return datetime(int(year), _MONTHS[mon], int(day),
                        int(hh), int(mm), int(ss)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def read_pffexport_text(path: Path) -> str:
    """pffexport writes UTF-16LE (no BOM) on Windows builds and UTF-8 elsewhere."""
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if len(data) >= 4 and data[1] == 0 and data[3] == 0:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


def parse_outlook_headers(path: Path) -> dict | None:
    """Read date/subject/sender from a pffexport OutlookHeaders.txt."""
    try:
        text = read_pffexport_text(path)
    except OSError:
        return None
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        name = _HEADER_FIELDS.get(key.strip().lower())
        if sep and name and name not in fields:
            fields[name] = value.strip()
    date = source = None
    for name in ("delivery_time", "client_submit_time", "creation_time"):
        if name in fields:
            date = parse_pff_time(fields[name])
            if date:
                source = name
                break
    return {
        "date_utc": date or "",
        "date_source": source or "",
        "folder": str(path.parent.parent),
        "subject": fields.get("subject", ""),
        "sender": fields.get("sender", ""),
        "identifier": path.parent.name,
        "note": "",
    }


def collect_pffexport_items(export_dirs: list[Path]) -> list[dict]:
    items = []
    for folder in export_dirs:
        for header_file in sorted(folder.rglob("OutlookHeaders.txt")):
            if "Attachments" in header_file.relative_to(folder).parts:
                continue  # a message attached to another message, not a mailbox item
            meta = parse_outlook_headers(header_file)
            if meta:
                items.append(meta)
    return items


def recover_deep(work_copy: Path, workdir: Path, log: Log, pffexport: str | None = None) -> dict:
    exe = find_pffexport(pffexport)
    if not exe:
        log.line("pffexport not found - deep mode unavailable.")
        log.line("  Install libpff tools (e.g. apt install pff-tools), put pffexport on PATH "
                 "or pass --pffexport.")
        return {"available": False, "items": [], "export_dirs": []}
    target = workdir / (work_copy.stem + "_export")
    cmd = [exe, "-m", "recovered", "-f", "text", "-q",
           "-l", str(workdir / "pffexport.log"), "-t", str(target), str(work_copy)]
    log.line(f"Running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        log.line(f"ERROR running pffexport: {exc}")
        return {"available": True, "items": [], "export_dirs": []}
    if proc.returncode != 0:
        log.line(f"pffexport exited with {proc.returncode}: {proc.stderr.strip()[:500]}")
    export_dirs = [Path(str(target) + s) for s in (".orphans", ".recovered")]
    export_dirs = [d for d in export_dirs if d.is_dir()]
    log.line(f"Export folders: {[str(d) for d in export_dirs]}")
    items = collect_pffexport_items(export_dirs)
    log.line(f"Deep recovery: {len(items)} message(s) parsed from OutlookHeaders.txt.")
    return {"available": True, "items": items, "export_dirs": [str(d) for d in export_dirs]}


# ── scanpst preparation ───────────────────────────────────────────────────────

SCANPST_CANDIDATES = [
    r"C:\Program Files\Microsoft Office\root\Office16\SCANPST.EXE",
    r"C:\Program Files (x86)\Microsoft Office\root\Office16\SCANPST.EXE",
    r"C:\Program Files\Microsoft Office\Office16\SCANPST.EXE",
    r"C:\Program Files (x86)\Microsoft Office\Office16\SCANPST.EXE",
]


def prepare_scanpst(work_copy: Path, truncate_bytes: int, log: Log, do_hash: bool) -> dict:
    """Trim the END of the copy so SCANPST.EXE does a full rebuild. Copy only."""
    size = os.path.getsize(work_copy)
    if truncate_bytes <= 0 or truncate_bytes >= size:
        log.line(f"ERROR: --truncate must be between 1 and {size - 1} bytes.")
        return {"prepared": False}
    log.line(f"Truncating {truncate_bytes} bytes from the end of the copy "
             f"({size} -> {size - truncate_bytes}).")
    with open(work_copy, "r+b") as fh:
        fh.truncate(size - truncate_bytes)
    truncated_hash = sha256_file(work_copy) if do_hash else None
    if truncated_hash:
        log.line(f"Truncated copy SHA-256: {truncated_hash}")
    scanpst = next((p for p in SCANPST_CANDIDATES if os.path.exists(p)), None)
    log.line("-" * 60)
    log.line("MANUAL STEP - SCANPST.EXE has no command line:")
    log.line("  1. Open the Inbox Repair Tool" + (f": {scanpst}" if scanpst else " (SCANPST.EXE)"))
    log.line(f"  2. Browse to the truncated copy: {work_copy}")
    log.line("  3. Start -> Repair. Recovered items land in 'Lost and Found'.")
    log.line("  4. Then run: email-date-range <repaired copy> --recover")
    log.line("-" * 60)
    return {"prepared": True, "truncated_bytes": truncate_bytes,
            "truncated_sha256": truncated_hash, "scanpst_path": scanpst}


# ── Main workflow ─────────────────────────────────────────────────────────────

ITEM_COLUMNS = ["date_utc", "date_source", "folder", "subject", "sender", "identifier", "note"]


def write_items_csv(items: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=ITEM_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(items)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="recover-pst",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Forensic recovery of deleted items from a PST/OST "
                    "(chain-of-custody safe: the original is never modified).",
        epilog=r"""
methods:
  orphan   orphan-node recovery via libpff-python (default)
  deep     orphan + recovered-block scan via the pffexport CLI
  scanpst  truncate a copy to prepare it for the Inbox Repair Tool

examples:
  recover-pst --src "C:\ev\mail.pst" --workdir "C:\work"
  recover-pst --src "C:\ev\mail.pst" --workdir "C:\work" --method deep
  recover-pst --src "C:\ev\mail.pst" --workdir "C:\work" --method scanpst --truncate 512

The source is hashed before copying and re-verified afterwards; a mismatch
is reported and the exit status is 2. The source is only ever read.
""")
    ap.add_argument("--src", required=True, help="source PST/OST (evidence, read-only)")
    ap.add_argument("--workdir", required=True,
                    help="working folder for the copy and outputs (not the source folder)")
    ap.add_argument("--method", choices=["orphan", "deep", "scanpst"], default="orphan",
                    help="recovery method (default: orphan)")
    ap.add_argument("--truncate", type=int, default=512, metavar="BYTES",
                    help="scanpst mode: bytes to trim from the end of the copy (default: 512)")
    ap.add_argument("--pffexport", metavar="PATH",
                    help="deep mode: pffexport executable to use (default: search PATH)")
    ap.add_argument("--no-hash", action="store_true",
                    help="skip SHA-256 hashing. Faster on huge files but breaks the chain "
                         "of custody - use only for non-evidentiary triage")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)

    src = Path(args.src).resolve()
    if not src.is_file():
        print(f"ERROR: source not found: {src}", file=sys.stderr)
        return EXIT_FAILED
    locked = lock_error(str(src))
    if locked:
        print(f"ERROR: {src}: {locked}", file=sys.stderr)
        return EXIT_FAILED
    workdir = Path(args.workdir).resolve()
    work_copy = workdir / src.name
    if os.path.normcase(str(work_copy)) == os.path.normcase(str(src)):
        print("ERROR: --workdir must not be the folder that holds the source "
              "(the working copy would overwrite the evidence).", file=sys.stderr)
        return EXIT_FAILED
    workdir.mkdir(parents=True, exist_ok=True)

    log = Log(workdir / "recovery_log.txt")
    do_hash = not args.no_hash
    manifest = {
        "tool": "recover-pst",
        "version": __version__,
        "libpff_version": pypff.get_version() if pypff else None,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(src),
        "source_size": src.stat().st_size,
        "method": args.method,
        "hashing": "sha256" if do_hash else "disabled",
        "status": "running",
    }
    exit_code = EXIT_OK
    report_path = workdir / "recovery_report.json"
    try:
        log.line(f"Source: {src}  ({manifest['source_size']:,} bytes)")
        src_hash = None
        if do_hash:
            log.line("Computing source SHA-256 (baseline)...")
            src_hash = sha256_file(src)
            log.line(f"Source SHA-256: {src_hash}")
        else:
            log.line("HASHING SKIPPED (--no-hash) - chain of custody NOT enforced.")
        manifest["source_sha256_before"] = src_hash

        if work_copy.exists():
            log.line(f"Replacing an existing working copy: {work_copy}")
        log.line(f"Copying to working copy: {work_copy}")
        shutil.copy2(src, work_copy)
        if do_hash:
            copy_hash = sha256_file(work_copy)
            if copy_hash != src_hash:
                log.line(f"ERROR: copy hash mismatch ({copy_hash} != {src_hash}) - aborting.")
                manifest["status"] = "aborted_copy_mismatch"
                return EXIT_FAILED
            log.line("Copy verified identical to the source.")
            manifest["working_copy_sha256"] = copy_hash
        elif os.path.getsize(work_copy) != manifest["source_size"]:
            log.line("ERROR: copy size mismatch - aborting.")
            manifest["status"] = "aborted_copy_size_mismatch"
            return EXIT_FAILED
        manifest["working_copy"] = str(work_copy)

        if args.method == "orphan":
            res = recover_orphan(work_copy, log)
        elif args.method == "deep":
            res = recover_deep(work_copy, workdir, log, args.pffexport)
        else:
            res = prepare_scanpst(work_copy, args.truncate, log, do_hash)
        if args.method in ("orphan", "deep"):
            items = res.pop("items")
            res["count"] = len(items)
            res["dated"] = sum(1 for i in items if i["date_utc"])
            res["items"] = items
            items_csv = workdir / "recovered_items.csv"
            write_items_csv(items, items_csv)
            res["items_csv"] = str(items_csv)
            log.line(f"Item list written: {items_csv}")
        manifest[args.method] = res

        if do_hash:
            log.line("Re-verifying the source (must be unchanged)...")
            after = sha256_file(src)
            manifest["source_sha256_after"] = after
            if after != src_hash:
                log.line("CRITICAL: source hash CHANGED - chain of custody broken!")
                manifest["status"] = "source_modified"
                exit_code = EXIT_SOURCE_CHANGED
            else:
                log.line("Source integrity confirmed - original unchanged.")
                manifest["status"] = "ok"
        else:
            manifest["status"] = "ok_no_hash"
        if args.method == "scanpst" and not res.get("prepared"):
            manifest["status"] = "failed"
            exit_code = EXIT_FAILED
    except Exception as exc:
        log.line(f"ERROR: {type(exc).__name__}: {exc}")
        manifest["status"] = "error"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = EXIT_FAILED
    finally:
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        log.line(f"Report written: {report_path}")
        log.close()

    print(f"\nDone. Report: {report_path}")
    section = manifest.get(args.method, {})
    if args.method in ("orphan", "deep") and section.get("available"):
        print(f"Recovered {section['count']} item(s) via '{args.method}' "
              f"({section['dated']} with a date).")
    elif args.method == "scanpst" and section.get("prepared"):
        print("scanpst copy prepared - see the log for the manual repair step.")
    return exit_code


def entry_point() -> None:
    multiprocessing.freeze_support()  # the orphan scan runs in a child process
    sys.exit(main())

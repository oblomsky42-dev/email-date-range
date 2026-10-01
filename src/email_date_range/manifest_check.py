"""Check scan results against a known-answer manifest written by pst-testgen.

https://github.com/oblomsky42-dev/pst-testgen - manifest schema_version 1.
Also reads manifests from the earlier generate_test_pst script (no schema_version).
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .cli import ScanOptions, scan_file

SUPPORTED_SCHEMAS = {None, 1}
MESSAGE_ID = re.compile(r"<test-\d+-\d+@(?:pst-testgen|generate-test-pst)\.example>")


@dataclass
class Check:
    label: str
    ok: bool
    got: object = None
    want: object = None


def manifest_path_for(pst: Path) -> Path:
    return pst.with_name(pst.stem + "_expected_manifest.json")


def _scan(pst: Path, export_csv: Path, recover: bool):
    result = scan_file(str(pst), ScanOptions(recover=recover), str(export_csv))
    if result.error:
        raise RuntimeError(f"scan failed: {result.error}")
    return result, list(csv.DictReader(export_csv.open(encoding="utf-8-sig")))


def check_pst(pst: Path, workdir: Path, manifest: dict | None = None) -> list[Check]:
    """Scan ``pst`` with and without --recover and compare with its manifest."""
    if manifest is None:
        manifest = json.loads(manifest_path_for(pst).read_text(encoding="utf-8"))
    schema = manifest.get("schema_version")
    if schema not in SUPPORTED_SCHEMAS:
        raise ValueError(f"unsupported manifest schema_version {schema!r}")
    checks: list[Check] = []

    def check(label, got, want):
        checks.append(Check(label, got == want, got, want))

    soft = {m["subject"]: m for m in manifest["deletions"]["soft_deleted_items"]}
    hard = {m["subject"]: m for m in manifest["deletions"]["hard_deleted_items"]}

    plain, rows = _scan(pst, workdir / "plain.csv", recover=False)
    exp = manifest["expected"]["plain_scan"]
    s = plain.stats
    check("plain: total", s.dated, exp["total_messages"])
    check("plain: by year", {str(y): n for y, n in sorted(s.by_year.items())},
          {str(y): n for y, n in exp["by_year"].items()})
    check("plain: by month", {f"{y}-{m:02d}": n for (y, m), n in sorted(s.by_month.items())},
          exp["by_month"])
    check("plain: every date from delivery_time", dict(s.sources),
          {"delivery_time": exp["total_messages"]})
    check("plain: no undated or implausible items", (s.no_date, s.implausible), (0, 0))
    check("plain: each date matches the date in its subject",
          [r["subject"] for r in rows if r["subject"][1:11] != r["date_utc"][:10]], [])
    check("plain: each item carries its generated Message-ID",
          sum(bool(MESSAGE_ID.fullmatch(r["message_id"])) for r in rows), len(rows))
    by_subject = {r["subject"]: r for r in rows}
    check("plain: soft-deleted items sit in Deleted Items with exact timestamps",
          sorted(k for k, m in soft.items()
                 if "Deleted" in by_subject.get(k, {}).get("folder", "")
                 and by_subject[k]["date_utc"] == m["date"]),
          sorted(soft))
    check("plain: hard-deleted items are gone", [k for k in hard if k in by_subject], [])

    recovered_result, rec_rows = _scan(pst, workdir / "recover.csv", recover=True)
    recovered = [r for r in rec_rows if r["recovered"] == "yes"]
    rs = manifest["expected"]["recover_scan"]
    bound = rs.get("max_additional_recovered", rs.get("additional_recovered", 0))
    check("recover: recovered count within the hard-deleted set",
          0 <= recovered_result.recovered <= bound, True)
    check("recover: every recovered item is a hard-deleted one, exact timestamp",
          [r["subject"] for r in recovered
           if hard.get(r["subject"], {}).get("date") != r["date_utc"]], [])
    check("recover: total = plain + recovered", recovered_result.stats.dated,
          s.dated + recovered_result.recovered)
    return checks

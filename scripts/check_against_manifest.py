"""Check email-date-range against a PST made by pst-testgen.

    python scripts/check_against_manifest.py C:\\test\\t.pst

Reads <name>_expected_manifest.json next to the PST, scans the PST with and
without --recover, and checks totals, per-year and per-month counts, every
item's timestamp, Message-IDs and where the deleted items ended up.
Exit status 0 = every check passed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from email_date_range.manifest_check import check_pst


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        checks = check_pst(Path(sys.argv[1]), Path(tmp))
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.label}")
        if not c.ok:
            print(f"      got : {c.got}\n      want: {c.want}")
    failed = sum(not c.ok for c in checks)
    print("ALL CHECKS PASSED" if not failed else f"{failed} CHECK(S) FAILED")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

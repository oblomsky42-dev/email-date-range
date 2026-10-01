"""Download the public PST fixtures used by the test suite into tests/data/.

The files come from the Apache Tika test corpus (Apache License 2.0) and are
pinned to a commit and verified by SHA-256, so they are never committed here.

    python scripts/fetch_test_data.py
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

TIKA_COMMIT = "d9c9cd1ac53663bc931d5048939e3fb3968bf3f8"
TIKA_DIR = ("tika-parsers/tika-parsers-standard/tika-parsers-standard-modules/"
            "tika-parser-microsoft-module/src/test/resources/test-documents")
BASE_URL = f"https://raw.githubusercontent.com/apache/tika/{TIKA_COMMIT}/{TIKA_DIR}"

FIXTURES = {
    "testPST.pst": "f2a6b1d2cad00f574e3d1c1211c4b1c854d6526caea77213adc3da92b7813ae3",
    "testPST_variousBodyTypes.pst": "24c5e6bbb8bf26a817c977283e40e7b69d2661fec0845abbe177f97efcb05fb0",
}

DATA_DIR = Path(__file__).resolve().parent.parent / "tests" / "data"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name, expected in FIXTURES.items():
        target = DATA_DIR / name
        if target.exists() and sha256(target) == expected:
            print(f"ok       {name}")
            continue
        url = f"{BASE_URL}/{name}"
        print(f"download {name}")
        with urllib.request.urlopen(url, timeout=60) as response:
            target.write_bytes(response.read())
        if sha256(target) != expected:
            target.unlink()
            print(f"ERROR    {name}: SHA-256 mismatch, file removed", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

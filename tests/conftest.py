from __future__ import annotations

from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent / "data"
TIKA_PST = DATA_DIR / "testPST.pst"
TIKA_PST_BODIES = DATA_DIR / "testPST_variousBodyTypes.pst"

try:
    import pypff  # noqa: F401
    HAS_PYPFF = True
except ImportError:
    HAS_PYPFF = False


def mbox_message(headers: dict | list, body: str = "Body text.\n",
                 envelope: str = "From sender@example.com Mon Jun 01 10:00:00 2009") -> str:
    items = headers.items() if isinstance(headers, dict) else headers
    lines = [envelope] + [f"{k}: {v}" for k, v in items] + ["", body.rstrip("\n"), ""]
    return "\n".join(lines) + "\n"


@pytest.fixture
def write_mbox(tmp_path):
    """write_mbox(name, *messages, newline="\\n") -> Path"""
    def _write(name: str, *messages: str, newline: str = "\n") -> Path:
        path = tmp_path / name
        text = "".join(messages)
        path.write_bytes(text.replace("\n", newline).encode("utf-8"))
        return path
    return _write


@pytest.fixture
def tika_pst():
    if not HAS_PYPFF:
        pytest.skip("libpff-python not installed")
    if not TIKA_PST.exists():
        pytest.skip("run `python scripts/fetch_test_data.py` to download PST fixtures")
    return TIKA_PST


@pytest.fixture
def tika_pst_bodies():
    if not HAS_PYPFF:
        pytest.skip("libpff-python not installed")
    if not TIKA_PST_BODIES.exists():
        pytest.skip("run `python scripts/fetch_test_data.py` to download PST fixtures")
    return TIKA_PST_BODIES

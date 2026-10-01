"""Per-message CSV export (--export)."""

from __future__ import annotations

import csv
from datetime import datetime

COLUMNS = [
    "file", "date_utc", "date_source", "implausible", "recovered",
    "folder", "item_class", "subject", "sender_name", "sender_email",
    "message_id",
]

# Excel treats a cell starting with one of these as a formula. Subjects and
# sender names are attacker-controlled, so such cells get a leading apostrophe
# (the standard CSV-injection guard; Excel hides the apostrophe).
_FORMULA_TRIGGERS = ("=", "+", "-", "@")


def clean_text(value) -> str:
    """Flatten a header/property value into a single safe CSV cell."""
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    text = " ".join(text.split())
    if text.startswith(_FORMULA_TRIGGERS):
        text = "'" + text
    return text


class ExportWriter:
    """Writes one CSV row per item, dated or not, so the export is a full item list."""

    def __init__(self, path: str, source_file: str):
        self.path = path
        self.source_file = source_file
        self.rows = 0
        self._fh = open(path, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(COLUMNS)

    def write(self, *, date: datetime | None, source: str | None,
              implausible: bool = False, recovered: bool = False,
              folder: str = "", item_class: str = "", subject: str = "",
              sender_name: str = "", sender_email: str = "",
              message_id: str = "") -> None:
        self._writer.writerow([
            self.source_file,
            date.strftime("%Y-%m-%d %H:%M:%S") if date else "",
            source or "none",
            "yes" if implausible else "",
            "yes" if recovered else "",
            clean_text(folder),
            clean_text(item_class),
            clean_text(subject),
            clean_text(sender_name),
            clean_text(sender_email),
            clean_text(message_id),
        ])
        self.rows += 1

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

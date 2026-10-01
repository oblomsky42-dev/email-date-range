"""Header-only MBOX scanner.

The standard library's ``mailbox.mbox`` parses every message in full,
attachments included, which caps throughput at a few tens of MB/s. Only the
headers are needed here, so the file is memory-mapped, message boundaries are
found with a plain byte search (the same "a line starting with 'From '" rule
``mailbox.mbox`` uses), and just the header block of each message is parsed.
"""

from __future__ import annotations

import mmap
import os
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import parseaddr, parsedate_to_datetime
from typing import NamedTuple

from .export import ExportWriter
from .model import ScanResult

# A header block longer than this is malformed; parse what we have.
HEADER_LIMIT = 256 * 1024

_parser = BytesHeaderParser(policy=compat32)


class RawMessage(NamedTuple):
    offset: int
    from_line: bytes
    headers: bytes


def message_starts(buf) -> list[int]:
    """Offsets of every line that starts with b'From ' (mboxo/mboxrd separators)."""
    starts = [0] if buf[:5] == b"From " else []
    find = buf.find
    pos = find(b"\nFrom ")
    while pos != -1:
        starts.append(pos + 1)
        pos = find(b"\nFrom ", pos + 1)
    return starts


# A blank line ends the header block. Besides LF and CRLF, accept CR CR LF:
# a CRLF mailbox that went through one more text-mode conversion.
_BLANK_LINES = (b"\n\n", b"\n\r\n", b"\n\r\r\n")


def _header_end(buf, start: int, limit: int) -> int:
    for blank in (b"\n", b"\r\n", b"\r\r\n"):
        if buf[start:start + len(blank)] == blank:
            return start  # empty first line: the message has no headers
    end = limit
    for blank in _BLANK_LINES:
        pos = buf.find(blank, start, end)
        if pos != -1:
            end = pos + 1
    return end


def iter_raw_messages(buf, starts: list[int]) -> Iterator[RawMessage]:
    size = len(buf)
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else size
        nl = buf.find(b"\n", start, end)
        if nl == -1:
            yield RawMessage(start, bytes(buf[start:end]).rstrip(b"\r"), b"")
            continue
        header_start = nl + 1
        header_end = _header_end(buf, header_start, min(end, header_start + HEADER_LIMIT))
        # normalise line ends: the header parser treats a lone CR as a line break
        headers = bytes(buf[header_start:header_end]).replace(b"\r\n", b"\n")
        yield RawMessage(start, bytes(buf[start:nl]).rstrip(b"\r"), headers)


# ── Header decoding ───────────────────────────────────────────────────────────

def _fix_surrogates(text: str) -> str:
    # Undeclared 8-bit header bytes arrive as surrogate escapes; they are
    # almost always UTF-8 in practice.
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def decode_header_value(value) -> str:
    """Decode RFC 2047 encoded words and unfold a header into one line."""
    if value is None:
        return ""
    try:
        text = str(make_header(decode_header(value)))
    except Exception:
        try:
            parts = []
            for chunk, charset in decode_header(value):
                if isinstance(chunk, bytes):
                    try:
                        chunk = chunk.decode(charset or "utf-8", "replace")
                    except LookupError:
                        chunk = chunk.decode("utf-8", "replace")
                parts.append(chunk)
            text = " ".join(parts)
        except Exception:
            text = str(value)
    return " ".join(_fix_surrogates(text).split())


# ── Dates ─────────────────────────────────────────────────────────────────────

def parse_date(value) -> datetime | None:
    """RFC 2822 date -> naive UTC datetime, or None if unparseable."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        except (OverflowError, ValueError):
            dt = dt.replace(tzinfo=None)
    return dt


def received_date(values) -> datetime | None:
    """Timestamp of the top-most parseable Received: header (final delivery hop)."""
    for value in values or ():
        text = str(value)
        if ";" in text:
            dt = parse_date(text.rsplit(";", 1)[1].strip())
            if dt is not None:
                return dt
    return None


_MONTHS = {m: i for i, m in enumerate(
    (b"Jan", b"Feb", b"Mar", b"Apr", b"May", b"Jun",
     b"Jul", b"Aug", b"Sep", b"Oct", b"Nov", b"Dec"), start=1)}

# asctime() in the "From " line: "Mon Jun  1 10:00:00 2009",
# Gmail Takeout adds a zone: "Thu Apr 09 11:07:58 +0000 2015".
_ENVELOPE_RE = re.compile(
    rb"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s+"
    rb"(\d{1,2}):(\d{2})(?::(\d{2}))?\s+(?:([+-]\d{4})\s+|[A-Za-z]{1,5}\s+)?(\d{4})")


def envelope_date(from_line: bytes) -> datetime | None:
    """Date from the mbox "From " separator line (when the mail was written to the mbox)."""
    matches = list(_ENVELOPE_RE.finditer(from_line))
    if not matches:
        return None
    mon, day, hh, mm, ss, tz, year = matches[-1].groups()
    try:
        dt = datetime(int(year), _MONTHS[mon], int(day), int(hh), int(mm), int(ss or 0))
    except ValueError:
        return None
    if tz:
        offset = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
        dt = dt - offset if tz[:1] == b"+" else dt + offset
    return dt


def message_date(headers, from_line: bytes) -> tuple[datetime | None, str | None]:
    """Best available date: Date header, then Received, then the envelope line."""
    dt = parse_date(headers.get("Date"))
    if dt is not None:
        return dt, "date_header"
    dt = received_date(headers.get_all("Received"))
    if dt is not None:
        return dt, "received_header"
    dt = envelope_date(from_line)
    if dt is not None:
        return dt, "envelope"
    return None, None


# "Display Name <user@host>" - the address in angle brackets at the very end
_ANGLE_ADDR = re.compile(r"<([^<>\s]+@[^<>\s]+)>\s*$")


def split_sender(value) -> tuple[str, str]:
    """(display name, address) from a From: header.

    parseaddr() treats a comma inside an unquoted or split display name
    ('"Janet Ho", "Esq." <jho@...>') as an address separator, so a trailing
    angle-bracket address is taken first and everything before it is the name.
    """
    text = decode_header_value(value)
    if not text:
        return "", ""
    match = _ANGLE_ADDR.search(text)
    if match:
        name = " ".join(text[:match.start()].replace('"', " ").split())
        return name.replace(" ,", ","), match.group(1)
    name, addr = parseaddr(text)
    if "@" not in addr:
        return text, ""
    return name, addr


# ── Scanner ───────────────────────────────────────────────────────────────────

def scan_mbox(path: str, result: ScanResult, *, export: ExportWriter | None = None,
              progress: Callable[[int, int], None] | None = None) -> None:
    result.method = "mbox-headers"
    stats = result.stats
    with open(path, "rb") as fh:
        if os.fstat(fh.fileno()).st_size == 0:
            return
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            starts = message_starts(buf)
            total = len(starts)
            if total == 0:
                result.error = "not an mbox file: no 'From ' separator lines found"
                return
            step = max(1, total // 200)
            if progress:
                progress(0, total)
            for done, raw in enumerate(iter_raw_messages(buf, starts), start=1):
                try:
                    headers = _parser.parsebytes(raw.headers)
                    dt, source = message_date(headers, raw.from_line)
                except Exception:
                    result.unreadable += 1
                    if export is not None:
                        export.write(date=None, source=None)
                else:
                    ok = stats.add(dt, source)
                    if export is not None:
                        _export_row(export, headers, dt, source, ok)
                if progress and done % step == 0:
                    progress(done, total)


def _export_row(export: ExportWriter, headers, dt, source, ok: bool) -> None:
    name, addr = split_sender(headers.get("From"))
    export.write(
        date=dt, source=source,
        implausible=dt is not None and not ok,
        folder=decode_header_value(headers.get("X-Gmail-Labels")),
        subject=decode_header_value(headers.get("Subject")),
        sender_name=name, sender_email=addr,
        message_id=decode_header_value(headers.get("Message-ID")),
    )

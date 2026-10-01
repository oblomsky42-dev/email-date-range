"""PST / OST scanner built on libpff (``pip install libpff-python``)."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

from .export import ExportWriter
from .model import ScanResult

try:
    import pypff
except ImportError:  # PST/OST support is optional; MBOX works without it
    pypff = None

INSTALL_HINT = "pypff is not installed - run: pip install libpff-python"

# MAPI property tags read from a message's property context
PR_MESSAGE_CLASS = 0x001A
PR_SENDER_EMAIL_ADDRESS = 0x0C1F
PR_INTERNET_MESSAGE_ID = 0x1035
PR_SENDER_SMTP_ADDRESS = 0x5D01
_STRING_TYPES = (0x001E, 0x001F)  # PT_STRING8, PT_UNICODE
_CLASS_ONLY = frozenset({PR_MESSAGE_CLASS})
_EXPORT_PROPS = frozenset({PR_MESSAGE_CLASS, PR_SENDER_EMAIL_ADDRESS,
                           PR_INTERNET_MESSAGE_ID, PR_SENDER_SMTP_ADDRESS})

# Preference order: when it arrived, when it was sent, when the item was
# created. creation_time is a fallback only - it changes on copy/import.
DATE_ATTRS = ("delivery_time", "client_submit_time", "creation_time")

# --mail-only keeps e-mail, meeting requests/responses and delivery reports;
# drops appointments, contacts, tasks, notes, journal entries.
MAIL_CLASS_PREFIXES = ("ipm.note", "ipm.schedule.meeting", "report.ipm.note")

ORPHAN_FOLDER = "<orphan>"


def _safe(fn, default=None):
    """libpff raises on corrupt structures; one bad item must not stop a scan."""
    try:
        return fn()
    except Exception:
        return default


def is_mail_class(item_class: str) -> bool:
    return not item_class or item_class.lower().startswith(MAIL_CLASS_PREFIXES)


def open_pst(path: str):
    if pypff is None:
        raise RuntimeError(INSTALL_HINT)
    pff = pypff.file()
    # libpff mangles forward-slash paths on Windows; hand it a native one
    pff.open(os.path.abspath(path))
    return pff


def message_date(msg):
    """(datetime, source attribute) of the best available timestamp, or (None, None)."""
    for attr in DATE_ATTRS:
        dt = _safe(lambda: getattr(msg, attr))
        if dt:
            return dt.replace(tzinfo=None), attr
    return None, None


def string_properties(item, wanted: frozenset) -> dict:
    """Read selected string properties from an item's first record set."""
    found = {}
    record_set = _safe(lambda: item.get_record_set(0))
    if record_set is None:
        return found
    for index in range(_safe(lambda: record_set.number_of_entries, 0)):
        try:
            entry = record_set.get_entry(index)
            tag = entry.entry_type
            if tag in wanted and tag not in found and entry.value_type in _STRING_TYPES:
                found[tag] = entry.get_data_as_string() or ""
                if len(found) == len(wanted):
                    break
        except Exception:
            continue
    return found


def folder_name(folder) -> str:
    return _safe(lambda: folder.name) or "<unnamed>"


def count_messages(folder) -> int:
    total = _safe(lambda: folder.number_of_sub_messages, 0)
    for index in range(_safe(lambda: folder.number_of_sub_folders, 0)):
        sub = _safe(lambda: folder.get_sub_folder(index))
        if sub is not None:
            total += count_messages(sub)
    return total


def iter_folder_messages(folder, path: str = "") -> Iterator[tuple]:
    """Yield (message, folder_path) for every message below ``folder``.

    The message is None when libpff cannot open it.
    """
    for index in range(_safe(lambda: folder.number_of_sub_messages, 0)):
        yield _safe(lambda: folder.get_sub_message(index)), path
    for index in range(_safe(lambda: folder.number_of_sub_folders, 0)):
        sub = _safe(lambda: folder.get_sub_folder(index))
        if sub is not None:
            name = folder_name(sub)
            yield from iter_folder_messages(sub, f"{path}/{name}" if path else name)


def iter_orphan_messages(pff) -> Iterator[tuple]:
    """Yield (message, folder_path) for messages in orphaned nodes.

    Orphans are nodes no longer linked into the folder tree - typically mail
    deleted past Deleted Items. An orphan can be a message or a whole folder;
    libpff may list a hard-deleted folder *and* its messages as separate
    orphans, so messages are de-duplicated by node identifier.
    """
    seen = set()
    for index in range(_safe(lambda: pff.number_of_orphan_items, 0)):
        item = _safe(lambda: pff.get_orphan_item(index))
        if isinstance(item, pypff.message):
            candidates = [(item, ORPHAN_FOLDER)]
        elif isinstance(item, pypff.folder):
            candidates = iter_folder_messages(item, f"{ORPHAN_FOLDER}/{folder_name(item)}")
        else:
            continue  # unreadable node, attachment, recipient table, ...
        for msg, path in candidates:
            ident = _safe(lambda: msg.identifier) if msg is not None else None
            if ident is not None:
                if ident in seen:
                    continue
                seen.add(ident)
            yield msg, path


class _MessageHandler:
    def __init__(self, result: ScanResult, export: ExportWriter | None, mail_only: bool):
        self.result = result
        self.export = export
        self.mail_only = mail_only
        self.wanted = _EXPORT_PROPS if export is not None else _CLASS_ONLY

    def __call__(self, msg, folder: str, recovered: bool) -> None:
        result = self.result
        if msg is None:
            result.unreadable += 1
            if self.export is not None:
                self.export.write(date=None, source=None, recovered=recovered, folder=folder)
            return
        props = string_properties(msg, self.wanted)
        item_class = props.get(PR_MESSAGE_CLASS, "")
        result.item_classes[item_class or "(none)"] += 1
        if self.mail_only and not is_mail_class(item_class):
            result.skipped_non_mail += 1
            return
        if recovered:
            result.recovered += 1
        dt, source = message_date(msg)
        ok = result.stats.add(dt, source)
        if self.export is not None:
            self.export.write(
                date=dt, source=source,
                implausible=dt is not None and not ok,
                recovered=recovered, folder=folder, item_class=item_class,
                subject=_safe(lambda: msg.subject) or "",
                sender_name=_safe(lambda: msg.sender_name) or "",
                sender_email=(props.get(PR_SENDER_SMTP_ADDRESS)
                              or props.get(PR_SENDER_EMAIL_ADDRESS, "")),
                message_id=props.get(PR_INTERNET_MESSAGE_ID, ""),
            )


LOCKED_HINT = ("the file is locked by another process - most likely it is open in Outlook, "
               "which holds the lock until it exits. Close Outlook or work on a copy.")


def lock_error(path: str) -> str | None:
    """LOCKED_HINT if Windows reports a sharing/lock violation on the file, else None."""
    try:
        with open(path, "rb") as fh:
            fh.read(4096)
    except OSError as exc:
        if getattr(exc, "winerror", None) in (32, 33):  # sharing / lock violation
            return LOCKED_HINT
    return None


def scan_pst(path: str, result: ScanResult, *, recover: bool = False,
             mail_only: bool = False, export: ExportWriter | None = None,
             progress: Callable[[int, int], None] | None = None) -> None:
    if pypff is None:
        result.error = INSTALL_HINT
        return
    result.method = "libpff"
    locked = lock_error(path)
    if locked:
        result.error = locked
        return
    pff = open_pst(path)
    try:
        try:
            root = pff.get_root_folder()
        except Exception as exc:
            result.error = ("libpff cannot read the folder tree "
                            f"({str(exc).split('.')[0]}); try --recover or scanpst")
            root = None
        handle = _MessageHandler(result, export, mail_only)
        if root is not None:
            total = count_messages(root)
            step = max(1, total // 200)
            if progress:
                progress(0, total)
            for done, (msg, folder) in enumerate(iter_folder_messages(root), start=1):
                handle(msg, folder, recovered=False)
                if progress and done % step == 0:
                    progress(done, total)
        if recover:
            for msg, folder in iter_orphan_messages(pff):
                handle(msg, folder, recovered=True)
        if root is None and result.stats.dated:
            result.error = None  # the orphans gave us something to report
    finally:
        _safe(pff.close)

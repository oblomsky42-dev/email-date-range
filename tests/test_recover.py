import csv
import hashlib
import json
import os
from pathlib import Path

import pytest

from email_date_range import recover


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_parse_pff_time():
    assert recover.parse_pff_time("Jan 05, 2001 07:08:09.123456789 UTC") == "2001-01-05 07:08:09"
    assert recover.parse_pff_time("Dec 31, 1999 23:59:59 UTC") == "1999-12-31 23:59:59"
    assert recover.parse_pff_time("not set") is None


def test_parse_outlook_headers(tmp_path):
    # layout and labels as written by pffexport (libpff pfftools/export_handle.c)
    msg_dir = tmp_path / "mail_export.orphans" / "Message00001"
    msg_dir.mkdir(parents=True)
    (msg_dir / "OutlookHeaders.txt").write_text(
        "Client submit time:\t\t\tMar 02, 2014 10:00:00.000000000 UTC\n"
        "Delivery time:\t\t\t\tMar 02, 2014 10:00:05.000000000 UTC\n"
        "Creation time:\t\t\t\tJan 01, 2020 00:00:00.000000000 UTC\n"
        "Subject:\t\t\t\tQuarterly numbers\n"
        "Sender name:\t\t\t\tAlice\n", encoding="utf-8")
    (msg_dir / "Message.txt").write_text("Delivery time: this is body text\n", encoding="utf-8")
    items = recover.collect_pffexport_items([tmp_path / "mail_export.orphans"])
    assert len(items) == 1
    item = items[0]
    assert item["date_utc"] == "2014-03-02 10:00:05" and item["date_source"] == "delivery_time"
    assert item["subject"] == "Quarterly numbers" and item["sender"] == "Alice"
    assert item["identifier"] == "Message00001"


def test_refuses_workdir_equal_to_source_folder(tmp_path, capsys):
    src = tmp_path / "evidence.pst"
    src.write_bytes(b"!BDN" + b"\0" * 64)
    before = sha256(src)
    assert recover.main(["--src", str(src), "--workdir", str(tmp_path)]) == recover.EXIT_FAILED
    assert "must not be the folder" in capsys.readouterr().err
    assert sha256(src) == before


def test_scanpst_prep_touches_only_the_copy(tmp_path):
    src = tmp_path / "evidence" / "mail.pst"
    src.parent.mkdir()
    src.write_bytes(b"!BDN" + bytes(range(256)) * 8)
    before = sha256(src)
    work = tmp_path / "work"
    code = recover.main(["--src", str(src), "--workdir", str(work),
                         "--method", "scanpst", "--truncate", "100"])
    assert code == recover.EXIT_OK
    assert sha256(src) == before
    assert (work / "mail.pst").stat().st_size == src.stat().st_size - 100
    report = json.loads((work / "recovery_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert report["source_sha256_before"] == report["source_sha256_after"] == before
    assert report["scanpst"]["truncated_bytes"] == 100


def test_orphan_mode_end_to_end(tika_pst, tmp_path):
    work = tmp_path / "work"
    code = recover.main(["--src", str(tika_pst), "--workdir", str(work)])
    assert code == recover.EXIT_OK
    report = json.loads((work / "recovery_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert report["source_sha256_before"] == report["working_copy_sha256"] == sha256(tika_pst)
    assert report["orphan"]["available"] is True
    assert report["orphan"]["count"] == 0  # the Tika fixture has no orphans
    rows = list(csv.reader((work / "recovered_items.csv").open(encoding="utf-8-sig")))
    assert rows[0] == recover.ITEM_COLUMNS
    assert (work / "recovery_log.txt").read_text(encoding="utf-8").count("SHA-256") >= 1


def test_deep_mode_without_pffexport(tika_pst, tmp_path, monkeypatch):
    monkeypatch.setattr(recover, "find_pffexport", lambda explicit=None: None)
    work = tmp_path / "work"
    assert recover.main(["--src", str(tika_pst), "--workdir", str(work),
                         "--method", "deep"]) == recover.EXIT_OK
    report = json.loads((work / "recovery_report.json").read_text(encoding="utf-8"))
    assert report["deep"]["available"] is False


@pytest.mark.parametrize("bad", ["0", "100000"])
def test_scanpst_rejects_bad_truncate(tmp_path, bad):
    src = tmp_path / "ev" / "m.pst"
    src.parent.mkdir()
    src.write_bytes(b"!BDN" + b"\0" * 100)
    code = recover.main(["--src", str(src), "--workdir", str(tmp_path / "w"),
                         "--method", "scanpst", "--truncate", bad])
    assert code == recover.EXIT_FAILED


def _write_headers(path, text, encoding):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", "\r\n").encode(encoding))


def test_windows_pffexport_writes_utf16_and_attachments_are_skipped(tmp_path):
    # pffexport built on Windows writes wide strings: UTF-16LE, no BOM, CRLF
    root = tmp_path / "x.recovered"
    _write_headers(root / "Message00001" / "OutlookHeaders.txt",
                   "Message:\nDelivery time:\t\t\t\tFeb 26, 2014 07:51:02.000000000 UTC\n"
                   "Subject:\t\t\t\tRe: Größe\nSender name:\t\t\t\tJörn\n", "utf-16-le")
    _write_headers(root / "Message00001" / "Attachments" / "Attachment00001" / "Message00001"
                   / "OutlookHeaders.txt",
                   "Delivery time:\t\t\t\tNov 26, 2020 22:18:00.000000000 UTC\n", "utf-16-le")
    items = recover.collect_pffexport_items([root])
    assert len(items) == 1
    assert (items[0]["date_utc"], items[0]["subject"], items[0]["sender"]) == \
        ("2014-02-26 07:51:02", "Re: Größe", "Jörn")


def crash_worker(path):
    """Stand-in for recover.orphan_items that dies like a libpff segfault."""
    import os
    os._exit(3)


def test_orphan_scan_crash_is_contained(tika_pst, tmp_path):
    log = recover.Log(tmp_path / "log.txt")
    try:
        res = recover.recover_orphan(tika_pst, log, worker=crash_worker)
    finally:
        log.close()
    assert res == {"available": True, "items": [], "crashed": True}
    assert "crashed during the orphan scan" in (tmp_path / "log.txt").read_text(encoding="utf-8")


def test_explicit_pffexport_path_must_exist(tika_pst, tmp_path):
    work = tmp_path / "work"
    assert recover.main(["--src", str(tika_pst), "--workdir", str(work), "--method", "deep",
                         "--pffexport", str(tmp_path / "missing.exe")]) == recover.EXIT_OK
    report = json.loads((work / "recovery_report.json").read_text(encoding="utf-8"))
    assert report["deep"]["available"] is False


def test_parser_matches_real_pffexport_output(tika_pst, tmp_path):
    """Export the fixture with the real pffexport and compare with libpff's own reading."""
    import subprocess

    from email_date_range.cli import ScanOptions, scan_file

    exe = recover.find_pffexport(os.environ.get("PFFEXPORT"))
    if not exe:
        pytest.skip("pffexport not available (apt install pff-tools, or set PFFEXPORT)")
    target = tmp_path / "tika"
    subprocess.run([exe, "-m", "all", "-f", "text", "-q", "-t", str(target), str(tika_pst)],
                   check=True, capture_output=True)
    deep = sorted((i["date_utc"], i["subject"])
                  for i in recover.collect_pffexport_items([Path(str(target) + ".export")]))
    export_csv = tmp_path / "libpff.csv"
    scan_file(str(tika_pst), ScanOptions(), str(export_csv))
    direct = sorted((r["date_utc"], r["subject"])
                    for r in csv.DictReader(export_csv.open(encoding="utf-8-sig")))
    assert len(deep) == 7 and deep == direct

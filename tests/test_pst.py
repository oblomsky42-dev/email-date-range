"""PST tests against the public Apache Tika fixtures (see scripts/fetch_test_data.py)."""

import csv
from datetime import datetime

from email_date_range.cli import main
from email_date_range.export import ExportWriter
from email_date_range.model import ScanResult
from email_date_range.pst import is_mail_class, scan_pst


def scan(path, **kwargs):
    result = ScanResult(file=str(path), size_mb=0)
    scan_pst(str(path), result, **kwargs)
    return result


def test_tika_pst_dates(tika_pst):
    r = scan(tika_pst)
    assert r.error is None
    s = r.stats
    assert s.dated == 7 and s.no_date == 0
    assert dict(s.by_year) == {2014: 6, 2020: 1}
    assert s.earliest.replace(microsecond=0) == datetime(2014, 2, 24, 21, 14, 37)
    assert s.latest.replace(microsecond=0) == datetime(2020, 11, 26, 22, 18)
    assert dict(s.sources) == {"delivery_time": 7}
    assert dict(r.item_classes) == {"IPM.Note": 7}


def test_various_body_types(tika_pst_bodies):
    s = scan(tika_pst_bodies).stats
    assert s.dated == 4 and set(s.by_year) == {2017}


def test_export_strips_subject_prefix_marker(tika_pst, tmp_path):
    out = tmp_path / "items.csv"
    with ExportWriter(str(out), str(tika_pst)) as export:
        scan(tika_pst, export=export)
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert len(rows) == 7
    assert all(r["date_source"] == "delivery_time" for r in rows)
    # PR_SUBJECT is stored as "\x01\x05Re: ..."; pypff's subject drops the marker
    assert all(not r["subject"].startswith("\x01") for r in rows)
    assert any(r["subject"].startswith("Re: ") for r in rows)
    assert all(r["folder"] for r in rows)


def test_recover_on_clean_pst_finds_nothing(tika_pst):
    r = scan(tika_pst, recover=True)
    assert r.recovered == 0 and r.stats.dated == 7


def test_mail_only_filter():
    assert is_mail_class("IPM.Note") and is_mail_class("IPM.Note.SMIME")
    assert is_mail_class("IPM.Schedule.Meeting.Request") and is_mail_class("REPORT.IPM.Note.NDR")
    assert is_mail_class("")  # unknown class is kept
    assert not is_mail_class("IPM.Appointment") and not is_mail_class("IPM.Contact")


def test_cli_on_pst_forward_slash_path(tika_pst, capsys):
    # libpff mangles forward-slash paths on Windows; the scanner must normalise them
    assert main([tika_pst.as_posix(), "--no-progress"]) == 0
    assert "Total messages  : 7" in capsys.readouterr().out

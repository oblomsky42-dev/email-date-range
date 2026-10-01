import csv
import json
import os
from pathlib import Path

import pytest
from conftest import mbox_message

from email_date_range.cli import (
    CRASH_ERROR,
    ScanOptions,
    collect_files,
    detect_format,
    main,
    plan_export_paths,
    run_scan,
    scan_file,
)


def crash_on_marked_files(path, options, export_path=None, progress_q=None):
    """Stand-in for scan_file that kills its process, like a segfault inside libpff."""
    if "crash" in Path(path).name:
        os._exit(3)
    return scan_file(path, options, export_path, progress_q)


def _two_year_mbox(write_mbox, name="mail.mbox"):
    return write_mbox(
        name,
        mbox_message({"Date": "Mon, 10 Aug 2020 09:00:00 +0000", "Subject": "a"}),
        mbox_message({"Date": "Wed, 10 Feb 2021 09:00:00 +0000", "Subject": "b"}),
        mbox_message({"Date": "Thu, 10 Jun 2021 09:00:00 +0000", "Subject": "c"}),
    )


def test_detect_format_by_signature(tmp_path):
    pst = tmp_path / "renamed.dat"
    pst.write_bytes(b"!BDN" + b"\0" * 100)
    mbox = tmp_path / "Inbox"  # Thunderbird: no extension
    mbox.write_bytes(b"From x Mon Jun 01 10:00:00 2009\n\n")
    other = tmp_path / "notes.txt"
    other.write_bytes(b"hello")
    assert detect_format(pst) == "pst"
    assert detect_format(mbox) == "mbox"
    assert detect_format(other) is None


def test_collect_files(tmp_path):
    (tmp_path / "sub").mkdir()
    for rel in ("a.mbox", "b.PST", "c.txt", "sub/d.ost", "sub/e.mbx"):
        (tmp_path / rel).write_bytes(b"x")
    flat, problems = collect_files([str(tmp_path)], recursive=False)
    assert sorted(p.name for p in flat) == ["a.mbox", "b.PST"] and not problems
    deep, _ = collect_files([str(tmp_path)], recursive=True)
    assert sorted(p.name for p in deep) == ["a.mbox", "b.PST", "d.ost", "e.mbx"]
    explicit, _ = collect_files([str(tmp_path / "c.txt"), str(tmp_path / "a.mbox"),
                                 str(tmp_path / "a.mbox")], recursive=False)
    assert [p.name for p in explicit] == ["c.txt", "a.mbox"]  # explicit files kept, duplicates dropped
    _, problems = collect_files([str(tmp_path / "missing")], recursive=False)
    assert problems and "not found" in problems[0]


def test_export_names_never_collide(tmp_path):
    files = [Path("x/mail.pst"), Path("y/mail.pst"), Path("z/mail.mbox"), Path("z/other.mbox")]
    plan = plan_export_paths(str(tmp_path / "items.csv"), files)
    names = [Path(plan[str(f)]).name for f in files]
    assert names == ["items_mail.csv", "items_mail_pst.csv", "items_mail_mbox.csv", "items_other.csv"]
    assert len(set(plan.values())) == len(files)
    folder_plan = plan_export_paths(str(tmp_path), [Path("a/one.mbox")])
    assert folder_plan[str(Path("a/one.mbox"))] == str(tmp_path / "one.csv")


def test_end_to_end_reports(write_mbox, tmp_path, capsys):
    mbox = _two_year_mbox(write_mbox)
    csv_path, json_path = tmp_path / "s.csv", tmp_path / "s.json"
    code = main([str(mbox), "--gaps", "--monthly", "--no-progress",
                 "--csv", str(csv_path), "--json", str(json_path),
                 "--export", str(tmp_path / "items.csv")])
    out = capsys.readouterr().out
    assert code == 0
    assert "Total messages  : 3" in out
    assert "2020-09 -> 2021-01" in out  # the gap

    doc = json.loads(json_path.read_text(encoding="utf-8"))
    f = doc["files"][0]
    assert f["messages"] == 3 and f["by_year"] == {"2020": 1, "2021": 2}
    assert f["gaps"] == [{"start": "2020-09", "end": "2021-01", "months": 5},
                         {"start": "2021-03", "end": "2021-05", "months": 3}]
    assert f["by_month"]["2020-12"] == 0 and len(f["by_month"]) == 11
    assert doc["summary"]["earliest_utc"] == "2020-08-10T09:00:00Z"

    rows = list(csv.reader(csv_path.open(encoding="utf-8-sig")))
    header = rows[1]
    first = dict(zip(header, rows[2], strict=False))
    assert first["messages"] == "3" and first["earliest_utc"] == "2020-08-10 09:00:00"
    assert ["TOTAL", "1", "2", "3"] in rows

    items = list(csv.DictReader((tmp_path / "items_mail.csv").open(encoding="utf-8-sig")))
    assert [r["subject"] for r in items] == ["a", "b", "c"]


def test_errors_set_exit_code_and_log(write_mbox, tmp_path, capsys):
    good = _two_year_mbox(write_mbox)
    bad = tmp_path / "broken.pst"
    bad.write_bytes(b"!BDN" + b"\xff" * 512)
    log = tmp_path / "errors.log"
    code = main([str(good), str(bad), "--no-progress", "--log", str(log)])
    out = capsys.readouterr().out
    assert code == 2
    assert "1 errors" in out
    assert "broken.pst" in log.read_text(encoding="utf-8")


def test_nothing_to_scan(tmp_path, capsys):
    assert main([str(tmp_path / "nope")]) == 1
    assert "not found" in capsys.readouterr().err


def test_workers_give_same_result(write_mbox, tmp_path):
    first = _two_year_mbox(write_mbox, "one.mbox")
    second = _two_year_mbox(write_mbox, "two.mbox")
    single, multi = tmp_path / "1.json", tmp_path / "2.json"
    assert main([str(first), str(second), "--no-progress", "--json", str(single)]) == 0
    assert main([str(first), str(second), "--no-progress", "--workers", "2",
                 "--json", str(multi)]) == 0
    a = json.loads(single.read_text(encoding="utf-8"))
    b = json.loads(multi.read_text(encoding="utf-8"))
    assert [f["file"] for f in a["files"]] == [f["file"] for f in b["files"]]
    assert a["summary"]["messages"] == b["summary"]["messages"] == 6


@pytest.mark.parametrize("workers", [1, 3])
def test_a_crashing_file_does_not_abort_the_run(write_mbox, workers, capsys):
    names = ["a.mbox", "crash1.mbox", "b.mbox", "crash2.mbox", "c.mbox"]
    files = [_two_year_mbox(write_mbox, name) for name in names]
    results = run_scan(files, ScanOptions(), workers, {}, False, scan=crash_on_marked_files)
    assert [r.name for r in results] == names
    for r in results:
        if "crash" in r.name:
            assert r.error == CRASH_ERROR
        else:
            assert r.error is None and r.messages == 3
    assert capsys.readouterr().out.count("ERROR:") == 2


def test_fast_flag_is_accepted_but_ignored(write_mbox, capsys):
    mbox = _two_year_mbox(write_mbox)
    assert main([str(mbox), "--fast", "--no-progress"]) == 0
    assert "--fast is obsolete" in capsys.readouterr().err

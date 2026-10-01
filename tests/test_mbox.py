import csv
import mailbox
import random
from datetime import datetime

from conftest import mbox_message

from email_date_range.export import ExportWriter
from email_date_range.mbox import (
    decode_header_value,
    envelope_date,
    parse_date,
    received_date,
    scan_mbox,
    split_sender,
)
from email_date_range.model import ScanResult


def scan(path, export=None):
    result = ScanResult(file=str(path), size_mb=0)
    scan_mbox(str(path), result, export=export)
    return result


# ── header helpers ────────────────────────────────────────────────────────────

def test_decode_rfc2047_subjects():
    assert decode_header_value("Project Kickoff =?utf-8?b?4oCU?= Q3 planning") == \
        "Project Kickoff — Q3 planning"
    assert decode_header_value("=?utf-8?b?0J3QsNC/0L7QvNC40L3QsNC90LjQtQ==?=") == "Напоминание"
    assert decode_header_value("=?koi8-r?b?8NLJ18XU?=") == "Привет"
    assert decode_header_value("Plain\n\tfolded   subject") == "Plain folded subject"
    assert decode_header_value(None) == ""


def test_decode_unknown_charset_does_not_crash():
    assert "abc" in decode_header_value("=?x-unknown?q?abc?=")


def test_split_sender_variants():
    assert split_sender('"Janet Ho", "Esq." <jho@law.example>') == ("Janet Ho, Esq.", "jho@law.example")
    assert split_sender("=?utf-8?q?Greta_M=C3=BCller?= <g@x.example>") == ("Greta Müller", "g@x.example")
    assert split_sender("bob@example.com (Bob)") == ("Bob", "bob@example.com")
    assert split_sender("<only@example.com>") == ("", "only@example.com")
    assert split_sender("Undisclosed recipients") == ("Undisclosed recipients", "")


def test_parse_date_converts_to_utc():
    assert parse_date("Tue, 04 Mar 2025 10:15:00 +0300") == datetime(2025, 3, 4, 7, 15)
    assert parse_date("Tue, 04 Mar 2025 10:15:00 -0000") == datetime(2025, 3, 4, 10, 15)
    assert parse_date("not a date") is None
    assert parse_date("") is None


def test_received_takes_first_parseable_hop():
    hops = ["from a by b with c; garbage",
            "from x by y with SMTP; Wed, 02 Apr 2014 08:00:00 +0200"]
    assert received_date(hops) == datetime(2014, 4, 2, 6, 0)
    assert received_date(None) is None


def test_envelope_formats():
    assert envelope_date(b"From someone Mon Jun 01 10:00:00 2009") == datetime(2009, 6, 1, 10)
    assert envelope_date(b"From x@y Mon Jun  1 10:00:00 2009") == datetime(2009, 6, 1, 10)
    # Gmail Takeout writes a numeric zone before the year
    assert envelope_date(b"From 1498017565779417697@xxx Thu Apr 09 11:07:58 +0300 2015") == \
        datetime(2015, 4, 9, 8, 7, 58)
    # the address may contain a month name; the last match wins
    assert envelope_date(b"From Jun 12@x.example Tue Feb 03 04:05:06 2004") == datetime(2004, 2, 3, 4, 5, 6)
    assert envelope_date(b"From MAILER-DAEMON") is None


# ── scanning ──────────────────────────────────────────────────────────────────

def test_date_source_fallback_chain(write_mbox):
    path = write_mbox(
        "chain.mbox",
        mbox_message({"Date": "Sat, 15 Jun 2019 10:00:00 +0000", "Subject": "a"}),
        mbox_message({"Received": "from a by b; Mon, 03 Mar 2014 12:00:00 +0000", "Subject": "b"}),
        mbox_message({"Subject": "c"}, envelope="From x Wed Jan 05 06:07:08 2011"),
        mbox_message({"Subject": "d"}, envelope="From nobody"),
    )
    r = scan(path)
    s = r.stats
    assert (s.dated, s.no_date) == (3, 1)
    assert dict(s.sources) == {"date_header": 1, "received_header": 1, "envelope": 1}
    assert s.fallback_dates == 2
    assert dict(s.by_year) == {2011: 1, 2014: 1, 2019: 1}


def test_implausible_dates_are_excluded(write_mbox):
    path = write_mbox(
        "odd.mbox",
        mbox_message({"Date": "Thu, 01 Jan 1970 00:00:00 +0000"}),
        mbox_message({"Date": "Fri, 01 Jan 2100 00:00:00 +0000"}),
        mbox_message({"Date": "Mon, 10 Aug 2020 09:00:00 +0000"}),
    )
    s = scan(path).stats
    assert s.dated == 1 and s.implausible == 2
    assert s.earliest == s.latest == datetime(2020, 8, 10, 9)
    assert datetime(1970, 1, 1) in s.implausible_samples


def test_escaped_from_lines_do_not_split(write_mbox):
    body = "Hi,\n>From the minutes: nothing.\nFromage is not a separator.\n"
    path = write_mbox("esc.mbox", mbox_message({"Date": "Mon, 10 Aug 2020 09:00:00 +0000"}, body))
    assert scan(path).stats.dated == 1


def test_crlf_mbox(write_mbox):
    path = write_mbox(
        "crlf.mbox",
        mbox_message({"Date": "Mon, 10 Aug 2020 09:00:00 +0000", "Subject": "one"}),
        mbox_message({"Date": "Tue, 11 Aug 2020 09:00:00 +0000", "Subject": "two"}),
        newline="\r\n",
    )
    s = scan(path).stats
    assert s.dated == 2 and s.latest == datetime(2020, 8, 11, 9)


def test_double_converted_crlf_mbox(write_mbox):
    # a CRLF mailbox that went through another text-mode conversion: CR CR LF
    path = write_mbox(
        "crcrlf.mbox",
        mbox_message({"X-First": "1", "Date": "Mon, 10 Aug 2020 09:00:00 +0000"}, "body\n"),
        mbox_message({"X-First": "1", "Date": "Tue, 11 Aug 2020 09:00:00 +0000"}),
        newline="\r\r\n",
    )
    s = scan(path).stats
    assert s.dated == 2 and dict(s.sources) == {"date_header": 2}


def test_message_without_headers_or_body(write_mbox):
    path = write_mbox("bare.mbox", "From x Mon Jun 01 10:00:00 2009\n\n",
                      "From y Tue Jun 02 10:00:00 2009")
    s = scan(path).stats
    assert s.dated == 2 and dict(s.sources) == {"envelope": 2}


def test_empty_file_and_non_mbox(write_mbox, tmp_path):
    empty = tmp_path / "empty.mbox"
    empty.write_bytes(b"")
    r = scan(empty)
    assert r.error is None and r.stats.dated == 0
    junk = write_mbox("junk.mbox", "Subject: not an mbox\n\nhello\n")
    assert "not an mbox" in scan(junk).error


def test_matches_stdlib_mailbox(write_mbox):
    rng = random.Random(7)
    messages = []
    for i in range(300):
        dt = datetime(2000 + rng.randint(0, 25), rng.randint(1, 12), rng.randint(1, 28),
                      rng.randint(0, 23), rng.randint(0, 59))
        headers = {"Subject": f"msg {i}", "Message-ID": f"<{i}@t.example>"}
        if rng.random() > 0.1:
            headers["Date"] = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        body = "x\n" * rng.randint(0, 5) + (">From quoted\n" if i % 7 == 0 else "")
        messages.append(mbox_message(headers, body, envelope="From test@example.com"))
    path = write_mbox("random.mbox", *messages)

    expected = [parse_date(m.get("Date")) for m in mailbox.mbox(str(path), create=False)]
    r = scan(path)
    dated = [d for d in expected if d is not None]
    assert r.stats.dated + r.stats.no_date == len(expected) == 300
    assert r.stats.sources["date_header"] == len(dated)
    assert r.stats.earliest == min(dated) and r.stats.latest == max(dated)


def test_export_rows(write_mbox, tmp_path):
    path = write_mbox(
        "exp.mbox",
        mbox_message({"Date": "Tue, 01 Oct 2024 09:00:00 +0000",
                      "From": "=?utf-8?b?0JXQu9C10L3QsA==?= <elena@x.example>",
                      "Subject": "=?utf-8?b?0J/RgNC40LLQtdGC?=",
                      "Message-ID": "<1@x.example>",
                      "X-Gmail-Labels": "Inbox,Important"}),
        mbox_message({"Subject": "=1+1"}, envelope="From nobody"),
    )
    out = tmp_path / "rows.csv"
    with ExportWriter(str(out), str(path)) as export:
        scan(path, export)
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert len(rows) == 2
    assert rows[0]["date_utc"] == "2024-10-01 09:00:00"
    assert rows[0]["subject"] == "Привет"
    assert (rows[0]["sender_name"], rows[0]["sender_email"]) == ("Елена", "elena@x.example")
    assert rows[0]["folder"] == "Inbox,Important"
    assert rows[0]["message_id"] == "<1@x.example>"
    assert rows[1]["date_source"] == "none" and rows[1]["date_utc"] == ""
    assert rows[1]["subject"] == "'=1+1"  # CSV-injection guard

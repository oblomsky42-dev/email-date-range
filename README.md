# email-date-range

[![CI](https://github.com/oblomsky42-dev/email-date-range/actions/workflows/ci.yml/badge.svg)](https://github.com/oblomsky42-dev/email-date-range/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.14-blue)
![License](https://img.shields.io/badge/license-MIT-green)

*[Русская версия](README.ru.md)*

Triage for mail archives in DFIR and eDiscovery work. Point it at a folder of
**PST, OST and MBOX** files and it tells you, before anything is loaded into a
review platform:

- what period the mail actually covers, per file and overall;
- how it is distributed by year and by month;
- where the **silent gaps** are — months with no mail inside an otherwise busy
  mailbox (deleted mail, an incomplete collection, spoliation);
- **how far the dates can be trusted** — which timestamp every date came
  from, how many are fallbacks, and which are impossible;
- what libpff can still find in **orphaned nodes** of a PST.

It ships with `recover-pst`, a chain-of-custody-safe recovery of deleted
items. Its sister project [pst-testgen](https://github.com/oblomsky42-dev/pst-testgen)
builds PSTs with a known answer, and the scanner is checked against one in CI.

```
  File       : kam_keiser_000.pst
  Size       : 166.6 MB  (0.3s, 599 MB/s)
  Messages   : 1,781
  Item types : IPM.Note 1,781
  Date source: delivery_time 1,781
  Earliest   : 2001-04-19 05:01:00 UTC
  Latest     : 2002-11-30 08:00:00 UTC

  Monthly breakdown (messages per month):
  Year    Jan  Feb  Mar  Apr  May  Jun  Jul  Aug  Sep  Oct  Nov  Dec  Total
  2001      .    .    .    2    8   36   37   73   66   37   22   48    329
  2002  1,239  203    .    .    .    .    .    .    .    .   10    .  1,452

  Gap analysis (monthly):
    Active span   : 2001-04 -> 2002-11  (20 months)
    Months w/ mail: 12/20  (60%)
      2002-03 -> 2002-10     8 mo  <-- notable
```
<sub>Public Enron EDRM mailbox: the mail stops after February 2002, when the
company collapsed; ten stragglers turn up in November.</sub>

## Install

```bash
pip install git+https://github.com/oblomsky42-dev/email-date-range
```

Python 3.10+ on Windows, Linux or macOS. PST/OST reading uses
[libpff](https://github.com/libyal/libpff) through `libpff-python`, which now
ships prebuilt wheels — no C compiler needed. MBOX needs nothing extra.

Windows users who do not want Python can take `email_date_range.exe` and
`recover_pst.exe` from the
[Releases](https://github.com/oblomsky42-dev/email-date-range/releases) page, or
build them with `scripts\build_exe.bat`.

## Usage

```bash
email-date-range "C:\Cases\Matter001" -r --csv summary.csv      # whole collection
email-date-range mailbox.pst --gaps --monthly                    # one mailbox, timeline view
email-date-range mailbox.pst --recover --mail-only               # + orphaned items, e-mail only
email-date-range archive.mbox --export items.csv                 # one CSV row per message
email-date-range "D:\Custodians" -r --workers 4 --json report.json
```

| Option | What it does |
|---|---|
| `PATH ...` | files and/or folders. A file named explicitly is detected by its signature, so a PST renamed to `.dat` or a Thunderbird mbox without an extension still works |
| `-r` | recurse into sub-folders |
| `--recover` | PST/OST: also count messages in orphaned nodes (see below) |
| `--mail-only` | PST/OST: count e-mail, meeting requests and delivery reports only — skip calendar, contacts, tasks, notes |
| `--monthly` | year × month table; `.` marks an empty month |
| `--gaps`, `--min-gap N` | list runs of empty months (≥ N months) between the first and last active month |
| `--csv FILE` | per-file summary and per-year table, UTF-8 with BOM for Excel |
| `--json FILE` | everything, including monthly counts, gaps, date sources and item classes |
| `--export FILE` | one CSV per input with a row per item (columns below) |
| `--workers N` | scan N files in parallel — helps on SSD with several large files |
| `--log FILE` | error log (default `email_date_range_errors.log`, written only on errors) |

Exit status: `0` all files scanned, `1` bad arguments or nothing to scan,
`2` one or more files could not be scanned.

### Where the dates come from

Each item gets the first timestamp that exists, and the report says which one
was used — a timeline built from fallback dates is a different thing from one
built from delivery times.

| Format | Order of preference | Fallback (flagged) |
|---|---|---|
| PST / OST | `PR_MESSAGE_DELIVERY_TIME` → `PR_CLIENT_SUBMIT_TIME` | `PR_CREATION_TIME` — changes when an item is copied or imported |
| MBOX | `Date:` header | top-most `Received:` header, then the `From ` envelope line — when the mail reached the server or the mbox, not when it was sent |

All dates are UTC. Dates before 1980-01-01 or more than two days in the future
(a zeroed FILETIME is 1601-01-01, a broken `Date:` header is often 1970) are
counted as **implausible**, kept out of the range and the distributions, and
flagged in the export.

Two examples from public test corpora where this matters:

- `source.pst` from the Aspose test set reports 2016-08-03 for all 60
  messages — and every one of those dates is a creation-time fallback. The
  date is when the items were written, not when the mail was sent.
- An Outlook.com OST from the pst-extractor tests holds 214 items spanning
  2016-12 → 2018-03. With `--mail-only` it is 9 e-mails over three days in
  March 2018; the rest is calendar and contacts.

### Orphaned items (`--recover`)

An orphan is a node that is still in the PST but no longer linked into the
folder tree. libpff exposes them; `--recover` counts the messages in them
(whole orphaned folders included, de-duplicated by node id) and marks them
`recovered` in the export.

What a deleted message leaves behind is up to whatever wrote the PST. Outlook
2016 and later drop a Shift+Deleted message from the PST's indexes at once: in
a test with Outlook 2024, 1,800 permanently deleted messages left no orphans
and nothing `pffexport` could recover either. Orphans turn up in PSTs written
by older Outlook versions and other tools, and in damaged files — which is
where this option earns its keep. A count of zero is a result, not a proof
that nothing was deleted; the gap analysis is often the better witness.

### Per-item export columns

`file, date_utc, date_source, implausible, recovered, folder, item_class,
subject, sender_name, sender_email, message_id`

Undated items are exported too (with an empty date), so the CSV is a complete
item list. RFC 2047 encoded subjects and names are decoded; for MBOX the
`folder` column carries Gmail's `X-Gmail-Labels`. Cells that Excel would read
as a formula (`=`, `+`, `-`, `@`) get a leading apostrophe.

### Damaged and locked files

- Every file is scanned in a child process. libpff can crash outright on some
  corrupt structures (found by fuzzing, see below); such a file is reported as
  an error and the run carries on with the next one.
- A PST that Outlook has open is locked until Outlook exits; the scanner says
  so instead of passing on libpff's low-level read error.
- CRLF, LF and double-converted CR-CR-LF mailboxes are all read.

## recover-pst

```bash
recover-pst --src "E:\evidence\mail.pst" --workdir "C:\work\case17"
recover-pst --src "E:\evidence\mail.pst" --workdir "C:\work\case17" --method deep
recover-pst --src "E:\evidence\mail.pst" --workdir "C:\work\case17" --method scanpst --truncate 512
```

- The source is SHA-256 hashed, copied, the copy is verified, all work happens
  on the copy, and the source is re-hashed at the end. A changed source gives
  exit status 2; `--workdir` may not be the folder that holds the source.
  libpff runs in a child process, so even a crash on corrupt data still ends
  with the re-hash and a written report.
- `orphan` (default) — messages in orphaned nodes, via libpff.
- `deep` — runs [`pffexport`](https://github.com/libyal/libpff) `-m recovered`
  on the copy (orphans plus items rebuilt from unallocated index entries) and
  parses its `OutlookHeaders.txt` files — UTF-8 from Linux builds, UTF-16 from
  Windows builds; messages attached to other messages are not counted. Needs
  `pffexport` on `PATH` (`apt install pff-tools` on Debian/Ubuntu) or
  `--pffexport PATH`.
- `scanpst` — trims the end of the copy so Microsoft's Inbox Repair Tool does
  a full rebuild; you then run SCANPST.EXE by hand and scan the result with
  `email-date-range --recover`.

Output in `--workdir`: `recovery_report.json` (hashes, tool and libpff
versions, counts, items), `recovered_items.csv`, `recovery_log.txt`.

## Known-answer testing

[pst-testgen](https://github.com/oblomsky42-dev/pst-testgen) builds a PST
through Outlook with a known date distribution and known deletions, and writes
a manifest of the right answer. `tests/fixtures/known_answer.pst` is such a
file (400 synthetic messages, 20 soft- and 20 hard-deleted), and the test
suite checks the scanner against it on every CI run. For your own generated
PSTs:

```bash
python scripts/check_against_manifest.py C:	est	.pst
```

It scans the PST with and without `--recover` and checks totals, per-year
and per-month counts, every message's timestamp and Message-ID, and where the
deleted messages ended up.

## Validation

- **Test suite** — 49 tests (`pytest`), passing locally on Windows with Python
  3.10, 3.12 and 3.14; CI runs them on Windows, Linux and macOS. Date parsing
  and fallbacks, RFC 2047, mbox edge cases (CRLF, CR-CR-LF, `>From `
  escaping, header-less messages), gap arithmetic, report formats, export
  naming, crash isolation, the recovery workflow, PST scans of the public
  Apache Tika fixtures, the known-answer PST, and — where `pffexport` is
  installed — the deep-mode parser against real `pffexport` output.
- **Known-answer PSTs** made by pst-testgen with Outlook 2024: the 400-message
  fixture in CI, plus local runs with 2,000 messages (100 soft and 100 hard
  deletes) and 3,000 messages (1,800 hard deletes). Totals, per-year and
  per-month counts, every message's timestamp, every Message-ID and the
  placement of soft-deleted items match the manifest exactly.
- **Fuzzing** — 295 corrupted variants of five PST/OST files (truncations,
  random bytes, zeroed pages, damaged headers): no hangs, one libpff crash,
  now contained. 450 corrupted mailboxes: no exceptions, and every one read
  exactly as Python's `mailbox` module reads it.
- **MBOX parser vs. the standard library** — identical per-message results
  on 13 mailboxes, including Gmail Takeout and Apache Tika edge cases, at
  roughly 950 MB/s against 28 MB/s for `mailbox.mbox` on the same machine.
- **Synthetic eDiscovery mailbox** (237 MB, 45 messages, subjects in six
  scripts) — 45/45 dates match the manifest by Message-ID.
- **Public PST/OST corpora** — Enron EDRM, Apache Tika, Aspose, pst-extractor
  (including a 2013-format OST) and java-libpst (including a
  password-protected PST): every file libpff can open is scanned; the one it
  cannot (Aspose `destination.pst`) is reported as an error, not as zero mail.

## Limitations

- PST/OST support is exactly as good as libpff's. A file whose folder tree
  libpff cannot read is reported as an error (`--recover` may still salvage
  orphans; otherwise try SCANPST).
- Recovery finds only what the PST still holds as orphans or recoverable index
  entries — with current Outlook versions that is usually nothing.
- mbox envelope (`From `) lines carry no time zone and are taken as UTC.

## Development

```bash
pip install -e ".[test]" ruff
python scripts/fetch_test_data.py   # public PST fixtures, pinned and SHA-256 checked
pytest                              # set PFFEXPORT=path\to\pffexport.exe to include the deep-mode test
ruff check src tests scripts
```

## License

MIT — see [LICENSE](LICENSE). The PST fixtures used by the tests come from
the [Apache Tika](https://github.com/apache/tika) test corpus (Apache License
2.0); they are downloaded at test time and not redistributed here.

# Changelog

## 1.0.0 — 2026-10-01

First public release. Previously three standalone scripts
(`email_date_range.py`, `recover_pst.py`, `generate_test_pst.py`); now an
installable package with console commands, tests and CI. The test PST
generator became a project of its own,
[pst-testgen](https://github.com/oblomsky42-dev/pst-testgen).

### Fixed
- `--fast` read the folder's own property set instead of the contents table:
  it never sped anything up, and on a folder carrying `PR_CREATION_TIME` it gave
  the first message of the folder the folder's creation date. The flag is now a
  documented no-op.
- `--recover` counted every message of an orphaned *folder* twice (once as a
  sub-message, once again as a sub-item). Orphans are now de-duplicated by node id.
- `recover_pst --method deep` found no dates: it parsed `Message.txt` (the
  body) instead of pffexport's `OutlookHeaders.txt`, read pffexport's Windows
  output (UTF-16) as UTF-8, and would have counted messages attached to other
  messages as separate items.
- `--export` wrote RFC 2047 subjects and names undecoded (`=?utf-8?b?...?=`),
  and split senders like `"Ho, Janet" <jho@...>` at the comma.
- `--export` with two inputs of the same name (`a\mail.pst`, `b\mail.pst`,
  `mail.mbox`) wrote them into the same CSV.
- MBOX messages with no `Date:` header were reported as undated although the
  `Received:` header or the `From ` line had a date.
- The summary counted files with no dated messages as errors.
- `recover_pst` crashed when `--workdir` was the source folder; it now refuses.

### Added
- OST support (including the 2013 4 KiB-page format), and signature-based
  format detection for explicitly named files.
- Crash isolation: files are scanned in child processes, so a libpff crash on
  corrupt data (found by fuzzing) fails one file instead of the whole run;
  `recover-pst` keeps its re-hash and report even if libpff dies.
- A clear message for PSTs locked by a running Outlook.
- Date-source accounting: every date is labelled (delivery, submit, creation,
  `Date:`, `Received:`, envelope) and fallbacks are summarised.
- Implausible dates (before 1980 or in the future) are counted separately and
  kept out of the range.
- `--mail-only`, item-class breakdown, `--json`, `--no-progress`, `--version`,
  files as well as folders on the command line, exit codes.
- Richer export: folder path, item class, sender e-mail, Message-ID, recovered
  and implausible flags, undated items; CSV-injection guard.
- `recover-pst`: `recovered_items.csv`, `--pffexport PATH`, tool/libpff
  versions in the manifest, exit status 2 when the source hash changes.
- Known-answer test: a 400-message PST made by pst-testgen ships in
  `tests/fixtures/`, and CI checks every message of it against its manifest;
  `scripts/check_against_manifest.py` does the same for any generated PST.

### Changed
- MBOX is read header-only from a memory map: identical results to
  `mailbox.mbox`, about 30× faster on mailboxes with large attachments; CRLF
  and double-converted CR-CR-LF mailboxes are handled.
- `libpff-python` 20260926 ships wheels for Python 3.10–3.15 on Windows, Linux
  and macOS, so neither MSVC Build Tools nor a specific Python version is needed.

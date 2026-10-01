"""Scan a PST whose right answer is known and compare message by message.

tests/fixtures/known_answer.pst was generated with pst-testgen through Outlook 2024
(400 synthetic messages 2015-2025, 20 soft- and 20 hard-deleted); its manifest
holds the expected values. The data is synthetic (example.com addresses).
"""

import json
from pathlib import Path

import pytest

from email_date_range.manifest_check import check_pst, manifest_path_for

FIXTURE = Path(__file__).parent / "fixtures" / "known_answer.pst"


@pytest.fixture(scope="module")
def checks(tmp_path_factory):
    pytest.importorskip("pypff")
    return check_pst(FIXTURE, tmp_path_factory.mktemp("known_answer"))


def test_fixture_and_manifest_belong_together():
    manifest = json.loads(manifest_path_for(FIXTURE).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["pst_file"] == FIXTURE.name
    assert manifest["total_messages_created"] == 400


def test_every_check_passes(checks):
    failed = [(c.label, c.got, c.want) for c in checks if not c.ok]
    assert failed == []
    assert len(checks) == 12

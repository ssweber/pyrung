"""Click validation for nicknames CLICK cannot hold."""

import pytest
from pyclickplc.validation import NICKNAME_MAX_LENGTH

from pyrung import Bool, Program, Rung, out
from pyrung.click import ClickBlocks, TagMap
from pyrung.click.ladder import LadderExportError, pyrung_to_ladder
from pyrung.click.validation import CLK_NICKNAME_TOO_LONG

LONG = "N" * (NICKNAME_MAX_LENGTH + 1)
LIMIT = "N" * NICKNAME_MAX_LENGTH


def _too_long(report):
    return [f for f in report.errors if f.code == CLK_NICKNAME_TOO_LONG]


def test_mapped_tag_nickname_over_limit():
    blocks = ClickBlocks()
    start = Bool(LONG)
    with Program() as logic:
        with Rung(start):
            out(blocks.y[1])

    mapping = TagMap({start: blocks.x[1]}, include_system=False)
    findings = _too_long(mapping.validate(logic, mode="strict"))

    assert len(findings) == 1
    assert f"{LONG!r} at X001" in findings[0].message
    assert findings[0].location.startswith("main.rung[0]")


def test_nickname_at_limit_passes():
    blocks = ClickBlocks()
    start = Bool(LIMIT)
    with Program() as logic:
        with Rung(start):
            out(blocks.y[1])

    mapping = TagMap({start: blocks.x[1]}, include_system=False)
    assert _too_long(mapping.validate(logic, mode="strict")) == []


def test_bank_slot_nickname_over_limit():
    blocks = ClickBlocks()
    blocks.c.slot(2, name=LONG)
    with Program() as logic:
        with Rung(blocks.c[2]):
            out(blocks.y[1])

    findings = _too_long(TagMap({}, include_system=False).validate(logic, mode="strict"))

    assert len(findings) == 1
    assert f"{LONG!r} at C2" in findings[0].message


def test_unused_mapped_tag_is_located_in_tag_map():
    blocks = ClickBlocks()
    with Program() as logic:
        with Rung(blocks.x[1]):
            out(blocks.y[1])

    mapping = TagMap({Bool(LONG): blocks.c[1]}, include_system=False)
    findings = _too_long(mapping.validate(logic, mode="strict"))

    assert [f.location for f in findings] == ["tag_map"]


def test_warn_mode_reports_a_hint():
    blocks = ClickBlocks()
    start = Bool(LONG)
    with Program() as logic:
        with Rung(start):
            out(blocks.y[1])

    report = TagMap({start: blocks.x[1]}, include_system=False).validate(logic)

    assert [f.code for f in report.hints] == [CLK_NICKNAME_TOO_LONG]


def test_ladder_export_refuses_long_nickname():
    blocks = ClickBlocks()
    start = Bool(LONG)
    with Program() as logic:
        with Rung(start):
            out(blocks.y[1])

    with pytest.raises(LadderExportError, match=CLK_NICKNAME_TOO_LONG):
        pyrung_to_ladder(logic, TagMap({start: blocks.x[1]}, include_system=False))

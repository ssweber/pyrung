"""Tests for TagMap with block-owned hardware tags."""

from __future__ import annotations

import pyclickplc
from pyclickplc.addresses import get_addr_key

from pyrung.click import TagMap, c, ds
from pyrung.core import Block, Bool, TagType

# ---------------------------------------------------------------------------
# Tag-to-Tag mapping: map_to stamps slot identity onto the hardware bank, so
# direct and indirect reads resolve to one tag per hardware register
# ---------------------------------------------------------------------------


def test_tagmap_stamps_slot_identity_on_hardware_block():
    motor = Bool("Motor")
    TagMap({motor: c[7]})
    assert c[7].name == "Motor"


def test_tagmap_resolves_standalone_tag_to_hardware():
    motor = Bool("Motor")
    tm = TagMap({motor: c[7]})
    assert tm.resolve(motor) == "C7"


def test_tagmap_block_range_mapping():
    alarms = Block("Alarm", TagType.INT, 1, 3)
    tm = TagMap({alarms: ds.select(500, 502)})
    assert tm.resolve(alarms, index=1) == "DS500"
    assert tm.resolve(alarms, index=2) == "DS501"
    assert tm.resolve(alarms, index=3) == "DS502"


# ---------------------------------------------------------------------------
# CSV round-trip: slot name overrides written as nicknames
# ---------------------------------------------------------------------------


def test_to_nickname_file_writes_slot_override_nickname(tmp_path):
    addr = 4450
    ds.slot(addr, name="cpHeel2nd")
    try:
        alarms = Block("Alarm", TagType.INT, 1, 3)
        tm = TagMap({alarms: ds.select(addr - 1, addr + 1)})

        path = tmp_path / "overrides.csv"
        tm.to_nickname_file(path)

        rows = pyclickplc.read_csv(path)
        assert rows[get_addr_key("DS", addr)].nickname == "cpHeel2nd"
    finally:
        ds._slot_name_overrides.pop(addr, None)
        ds._tag_cache.pop(addr, None)
        ds._tag_cache.pop(addr - 1, None)
        ds._tag_cache.pop(addr + 1, None)

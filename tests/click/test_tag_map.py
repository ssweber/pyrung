"""Tests for Click TagMap logical-to-hardware mapping."""

from __future__ import annotations

import pyclickplc
import pytest
from pyclickplc.addresses import AddressRecord, get_addr_key
from pyclickplc.banks import DataType

from pyrung.click import TagMap, c, ds, x
from pyrung.click.tag_map import UNSET
from pyrung.core import Block, Bool, Tag, TagType


def test_resolve_standalone_tag():
    valve = Bool("Valve")
    mapping = TagMap({valve: c[1]})

    assert mapping.resolve(valve) == "C1"
    assert mapping.resolve("Valve") == "C1"


def test_resolve_block_slot():
    alarms = Block("Alarm", TagType.BOOL, 1, 3)
    mapping = TagMap({alarms: c.select(101, 103)})

    assert mapping.resolve(alarms, 1) == "C101"
    assert mapping.resolve(alarms, 3) == "C103"


def test_resolve_block_slot_sparse_bank():
    alarms = Block("Alarm", TagType.BOOL, 1, 17)
    mapping = TagMap({alarms: x.select(1, 21)})

    assert mapping.resolve(alarms, 17) == "X021"


def test_offset_for_block():
    alarms = Block("Alarm", TagType.BOOL, 1, 3)
    mapping = TagMap({alarms: c.select(101, 103)})

    assert mapping.offset_for(alarms) == 100


def test_offset_for_sparse_block_raises():
    alarms = Block("Alarm", TagType.BOOL, 1, 17)
    mapping = TagMap({alarms: x.select(1, 21)})

    with pytest.raises(ValueError, match="affine"):
        mapping.offset_for(alarms)


def test_map_to_syntax():
    valve = Bool("Valve")
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap([valve.map_to(c[1]), alarms.map_to(c.select(101, 102))])

    assert mapping.resolve("Valve") == "C1"
    assert mapping.resolve(alarms, 2) == "C102"


def test_dict_constructor():
    valve = Bool("Valve")
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({valve: c[1], alarms: c.select(101, 102)})

    assert mapping.resolve("Valve") == "C1"
    assert mapping.resolve(alarms, 1) == "C101"


def test_empty_map():
    mapping = TagMap(include_system=False)

    assert len(mapping) == 0
    assert mapping.tags() == ()
    assert mapping.blocks() == ()
    assert mapping.entries == ()
    assert mapping.mapped_slots() == ()


def test_contains_tag_name_and_object():
    valve = Bool("Valve")
    mapping = TagMap({valve: c[1]})

    assert "Valve" in mapping
    assert valve in mapping
    assert Bool("Valve") in mapping


def test_mapped_slots_include_standalone_and_block_slots():
    valve = Bool("Valve")
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({valve: c[1], alarms: x.select(1, 2)}, include_system=False)

    slots = mapping.mapped_slots()
    assert len(slots) == 3

    assert slots[0].hardware_address == "C1"
    assert slots[0].memory_type == "C"
    assert slots[0].address == 1
    assert slots[0].logical_name == "Valve"
    assert slots[0].default is False

    assert slots[1].hardware_address == "X001"
    assert slots[1].memory_type == "X"
    assert slots[1].address == 1
    assert slots[1].logical_name == "Alarm1"

    assert slots[2].hardware_address == "X002"
    assert slots[2].memory_type == "X"
    assert slots[2].address == 2
    assert slots[2].logical_name == "Alarm2"


def test_mapped_slots_use_override_default():
    value = Tag("Value", TagType.INT, default=5)
    mapping = TagMap({value: ds[1]}, include_system=False)
    mapping.override(value, default=7)

    slots = mapping.mapped_slots()
    assert len(slots) == 1
    assert slots[0].default == 7


def test_contains_block():
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({alarms: c.select(101, 102)})

    assert alarms in mapping


def test_len_counts_entries_not_slots():
    valve = Bool("Valve")
    alarms = Block("Alarm", TagType.BOOL, 1, 20)
    mapping = TagMap({valve: c[1], alarms: c.select(101, 120)})

    assert len(mapping) == 2


def test_type_mismatch_tag_raises():
    value = Tag("Value", TagType.INT)

    with pytest.raises(ValueError, match="Type mismatch"):
        TagMap({value: c[1]})


def test_type_mismatch_block_raises():
    values = Block("Values", TagType.INT, 1, 2)

    with pytest.raises(ValueError, match="Type mismatch"):
        TagMap({values: c.select(101, 102)})


def test_block_size_mismatch_raises():
    alarms = Block("Alarm", TagType.BOOL, 1, 20)

    with pytest.raises(ValueError, match="Block size mismatch"):
        TagMap({alarms: x.select(1, 21)})


def test_address_conflict_tag_tag_raises():
    valve = Bool("Valve")
    pump = Bool("Pump")

    with pytest.raises(ValueError, match="Hardware address conflict"):
        TagMap({valve: c[1], pump: c[1]})


def test_address_conflict_tag_block_raises():
    valve = Bool("Valve")
    alarms = Block("Alarm", TagType.BOOL, 1, 3)

    with pytest.raises(ValueError, match="Hardware address conflict"):
        TagMap({valve: c[101], alarms: c.select(100, 102)})


def test_address_conflict_block_block_raises():
    alarm_a = Block("AlarmA", TagType.BOOL, 1, 3)
    alarm_b = Block("AlarmB", TagType.BOOL, 1, 3)

    with pytest.raises(ValueError, match="Hardware address conflict"):
        TagMap({alarm_a: c.select(100, 102), alarm_b: c.select(102, 104)})


def test_name_conflict_standalone_tags_raises():
    first = Bool("Valve")
    second = Bool("Valve")

    with pytest.raises(ValueError, match="Duplicate standalone logical tag name"):
        TagMap([first.map_to(c[1]), second.map_to(c[2])])


def test_override_block_slot_name():
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({alarms: c.select(101, 102)})
    slot = alarms[1]
    mapping.override(slot, name="Alarm_1")

    override = mapping.get_override(slot)
    assert override is not None
    assert override.name == "Alarm_1"


def test_override_block_slot_default():
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({alarms: c.select(101, 102)})
    slot = alarms[1]
    mapping.override(slot, default=1)

    override = mapping.get_override(slot)
    assert override is not None
    assert override.default == 1


def test_override_block_slot_retentive():
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({alarms: c.select(101, 102)})
    slot = alarms[1]
    mapping.override(slot, retentive=True)

    override = mapping.get_override(slot)
    assert override is not None
    assert override.retentive is True


def test_override_requires_mapped_slot():
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({alarms: c.select(101, 102)})

    with pytest.raises(KeyError):
        mapping.override(Bool("NotMapped"), name="X")


def test_override_standalone_by_name_key():
    valve = Bool("Valve")
    mapping = TagMap({valve: c[1]})
    mapping.override(Bool("Valve"), name="ValveAlias")

    override = mapping.get_override(valve)
    assert override is not None
    assert override.name == "ValveAlias"


def test_clear_override():
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({alarms: c.select(101, 102)})
    slot = alarms[1]
    mapping.override(slot, name="Alarm_1")
    mapping.clear_override(slot)

    assert mapping.get_override(slot) is None


def test_nickname_validation_warnings_standalone():
    bad = Bool("Bad-Name")
    mapping = TagMap({bad: c[1]})

    assert mapping.warnings
    assert "invalid" in mapping.warnings[0]


def test_nickname_validation_warnings_block_slots():
    bad_block = Block("Bad-", TagType.BOOL, 1, 2)
    mapping = TagMap({bad_block: c.select(101, 102)})

    assert mapping.warnings


def test_nickname_validation_warns_leading_underscore():
    mapping = TagMap({Bool("_Bad"): c[1]})

    assert mapping.warnings
    assert "Cannot start with _" in mapping.warnings[0]


def test_effective_nickname_collision_raises():
    valve = Bool("Valve")
    pump = Bool("Pump")
    mapping = TagMap({valve: c[1], pump: c[2]})

    with pytest.raises(ValueError, match="collision"):
        mapping.override(pump, name="Valve")

    assert mapping.get_override(pump) is None


def test_to_nickname_file_sparse_only_mapped_rows(tmp_path):
    valve = Bool("Valve")
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({valve: c[1], alarms: c.select(101, 102)})

    path = tmp_path / "mapped.csv"
    count = mapping.to_nickname_file(path)
    rows = pyclickplc.read_csv(path)

    assert count == 3
    assert len(rows) == 3
    assert get_addr_key("C", 1) in rows
    assert get_addr_key("C", 101) in rows
    assert get_addr_key("C", 102) in rows


def test_to_nickname_file_uses_override_metadata(tmp_path):
    alarms = Block("Alarm", TagType.BOOL, 1, 1)
    mapping = TagMap({alarms: c.select(101, 101)})
    slot = alarms[1]
    mapping.override(slot, name="Alarm_1", retentive=True, default=1)

    path = tmp_path / "override.csv"
    mapping.to_nickname_file(path)
    rows = pyclickplc.read_csv(path)
    record = rows[get_addr_key("C", 101)]

    assert record.nickname == "Alarm_1"
    assert record.retentive is True
    assert record.initial_value == "1"


def test_from_nickname_file_round_trip(tmp_path):
    valve = Bool("Valve")
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({valve: c[1], alarms: c.select(101, 102)})
    mapping.override(alarms[1], name="Alarm_1", default=1)

    path = tmp_path / "round_trip.csv"
    mapping.to_nickname_file(path)
    restored = TagMap.from_nickname_file(path)

    assert restored.resolve("Valve") == "C1"
    restored_block = next(block_entry.logical for block_entry in restored.blocks())
    assert restored.resolve(restored_block, 1) == "C101"
    override = restored.get_override(restored_block[1])
    assert override is not None
    assert override.name == "Alarm_1"
    assert override.default == 1


def test_from_nickname_file_sparse_block_rows_preserve_full_span(tmp_path):
    path = tmp_path / "sparse.csv"
    records = {
        get_addr_key("C", 101): AddressRecord(
            memory_type="C",
            address=101,
            nickname="",
            comment="<Alarm>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
        get_addr_key("C", 200): AddressRecord(
            memory_type="C",
            address=200,
            nickname="",
            comment="</Alarm>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
    }
    pyclickplc.write_csv(path, records)

    restored = TagMap.from_nickname_file(path)
    assert len(restored.blocks()) == 1

    block = restored.blocks()[0].logical
    assert block.start == 1
    assert block.end == 100
    assert restored.resolve(block, 1) == "C101"
    assert restored.resolve(block, 100) == "C200"


def test_override_clear_returns_to_unset_default():
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({alarms: c.select(101, 102)})
    slot = alarms[1]
    mapping.override(slot, default=1)
    mapping.clear_override(slot)

    override = mapping.get_override(slot)
    assert override is None
    mapping.override(slot, default=UNSET)
    override = mapping.get_override(slot)
    assert override is not None
    assert override.default is UNSET

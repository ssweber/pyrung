"""Tests for Click TagMap logical-to-hardware mapping."""

from __future__ import annotations

from typing import Any, cast

import pyclickplc
import pytest
from pyclickplc.addresses import AddressRecord, get_addr_key
from pyclickplc.banks import DataType

from pyrung.click import TagMap, c, ds, x
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


def test_mapped_slots_use_canonical_default_metadata():
    value = Tag("Value", TagType.INT, default=5)
    mapping = TagMap({value: ds[1]}, include_system=False)

    slots = mapping.mapped_slots()
    assert len(slots) == 1
    assert slots[0].default == 5


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

    with pytest.raises(ValueError, match="Duplicate user logical tag name"):
        TagMap([first.map_to(c[1]), second.map_to(c[2])])


def test_name_conflict_standalone_and_block_slot_raises():
    valve = Bool("Alarm1")
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    with pytest.raises(ValueError, match="Duplicate user logical tag name"):
        TagMap({valve: c[1], alarms: c.select(101, 102)})


def test_name_conflict_across_blocks_raises():
    alarm_a = Block("AlarmA", TagType.BOOL, 1, 1)
    alarm_b = Block("AlarmB", TagType.BOOL, 1, 1)
    alarm_a.rename_slot(1, "Shared")
    alarm_b.rename_slot(1, "Shared")

    with pytest.raises(ValueError, match="Duplicate user logical tag name"):
        TagMap({alarm_a: c.select(101, 101), alarm_b: c.select(201, 201)})


def test_block_slot_name_rename_exported_to_csv(tmp_path):
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    alarms.rename_slot(1, "Alarm_1")
    mapping = TagMap({alarms: c.select(101, 102)})
    path = tmp_path / "renamed.csv"
    mapping.to_nickname_file(path)
    rows = pyclickplc.read_csv(path)
    assert rows[get_addr_key("C", 101)].nickname == "Alarm_1"


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


def test_to_nickname_file_uses_first_class_slot_name_and_runtime_policy(tmp_path):
    alarms = Block("Alarm", TagType.BOOL, 1, 1, retentive=False)
    alarms.rename_slot(1, "Alarm_1")
    alarms.configure_slot(1, retentive=True, default=True)
    mapping = TagMap({alarms: c.select(101, 101)})

    path = tmp_path / "slot_metadata.csv"
    mapping.to_nickname_file(path)
    rows = pyclickplc.read_csv(path)
    record = rows[get_addr_key("C", 101)]

    assert record.nickname == "Alarm_1"
    assert record.retentive is True
    assert record.initial_value == "1"


def test_to_nickname_file_uses_first_class_slot_runtime_policy(tmp_path):
    alarms = Block("AlarmCfg", TagType.BOOL, 1, 1, retentive=False)
    alarms.configure_slot(1, retentive=True, default=True)
    mapping = TagMap({alarms: c.select(301, 301)})

    path = tmp_path / "slot_runtime.csv"
    mapping.to_nickname_file(path)
    rows = pyclickplc.read_csv(path)
    record = rows[get_addr_key("C", 301)]

    assert record.retentive is True
    assert record.initial_value == "1"


def test_from_nickname_file_round_trip(tmp_path):
    valve = Bool("Valve")
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    alarms.rename_slot(1, "Alarm_1")
    alarms.configure_slot(1, default=True)
    mapping = TagMap({valve: c[1], alarms: c.select(101, 102)})

    path = tmp_path / "round_trip.csv"
    mapping.to_nickname_file(path)
    restored = TagMap.from_nickname_file(path)

    assert restored.resolve("Valve") == "C1"
    restored_block = next(block_entry.logical for block_entry in restored.blocks())
    assert restored.resolve(restored_block, 1) == "C101"
    assert restored_block[1].name == "Alarm_1"
    assert restored_block[1].default is True


def test_from_nickname_file_hydrates_block_slot_runtime_policy(tmp_path):
    path = tmp_path / "slot_runtime_import.csv"
    records = {
        get_addr_key("C", 401): AddressRecord(
            memory_type="C",
            address=401,
            nickname="AlarmCfg1",
            comment="<AlarmCfg>",
            initial_value="1",
            retentive=True,
            data_type=DataType.BIT,
        ),
        get_addr_key("C", 402): AddressRecord(
            memory_type="C",
            address=402,
            nickname="AlarmCfg2",
            comment="</AlarmCfg>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
    }
    pyclickplc.write_csv(path, records)

    restored = TagMap.from_nickname_file(path)
    block = restored.blocks()[0].logical
    assert block[1].retentive is True
    assert block[1].default is True
    assert block[2].retentive is False
    assert block[2].default is False


def test_from_nickname_file_sparse_block_rows_preserve_full_span(tmp_path):
    path = tmp_path / "sparse.csv"
    records = {
        get_addr_key("C", 101): AddressRecord(
            memory_type="C",
            address=101,
            nickname="Alarm1",
            comment="<Alarm>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
        get_addr_key("C", 200): AddressRecord(
            memory_type="C",
            address=200,
            nickname="Alarm100",
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


def test_from_nickname_file_rejects_blank_block_nickname(tmp_path):
    path = tmp_path / "blank_block_name.csv"
    records = {
        get_addr_key("C", 401): AddressRecord(
            memory_type="C",
            address=401,
            nickname="",
            comment="<AlarmCfg>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
        get_addr_key("C", 402): AddressRecord(
            memory_type="C",
            address=402,
            nickname="AlarmCfg2",
            comment="</AlarmCfg>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="C401"):
        TagMap.from_nickname_file(path)


def test_from_nickname_file_rejects_invalid_block_nickname(tmp_path):
    path = tmp_path / "invalid_block_name.csv"
    records = {
        get_addr_key("C", 501): AddressRecord(
            memory_type="C",
            address=501,
            nickname="Bad-Name",
            comment="<AlarmCfg>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
        get_addr_key("C", 502): AddressRecord(
            memory_type="C",
            address=502,
            nickname="AlarmCfg2",
            comment="</AlarmCfg>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="C501"):
        TagMap.from_nickname_file(path)


def test_from_nickname_file_rejects_duplicate_block_nickname(tmp_path):
    path = tmp_path / "duplicate_block_name.csv"
    records = {
        get_addr_key("C", 601): AddressRecord(
            memory_type="C",
            address=601,
            nickname="AlarmCfg",
            comment="<AlarmCfg>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
        get_addr_key("C", 602): AddressRecord(
            memory_type="C",
            address=602,
            nickname="AlarmCfg",
            comment="</AlarmCfg>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="C602"):
        TagMap.from_nickname_file(path)


def test_block_entry_by_name_found():
    alarms = Block("Alarm", TagType.BOOL, 1, 3)
    mapping = TagMap({alarms: c.select(101, 103)})

    entry = mapping.block_entry_by_name("Alarm")
    assert entry is not None
    assert entry.logical is alarms


def test_block_entry_by_name_not_found():
    mapping = TagMap(include_system=False)

    assert mapping.block_entry_by_name("NoSuchBlock") is None


def test_from_nickname_file_udt_grouping_success(tmp_path):
    path = tmp_path / "udt_success.csv"
    records = {
        get_addr_key("DS", 1001): AddressRecord(
            memory_type="DS",
            address=1001,
            nickname="Alarm1_id",
            comment="<Alarm.id>",
            initial_value="1",
            retentive=True,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 1002): AddressRecord(
            memory_type="DS",
            address=1002,
            nickname="Alarm2_id",
            comment="</Alarm.id>",
            initial_value="2",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("C", 101): AddressRecord(
            memory_type="C",
            address=101,
            nickname="Alarm1_On",
            comment="<Alarm.On>",
            initial_value="1",
            retentive=True,
            data_type=DataType.BIT,
        ),
        get_addr_key("C", 102): AddressRecord(
            memory_type="C",
            address=102,
            nickname="Alarm2_On",
            comment="</Alarm.On>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
    }
    pyclickplc.write_csv(path, records)

    restored = TagMap.from_nickname_file(path)
    assert len(restored.structures) == 1
    struct = restored.structures[0]
    assert struct.kind == "udt"
    assert struct.name == "Alarm"
    assert struct.count == 2
    assert struct.stride is None
    assert restored.structure_by_name("Alarm") == struct
    assert restored.structure_warnings == ()

    runtime = cast(Any, struct.runtime)
    assert restored.resolve(runtime[2].id) == "DS1002"
    assert restored.resolve(runtime[1].On) == "C101"


def test_from_nickname_file_udt_grouping_count_mismatch_falls_back(tmp_path):
    path = tmp_path / "udt_fallback.csv"
    records = {
        get_addr_key("DS", 1001): AddressRecord(
            memory_type="DS",
            address=1001,
            nickname="Alarm1_id",
            comment="<Alarm.id>",
            initial_value="1",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 1002): AddressRecord(
            memory_type="DS",
            address=1002,
            nickname="Alarm2_id",
            comment="</Alarm.id>",
            initial_value="2",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("C", 101): AddressRecord(
            memory_type="C",
            address=101,
            nickname="Alarm1_On",
            comment="<Alarm.On />",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
    }
    pyclickplc.write_csv(path, records)

    restored = TagMap.from_nickname_file(path)
    assert restored.structures == ()
    assert restored.structure_by_name("Alarm") is None
    assert restored.structure_warnings
    assert "Alarm" in restored.structure_warnings[0]
    assert restored.block_entry_by_name("Alarm.id") is not None
    assert restored.block_entry_by_name("Alarm.On") is not None


def test_from_nickname_file_udt_grouping_count_mismatch_fails_in_strict_mode(tmp_path):
    path = tmp_path / "udt_fallback_fail.csv"
    records = {
        get_addr_key("DS", 1001): AddressRecord(
            memory_type="DS",
            address=1001,
            nickname="Alarm1_id",
            comment="<Alarm.id>",
            initial_value="1",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 1002): AddressRecord(
            memory_type="DS",
            address=1002,
            nickname="Alarm2_id",
            comment="</Alarm.id>",
            initial_value="2",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("C", 101): AddressRecord(
            memory_type="C",
            address=101,
            nickname="Alarm1_On",
            comment="<Alarm.On />",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="UDT grouping failed for base 'Alarm'"):
        TagMap.from_nickname_file(path, mode="strict")


def test_from_nickname_file_udt_grouping_success_in_strict_mode(tmp_path):
    path = tmp_path / "udt_success_strict_mode.csv"
    records = {
        get_addr_key("DS", 1001): AddressRecord(
            memory_type="DS",
            address=1001,
            nickname="Alarm1_id",
            comment="<Alarm.id>",
            initial_value="1",
            retentive=True,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 1002): AddressRecord(
            memory_type="DS",
            address=1002,
            nickname="Alarm2_id",
            comment="</Alarm.id>",
            initial_value="2",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("C", 101): AddressRecord(
            memory_type="C",
            address=101,
            nickname="Alarm1_On",
            comment="<Alarm.On>",
            initial_value="1",
            retentive=True,
            data_type=DataType.BIT,
        ),
        get_addr_key("C", 102): AddressRecord(
            memory_type="C",
            address=102,
            nickname="Alarm2_On",
            comment="</Alarm.On>",
            initial_value="0",
            retentive=False,
            data_type=DataType.BIT,
        ),
    }
    pyclickplc.write_csv(path, records)

    restored = TagMap.from_nickname_file(path, mode="strict")
    assert len(restored.structures) == 1
    assert restored.structures[0].kind == "udt"
    assert restored.structure_warnings == ()


def test_from_nickname_file_named_array_success(tmp_path):
    path = tmp_path / "named_array_success.csv"
    records = {
        get_addr_key("DS", 501): AddressRecord(
            memory_type="DS",
            address=501,
            nickname="AlarmPacked1_id",
            comment="<AlarmPacked:named_array(2,3)>",
            initial_value="1",
            retentive=True,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 502): AddressRecord(
            memory_type="DS",
            address=502,
            nickname="AlarmPacked1_val",
            comment="",
            initial_value="10",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 503): AddressRecord(
            memory_type="DS",
            address=503,
            nickname="",
            comment="",
            initial_value="",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 504): AddressRecord(
            memory_type="DS",
            address=504,
            nickname="AlarmPacked2_id",
            comment="",
            initial_value="2",
            retentive=True,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 505): AddressRecord(
            memory_type="DS",
            address=505,
            nickname="AlarmPacked2_val",
            comment="",
            initial_value="20",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 506): AddressRecord(
            memory_type="DS",
            address=506,
            nickname="",
            comment="</AlarmPacked:named_array(2,3)>",
            initial_value="",
            retentive=False,
            data_type=DataType.INT,
        ),
    }
    pyclickplc.write_csv(path, records)

    restored = TagMap.from_nickname_file(path)
    assert len(restored.structures) == 1
    struct = restored.structures[0]
    assert struct.kind == "named_array"
    assert struct.name == "AlarmPacked"
    assert struct.count == 2
    assert struct.stride == 3
    assert restored.structure_warnings == ()

    runtime = cast(Any, struct.runtime)
    assert restored.resolve(runtime[1].id) == "DS501"
    assert restored.resolve(runtime[2].id) == "DS504"
    assert restored.resolve(runtime[2].val) == "DS505"


def test_from_nickname_file_named_array_missing_required_rows_raises(tmp_path):
    path = tmp_path / "named_array_missing.csv"
    records = {
        get_addr_key("DS", 501): AddressRecord(
            memory_type="DS",
            address=501,
            nickname="AlarmPacked1_id",
            comment="<AlarmPacked:named_array(2,3)>",
            initial_value="1",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 502): AddressRecord(
            memory_type="DS",
            address=502,
            nickname="AlarmPacked1_val",
            comment="",
            initial_value="10",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 504): AddressRecord(
            memory_type="DS",
            address=504,
            nickname="AlarmPacked2_id",
            comment="",
            initial_value="2",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 506): AddressRecord(
            memory_type="DS",
            address=506,
            nickname="",
            comment="</AlarmPacked:named_array(2,3)>",
            initial_value="",
            retentive=False,
            data_type=DataType.INT,
        ),
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="missing required row"):
        TagMap.from_nickname_file(path)


@pytest.mark.parametrize("mode", ["warn", "strict"])
def test_from_nickname_file_named_array_missing_required_rows_raises_in_both_modes(
    tmp_path, mode: str
):
    path = tmp_path / f"named_array_missing_{mode}.csv"
    records = {
        get_addr_key("DS", 501): AddressRecord(
            memory_type="DS",
            address=501,
            nickname="AlarmPacked1_id",
            comment="<AlarmPacked:named_array(2,3)>",
            initial_value="1",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 502): AddressRecord(
            memory_type="DS",
            address=502,
            nickname="AlarmPacked1_val",
            comment="",
            initial_value="10",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 504): AddressRecord(
            memory_type="DS",
            address=504,
            nickname="AlarmPacked2_id",
            comment="",
            initial_value="2",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 506): AddressRecord(
            memory_type="DS",
            address=506,
            nickname="",
            comment="</AlarmPacked:named_array(2,3)>",
            initial_value="",
            retentive=False,
            data_type=DataType.INT,
        ),
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="missing required row"):
        TagMap.from_nickname_file(path, mode=cast(Any, mode))


def test_from_nickname_file_named_array_bad_nickname_pattern_raises(tmp_path):
    path = tmp_path / "named_array_bad_pattern.csv"
    records = {
        get_addr_key("DS", 501): AddressRecord(
            memory_type="DS",
            address=501,
            nickname="AlarmPacked1-id",
            comment="<AlarmPacked:named_array(1,1) />",
            initial_value="1",
            retentive=False,
            data_type=DataType.INT,
        )
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="invalid nickname"):
        TagMap.from_nickname_file(path)


def test_from_nickname_file_named_array_inconsistent_offset_raises(tmp_path):
    path = tmp_path / "named_array_bad_offset.csv"
    records = {
        get_addr_key("DS", 501): AddressRecord(
            memory_type="DS",
            address=501,
            nickname="AlarmPacked1_id",
            comment="<AlarmPacked:named_array(2,3)>",
            initial_value="1",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 502): AddressRecord(
            memory_type="DS",
            address=502,
            nickname="AlarmPacked1_val",
            comment="",
            initial_value="10",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 504): AddressRecord(
            memory_type="DS",
            address=504,
            nickname="AlarmPacked2_val",
            comment="",
            initial_value="20",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 505): AddressRecord(
            memory_type="DS",
            address=505,
            nickname="AlarmPacked2_id",
            comment="",
            initial_value="2",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 506): AddressRecord(
            memory_type="DS",
            address=506,
            nickname="",
            comment="</AlarmPacked:named_array(2,3)>",
            initial_value="",
            retentive=False,
            data_type=DataType.INT,
        ),
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="appears at offset"):
        TagMap.from_nickname_file(path)


def test_from_nickname_file_structured_identifier_policy_rejects_invalid_tokens(tmp_path):
    path = tmp_path / "invalid_structured_name.csv"
    records = {
        get_addr_key("DS", 701): AddressRecord(
            memory_type="DS",
            address=701,
            nickname="Bad1",
            comment="<Alarm.bad-name />",
            initial_value="0",
            retentive=False,
            data_type=DataType.INT,
        )
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="Invalid UDT block tag"):
        TagMap.from_nickname_file(path)


def test_from_nickname_file_rejects_duplicate_block_definition_names(tmp_path):
    path = tmp_path / "duplicate_block_defs.csv"
    records = {
        get_addr_key("DS", 801): AddressRecord(
            memory_type="DS",
            address=801,
            nickname="A1",
            comment="<Alarm.id />",
            initial_value="0",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 802): AddressRecord(
            memory_type="DS",
            address=802,
            nickname="A2",
            comment="<Alarm.id />",
            initial_value="0",
            retentive=False,
            data_type=DataType.INT,
        ),
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="Duplicate block definition name"):
        TagMap.from_nickname_file(path)


def test_from_nickname_file_plain_block_auto_starts_at_zero_for_zero_address(tmp_path):
    path = tmp_path / "plain_block_start_zero.csv"
    records = {
        get_addr_key("XD", 0): AddressRecord(
            memory_type="XD",
            address=0,
            nickname="Word0",
            comment="<WordBank>",
            initial_value="0",
            retentive=False,
            data_type=DataType.HEX,
        ),
        get_addr_key("XD", 2): AddressRecord(
            memory_type="XD",
            address=2,
            nickname="Word2",
            comment="</WordBank>",
            initial_value="0",
            retentive=False,
            data_type=DataType.HEX,
        ),
    }
    pyclickplc.write_csv(path, records)

    restored = TagMap.from_nickname_file(path)
    block = restored.blocks()[0].logical
    assert block.name == "WordBank"
    assert block.start == 0
    assert block.end == 2
    assert restored.resolve(block, 0) == "XD0"
    assert restored.resolve(block, 2) == "XD1"


def test_from_nickname_file_plain_block_explicit_start_override(tmp_path):
    path = tmp_path / "plain_block_explicit_start.csv"
    records = {
        get_addr_key("XD", 0): AddressRecord(
            memory_type="XD",
            address=0,
            nickname="Word5",
            comment="<WordBank:block(5)>",
            initial_value="0",
            retentive=False,
            data_type=DataType.HEX,
        ),
        get_addr_key("XD", 2): AddressRecord(
            memory_type="XD",
            address=2,
            nickname="Word7",
            comment="</WordBank:block(5)>",
            initial_value="0",
            retentive=False,
            data_type=DataType.HEX,
        ),
    }
    pyclickplc.write_csv(path, records)

    restored = TagMap.from_nickname_file(path)
    block = restored.blocks()[0].logical
    assert block.name == "WordBank"
    assert block.start == 5
    assert block.end == 7
    assert restored.resolve(block, 5) == "XD0"
    assert restored.resolve(block, 7) == "XD1"


def test_from_nickname_file_plain_block_invalid_explicit_start_tag_raises(tmp_path):
    path = tmp_path / "plain_block_bad_start_tag.csv"
    records = {
        get_addr_key("XD", 0): AddressRecord(
            memory_type="XD",
            address=0,
            nickname="Word0",
            comment="<WordBank:block(start=-1) />",
            initial_value="0",
            retentive=False,
            data_type=DataType.HEX,
        )
    }
    pyclickplc.write_csv(path, records)

    with pytest.raises(ValueError, match="Invalid block start tag"):
        TagMap.from_nickname_file(path)


def test_from_nickname_file_invalid_mode_raises_immediately(tmp_path):
    missing_path = tmp_path / "does_not_exist.csv"
    with pytest.raises(ValueError, match="Invalid mode"):
        TagMap.from_nickname_file(missing_path, mode=cast(Any, "nope"))


def test_to_nickname_file_exports_named_array_markers_from_structured_metadata(tmp_path):
    source = tmp_path / "source_named_array.csv"
    records = {
        get_addr_key("DS", 901): AddressRecord(
            memory_type="DS",
            address=901,
            nickname="AlarmPacked1_id",
            comment="<AlarmPacked:named_array(2,3)>",
            initial_value="1",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 902): AddressRecord(
            memory_type="DS",
            address=902,
            nickname="AlarmPacked1_val",
            comment="",
            initial_value="10",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 904): AddressRecord(
            memory_type="DS",
            address=904,
            nickname="AlarmPacked2_id",
            comment="",
            initial_value="2",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 905): AddressRecord(
            memory_type="DS",
            address=905,
            nickname="AlarmPacked2_val",
            comment="",
            initial_value="20",
            retentive=False,
            data_type=DataType.INT,
        ),
        get_addr_key("DS", 906): AddressRecord(
            memory_type="DS",
            address=906,
            nickname="",
            comment="</AlarmPacked:named_array(2,3)>",
            initial_value="",
            retentive=False,
            data_type=DataType.INT,
        ),
    }
    pyclickplc.write_csv(source, records)

    restored = TagMap.from_nickname_file(source)
    exported = tmp_path / "exported_named_array.csv"
    restored.to_nickname_file(exported)
    exported_rows = pyclickplc.read_csv(exported)

    assert exported_rows[get_addr_key("DS", 901)].comment == "<AlarmPacked:named_array(2,3)>"
    # End marker is synthesized because DS906 is a gap for this named-array layout.
    assert exported_rows[get_addr_key("DS", 906)].comment == "</AlarmPacked:named_array(2,3)>"
    assert exported_rows[get_addr_key("DS", 906)].nickname == ""

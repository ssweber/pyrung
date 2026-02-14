"""Click TagMap integration tests for @udt and @named_array."""

from __future__ import annotations

from typing import Any, cast

import pyclickplc
import pytest
from pyclickplc.addresses import get_addr_key

from pyrung.click import TagMap, c, ds
from pyrung.core import Bool, Field, Int, auto, named_array, udt


def test_udt_resolve_supports_block_and_instance_access():
    @udt(count=3)
    class Alarm:
        id: Int = auto()  # type: ignore[invalid-assignment]
        On: Bool

    alarms = cast(Any, Alarm)
    mapping = TagMap({alarms.id: ds.select(1001, 1003), alarms.On: c.select(1, 3)})

    assert mapping.resolve(alarms[2].id) == "DS1002"
    assert mapping.resolve(alarms.id, 2) == "DS1002"
    assert mapping.resolve(alarms[3].On) == "C3"


def test_named_array_width_gt_one_resolves_instance_slots_only():
    @named_array(Int, count=2, stride=3)
    class Alarm:
        id = auto()
        val = 0

    alarms = cast(Any, Alarm)
    mapping = TagMap([*alarms.map_to(ds.select(2001, 2006))])

    assert mapping.resolve(alarms[2].id) == "DS2004"

    with pytest.raises(KeyError, match="No mapping for block"):
        mapping.resolve(alarms.id, 2)


def test_udt_and_named_array_csv_export_include_expected_slot_metadata(tmp_path):
    @udt(count=2)
    class Alarm:
        id: Int = Field(default=auto(), retentive=True)  # type: ignore[invalid-assignment]
        val: Int = 0  # type: ignore[invalid-assignment]

    alarms = cast(Any, Alarm)

    @named_array(Int, count=2, stride=3)
    class AlarmPacked:
        id = Field(default=auto(), retentive=True)
        val = 0

    alarm_ints = cast(Any, AlarmPacked)
    mapping = TagMap(
        [
            alarms.id.map_to(ds.select(3001, 3002)),
            alarms.val.map_to(ds.select(3003, 3004)),
            *alarm_ints.map_to(ds.select(4001, 4006)),
        ]
    )

    path = tmp_path / "structs.csv"
    mapping.to_nickname_file(path)
    rows = pyclickplc.read_csv(path)

    assert rows[get_addr_key("DS", 3001)].nickname == "Alarm1_id"
    assert rows[get_addr_key("DS", 3001)].retentive is True
    assert rows[get_addr_key("DS", 3001)].initial_value == "1"
    assert rows[get_addr_key("DS", 3002)].initial_value == "2"
    assert rows[get_addr_key("DS", 3003)].nickname == "Alarm1_val"
    assert rows[get_addr_key("DS", 3003)].retentive is False
    assert rows[get_addr_key("DS", 3001)].comment == "<Alarm.id>"
    assert rows[get_addr_key("DS", 3002)].comment == "</Alarm.id>"
    assert rows[get_addr_key("DS", 4001)].nickname == "AlarmPacked1_id"
    assert rows[get_addr_key("DS", 4001)].retentive is True
    assert rows[get_addr_key("DS", 4001)].comment == ""

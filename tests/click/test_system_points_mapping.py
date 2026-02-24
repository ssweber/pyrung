"""Tests for Click system-point auto mapping and provider behavior."""

from __future__ import annotations

from datetime import datetime

import pytest

from pyrung.click import ClickDataProvider, TagMap, sc
from pyrung.core import Bool, PLCRunner, system


class _FrozenDateTime(datetime):
    fixed_now = datetime(2026, 1, 15, 10, 20, 30)

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return cls.fixed_now


def test_tag_map_includes_system_points_by_default():
    mapping = TagMap()

    assert mapping.resolve("sys.first_scan") == "SC2"
    assert mapping.resolve("sys.battery_present") == "SC203"
    assert mapping.resolve("fault.code") == "SD1"
    assert mapping.resolve("rtc.year4") == "SD19"


def test_tag_map_can_disable_system_points():
    mapping = TagMap(include_system=False)

    with pytest.raises(KeyError):
        mapping.resolve("sys.first_scan")


def test_system_mapped_slots_include_read_only_and_source_metadata():
    mapping = TagMap()
    system_slots = {
        slot.logical_name: slot for slot in mapping.mapped_slots() if slot.source == "system"
    }

    assert system_slots["sys.first_scan"].read_only is True
    assert system_slots["sys.battery_present"].read_only is True
    assert system_slots["rtc.new_year4"].read_only is False
    assert system_slots["rtc.apply_date"].read_only is False


def test_same_address_alias_is_allowed_but_conflicting_name_is_rejected():
    alias = Bool("_1st_SCAN")
    mapping = TagMap({alias: sc[2]})
    assert mapping.resolve("_1st_SCAN") == "SC2"

    with pytest.raises(ValueError, match="Hardware address conflict"):
        TagMap({Bool("DifferentName"): sc[2]})


def test_reserved_system_logical_names_are_rejected():
    with pytest.raises(ValueError, match="reserved for system points"):
        TagMap({Bool("sys.first_scan"): sc[200]})


def test_provider_reads_system_slots_from_runtime_and_blocks_read_only_writes():
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, TagMap())

    assert provider.read("SC2") is True
    runner.step()
    assert provider.read("SC2") is False

    with pytest.raises(ValueError, match="read-only system point"):
        provider.write("SC1", False)


def test_provider_write_to_writable_system_slots_succeeds(monkeypatch):
    _FrozenDateTime.fixed_now = datetime(2026, 1, 15, 10, 20, 30)
    monkeypatch.setattr("pyrung.core.system_points.datetime", _FrozenDateTime)

    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, TagMap())

    provider.write("SD29", 2030)
    provider.write("SD31", 4)
    provider.write("SD32", 10)
    provider.write("SC53", True)

    assert provider.read("SD29") == 0

    runner.step()
    assert provider.read("SD29") == 2030

    runner.step()
    assert provider.read("SD19") == 2030
    assert provider.read("SD21") == 4
    assert provider.read("SD22") == 10
    assert provider.read("SC53") is False


def test_provider_write_to_writable_command_bits_routes_through_patch():
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, TagMap())

    provider.write("SC55", True)
    assert runner.current_state.tags.get("rtc.apply_time", False) is False
    runner.step()
    assert runner.current_state.tags["rtc.apply_time"] is True


def test_provider_write_sc50_stops_runner_immediately():
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, TagMap())

    provider.write("SC50", True)

    assert runner.system_runtime.resolve(system.sys.mode_run.name, runner.current_state) == (True, False)

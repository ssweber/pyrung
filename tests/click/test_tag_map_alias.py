"""Tests for TagMap alias API, DataView alias propagation, and codegen alias emission."""

from __future__ import annotations

import pyclickplc
import pytest
from pyclickplc.addresses import get_addr_key

from pyrung.click import TagMap, c, ds
from pyrung.core import Block, Bool, Program, Rung, TagType, out

# ---------------------------------------------------------------------------
# TagMap alias storage and API
# ---------------------------------------------------------------------------


def _make_mapping_with_range():
    alarms = Block("Alarm", TagType.INT, 1, 5)
    mapping = TagMap({alarms: ds.select(500, 504)})
    return mapping, alarms


def test_alias_stores_and_retrieves():
    mapping, _alarms = _make_mapping_with_range()
    mapping.alias(ds[502], "cpHeel2nd")

    assert mapping.alias_for("DS502") == "cpHeel2nd"
    assert mapping.aliases == {"DS502": "cpHeel2nd"}


def test_alias_resolve_name():
    mapping, _alarms = _make_mapping_with_range()
    mapping.alias(ds[502], "cpHeel2nd")

    assert mapping.resolve_name("cpHeel2nd") == "DS502"
    assert mapping.resolve_name("UnknownTag") == "UnknownTag"


def test_alias_rejects_invalid_nickname():
    mapping, _alarms = _make_mapping_with_range()
    with pytest.raises(ValueError, match="not a valid"):
        mapping.alias(ds[502], "_BadName")


def test_alias_rejects_collision_with_logical_name():
    valve = Bool("Valve")
    mapping = TagMap({valve: c[1]})
    with pytest.raises(ValueError, match="collides with logical tag"):
        mapping.alias(c[1], "Valve")


def test_alias_rejects_duplicate():
    mapping, _alarms = _make_mapping_with_range()
    mapping.alias(ds[502], "cpHeel2nd")
    with pytest.raises(ValueError, match="already registered"):
        mapping.alias(ds[503], "cpHeel2nd")


def test_alias_for_returns_none_when_absent():
    mapping, _alarms = _make_mapping_with_range()
    assert mapping.alias_for("DS999") is None


# ---------------------------------------------------------------------------
# CSV round-trip: to_nickname_file writes aliases as nicknames
# ---------------------------------------------------------------------------


def test_to_nickname_file_writes_aliases(tmp_path):
    alarms = Block("Alarm", TagType.INT, 1, 3)
    mapping = TagMap({alarms: ds.select(500, 502)})
    mapping.alias(ds[501], "cpHeel2nd")

    path = tmp_path / "aliases.csv"
    mapping.to_nickname_file(path)

    rows = pyclickplc.read_csv(path)
    assert rows[get_addr_key("DS", 501)].nickname == "cpHeel2nd"


# ---------------------------------------------------------------------------
# DataView alias propagation
# ---------------------------------------------------------------------------


def _make_program_with_two_tags():
    motor = Bool("Motor")
    running = Bool("Running")
    with Program() as prog:
        with Rung(motor):
            out(running)
    return prog, motor, running


def test_dataview_with_aliases_details():
    prog, _motor, _running = _make_program_with_two_tags()
    view = prog.dataview().with_aliases({"Motor": "MotorAlias"})
    details = view.details()

    assert details["Motor"].alias == "MotorAlias"
    assert details["Running"].alias is None


def test_dataview_with_aliases_contains():
    prog, _motor, _running = _make_program_with_two_tags()
    view = prog.dataview().with_aliases({"Motor": "MotorAlias"})
    filtered = view.contains("MotorAlias")
    assert "Motor" in filtered.details()


def test_dataview_with_aliases_narrow_propagates():
    prog, _motor, _running = _make_program_with_two_tags()
    view = prog.dataview().with_aliases({"Motor": "MotorAlias"})
    upstream = view.upstream("Running")
    details = upstream.details()
    assert details["Motor"].alias == "MotorAlias"

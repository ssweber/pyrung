"""A patch or force leaves a value the tag's own type can serve.

A Modbus read packs a tag's value by the tag's type, and ``struct`` raises on a float
into an INT, so a value of another type is a register nobody can read.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from pyclickplc.addresses import parse_address
from pyclickplc.banks import BANKS
from pyclickplc.modbus import pack_value

from pyrung.click import ClickBlocks, ClickDataProvider, TagMap
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Char,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Word,
    blockcopy,
    copy,
    out,
    search,
)
from pyrung.core.compiled_plc import CompiledPLC

ds = ClickBlocks()[6]
Enable, Lamp = Bool("Enable"), Bool("Lamp")
Ii, D, W = Int("I"), Dint("D"), Word("W")
R, C = Real("R"), Char("C")
TAGS = {"I": Ii, "D": D, "W": W, "R": R, "C": C, "Enable": Enable}


def _program() -> Program:
    with Program(strict=False) as program:
        with Rung(Enable):
            out(Lamp)
            copy(Ii, D)
        with Rung(Lamp):
            copy(W, W)
            copy(R, R)
            copy(C, C)
    return program


def _engines() -> list[Any]:
    return [PLC(_program(), dt=0.01), CompiledPLC(_program(), dt=0.01)]


HELD = [
    # A zero of another type used to publish over the default it equals.
    ("I", 0.0, 0),
    ("I", False, 0),
    ("I", -0.0, 0),
    ("I", True, 1),
    ("I", 2.0, 2),
    ("D", 7.0, 7),
    ("W", 0.0, 0),
    ("Enable", 0, False),
    ("Enable", 1, True),
    ("Enable", 0.0, False),
    ("R", 0, 0.0),
    ("R", 3, 3.0),
    ("R", True, 1.0),
    ("R", -0.0, -0.0),
    ("C", "", "\x00"),
    ("C", "A", "A"),
    # What a copy() of the same value stores.
    ("I", 3.14, 3),
    ("I", 70000, 32767),
    ("I", -70000.0, -32768),
    ("D", 0xFFFF0001, 2147483647),
    ("W", -1, 65535),
    ("W", 65537, 1),
    ("I", math.inf, 32767),
    ("I", -math.inf, -32768),
    ("I", math.nan, 0),
    # Not bool(inf): a non-finite stores as 0 into anything but a clamp's bound.
    ("Enable", math.inf, False),
    ("Enable", -math.inf, False),
    ("Enable", math.nan, False),
]


def _same(held: Any, want: Any) -> bool:
    return type(held) is type(want) and math.copysign(1, held) == math.copysign(1, want)


@pytest.mark.parametrize(("name", "value", "want"), HELD, ids=repr)
@pytest.mark.parametrize("by_tag", [False, True], ids=["by name", "by tag"])
def test_a_patch_stores_what_the_tag_holds(name: str, value: Any, want: Any, by_tag: bool) -> None:
    for plc in _engines():
        plc.patch({TAGS[name] if by_tag else name: value})
        plc.step()
        held = plc.current_state.tags[name]
        assert held == want
        assert type(held) is type(want)
        if isinstance(want, float):
            assert _same(held, want)


@pytest.mark.parametrize(("name", "value", "want"), HELD, ids=repr)
def test_a_force_stores_what_the_tag_holds(name: str, value: Any, want: Any) -> None:
    for plc in _engines():
        plc.force(name, value)
        assert type(plc.forces[name]) is type(want)
        plc.step()
        held = plc.current_state.tags[name]
        assert held == want
        assert type(held) is type(want)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("I", "abc", "INT holds a number"),
        ("Enable", "x", "BOOL holds a number"),
        ("R", "1.5", "REAL holds a number"),
        ("W", "", "WORD holds a number"),
        ("C", "AB", "one character"),
        ("C", 65, "one character"),
        ("C", 0, "one character"),
        ("R", 1e39, "outside the REAL range"),
    ],
)
def test_a_value_no_store_can_make_sense_of_is_refused(name: str, value: Any, message: str) -> None:
    for plc in _engines():
        with pytest.raises(ValueError, match=message):
            plc.patch({name: value})
        with pytest.raises(ValueError, match=message):
            plc.force(name, value)
        assert name not in plc.forces


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_a_real_still_takes_inf_and_nan(value: float) -> None:
    for plc in _engines():
        plc.patch({R: value})
        plc.step()
        assert repr(plc.current_state.tags["R"]) == repr(value)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_a_copy_of_a_non_finite_into_a_bool_is_false_on_every_path(value: float) -> None:
    def program() -> Program:
        with Program(strict=False) as built:
            with Rung():
                copy(Real("Src"), Bool("Dst"))
        return built

    for plc in (PLC(program(), dt=0.01), CompiledPLC(program(), dt=0.01)):
        plc.patch({"Src": value})
        plc.step()
        assert plc.current_state.tags["Dst"] is False
    looped = PLC(program(), dt=0.01)
    looped.patch({"Src": value})
    looped.run(1_500)
    assert looped.state.tags["Dst"] is False


def test_a_name_nothing_reads_has_no_type_to_hold() -> None:
    """Nothing in the program says what it is, so it stays as written, and is reported."""
    plc = PLC(_program(), dt=0.01)
    plc.patch({"NoSuchTag": 0.5})
    plc.step()
    assert plc.state.tags["NoSuchTag"] == 0.5


def test_a_replay_holds_what_the_scan_held() -> None:
    plc = PLC(_program(), dt=0.01)
    for scan in range(1, 41):
        plc.patch({"I": 3.14, "Enable": 1} if scan == 10 else {})
        plc.step()
    truth = plc.history.at(11)
    assert type(truth.tags["I"]) is int
    plc._reset_cache(plc.state)
    again = plc.history.at(11)
    assert repr(again.tags["I"]) == "3"
    assert again.tags["Enable"] is True


def _slots_only_a_range_uses() -> tuple[Program, Any]:
    slots = Block("DS", TagType.INT, 1, 5)
    run, hit, found = Bool("Run"), Bool("Hit"), Int("Found")
    with Program(strict=False) as program:
        with Rung(run):
            search(slots.select(1, 3) >= 3.5, result=Int("Result"), found=hit)
        with Rung(hit):
            copy(1, found)
    return program, slots


def test_a_replay_gives_a_slot_named_by_string_what_the_scan_gave_it() -> None:
    """A kernel knows every slot and the runner only those named: the log is the answer."""
    program, _ = _slots_only_a_range_uses()
    plc = PLC(program, dt=0.01)
    plc.patch({"Run": True, "DS2": 3.9})
    live = {}
    for _ in range(6):
        plc.step()
        live[plc.state.scan_id] = plc.state
    assert live[1].tags["DS2"] == 3.9
    assert live[1].tags["Hit"] is True
    assert live[6].tags["Found"] == 1
    assert plc._compiled_replay_supported_kernel() is not None
    for scan in (1, 3, 6):
        plc._reset_cache(plc.state)
        again = plc.history.at(scan)
        for name in ("DS2", "Hit", "Found"):
            assert repr(again.tags[name]) == repr(live[scan].tags[name]), (scan, name)
    plc._reset_cache(plc.state)
    forked = plc.fork_from(3)
    assert forked.state.tags["DS2"] == 3.9
    forked.step()
    assert forked.state.tags["Hit"] is True


def test_a_replay_takes_text_in_a_slot_as_the_scan_did() -> None:
    slots = Block("DS", TagType.INT, 1, 5)
    other = Block("DD", TagType.INT, 1, 5)
    with Program(strict=False) as program:
        with Rung(Bool("Run")):
            blockcopy(slots.select(1, 3), other.select(1, 3))
    plc = PLC(program, dt=0.01)
    plc.patch({"DS2": "txt"})
    plc.run(5)
    assert plc.state.tags["DS2"] == "txt"
    plc._reset_cache(plc.state)
    assert plc.history.at(3).tags["DS2"] == "txt"
    plc._reset_cache(plc.state)
    assert plc.fork_from(3).state.tags["DS2"] == "txt"
    plc._reset_cache(plc.state)
    assert plc.replay_to(3).state.tags["DS2"] == "txt"


def test_a_replay_past_a_name_the_runner_learned_later_holds_what_was_given() -> None:
    """A name no one knew when patched, known by the time of the seek."""
    plc = PLC(_program(), dt=0.01)
    plc.patch({"Spare": "text"})
    plc.run(4)
    assert plc.state.tags["Spare"] == "text"
    plc.force(Int("Spare"), 7)
    plc.unforce("Spare")
    plc._reset_cache(plc.state)
    assert plc.history.at(2).tags["Spare"] == "text"


def test_a_kernel_run_starts_from_what_the_tag_holds() -> None:
    plc = PLC(_program(), dt=0.01)
    plc.patch({"I": 0.0, "Enable": 0})
    plc.force("W", 2.0)
    plc.run_for(30.0)
    twin = PLC(_program(), dt=0.01)
    twin.patch({"I": 0, "Enable": False})
    twin.force("W", 2)
    for _ in range(plc.state.scan_id):
        twin.step()
    assert repr(dict(plc.state.tags)) == repr(dict(twin.state.tags))


@pytest.mark.parametrize("value", [0.0, 2.5, False, True, 70000])
def test_a_modbus_read_serves_a_patched_register(value: Any) -> None:
    tag = Int("Mapped")
    with Program(strict=False) as program:
        with Rung(Enable):
            copy(tag, tag)
    plc = PLC(program, dt=0.01)
    provider = ClickDataProvider(plc, TagMap({tag: ds[1]}))

    plc.patch({tag.name: value})
    plc.step()

    read = provider.read("DS1")
    assert type(read) is int
    bank, _index = parse_address("DS1")
    pack_value(read, BANKS[bank].data_type)

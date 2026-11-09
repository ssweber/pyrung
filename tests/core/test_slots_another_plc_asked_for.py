"""A PLC answers as one built first, whatever another PLC or an analysis asked of a block.

A block makes a tag for each slot anything asks for, and keeps it.  Scans through a
pointer, how(), a coverage report and the prover all ask.  None of that may change what
a PLC built later knows, holds or answers.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import count
from typing import Any

import pytest

from pyrung.core import PLC, Block, Bool, Int, Program, Rung, TagType, copy, out
from pyrung.core.analysis.causal.recorded import _tag_in_block
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import always
from pyrung.core.harness import Coupling, link_problem
from pyrung.core.physical import Physical
from pyrung.pytest_plugin import CoverageCollector


def _pointer_program() -> tuple[Program, Block, Bool]:
    """A block read only through a pointer, so no rung names a slot."""
    slots = Block("Iq", TagType.BOOL, 1, 8)
    pointer = Int("IqPtr", min=1, max=8, default=1)
    lamp = Bool("IqLamp")
    with Program(strict=False) as program:
        with Rung(slots[pointer] == True):  # noqa: E712
            out(lamp)
    return program, slots, lamp


def _pointer_scans(program: Program, _lamp: Bool) -> None:
    other = PLC(program, dt=0.01)
    for addr in (2, 5, 7):
        other.patch({"IqPtr": addr})
        other.step()


def _how(program: Program, lamp: Bool) -> None:
    PLC(program, dt=0.01).how(lamp, max_scans=200)


def _coverage(program: Program, _lamp: Bool) -> None:
    other = PLC(program, dt=0.01)
    other.patch({"IqPtr": 3})
    other.run_for(0.1)
    CoverageCollector().collect(other)


def _prove(program: Program, lamp: Bool) -> None:
    always(program, ~lamp)


def _program_graph(program: Program, _lamp: Bool) -> None:
    build_program_graph(program)


ACTIVITIES = [
    pytest.param(_pointer_scans, id="pointer-scans"),
    pytest.param(_how, id="how"),
    pytest.param(_coverage, id="coverage"),
    pytest.param(_prove, id="prove"),
    pytest.param(_program_graph, id="program-graph"),
]


@pytest.mark.parametrize("activity", ACTIVITIES)
def test_a_plc_built_later_knows_and_holds_what_one_built_first_does(
    activity: Callable[[Program, Bool], None],
) -> None:
    program, slots, lamp = _pointer_program()
    first = PLC(program, dt=0.01)

    activity(program, lamp)
    assert slots._tag_cache, "the activity made slot tags"
    later = PLC(program, dt=0.01)

    assert later._known_tags_by_name.keys() == first._known_tags_by_name.keys()
    assert set(later.current_state.tags) == set(first.current_state.tags)


@pytest.mark.parametrize("activity", ACTIVITIES)
def test_pyrung_leaves_the_record_of_what_the_users_code_indexed_alone(
    activity: Callable[[Program, Bool], None],
) -> None:
    program, slots, lamp = _pointer_program()
    activity(program, lamp)
    assert slots._indexed == set()


def test_a_slot_the_users_code_indexed_is_known_to_every_plc() -> None:
    program, slots, lamp = _pointer_program()
    named = slots[4]
    first = PLC(program, dt=0.01)
    _how(program, lamp)
    later = PLC(program, dt=0.01)
    assert first._known_tags_by_name["Iq4"] is later._known_tags_by_name["Iq4"] is named


def _reset_one(slots: Block) -> None:
    slots.slot(2).reset()


def _reset_a_range(slots: Block) -> None:
    slots.slot(1, 3).reset()


@pytest.mark.parametrize("reset", [_reset_one, _reset_a_range], ids=["slot", "range"])
def test_a_slot_another_plc_made_a_tag_of_can_still_be_reset(
    reset: Callable[[Block], None],
) -> None:
    program, slots, lamp = _pointer_program()
    slots.slot(2, retentive=True, name="Kept")
    _how(program, lamp)
    assert 2 in slots._tag_cache

    reset(slots)

    later = PLC(program, dt=0.01)
    assert "Kept" not in later.tags
    assert later.tags["Iq2"].retentive is False
    assert later.tags["Iq2"] is slots._tag_cache[2]


def test_a_slot_the_users_code_indexed_still_refuses_a_reset() -> None:
    _program, slots, _lamp = _pointer_program()
    slots.slot(2, retentive=True)
    _ = slots[2]
    with pytest.raises(ValueError, match="after tag materialization"):
        slots.slot(2).reset()


def _counting_factory_program() -> tuple[Program, Int]:
    calls = count(101)
    slots = Block("Fq", TagType.INT, 1, 6, default_factory=lambda _addr: next(calls))
    pointer = Int("FqPtr", default=4)
    seen = Int("FqSeen")
    with Program(strict=False) as program:
        with Rung():
            copy(slots[pointer], seen)
    return program, seen


def test_a_counting_factory_gives_a_slot_one_default_whichever_slot_is_read_first() -> None:
    program, seen = _counting_factory_program()
    first = PLC(program, dt=0.01)
    first.step()

    program, seen = _counting_factory_program()
    other = PLC(program, dt=0.01)
    other.patch({"FqPtr": 6})
    other.step()
    later = PLC(program, dt=0.01)
    later.step()

    # The factory is asked in address order, so slot 4 gets its fourth answer.
    assert later.current_state.tags[seen.name] == first.current_state.tags[seen.name] == 104


def test_a_link_to_a_slot_only_a_pointer_reaches_is_judged_the_same_on_every_plc() -> None:
    program, _slots, lamp = _pointer_program()
    first = PLC(program, dt=0.01)
    _how(program, lamp)
    later = PLC(program, dt=0.01)
    coupling = Coupling("IqLamp", "Iq3", Physical("Switch", on_delay="20ms"), None)
    assert link_problem(later, coupling) == link_problem(first, coupling)


def test_a_slot_of_a_big_block_is_in_it_before_anything_asks_for_it() -> None:
    # A block past the program graph's cap is written through a pointer as a whole,
    # and cause() looks for the slot's writer by the slot's name.
    big = Block("Bq", TagType.INT, 1, 1500)
    assert _tag_in_block(big, "Bq900")
    assert not _tag_in_block(big, "Bq1501")
    assert not big._tag_cache


def _whole(plc: Any) -> dict[str, Any]:
    return dict(plc.current_state.tags)


def test_a_plc_built_later_steps_as_one_built_first() -> None:
    program, _slots, lamp = _pointer_program()
    first = PLC(program, dt=0.01)
    _how(program, lamp)
    later = PLC(program, dt=0.01)
    for plc in (first, later):
        plc.patch({"IqPtr": 6})
        plc.run_for(0.05)
    assert _whole(later) == _whole(first)

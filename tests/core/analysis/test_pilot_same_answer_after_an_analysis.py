"""how() gives the same answer on a PLC built before or after another how() of its program.

A how() asks each Block for the slots a pointer reaches. The Block keeps those Tags, and a
PLC built later knows them. A PLC built earlier does not, so how() also looks a slot up
among the tags the rungs reach.
"""

from __future__ import annotations

from typing import Any

from pyrung.core import PLC, Block, Bool, Int, Program, Rung, TagType, blockcopy, copy, out
from pyrung.core.analysis.pilot.coast import value_trigger
from pyrung.core.analysis.pilot.drive_setup import prepare_drive
from pyrung.core.analysis.pilot.last_resort import _input_values
from pyrung.core.analysis.pilot.restart import _restarted, _survivors


def _before_and_after(prog: Program, target: Any, session: Any = None) -> tuple[PLC, PLC]:
    """A PLC built before a how() on another PLC of *prog*, and one built after it."""

    def built() -> PLC:
        plc = PLC(prog)
        if session is not None:
            session(plc)
        return plc

    before = built()
    PLC(prog).how(target)
    after = built()
    return before, after


def _readonly_slots() -> tuple[Program, Bool]:
    cmd = Block("Cmd", TagType.BOOL, 1, 4)
    cmd.slot(2, readonly=True)
    cmd.slot(3, readonly=True)
    ptr = Int("Ptr", min=2, max=3, default=2, readonly=True)
    done = Bool("Done")
    with Program(strict=False) as prog:
        with Rung(cmd[ptr] == True):  # noqa: E712
            out(done)
    return prog, done


def test_a_plan_sets_no_readonly_slot_whichever_plc_asks() -> None:
    prog, done = _readonly_slots()
    before, after = _before_and_after(prog, done)

    plan = before.how(done)

    assert str(plan) == str(after.how(done))
    assert not plan.reachable


def test_a_slot_looked_up_is_the_tag_a_later_plc_knows() -> None:
    prog, done = _readonly_slots()
    before, after = _before_and_after(prog, done)
    slots = {f"Cmd{addr}" for addr in range(1, 5)}

    assert slots <= after._known_tags_by_name.keys()
    assert not slots & before._known_tags_by_name.keys()
    for name in slots:
        assert before._tag_known_or_reached(name) is after._known_tags_by_name[name]


def test_the_levers_and_their_resting_values_do_not_depend_on_an_earlier_how() -> None:
    cmd = Block("Cmd", TagType.BOOL, 1, 4)
    cmd.slot(2, readonly=True)
    cmd.slot(3, default=True)
    ptr = Int("Ptr", min=1, max=4, default=1)
    done = Bool("Done")
    with Program(strict=False) as prog:
        with Rung(cmd[ptr] == True):  # noqa: E712
            out(done)
    before, after = _before_and_after(prog, done)

    first = prepare_drive(before, unlink=None)
    second = prepare_drive(after, unlink=None)

    assert first.steerable == second.steerable
    assert "Cmd2" not in first.steerable
    assert first.resting == second.resting
    assert first.resting["Cmd3"] is True


def _retentive_slot() -> tuple[Program, Bool]:
    ds = Block("Ds", TagType.INT, 1, 4, retentive=True)
    ptr = Int("Ptr", min=1, max=4, default=2)
    go = Bool("Go")
    done = Bool("Done")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(7, ds[ptr])
        with Rung(ds[ptr] == 7):
            out(done)
    return prog, done


def _store_seven(plc: PLC) -> None:
    plc.patch({"Go": True})
    plc.step()
    plc.patch({"Go": False})
    plc.step()


def test_a_restart_reports_the_same_slots_kept_whichever_plc_asks() -> None:
    prog, done = _retentive_slot()
    before, after = _before_and_after(prog, done, _store_seven)

    kept = _survivors(_restarted(before), frozenset({"Go"}))

    assert kept == _survivors(_restarted(after), frozenset({"Go"}))
    assert kept == {"Ds2": (7, 0)}


def test_a_wait_on_a_slot_compiles_its_stop_whichever_plc_asks() -> None:
    prog, done = _retentive_slot()
    before, after = _before_and_after(prog, done)

    trigger = value_trigger(before, "slot", "target", "Ds3", 7)

    assert trigger.condition is not None
    assert str(trigger.condition) == str(value_trigger(after, "slot", "target", "Ds3", 7).condition)


def test_a_slot_copied_through_a_range_is_reached_whichever_plc_asks() -> None:
    cmd = Block("Cmd", TagType.BOOL, 1, 4)
    cmd.slot(2, default=True)
    sink = Block("Sink", TagType.BOOL, 1, 4)
    ptr = Int("Ptr", min=1, max=4, default=2, readonly=True)
    done = Bool("Done")
    with Program(strict=False) as prog:
        with Rung():
            blockcopy(cmd.select(1, 4), sink.select(1, 4))
        with Rung(sink[ptr] == False):  # noqa: E712
            out(done)
    before, after = _before_and_after(prog, done)

    plan = before.how(done)

    assert plan.reachable
    assert str(plan) == str(after.how(done))
    assert "Set Cmd2=False" in str(plan)
    assert "Cmd2" not in before.state.tags


def test_a_slot_is_tried_at_values_it_does_not_hold_whichever_plc_asks() -> None:
    cmd = Block("Cmd", TagType.BOOL, 1, 4)
    cmd.slot(2, default=True)
    vals = Block("Val", TagType.INT, 1, 4)
    vals.slot(2, choices={0: "off", 1: "one", 2: "two"})
    ptr = Int("Ptr", min=1, max=4, default=2, readonly=True)
    done = Bool("Done")
    with Program(strict=False) as prog:
        with Rung(cmd[ptr] == False, vals[ptr] == 2):  # noqa: E712
            out(done)
    before, after = _before_and_after(prog, done)

    tried = []
    for plc in (before, after):
        setup = prepare_drive(plc, unlink=None)
        tags = setup.work.state.tags
        tried.append(
            {name: _input_values(setup, name, tags.get(name)) for name in ("Cmd2", "Val2")}
        )

    assert tried[0] == tried[1]
    assert tried[0]["Cmd2"] == (False,)
    assert 0 not in tried[0]["Val2"]

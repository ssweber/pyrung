"""A slot a range reaches is the program's, whatever else has asked for it.

What a reset keeps, whose limits count and which plants are declared come from what the
rungs reach, not from which slots happen to have been asked for, so the same program
behaves the same on either engine and on any runner of it.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from itertools import count
from types import SimpleNamespace
from typing import Any

import pytest

from pyrung.click import ClickBlocks
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Field,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    blockcopy,
    call,
    copy,
    fill,
    out,
    subroutine,
    udt,
)
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical, Ramp
from pyrung.core.tag import Tag
from pyrung.dap.bounds_console import emit_bounds_violations
from pyrung.dap.console import _resolve_tags
from pyrung.dap.handlers.simulation import resolve_target_locked

Go = Bool("Go", external=True)
Enable, Other = Bool("Enable"), Bool("Other")
ENGINES = [PLC, CompiledPLC]
IDS = ["PLC", "CompiledPLC"]


def _restart(plc: Any) -> None:
    plc.stop()
    plc.start()


def _reboot(plc: Any) -> None:
    plc.reboot()


RESETS = pytest.mark.parametrize("reset", [_restart, _reboot], ids=["restart", "reboot"])
ON_EITHER_ENGINE = pytest.mark.parametrize("engine", ENGINES, ids=IDS)


def _blocks(*, retentive: bool = True) -> tuple[Block, Block]:
    return Block("Rc", TagType.INT, 1, 5, retentive=retentive), Block("Sink", TagType.INT, 1, 5)


def _through_a_range(rc: Block, sink: Block) -> Program:
    with Program(strict=False) as program:
        with Rung(Go):
            fill(7, rc.select(1, 3))
        with Rung():
            blockcopy(rc.select(1, 3), sink.select(1, 3))
    return program


def _after(engine: Any, program: Program, sink: Block, reset: Callable[[Any], None]) -> Any:
    plc = engine(program, dt=0.01)
    plc.patch({"Go": True})
    plc.step()
    plc.patch({"Go": False})
    plc.step()
    assert plc.current_state.tags[sink[2].name] == 7
    reset(plc)
    plc.step()
    return plc.current_state.tags.get(sink[2].name)


@ON_EITHER_ENGINE
@RESETS
def test_a_retentive_slot_nothing_named_keeps_its_value(engine, reset) -> None:
    rc, sink = _blocks()
    assert _after(engine, _through_a_range(rc, sink), sink, reset) == 7


@ON_EITHER_ENGINE
@RESETS
def test_a_retentive_slot_named_outside_the_rungs_keeps_its_value(engine, reset) -> None:
    rc, sink = _blocks()
    assert rc[2].name == "Rc2"
    assert _after(engine, _through_a_range(rc, sink), sink, reset) == 7


@ON_EITHER_ENGINE
@RESETS
def test_a_retentive_slot_a_rung_names_keeps_its_value(engine, reset) -> None:
    rc, sink = _blocks()
    with Program(strict=False) as program:
        with Rung(Go):
            fill(7, rc.select(1, 3))
        with Rung():
            blockcopy(rc.select(1, 3), sink.select(1, 3))
        with Rung(rc[2] == 7):
            out(Other)
    assert _after(engine, program, sink, reset) == 7


@ON_EITHER_ENGINE
@pytest.mark.parametrize("named", [False, True], ids=["unnamed", "named"])
def test_a_slot_that_is_not_retentive_goes_on_a_restart_and_stays_on_a_reboot(
    engine, named
) -> None:
    rc, sink = _blocks(retentive=False)
    if named:
        assert rc[2].name == "Rc2"
    program = _through_a_range(rc, sink)
    assert _after(engine, program, sink, _restart) == 0
    assert _after(engine, program, sink, _reboot) == 7


@ON_EITHER_ENGINE
@RESETS
@pytest.mark.parametrize("named", [False, True], ids=["unnamed", "named"])
def test_clicks_ds_keeps_its_value(engine, reset, named) -> None:
    ds = ClickBlocks().ds
    sink = Block("Sink", TagType.INT, 1, 5)
    if named:
        assert ds[2].name == "DS2"
    assert _after(engine, _through_a_range(ds, sink), sink, reset) == 7


@ON_EITHER_ENGINE
@RESETS
def test_a_slot_a_pointer_reaches_keeps_its_value(engine, reset) -> None:
    rc, sink = _blocks()
    idx = Int("Idx", external=True, default=2)
    with Program(strict=False) as program:
        with Rung(Go):
            copy(7, rc[idx])
        with Rung():
            blockcopy(rc.select(1, 3), sink.select(1, 3))
    assert _after(engine, program, sink, reset) == 7


@ON_EITHER_ENGINE
@RESETS
def test_a_slot_a_subroutine_reaches_keeps_its_value(engine, reset) -> None:
    rc, sink = _blocks()
    with Program(strict=False) as program:
        with Rung():
            call("writes")
        with Rung():
            blockcopy(sink.select(4, 5), sink.select(1, 2))
        with subroutine("writes"):
            with Rung(Go):
                fill(7, rc.select(1, 3))
            with Rung():
                blockcopy(rc.select(1, 2), sink.select(4, 5))
    assert _after(engine, program, sink, reset) == 7


@ON_EITHER_ENGINE
@RESETS
def test_one_program_means_the_same_on_each_runner_of_it(engine, reset) -> None:
    rc, sink = _blocks()
    program = _through_a_range(rc, sink)
    first = PLC(program, dt=0.01)
    first.patch({"Go": True})
    first.run(2)
    second = CompiledPLC(program, dt=0.01)
    second.patch({"Go": True})
    second.run(2)
    assert _after(engine, program, sink, reset) == 7


def test_what_a_run_asked_of_a_program_is_not_what_its_rungs_say() -> None:
    # What the runner knows can grow with what has been asked, as a block's cache does.  What
    # the rungs reach, which a reset, the limits and the plants go by, cannot.
    def reach(plc) -> tuple[set[str], set[str]]:
        named = {tag.name for tag in plc._tags_rungs_reach.walk().named}
        return named, plc._tags_rungs_reach.names()

    rc, sink = _blocks()
    program = _through_a_range(rc, sink)
    before = reach(PLC(program, dt=0.01))
    ran = PLC(program, dt=0.01)
    ran.patch({"Go": True})
    ran.run(2)
    CompiledPLC(program, dt=0.01)
    assert reach(PLC(program, dt=0.01)) == before


def test_a_call_in_bare_rungs_reaches_its_subroutine() -> None:
    a, b = Bool("A", external=True), Bool("B")
    with Program(strict=False) as program:
        with Rung():
            call("writes")
        with subroutine("writes"):
            with Rung(a):
                out(b)
    assert set(PLC(program.rungs, dt=0.01)._known_tags_by_name) == {"A", "B"}


@RESETS
def test_a_reset_leaves_the_engines_holding_the_same_names(reset) -> None:
    def held(engine: Any) -> set[str]:
        rc, sink = _blocks()
        plc = engine(_through_a_range(rc, sink), dt=0.01)
        plc.patch({"Go": True})
        plc.run(2)
        reset(plc)
        plc.run(2)
        return {name for name in plc.current_state.tags if name.startswith(("Rc", "Sink"))}

    assert held(PLC) == held(CompiledPLC)


@ON_EITHER_ENGINE
@RESETS
def test_a_slot_nothing_wrote_stays_out_of_the_state(engine, reset) -> None:
    # CLICK's ds is 4500 slots: a reset must not seed them all to carry the three it holds.
    ds = ClickBlocks().ds
    sink = Block("Sink", TagType.INT, 1, 5)
    plc = engine(_through_a_range(ds, sink), dt=0.01)
    plc.patch({"Go": True})
    plc.run(2)
    reset(plc)
    assert "DS4" not in plc.current_state.tags


@pytest.mark.parametrize(
    "config",
    [{"max": 5}, {"choices": {1: "one"}}],
    ids=["max", "choices"],
)
@pytest.mark.parametrize("named", [False, True], ids=["unnamed", "named"])
def test_a_slots_limits_hold_when_a_range_writes_it(config, named) -> None:
    rc, sink = _blocks()
    rc.slot(2, **config)
    if named:
        assert rc[2].name == "Rc2"
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    plc.patch({"Go": True})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plc.step()
    assert "Rc2" in plc.bounds_violations
    assert "BoundsViolation" in str(caught[0].message)


def test_a_slot_with_limits_outside_the_range_is_not_watched() -> None:
    rc, sink = _blocks()
    rc.slot(5, max=5)
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    assert "Rc5" not in plc._constrained_tags


@pytest.mark.parametrize("touched", [False, True], ids=["untouched", "touched"])
def test_a_field_with_limits_holds_when_a_range_writes_it(touched) -> None:
    @udt(count=3)
    class Motor:
        Speed: Int = Field(max=5)

    if touched:
        assert Motor[1].Speed.max == 5
    sink = Block("Sink", TagType.INT, 1, 5)
    with Program(strict=False) as program:
        with Rung(Go):
            fill(7, Motor.Speed.select(1, 2))
        with Rung():
            blockcopy(Motor.Speed.select(1, 2), sink.select(1, 2))
    plc = PLC(program, dt=0.01)
    plc.patch({"Go": True})
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        plc.step()
    assert {Motor.Speed[1].name, Motor.Speed[2].name} <= set(plc.bounds_violations)


def test_a_limit_holds_on_a_fork_too() -> None:
    rc, sink = _blocks()
    rc.slot(2, max=5)
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    plc.step()
    fork = plc.fork()
    fork.patch({"Go": True})
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        fork.step()
    assert "Rc2" in fork.bounds_violations


def test_compiled_plc_has_no_limits_or_plants_to_ask_about() -> None:
    # The bounds and harness rows here are PLC's: if this changes, they need CompiledPLC's too.
    rc, sink = _blocks()
    plc = CompiledPLC(_through_a_range(rc, sink), dt=0.01)
    assert not hasattr(plc, "bounds_violations")
    assert not hasattr(plc, "_constrained_tags")
    with pytest.raises(AttributeError):
        Harness(plc).install()


@pytest.mark.parametrize("named", [False, True], ids=["unnamed", "named"])
def test_plc_tags_holds_a_slot_a_range_reaches(named) -> None:
    rc, sink = _blocks()
    if named:
        assert rc[2].name == "Rc2"
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    assert plc.tags["Rc2"] is rc[2]
    assert "Rc5" not in plc.tags
    # A slot something named before the runner was built starts in the state, as ever.
    assert ("Rc2" in plc.current_state.tags) is named
    plc.step()
    assert ("Rc2" in plc.current_state.tags) is named


def test_plc_tags_makes_a_slot_a_tag_only_when_it_is_read() -> None:
    rc, sink = _blocks()
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    tags = plc.tags
    assert "Rc2" in tags and "Rc5" not in tags
    assert {"Rc1", "Rc2", "Rc3", "Go"} <= set(tags)
    assert len(tags) == len(set(tags))
    assert not rc._tag_cache
    assert tags["Rc2"] is rc[2]
    assert tags.get("Rc5") is None
    with pytest.raises(KeyError):
        tags["Rc5"]


def test_plc_tags_follows_a_slot_named_after_it_was_asked() -> None:
    rc, sink = _blocks()
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    assert "Rc2" in plc.tags
    rc.slot(2, name="Late")
    assert "Late" in plc.tags and "Rc2" not in plc.tags
    assert plc.tags["Late"] is rc[2]
    plc.patch({"Late": 99999})
    assert plc.pending_patches["Late"] == 32767


def test_plc_tags_holds_both_ends_of_a_plant_on_a_slot() -> None:
    enables = Block("En", TagType.BOOL, 1, 3)
    bank, dst = Block("Bank", TagType.REAL, 1, 8), Block("Dst", TagType.REAL, 1, 8)
    bank.slot(2, physical=RAMP, link="En2")

    def rungs() -> None:
        with Rung():
            fill(True, enables.select(1, 3))

    harness, plc = _ran_a_plant(bank, dst, rungs)
    coupling = harness._declared[0]
    assert plc.tags[coupling.en_name] is enables[2]
    assert plc.tags[coupling.fb_name] is bank[2]


def test_the_debugger_resolves_a_slot_a_range_reaches_before_it_is_written() -> None:
    rc, sink = _blocks()
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    adapter = SimpleNamespace(_require_runner_locked=lambda: plc)
    assert "Rc2" not in plc.current_state.tags
    assert resolve_target_locked(adapter, "Rc2") == "Rc2"


def test_the_console_resolves_a_slot_a_range_reaches() -> None:
    rc, sink = _blocks()
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    assert [tag.name for tag in _resolve_tags(plc, ["Rc2"], verb="x")] == ["Rc2"]
    with pytest.raises(ValueError, match="unknown tag 'Rc5'"):
        _resolve_tags(plc, ["Rc5"], verb="x")


def test_the_bounds_console_labels_a_slot_a_range_reaches() -> None:
    rc, sink = _blocks()
    rc.slot(2, choices={1: "one"})
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    plc.patch({"Go": True})
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        plc.step()
    adapter = SimpleNamespace(_runner=plc, _bounds_accumulator={})
    emitted: list[str] = []
    adapter._enqueue_internal_event = lambda _name, body: emitted.append(body["output"])
    emit_bounds_violations(adapter)
    assert "one" in emitted[0]


RAMP = Physical("Slow", profile=Ramp(up=1.0))


def _ran_a_plant(bank: Block, dst: Block, rungs: Callable[[], None]) -> tuple[Harness, PLC]:
    with Program(strict=False) as program:
        rungs()
        with Rung():
            blockcopy(bank.select(1, 3), dst.select(1, 3))
    plc = PLC(program, dt=0.01)
    harness = Harness(plc)
    harness.install()
    plc.run_for(1.0)
    return harness, plc


@pytest.mark.parametrize("named", [False, True], ids=["unnamed", "named"])
def test_a_plant_on_a_slot_a_range_reads_drives_it(named) -> None:
    bank, dst = Block("Bank", TagType.REAL, 1, 8), Block("Dst", TagType.REAL, 1, 8)
    bank.slot(2, physical=RAMP, link="Enable")
    if named:
        assert bank[2].name == "Bank2"

    def rungs() -> None:
        with Rung():
            out(Enable)

    harness, plc = _ran_a_plant(bank, dst, rungs)
    assert [(c.en_name, c.fb_name) for c in harness._declared] == [("Enable", "Bank2")]
    assert plc.current_state.tags["Dst2"] == pytest.approx(0.99)


def test_a_plant_linked_to_a_slot_a_range_writes_runs_on_it() -> None:
    enables = Block("En", TagType.BOOL, 1, 3)
    bank, dst = Block("Bank", TagType.REAL, 1, 8), Block("Dst", TagType.REAL, 1, 8)
    bank.slot(2, physical=RAMP, link="En2")

    def rungs() -> None:
        with Rung():
            fill(True, enables.select(1, 3))

    harness, plc = _ran_a_plant(bank, dst, rungs)
    assert [(c.en_name, c.fb_name) for c in harness._declared] == [("En2", "Bank2")]
    assert plc.current_state.tags["Dst2"] == pytest.approx(0.99)


def test_a_plant_on_a_slot_no_rung_reaches_is_not_declared() -> None:
    bank, dst = Block("Bank", TagType.REAL, 1, 8), Block("Dst", TagType.REAL, 1, 8)
    bank.slot(7, physical=RAMP, link="Enable")

    def rungs() -> None:
        with Rung():
            out(Enable)

    harness, _ = _ran_a_plant(bank, dst, rungs)
    assert harness._declared == []


@pytest.mark.parametrize("touched", [False, True], ids=["untouched", "touched"])
def test_a_plant_on_a_field_a_range_reads_drives_it(touched) -> None:
    @udt(count=2)
    class Valve:
        En: Bool
        Fb: Real = Field(physical=RAMP, link="En")

    if touched:
        assert Valve[1].Fb.physical is RAMP
    dst = Block("Dst", TagType.REAL, 1, 3)
    with Program(strict=False) as program:
        with Rung():
            fill(True, Valve.En.select(1, 2))
        with Rung():
            blockcopy(Valve.Fb.select(1, 2), dst.select(1, 2))
    plc = PLC(program, dt=0.01)
    harness = Harness(plc)
    harness.install()
    plc.run_for(1.0)
    assert len(harness._declared) == 2
    assert plc.current_state.tags["Dst2"] == pytest.approx(0.99)


@pytest.mark.parametrize("scans", [1, 40], ids=["one", "many"])
def test_a_slots_limits_hold_over_a_run_as_they_do_stepping(scans) -> None:
    def violations(run: Callable[[PLC], None]) -> Any:
        rc, sink = _blocks()
        rc.slot(2, max=5)
        plc = PLC(_through_a_range(rc, sink), dt=0.01)
        plc.patch({"Go": True})
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            run(plc)
        return plc.current_state.tags["Rc2"], sorted(plc.bounds_violations)

    def stepped(plc: PLC) -> None:
        for _ in range(scans):
            plc.step()

    assert violations(lambda plc: plc.run(scans)) == violations(stepped) == (7, ["Rc2"])


def _pointing_at(addr: int, factory) -> tuple[Program, Block, Tag]:
    """A block read through a pointer, so every slot is reached and only *addr* is read."""
    slots = Block("Sq", TagType.INT, 1, 5, default_factory=factory)
    pointer = Int("Ptr", default=addr)
    seen = Int("Seen")
    with Program(strict=False) as program:
        with Rung():
            copy(slots[pointer], seen)
    return program, slots, seen


def _refuses_three(addr: int) -> int:
    if addr == 3:
        raise ValueError("no slot 3")
    return addr * 10


def _ask_for_the_tags(plc) -> None:
    assert "Sq2" in plc.tags


def _patch_another_slot(plc) -> None:
    plc.patch({"Sq4": 99999})


def _patch_the_refusing_slot(plc) -> None:
    plc.patch({"Sq3": 99999})
    assert plc.pending_patches["Sq3"] == 32767


WITHOUT_THE_SLOT_READ = [
    pytest.param(_ask_for_the_tags, id="plc.tags"),
    pytest.param(_reboot, id="reboot"),
    pytest.param(_restart, id="restart"),
    pytest.param(_patch_another_slot, id="patch by name"),
]


@pytest.mark.parametrize(
    "asked", [*WITHOUT_THE_SLOT_READ, pytest.param(_patch_the_refusing_slot, id="patch it")]
)
def test_a_factory_refusing_a_slot_nothing_reads_stops_nothing(asked) -> None:
    program, _slots, seen = _pointing_at(2, _refuses_three)
    plc = PLC(program, dt=0.01)
    asked(plc)
    plc.step()
    assert plc.current_state.tags[seen.name] == 20


def test_a_factory_is_called_for_a_reset_slot_no_more_than_for_a_run() -> None:
    def first_read(asked) -> int:
        calls = count(101)
        program, _slots, seen = _pointing_at(1, lambda addr: next(calls))
        plc = PLC(program, dt=0.01)
        if asked is not None:
            asked(plc)
        plc.step()
        return plc.current_state.tags[seen.name]

    assert first_read(None) == 101
    for asked in (_ask_for_the_tags, _reboot, _restart, _patch_another_slot):
        assert first_read(asked) == 101


@pytest.mark.parametrize("asked", WITHOUT_THE_SLOT_READ)
def test_a_slot_stays_configurable_until_something_reads_it(asked) -> None:
    program, slots, _seen = _pointing_at(1, None)
    slots.slot(2, retentive=True)
    plc = PLC(program, dt=0.01)
    asked(plc)
    slots.slot(2).reset()
    assert slots[2].retentive is False


def test_patching_a_slot_by_name_leaves_it_configurable() -> None:
    program, slots, _seen = _pointing_at(1, None)
    slots.slot(2, retentive=True)
    plc = PLC(program, dt=0.01)
    plc.patch({"Sq2": 99999})
    assert plc.pending_patches["Sq2"] == 32767
    slots.slot(2).reset()


def test_a_cached_slot_a_rung_does_not_reach_is_known_as_ever() -> None:
    rc, sink = _blocks()
    _ = rc[5]
    plc = PLC(_through_a_range(rc, sink), dt=0.01)
    assert plc._known_tags_by_name["Rc5"] is rc[5]
    assert plc.current_state.tags["Rc5"] == 0
    assert plc.tags["Rc5"] is rc[5]


def test_how_reads_a_readonly_slot_something_named_outside_the_rungs() -> None:
    held, copied = Block("Hq", TagType.INT, 1, 5), Block("Dq", TagType.INT, 1, 5)
    seen = Bool("Seen")
    held.slot(2, readonly=True, external=False)
    _ = held[2]
    with Program(strict=False) as program:
        with Rung():
            blockcopy(held.select(1, 3), copied.select(1, 3))
        with Rung(copied[2] == 7):
            out(seen)
    plan = PLC(program, dt=0.01).how(seen)
    assert "Hq2" not in str(plan), plan

"""Out coil variants under the prover and PILOT.

The prover models toggle exactly (its state is the bit and the previous rung)
and names the timed variants Intractable.  PILOT reaches either value of each
variant's bit from cold and from the awkward states: a toggle or timed coil
whose rung is already held, a debounced bit that must drop, a flasher that must
go dark while its rung stays true.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung import (
    Bool,
    Counter,
    Program,
    Rung,
    Timer,
    count_up,
    debounce,
    flasher,
    latch,
    out,
    rise,
    timed,
    toggle,
)
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.prove import Counterexample, Proven, always
from pyrung.core.runner import PLC

T = Timer.clone("AT", count=4)


def _coil(verb: str, target: Bool) -> None:
    if verb == "toggle":
        toggle(target)
    elif verb == "debounce":
        debounce(target, on=(T[1], 30), off=(T[2], 20))
    elif verb == "flasher":
        flasher(target, on=(T[1], 30), off=(T[2], 20))
    elif verb == "timed":
        timed(target, T[1], 30)
    else:
        timed(target, T[1], 30, maintained=True)


def _assert_replays(program: Program, result: Any, tag: str) -> None:
    """Two oracles: the search found it, the concrete PLC reaches it."""
    assert isinstance(result, Counterexample), result
    plc = PLC(program, dt=0.010)
    for step in result.trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    assert plc.current_state.tags[tag] is True


def _program(verb: str) -> Program:
    btn, y, z = Bool("Btn", external=True), Bool("Y"), Bool("Z")
    with Program(strict=False) as logic:
        with Rung(btn):
            _coil(verb, y)
        with Rung(y):
            out(z)
    return logic


VERBS = ["toggle", "debounce", "flasher", "timed", "maintained"]


def _started(verb: str, presses: list[tuple[bool, int]]) -> PLC:
    plc = PLC(_program(verb), dt=0.010)
    for value, scans in presses:
        plc.patch({"Btn": value})
        for _ in range(scans):
            plc.step()
    return plc


def _reach(plc: PLC, target: Any, value: bool) -> None:
    path = pilot_how(plc, target == value if not value else target, max_scans=400)
    assert path.reachable, path.reason
    assert path.replay().state.tags[target.name] is value


@pytest.mark.parametrize("verb", VERBS)
def test_pilot_reaches_the_coil_and_its_reader_from_cold(verb: str) -> None:
    _reach(_started(verb, []), Bool("Z"), True)


@pytest.mark.parametrize(
    ("verb", "presses"),
    [
        # Pressed, released, pressed again: off, with the rung held.
        ("toggle", [(True, 1), (False, 1), (True, 1)]),
        # Timed out with the rung still held.
        ("timed", [(True, 10)]),
    ],
)
def test_pilot_makes_a_fresh_edge_when_the_rung_is_held(verb: str, presses) -> None:
    plc = _started(verb, presses)
    assert plc.current_state.tags["Y"] is False
    assert plc.current_state.tags["Btn"] is True
    _reach(plc, Bool("Y"), True)


@pytest.mark.parametrize("verb", VERBS)
def test_pilot_turns_the_coil_off(verb: str) -> None:
    plc = _started(verb, [(True, 1 if verb != "debounce" else 4)])
    assert plc.current_state.tags["Y"] is True
    _reach(plc, Bool("Y"), False)


@pytest.mark.parametrize("verb", ["toggle", "flasher", "timed", "maintained"])
def test_pilot_counts_the_coil_edges(verb: str) -> None:
    C = Counter.clone("AC")
    btn, y = Bool("Btn", external=True), Bool("Y")
    with Program(strict=False) as logic:
        with Rung(btn):
            _coil(verb, y)
        with Rung(rise(y)):
            count_up(C, 3).reset(Bool("Rst", external=True))

    path = pilot_how(PLC(logic, dt=0.010), C.Done, max_scans=600)
    assert path.reachable, path.reason
    assert path.replay().state.tags["AC_Acc"] == 3


@pytest.mark.xfail(
    strict=True,
    reason="PILOT's edge driver flips the input every scan, so a debounce never fires",
)
def test_pilot_counts_debounced_edges() -> None:
    C = Counter.clone("AC")
    btn, y = Bool("Btn", external=True), Bool("Y")
    with Program(strict=False) as logic:
        with Rung(btn):
            debounce(y, on=(T[1], 30), off=(T[2], 20))
        with Rung(rise(y)):
            count_up(C, 3).reset(Bool("Rst", external=True))

    path = pilot_how(PLC(logic, dt=0.010), C.Done, max_scans=600)
    assert path.reachable, path.reason


def test_advance_profile_channels_are_declared_writes() -> None:
    from pyrung.core.analysis.write_sites import (
        instruction_write_targets,
        static_write_target_names,
    )
    from pyrung.core.validation._common import walk_instructions

    for verb in VERBS:
        for instr in walk_instructions(_program(verb)):
            profile = instr.advance_profile()
            if profile is None:
                continue
            writes = {
                name
                for target in instruction_write_targets(instr)
                for name in static_write_target_names(target)
            }
            assert {tag.name for tag in profile.channels} <= writes


class TestProver:
    @staticmethod
    def _toggle_twice() -> tuple[Program, Bool]:
        a, lamp, seen, hit = Bool("A"), Bool("L"), Bool("M"), Bool("H")
        with Program(strict=False) as logic:
            with Rung(a):
                toggle(lamp)
            with Rung(lamp):
                latch(seen)
            with Rung(seen, ~lamp):
                out(hit)
        return logic, hit

    def test_toggle_is_modeled_exactly(self) -> None:
        logic, hit = self._toggle_twice()
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)

    def test_toggle_with_a_dead_rung_is_proven(self) -> None:
        a, lamp, hit = Bool("A"), Bool("L"), Bool("H")
        with Program(strict=False) as logic:
            with Rung(a, ~a):
                toggle(lamp)
            with Rung(lamp):
                out(hit)
        assert isinstance(always(logic, ~hit), Proven)

    @pytest.mark.parametrize("verb", ["debounce", "flasher", "timed", "maintained"])
    def test_timed_variants_are_black_box_timers(self, verb: str) -> None:
        """Their internal timer ticks by choice like any timer's: the search
        reaches Z and the concrete PLC replays the trace."""
        logic = _program(verb)
        result = always(logic, ~Bool("Z"))
        _assert_replays(logic, result, "Z")

    def test_declared_accumulator_domain_still_reaches_the_end(self) -> None:
        """A declared domain on the accumulator does not hide the end of the
        seconds-unit pulse: the tick that ends it is a choice, and the PLC
        replays it."""
        from pyrung import Field, Int, udt

        @udt()
        class DomTimer:
            Done: Bool  # noqa: F821
            Acc: Int = Field(min=0, max=5)  # noqa: F821

        y, hit = Bool("Y"), Bool("H")
        with Program(strict=False) as logic:
            with Rung(Bool("Btn")):
                timed(y, DomTimer, 1, "sec")
            with Rung(Bool("DomTimer_Done")):
                out(hit)
        result = always(logic, ~hit)
        _assert_replays(logic, result, "H")

    @pytest.mark.parametrize("verb", VERBS)
    def test_how_keeps_its_prover_context(self, verb: str) -> None:
        from pyrung.core.analysis.pilot.drive_setup import build_prover_context

        logic = _program(verb)
        plc = PLC(logic, dt=0.010)
        prover = build_prover_context(logic, dict(plc.state.tags))
        assert prover.nd_domains
        assert prover.key_config is not None

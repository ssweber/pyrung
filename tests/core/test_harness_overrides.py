"""Harness overrides: replace, cut or add couplings without touching the program."""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.core import PLC, Bool, Coupling, Harness, Program, Real, Rung, out
from pyrung.core.harness import link_problem
from pyrung.core.physical import Physical, Ramp

AUX = Physical("Aux", on_delay="20ms", off_delay="10ms")

Cmd = Bool("Cmd")
Motor = Bool("Motor")
Aux = Bool("Aux", physical=AUX, link="Motor")
Spare = Bool("Spare")
Running = Bool("Running")
Lamp = Bool("Lamp")
Temp = Real("Temp")
TempHigh = Bool("TempHigh")


def _plc() -> PLC:
    with Program() as logic:
        with Rung(Cmd):
            out(Motor)
        with Rung(Aux):
            out(Running)
        with Rung(Spare):
            out(Lamp)
        with Rung(Temp > 50.0):
            out(TempHigh)
    return PLC(logic, dt=0.010)


def _value(plc: PLC, tag: Any) -> object:
    return plc.current_state.tags.get(tag.name, tag.default)


def _installed(plc: PLC) -> Harness:
    harness = Harness(plc)
    harness.install()
    return harness


def _run_motor(plc: PLC, seconds: float = 0.1) -> None:
    plc.patch({Cmd: True})
    plc.run_for(seconds)


def test_declared_lists_program_couplings_before_overrides():
    harness = _installed(_plc())
    assert harness.declared() == (Coupling("Motor", "Aux", AUX),)
    harness.set_overrides({"Aux": None})
    assert harness.declared() == (Coupling("Motor", "Aux", AUX),)
    assert list(harness.couplings()) == []


def test_cut_leaves_the_feedback_a_free_input():
    plc = _plc()
    harness = _installed(plc)
    harness.set_overrides({"Aux": None})
    _run_motor(plc)
    assert _value(plc, Aux) is False

    plc.patch({Aux: True})
    plc.step()
    assert _value(plc, Aux) is True
    assert _value(plc, Running) is True


def test_replacement_runs_instead_of_the_declared_coupling():
    plc = _plc()
    harness = _installed(plc)
    slow = Coupling("Motor", "Aux", Physical("Aux", on_delay="200ms"))
    harness.set_overrides({"Aux": slow})
    assert list(harness.couplings()) == [slow]

    _run_motor(plc, 0.1)
    assert _value(plc, Aux) is False
    plc.run_for(0.15)
    assert _value(plc, Aux) is True


def test_added_coupling_drives_a_new_feedback():
    plc = _plc()
    harness = _installed(plc)
    harness.set_overrides({"Spare": Coupling("Motor", "Spare", Physical("Spare", on_delay="0ms"))})
    _run_motor(plc)
    assert _value(plc, Spare) is True
    assert _value(plc, Lamp) is True
    assert _value(plc, Aux) is True  # the declared coupling still runs


def test_analog_override_ramps_a_real_feedback():
    plc = _plc()
    harness = _installed(plc)
    heater = Coupling("Motor", "Temp", Physical("Temp", profile=Ramp(up=1000.0)))
    harness.set_overrides({"Temp": heater})
    _run_motor(plc, 0.2)
    assert _value(plc, TempHigh) is True


def test_clearing_overrides_restores_the_declared_wiring():
    plc = _plc()
    harness = _installed(plc)
    harness.set_overrides({"Aux": None})
    harness.set_overrides({})
    _run_motor(plc)
    assert _value(plc, Aux) is True


def test_unlink_is_a_cut_override():
    harness = _installed(_plc())
    harness.unlink(["Aux"])
    assert harness.overrides == {"Aux": None}


def test_override_must_drive_its_key():
    harness = _installed(_plc())
    with pytest.raises(ValueError, match="drives 'Spare'"):
        harness.set_overrides(
            {"Aux": Coupling("Motor", "Spare", Physical("Spare", on_delay="0ms"))}
        )


def test_fork_keeps_overrides():
    plc = _plc()
    harness = _installed(plc)
    harness.set_overrides({"Aux": None})
    forked = plc.fork()
    assert forked._harness is not None
    assert forked._harness.overrides == {"Aux": None}
    assert forked._harness.declared() == harness.declared()


def test_pilot_plans_with_the_overridden_wiring():
    from pyrung.core.analysis.pilot.physical import install_harness

    plc = _plc()
    harness = _installed(plc)
    harness.set_overrides(
        {"Aux": None, "Spare": Coupling("Motor", "Spare", Physical("Spare", on_delay="0ms"))}
    )
    work = plc.fork()
    assert install_harness(work) == frozenset({"Spare"})
    assert work._harness is not None
    assert work._harness.overrides == harness.overrides


def test_pending_while_the_dwell_counts():
    plc = _plc()
    harness = _installed(plc)
    plc.patch({Cmd: True})
    plc.step()
    plc.step()
    assert harness.pending("Aux") is True
    plc.run_for(0.1)
    assert harness.pending("Aux") is False


class TestLinkProblem:
    def test_valid_link(self):
        plc = _plc()
        assert (
            link_problem(plc, Coupling("Motor", "Spare", Physical("Spare", on_delay="1s"))) is None
        )

    def test_program_written_feedback_names_the_rung(self):
        plc = _plc()
        problem = link_problem(plc, Coupling("Cmd", "Lamp", Physical("Lamp", on_delay="1s")))
        assert problem == "Lamp is written by the program at Main:R3"

    def test_unknown_tags(self):
        plc = _plc()
        assert link_problem(plc, Coupling("Motor", "Nope", Physical("Nope", on_delay="1s"))) == (
            "unknown tag: Nope"
        )
        assert link_problem(plc, Coupling("Nope", "Spare", Physical("Spare", on_delay="1s"))) == (
            "unknown tag: Nope"
        )

    def test_feedback_type_must_suit_the_response(self):
        plc = _plc()
        assert "drive a bool feedback" in str(
            link_problem(plc, Coupling("Motor", "Temp", Physical("Temp", on_delay="1s")))
        )
        assert "drive a number" in str(
            link_problem(plc, Coupling("Motor", "Spare", Physical("Spare", profile=Ramp(up=1.0))))
        )

    def test_self_link(self):
        plc = _plc()
        assert link_problem(plc, Coupling("Spare", "Spare", Physical("Spare", on_delay="1s"))) == (
            "Spare cannot drive itself"
        )

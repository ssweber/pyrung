"""Tests for ``core.analysis.steerable`` — the neutral steerability predicate.

Pins the predicate's own terms (never-written input, ack-cleared command,
external nudge, program-authored) so a reader can see what steerable means
without reading pilot.
"""

from __future__ import annotations

from pyrung.core import PLC, Bool, Int, Program, Real, Rung, copy, fill, latch, out, reset
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.steerable import compute_clear_only, compute_steerable


def _steerable(program: Program) -> frozenset[str]:
    plc = PLC(program)
    return compute_steerable(build_program_graph(program), plc._known_tags_by_name, program)


def _clear_only(program: Program) -> frozenset[str]:
    plc = PLC(program)
    return compute_clear_only(build_program_graph(program), plc._known_tags_by_name, program)


class TestSteerable:
    def test_never_written_input_is_steerable(self) -> None:
        """A pure input: nothing writes it, so its value comes from outside."""
        FB = Bool("FB")
        Motor = Bool("Motor")
        prog = Program(strict=False)
        with prog:
            with Rung(FB):
                out(Motor)

        assert "FB" in _steerable(prog)

    def test_program_written_coil_is_not_steerable(self) -> None:
        """An out coil is authored by the ladder every scan."""
        FB = Bool("FB")
        Motor = Bool("Motor")
        prog = Program(strict=False)
        with prog:
            with Rung(FB):
                out(Motor)

        assert "Motor" not in _steerable(prog)

    def test_unread_declaration_is_not_a_lever(self) -> None:
        """Never written AND never read — a phantom, not an interface."""
        FB = Bool("FB")
        Motor = Bool("Motor")
        Unused = Bool("Unused")  # declared, never referenced in any rung
        prog = Program(strict=False)
        with prog:
            with Rung(FB):
                out(Motor)

        assert "Unused" not in _steerable(prog)
        assert Unused.name == "Unused"  # keep the declaration alive

    def test_ack_cleared_command_is_steerable_though_written(self) -> None:
        """The program only ever RESETS it; the operator supplies the active value."""
        C_Ack = Bool("C_Ack")
        Done = Bool("Done")
        prog = Program(strict=False)
        with prog:
            with Rung(C_Ack):
                latch(Done)
            with Rung(C_Ack):
                reset(C_Ack)  # ack: cleared, never asserted

        steer = _steerable(prog)
        assert "C_Ack" in steer, "ack-cleared command must be steerable despite the write"
        assert "C_Ack" in _clear_only(prog)

    def test_program_asserted_bit_is_not_clear_only(self) -> None:
        """A tag the program latches TRUE is authored by the program."""
        Trigger = Bool("Trigger")
        Flag = Bool("Flag")
        prog = Program(strict=False)
        with prog:
            with Rung(Trigger):
                latch(Flag)  # asserts the active value
            with Rung(~Trigger):
                reset(Flag)

        assert "Flag" not in _clear_only(prog)
        assert "Flag" not in _steerable(prog)

    def test_bulk_fill_reset_is_housekeeping_not_an_ack(self) -> None:
        """A whole-band ``fill(0, ...)`` is the program's own housekeeping."""
        from pyrung.click import ClickBlocks

        _x, _y, _c, _t, _ct, _sc, ds, *_rest = ClickBlocks()
        Trigger = Bool("Trigger")
        prog = Program(strict=False)
        with prog:
            with Rung(Trigger):
                fill(0, ds.select(201, 210))
            with Rung(ds[201] != 0):
                out(Bool("Alarm"))

        assert "ds201" not in _clear_only(prog)

    def test_external_nudged_register_is_steerable(self) -> None:
        """external=True + only conditional literal stamps: the operator's value persists."""
        Setpoint = Real("Setpoint", external=True)
        Reset = Bool("Reset")
        Hot = Bool("Hot")
        prog = Program(strict=False)
        with prog:
            with Rung(Reset):
                copy(0.0, Setpoint)  # a nudge, not authorship
            with Rung(Setpoint > 100.0):
                out(Hot)

        assert "Setpoint" in _steerable(prog)

    def test_steerable_is_type_independent(self) -> None:
        """Never-written inputs qualify at any type, not just Bool."""
        Contact = Bool("Contact")
        Count = Int("Count")
        Temp = Real("Temp")
        Alarm = Bool("Alarm")
        prog = Program(strict=False)
        with prog:
            with Rung(Contact, Count > 5, Temp > 1.5):
                out(Alarm)

        steer = _steerable(prog)
        assert {"Contact", "Count", "Temp"} <= steer

    def test_config_register_is_steerable_the_survey_must_narrow_itself(self) -> None:
        """A never-written config register IS steerable — pilot can force it.

        The hang survey must NOT treat this as "an operator will set it": it reads
        steerable as "the program cannot guarantee this", which is the point. Pinned
        because it is the exact fact that makes a config-gated timeout dead code.
        """
        EnableLimit = Int("EnableLimit")  # never written; defaults 0
        Acc = Int("Acc")
        Err = Bool("Err")
        prog = Program(strict=False)
        with prog:
            with Rung(Acc > 10, EnableLimit == 1):
                out(Err)

        assert "EnableLimit" in _steerable(prog)

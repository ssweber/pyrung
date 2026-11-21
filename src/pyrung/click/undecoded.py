"""The undecoded marker, which stands in for a CLICK rung that laddercodec could not decode.

laddercodec puts a marker rung in place of a rung that it cannot decode or write as CSV.
``ladder_to_pyrung()`` reads that rung as::

    from pyrung.click import undecoded

    comment("UNDECODED: rung 4, row 0: no CSV token holds this unknown instruction (40 bytes)")
    with rung():
        undecoded(
            "no CSV token holds this unknown instruction (40 bytes)",
            rung=4, row=0, column=31, stop="end",
        )

The marker stops its scope each time the scan reaches it. In the main program the scan
ends there (``stop="end"``), and a subroutine returns there (``stop="return"``). The program
leaves out the rungs that follow the marker in the same scope, because they never run.
CLICK cannot take the marker, so the export to CLICK and the CircuitPython code refuse it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from laddercodec import Undecoded, UndecodedStop

from pyrung.core._source import _capture_source
from pyrung.core.instruction import Instruction
from pyrung.core.program.context import Program, _require_rung_context
from pyrung.core.program.context import _state as _building_state
from pyrung.core.validation._common import rung_label

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.rung import Rung


class UndecodedInstruction(Instruction):
    """A marker that stops its scope, in place of a rung that laddercodec could not decode.

    ``marker`` is laddercodec's :class:`~laddercodec.Undecoded`. It says where decoding failed
    and why. The marker writes nothing. It is the last rung of its scope, so a no-op
    ``execute`` is enough to stop the scope.
    """

    INERT_WHEN_DISABLED = True
    ENDS_SCOPE = True
    _reads = ()
    _writes = ()
    _conditions = ()
    _structural_fields = ("error", "rung", "row", "column", "stop")

    def __init__(self, marker: Undecoded) -> None:
        self.marker = marker

    @property
    def error(self) -> str:
        """Why laddercodec could not decode the rung, on one line."""
        return self.marker.error

    @property
    def rung(self) -> int:
        """The index of the CLICK rung that the marker stands for, from 0."""
        return self.marker.rung

    @property
    def row(self) -> int | None:
        """The row of that rung where decoding failed, from 0, or None."""
        return self.marker.row

    @property
    def column(self) -> int | None:
        """The column where decoding failed, from 0 (31 is AF), or None."""
        return self.marker.column

    @property
    def stop(self) -> UndecodedStop:
        """``"end"`` in the main program, ``"return"`` in a subroutine."""
        return self.marker.stop

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """Do nothing. No rung follows the marker in its scope."""

    def is_terminal(self) -> bool:
        """The marker must be the only instruction in its rung."""
        return True

    def reason(self, at: str | None = None) -> str:
        """Return why the marker is there, in two sentences.

        The first names the rung as CLICK numbers it, from 1. The second names *at*, the
        marker's place in pyrung, when it is given. The two numbers can differ, because the
        importer splits a CLICK rung that holds several wires into several pyrung rungs.
        """
        error = self.error.rstrip()
        if not error.endswith((".", "!", "?")):
            error += "."
        scope = "the main program" if self.stop == "end" else "its subroutine"
        where = f" at {at} in pyrung" if at else ""
        return (
            f"laddercodec could not decode CLICK rung {self.rung + 1} of {scope}: {error} "
            f"undecoded() stands in for that rung{where}."
        )

    def effect(self) -> str:
        """Return what the marker does to the scan, as a sentence."""
        if self.stop == "end":
            return "Each scan ends at this rung, so no rung after it in main runs."
        return "The subroutine returns at this rung, so no rung after it runs."


#: What to do about a marker. CLICK cannot take one, because it has no binary form.
FIX_TEXT = "CLICK cannot take undecoded(). Write the rung's logic in its place, or remove the rung."


def undecoded(
    error: str,
    *,
    rung: int,
    row: int | None = None,
    column: int | None = None,
    stop: UndecodedStop,
) -> None:
    """Stop the scope here, in place of a CLICK rung that laddercodec could not decode.

    ``ladder_to_pyrung()`` writes this call for laddercodec's marker rung. Each argument is
    a field of :class:`laddercodec.Undecoded`. *error* says why decoding failed. *rung*,
    *row* and *column* say where, each counted from 0. Pass ``stop="end"`` in the main
    program and ``stop="return"`` in a subroutine.

    The call must be the only thing in a rung with no conditions, outside a forloop. The
    rungs after it in the same scope are left out of the program, because no scan reaches
    them.

    Raises:
        ValueError: If laddercodec refuses a field, such as a blank *error*.
        RuntimeError: If the call is not alone in a rung of its own with no conditions,
            or if *stop* does not match the scope.
    """
    ctx = _require_rung_context("undecoded")
    ctx._assert_no_pending_required_builder("undecoded")
    program = Program._current()
    if program is None:
        raise RuntimeError("undecoded() must be used inside a Program.")
    logic = ctx._rung
    # A rung() context sets _loop, to None outside a forloop. The context of a branch()
    # or a forloop() capture has no _loop at all.
    alone = (
        getattr(ctx, "_loop", True) is None
        and not _building_state._building.forloops
        and not logic._use_prior_snapshot
        and not logic._conditions
        and not logic._instructions
        and not logic._branches
    )
    if not alone:
        raise RuntimeError(
            "undecoded() must be the only thing in a rung with no conditions, outside a "
            "forloop. Write it as: with rung(): undecoded(...)"
        )
    marker = Undecoded(error, rung, row, column, stop)
    expected = "end" if program._current_subroutine is None else "return"
    if marker.stop != expected:
        where = "the main program" if expected == "end" else "a subroutine"
        raise RuntimeError(
            f"undecoded(stop={marker.stop!r}) cannot be in {where}. Pass stop={expected!r} there."
        )
    source_file, source_line = _capture_source(depth=2)
    instruction = UndecodedInstruction(marker)
    instruction.source_file, instruction.source_line = source_file, source_line
    logic.add_instruction(instruction)


def undecoded_rungs(program: Program) -> Iterator[tuple[str | None, int, UndecodedInstruction]]:
    """Yield ``(subroutine, rung_index, marker)`` for each marker in *program*.

    *subroutine* is None for the main program. *rung_index* counts from 0.
    """
    scopes: list[tuple[str | None, list[Rung]]] = [(None, program.rungs)]
    scopes.extend((name, program.subroutines[name]) for name in sorted(program.subroutines))
    for subroutine, rungs in scopes:
        for index, rung in enumerate(rungs):
            for instruction in rung._instructions:
                if isinstance(instruction, UndecodedInstruction):
                    yield subroutine, index, instruction


def undecoded_notice(program: Program) -> str:
    """Return one line for each marker in *program*, or ``""`` if it has none.

    Each line names the rung, says why the marker is there and says what it does to the
    scan.
    """
    return "\n".join(
        f"{marker.reason(rung_label(subroutine, index))} {marker.effect()}"
        for subroutine, index, marker in undecoded_rungs(program)
    )

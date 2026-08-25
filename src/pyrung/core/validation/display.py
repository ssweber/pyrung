"""Presentation-ready view of a validation finding, modelled on a compiler diagnostic.

Every finding renders the way ``rustc``/``cargo`` report an error: one or more
*frames* — a ``--> location`` line and the offending code as written, framed by a
``|`` rail — with a *caret* underlining the exact token and a short label riding on
it (the problem, in a few words).  A ``= hint:`` line offers the fix::

     --> Blower:R5
      |
      |  with rung(Blower_Limit_Ts >= Blower_tmr_Acc, Blower_EnableLimit == 1):
      |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ true at reset
      |
      = hint: use Blower_tmr_Acc >= Blower_Limit_Ts

The caret *label* carries the problem in short form, so it is never restated below.
When a finding has no single offending token — a contradiction between two disjoint
terms, or one shared cause across several sites — a ``problem`` line leads instead,
and the per-site carets go unlabelled::

    Motor is set by multiple instructions in one scan.
     --> Main:R3
      |  with rung(Start):
      |      out(Motor)
      |          ^^^^^
     --> Main:R4
      |  with rung(~Start):
      |      out(Motor)
      |          ^^^^^
      = hint: only one should drive it — gate them exclusively

:class:`FindingDisplay` makes that structure the source of truth: validators build it
directly, and the classic flat ``message`` is derived via :meth:`FindingDisplay.as_text`.
A UI reads ``frames`` / ``problem`` / ``hint`` and never parses a string.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyrung.core.validation.severity import Severity


@dataclass(frozen=True)
class Frame:
    """One diagnostic frame: a location and the offending code beneath it.

    ``lines`` are the code lines as written (a ``with rung(...):`` header, an indented
    instruction, or a bare instruction); empty for a tag-scoped finding whose only
    anchor is the location.  ``caret`` is ``(line_index, col, length)`` — the span
    underlined within ``lines[line_index]`` — or ``None``.  ``caret_label`` is the short
    problem riding on the caret (``^^^ true at reset``); empty for an unlabelled caret.
    ``col`` is measured against the line's own text (internal indentation included).
    """

    location: str
    lines: tuple[str, ...] = ()
    caret: tuple[int, int, int] | None = None
    caret_label: str = ""
    # Optional codegen provenance (``<analysis>:7076`` / ``line 12``).  Internal.
    source: str = ""


def frame(
    location: str,
    lines: tuple[str, ...] = (),
    caret: tuple[int, int, int] | None = None,
    caret_label: str = "",
    source: str = "",
) -> Frame:
    return Frame(
        location=location, lines=lines, caret=caret, caret_label=caret_label, source=source
    )


@dataclass(frozen=True)
class FindingDisplay:
    """Everything a UI needs to render a finding — no string parsing required.

    ``problem`` is a shared lead line, used when there is no single offending token to
    label (multi-site findings, or a contradiction).  ``hint`` is the fix.
    """

    code: str
    severity: Severity
    frames: tuple[Frame, ...] = field(default_factory=tuple)
    problem: str = ""
    hint: str = ""

    def as_text(self) -> str:
        """The classic flat ``message`` string, derived from the structure."""
        out: list[str] = []
        if self.problem:
            out.append(f"  {self.problem}")
        for fr in self.frames:
            out.append(f" --> {fr.location}")
            if fr.lines:
                out.append("  |")
                for i, line in enumerate(fr.lines):
                    out.append(f"  |  {line}")
                    if fr.caret is not None and fr.caret[0] == i:
                        _, col, length = fr.caret
                        label = f" {fr.caret_label}" if fr.caret_label else ""
                        out.append(f"  |  {' ' * col}{'^' * length}{label}")
                out.append("  |")
        if self.hint:
            out.append(f"  = hint: {self.hint}")
        return "\n".join(out)

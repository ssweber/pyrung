"""STEP_NO_ESCAPE: wait-shaped steps that can hang forever.

A step whose only advance out of value ``k`` is gated on an external input, with
no timeout or error rung that can fire while the machine waits.  The input may
never arrive; nothing else moves the step; the machine sits there looking fine.

**warning**, and it stays a warning: this is a design decision, not a provable
defect.  A wait with no escape is legitimate when a supervisor upstream owns the
timeout, so the rule reports the shape and leaves the call to the engineer.  It
is dialect-agnostic — a step with no escape hangs the same on Click and on P1AM —
which is why it lives here rather than behind a ``CLK_``/``CPY_`` prefix.

The diagnostic leads with the *near-misses*, because the rule earns its keep on
programs that look covered.  An escape rung written for the wrong step, or one
switched off by a config register nothing writes, both read as "there's a
timeout, we're fine" to someone skimming the ladder.  Each gets its own frame
showing the rung as written with the killing clause underlined, so the claim is
visible rather than asserted.

The analysis is :func:`pyrung.core.analysis.query.wait_edges_without_escape` —
static and read-side, needing no scan history.  It fails closed: an escape guard
it cannot read statically counts as a possible escape, so this goes quiet rather
than inventing a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.validation.display import FindingDisplay, Frame
from pyrung.core.validation.render import caret_of, render_condition, with_rung_line
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from pyrung.core.analysis.query import WaitEscapeFinding
    from pyrung.core.program import Program

STEP_NO_ESCAPE = "STEP_NO_ESCAPE"


def _frame(location: str, conditions: tuple[Any, ...], guard: Any | None, label: str) -> Frame:
    """One rung as written, with *guard* underlined and *label* riding on it.

    Both renders go through the same ``render.py`` pass, so the token is found
    by construction.  A guard the survey could not recover leaves the frame
    caret-less — the line still shows, unannotated.
    """
    header = with_rung_line(conditions)
    span = caret_of(header, render_condition(guard)) if guard is not None else None
    return Frame(
        location=location,
        lines=(header,),
        caret=(0, span[0], span[1]) if span else None,
        caret_label=label if span else "",
    )


def _frames(finding: WaitEscapeFinding) -> tuple[Frame, ...]:
    """The waiting rung, then each escape that looked like coverage."""
    scope = "" if finding.subroutine is None else f"{finding.subroutine}:"
    frames = [
        _frame(
            f"{scope}{finding.advance_rung}",
            finding.advance_conditions,
            finding.wait_guard,
            "external, no fallback",
        )
    ]
    for esc in finding.ranged_escapes:
        frames.append(
            _frame(
                f"{scope}{esc.rung_label}",
                esc.conditions,
                esc.guard,
                f"excludes step {finding.step_value}",
            )
        )
    for esc in finding.unmet_escapes:
        frames.append(
            _frame(
                f"{scope}{esc.rung_label}",
                esc.conditions,
                esc.guard,
                f"nothing sets this; rests at {esc.resting}",
            )
        )
    return tuple(frames)


def _hint(finding: WaitEscapeFinding) -> str:
    """The repair, named against whichever escape failed to fire.

    Each remedy points at a rung rather than prescribing a value.  "Set
    ``EnableLimit = 1``" is the right advice for a config register and terrible
    advice for a button (it reads as *wire it permanently on*), and the survey
    cannot tell those apart — see :func:`~pyrung.core.analysis.query._unmet_atom`.
    Naming the rung that cannot fire is true either way, and the frame above
    already shows what it is waiting on.

    With no ranged or unmet escape there is no rung to repair — the step simply
    has none — so the hint asks for one instead of guessing at a design.
    """
    remedies: list[str] = []
    for esc in finding.ranged_escapes:
        remedies.append(f"widen {esc.rung_label} to cover step {finding.step_value}")
    for esc in finding.unmet_escapes:
        remedies.append(f"make {esc.rung_label} fire on its own (nothing sets {esc.tag})")
    if not remedies:
        return f"give step {finding.step_value} a timeout, or confirm a supervisor upstream owns it"
    return "no escape fires on its own: " + ", or ".join(remedies)


def _display(finding: WaitEscapeFinding) -> FindingDisplay:
    return FindingDisplay(
        code=STEP_NO_ESCAPE,
        severity="warning",
        frames=_frames(finding),
        hint=_hint(finding),
    )


@dataclass(frozen=True)
class StepEscapeFinding:
    """One wait-shaped step with no fireable escape."""

    code: str
    target_name: str
    display: FindingDisplay
    severity: Severity

    @property
    def message(self) -> str:
        return self.display.as_text()


@dataclass(frozen=True)
class StepEscapeReport:
    findings: tuple[StepEscapeFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No wait-escape findings."
        return f"{len(self.findings)} wait-escape finding(s)."


def validate_wait_escapes(program: Program) -> StepEscapeReport:
    """Validate that wait-shaped steps have a fireable escape (STEP_NO_ESCAPE)."""
    from pyrung.core.analysis.query import wait_edges_without_escape

    return StepEscapeReport(
        findings=tuple(
            StepEscapeFinding(
                code=STEP_NO_ESCAPE,
                target_name=finding.location,
                display=_display(finding),
                severity="warning",
            )
            for finding in wait_edges_without_escape(program)
        )
    )

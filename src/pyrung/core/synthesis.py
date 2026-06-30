"""Synthesis overlay — feedback couplings and PILOT holds as bracketing rungs.

The soft-exec runner brackets one scan with synthesis rungs::

    holds (pre)  →  user rungs  →  plant (post)

``holds`` steer inputs *before* the program reads them (PILOT's holds — the input
vector the program sees this scan); ``plant`` synthesizes feedback *after* the
program settles its commands (the harness couplings — the plant responding to the
scan's outputs, visible to the program next scan).  Both are ordinary
:class:`~pyrung.core.rung.Rung` objects built programmatically here, so the
reader, fold, compile, and causal subsystems consume them with no special case —
synthesis *is* rungs.

Synthesis lives on the soft-exec :class:`~pyrung.core.runner.PLC` only (see
``PLC._synthesis``); it is **never** part of the user :class:`Program`, so deploy
(Click ladder / CircuitPython codegen) and the ``prove`` verifier — which walk the
user program's rungs and subroutines — never see it.  That is the structural
"two roots" discipline: the brackets are a property of the soft fork, not of the
program.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.rung import Rung

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.tag import Tag


@dataclass
class Synthesis:
    """The two bracketing rung lists the runner scans around the user program.

    ``holds`` run before user logic (pre-scan input steering); ``plant`` runs
    after (post-scan feedback).  An empty ``Synthesis`` is equivalent to no
    overlay — the runner skips the bracket entirely (zero overhead).
    """

    holds: list[Rung] = field(default_factory=list)
    plant: list[Rung] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.holds and not self.plant

    def all_rungs(self) -> Iterator[Rung]:
        """Yield every synthesis rung (holds then plant) — for fold/compile walks."""
        yield from self.holds
        yield from self.plant


def _rung_holding(condition: Condition | Tag | None, instruction: Any) -> Rung:
    """Build a one-instruction rung; unconditional when *condition* is ``None``.

    The rung carries no source location — synthesis rungs are not user code, so
    they never anchor a debugger step or a source-line report.
    """
    rung = Rung() if condition is None else Rung(condition)
    rung.add_instruction(instruction)
    return rung


def bool_feedback_rungs(
    *,
    enable: Condition,
    fb_tag: Tag,
    ton_done: Tag,
    ton_acc: Tag,
    tof_acc: Tag,
    on_delay_ms: int,
    off_delay_ms: int,
) -> list[Rung]:
    """Lower a bool coupling to a real on-delay/off-delay timer pair (dwell).

    Two plant rungs, exactly what a hand-written ladder would use::

        Rung(enable):   on_delay(ton_done, ton_acc, on_delay_ms)
        Rung(ton_done): off_delay(fb_tag,  tof_acc, off_delay_ms)

    ``enable`` is sustained for ``on_delay`` before ``ton_done`` rises; feeding
    ``ton_done`` through the off-delay keeps ``fb_tag`` asserted for
    ``off_delay`` after the enable drops.  A glitch shorter than ``on_delay``
    resets the accumulator, so feedback that was never sustained is never
    fabricated — the dwell semantics, now native (no transport-delay heap).
    Presets are in ms with a ms accumulator unit, so a held enable crosses after
    ``ceil(delay_ms / dt_ms)`` scans.
    """
    from pyrung.core.condition import BitCondition
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction

    ton = OnDelayInstruction(ton_done, ton_acc, on_delay_ms, enable, unit="Tms")
    ton_power = BitCondition(ton_done)
    tof = OffDelayInstruction(fb_tag, tof_acc, off_delay_ms, ton_power, unit="Tms")
    return [_rung_holding(enable, ton), _rung_holding(ton_power, tof)]


def copy_hold_rung(
    *,
    value: Any,
    dest: Tag,
    guard: Condition | Tag | None = None,
) -> Rung:
    """Build a hold rung that copies *value* into *dest*.

    ``guard=None`` is a steady hold (drive every scan); a ``guard`` makes it a
    self-releasing hold — drive ``dest`` to ``value`` only while the guard holds
    (the conditional-hold / reactive-re-assert shape).
    """
    from pyrung.core.instruction.data_transfer import CopyInstruction

    return _rung_holding(guard, CopyInstruction(value, dest))


def function_rung(
    fn: Any,
    *,
    ins: dict[str, Any],
    outs: dict[str, Any],
    guard: Condition | Tag | None = None,
) -> Rung:
    """Build a guarded rung that runs an opaque ``fn`` with declared ins/outs.

    The ``ins``/``outs`` are declared, so the dataflow stays visible to PDG /
    fold / trace / causal even though the body is opaque (the analog profile, a
    sandbox probe).  ``guard=None`` runs every scan; a guard arms it (e.g. an
    analog coupling that ticks only while its enable has activated it).
    """
    from pyrung.core.instruction.control import FunctionCallInstruction

    return _rung_holding(guard, FunctionCallInstruction(fn, ins=ins, outs=outs))

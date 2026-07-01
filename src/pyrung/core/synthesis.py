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


def analog_feedback_rungs(
    *,
    enable: Condition,
    disable: Condition,
    fb_tag: Tag,
    armed: Tag,
    dt_tag: Tag,
    up: float,
    down: float,
) -> list[Rung]:
    """Lower a linear analog coupling (a :class:`~pyrung.core.physical.Ramp`) to plant rungs.

    Rates are per **second**, applied against ``dt_tag`` (``sys.dt``), so the ramp
    is stable across scan periods *and folds for free*: ``dt`` already carries the
    macro-skip count, so ``fb += up*dt`` advances by the full N-scan amount in a
    single folded scan — no accumulator special-casing.  The rungs are exactly
    what a hand-written plant-sim ladder would use::

        Rung(enable):            calc(fb + up*dt,   fb)   # advance while energized
        Rung(enable):            latch(armed)             # wake on first energize
        Rung(armed AND disable): calc(fb + down*dt, fb)   # decay once armed, off-enable only

    ``disable`` is the caller-supplied negation of ``enable`` (the harness knows
    whether the enable is a bit or an ``== trigger`` compare, so it builds the
    exact complement).  ``down == 0`` means "hold on enable fall" — no decay, so
    the arm latch and decay rung are omitted (nothing to gate) and only the
    advance rung is built.  The ``armed`` latch is the rung-native replacement for
    the old enable-edge ``active`` monitor: a plant sits at rest until first
    energized, and unlike a monitor it recomputes deterministically under replay.
    """
    from pyrung.core.condition import AllCondition, BitCondition
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.coils import LatchInstruction

    rungs = [_rung_holding(enable, CalcInstruction(fb_tag + up * dt_tag, fb_tag))]
    if down != 0.0:
        rungs.append(_rung_holding(enable, LatchInstruction(armed)))
        decay_guard = AllCondition(BitCondition(armed), disable)
        rungs.append(_rung_holding(decay_guard, CalcInstruction(fb_tag + down * dt_tag, fb_tag)))
    return rungs


def analog_approach_rung(
    *,
    enable: Condition,
    fb_tag: Tag,
    toward: Tag | float,
    rate: float,
    dt_tag: Tag,
) -> list[Rung]:
    """Lower a first-order analog coupling (an :class:`~pyrung.core.physical.Approach`).

    One guarded ``calc`` plant rung — ``fb += rate*(toward - fb)*dt`` while
    energized, holding on enable fall::

        Rung(enable): calc(fb + rate*(toward - fb)*dt, fb)

    ``toward`` is a constant setpoint or a setpoint tag.  There is no decay/arm:
    the plant holds its last value when disabled (a first-order lag has no
    ambient drift term), so a never-energized plant simply stays at rest.  The
    slope depends on ``fb`` itself, so ``how()`` measures the coast empirically.
    """
    from pyrung.core.instruction.calc import CalcInstruction

    expr = fb_tag + rate * (toward - fb_tag) * dt_tag
    return [_rung_holding(enable, CalcInstruction(expr, fb_tag))]


def pulse_feedback_rungs(
    *,
    enable: Condition,
    disable: Condition,
    fb_tag: Tag,
    on_done: Tag,
    on_acc: Tag,
    off_done: Tag,
    off_acc: Tag,
    on_dwell_ms: int,
    off_dwell_ms: int,
) -> list[Rung]:
    """Lower a bool pulse-train coupling (a :class:`~pyrung.core.physical.Pulse`).

    An astable oscillator in plant rungs — the classic ladder "flasher".  While
    enabled, ``fb`` cycles high for ``on_dwell`` then low for ``off_dwell``; when
    disabled it rests low::

        Rung(enable & fb):             on_delay(on_done,  on_acc,  on_dwell)   # high dwell
        Rung(enable & ~fb):            on_delay(off_done, off_acc, off_dwell)  # low dwell
        Rung(enable & ~fb & off_done): latch(fb)   # low long enough → go high
        Rung(enable & fb & on_done):   reset(fb)   # high long enough → go low
        Rung(~enable):                 reset(fb)   # disabled → low

    Each dwell timer is a real on-delay that auto-resets when its guard drops
    (``fb`` flips), so the two ping-pong.  Guards read the rung-entry snapshot, so
    a half-cycle costs one scan of slop — inconsequential for a pulse train.
    Presets are ms with a ms accumulator, so a dwell lasts ``ceil(dwell_ms/dt_ms)``
    scans.
    """
    from pyrung.core.condition import AllCondition, BitCondition
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
    from pyrung.core.instruction.timers import OnDelayInstruction

    fb_high = BitCondition(fb_tag)
    fb_low = ~fb_tag
    on_guard = AllCondition(enable, fb_high)
    off_guard = AllCondition(enable, fb_low)
    ton_high = OnDelayInstruction(on_done, on_acc, on_dwell_ms, on_guard, unit="Tms")
    ton_low = OnDelayInstruction(off_done, off_acc, off_dwell_ms, off_guard, unit="Tms")
    return [
        _rung_holding(on_guard, ton_high),
        _rung_holding(off_guard, ton_low),
        _rung_holding(
            AllCondition(enable, fb_low, BitCondition(off_done)), LatchInstruction(fb_tag)
        ),
        _rung_holding(
            AllCondition(enable, fb_high, BitCondition(on_done)), ResetInstruction(fb_tag)
        ),
        _rung_holding(disable, ResetInstruction(fb_tag)),
    ]


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


def conditional_hold_rung(
    *,
    dest: Tag,
    rules: list[tuple[Any, Condition | Tag | None]],
) -> Rung:
    """Build a multi-branch hold: one parallel branch per ``(value, guard)`` rule.

    Each branch copies ``value`` into ``dest`` while its ``guard`` holds.  Because
    every branch's guard evaluates against the **rung-entry snapshot** (the frozen
    ``ConditionView``), mutually-exclusive rules — a liveness oscillator's "drive
    True while ``dest != True``" + "drive False while ``dest != False``" — stay
    exclusive with no mid-scan chaining (branch 1's write is invisible to branch
    2's guard).  It is an ordinary ladder rung, so it compiles natively (no
    io-gap) and folds like any other.

    A single rule is better expressed by :func:`copy_hold_rung`; this is for the
    multi-rule (oscillating) case.
    """
    outer = Rung()
    for value, guard in rules:
        outer.add_branch(copy_hold_rung(value=value, dest=dest, guard=guard))
    return outer


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

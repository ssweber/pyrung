"""Uniform structural profile for accumulating instructions.

An *accumulating* instruction advances a register every scan its ``advance``
condition holds, until the register crosses a threshold — the ``done`` bit
(accumulator vs ``preset``) or any ``Acc <cmp> target`` comparison a downstream
rung reads.  Timers, counters, and (later) drums all share this shape.

``Instruction.accumulating_profile()`` returns one of these so PILOT — and, in
time, the causal layer — can reason about *"a held input is driving this
accumulator to completion"* without special-casing every instruction type.

The ``KIND_*`` strings match the prover's vocabulary in
``core/analysis/prove/absorb.py`` (``_DONE_KIND_*``); keep them in sync.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.tag import Tag

KIND_ON_DELAY = "on_delay"
KIND_OFF_DELAY = "off_delay"
KIND_COUNT_UP = "count_up"
KIND_COUNT_DOWN = "count_down"
# Analog harness coupling (En drives a sensor register toward a read threshold).
# Unlike the others this kind has NO done bit and never feeds the prover's
# done-abstraction — it only labels the profile.
KIND_APPROACH = "approach"


@dataclass(frozen=True)
class _NoDone:
    """Sentinel ``done`` for profiles with no latching bit — an analog harness
    coupling, where a sensor register ramps and nothing latches.

    Carries a synthetic ``name`` so consumers that read ``profile.done.name``
    never collide with a real tag; such a profile is matched via its
    ``accumulator`` instead (``via_done=False``).
    """

    name: str
    default: bool = False


@dataclass(frozen=True)
class AccProfile:
    """How an accumulating instruction advances toward a threshold.

    Attributes:
        kind: One of the ``KIND_*`` strings.
        advance: The ``Condition`` whose holding drives the accumulator.
        advance_value: The boolean value of ``advance`` that *advances* the
            accumulator — ``True`` for on-delay timers and counters, ``False``
            for an off-delay (it accumulates while its rung is *not* powered).
        accumulator: The register that advances (``Acc`` / ``current_step``).
        done: The bit that latches when the accumulator crosses ``preset``.
        timing: The *timing* status bit — True while the accumulator is actively
            advancing toward ``preset`` (a timer's ``TT`` bit).  ``None`` when the
            instruction has no timing bit (counters, or a timer configured without
            one) — consumers that fast-forward past a dwell fall back to watching
            the accumulator/done directly.
        preset: Target magnitude (``Tag`` or ``int``); resolve against state.
        reset: The reset ``Condition`` (or ``None`` when the instruction has
            none).
        direction: ``+1`` counts up toward ``+preset``, ``-1`` counts down
            toward ``-preset``.
        rate_per_scan: Positive units the accumulator moves per advancing scan
            given ``dt`` (timers depend on ``dt``; counters move ``1``/scan).
    """

    kind: str
    advance: Any
    advance_value: bool
    accumulator: Tag
    done: Tag | _NoDone
    timing: Tag | None
    preset: Tag | int
    reset: Any | None
    direction: int
    rate_per_scan: Callable[[float], float]

    def done_target(self, preset_value: int) -> int:
        """Accumulator value at which ``done`` latches for a resolved preset."""
        return self.direction * abs(int(preset_value))

    def scans_until(self, target: int, *, acc_now: int, dt: float) -> int | None:
        """Held-``advance`` scans until the accumulator crosses ``target``.

        Returns ``0`` when the accumulator has already crossed, a positive
        estimate when the crossing is analytic, or ``None`` when it cannot be
        computed statically — the caller then measures it empirically (PILOT's
        Tier 2 fallback: fork, hold ``advance``, run until the crossing).
        """
        try:
            rate = abs(float(self.rate_per_scan(dt)))
        except Exception:  # noqa: BLE001 — any rate failure → measure empirically
            return None
        if rate == 0.0:
            return None
        crossed = acc_now >= target if self.direction > 0 else acc_now <= target
        if crossed:
            return 0
        return max(1, int(math.ceil(abs(float(target) - float(acc_now)) / rate)))

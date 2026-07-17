"""Resolve accumulating instructions and their next relevant crossing.

Given an observed consumer or held driver, this module finds the corresponding
``AccProfile`` and target threshold. ``scans_to_eject`` computes the crossing
distance analytically when the profile supports it; ``measure_scans`` uses a
bounded forked run when it does not.

No profile or unresolvable threshold produces no estimate rather than a guessed
crossing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.validation._common import walk_instructions

if TYPE_CHECKING:
    from pyrung.core.instruction.accumulating import AccProfile
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_DEFAULT_DT = 0.01
_MEASURE_BUDGET = 2000


@dataclass(frozen=True)
class AccumulatorMatch:
    """An accumulating instruction whose output a consumer tag reads."""

    profile: AccProfile
    instr: Any
    via_done: bool  # True if the consumer read the done bit; False if the Acc register


def iter_profiles(program: Any, harness: Any = None) -> Iterator[tuple[AccProfile, Any]]:
    """Yield ``(profile, instruction)`` for every accumulating instruction.

    When *harness* is given, also yield each analog coupling's
    :class:`AccProfile` (with ``instruction`` ``None`` — a coupling has no owning
    instruction).  Default ``None`` preserves the program-only behaviour for
    every existing caller.
    """
    for instr in walk_instructions(program):
        profile = instr.accumulating_profile()
        if profile is not None:
            yield profile, instr
    if harness is not None:
        for profile in harness.coupling_profiles():
            yield profile, None


def resolve_profile(
    consumer_tag: str, program: Any, harness: Any = None
) -> AccumulatorMatch | None:
    """The accumulating instruction (or analog coupling) whose ``done`` bit or
    ``accumulator`` register *consumer_tag* reads, or ``None`` when none owns it.

    Generalizes ``walk/rules.py::_timer_instruction_for_done`` — no
    ``isinstance`` gate; any instruction with an ``accumulating_profile()``
    qualifies, matched on either its done bit (a ``Done`` consumer) or its
    accumulator (an ``Acc <cmp> target`` consumer).  With *harness*, an analog
    coupling's Fb register is matched the same way (via accumulator).
    """
    for profile, instr in iter_profiles(program, harness):
        done_name = getattr(profile.done, "name", None)
        acc_name = getattr(profile.accumulator, "name", None)
        if consumer_tag == done_name:
            return AccumulatorMatch(profile, instr, via_done=True)
        if consumer_tag == acc_name:
            return AccumulatorMatch(profile, instr, via_done=False)
    return None


def _resolve_int(value: Any, plc: PLC) -> int | None:
    """Resolve a preset/operand (``Tag`` or literal) to an int against state."""
    name = getattr(value, "name", None)
    if name is not None:
        value = plc.state.tags.get(name, getattr(value, "default", None))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def eject_target(match: AccumulatorMatch, plc: PLC, *, threshold: int | None = None) -> int | None:
    """The accumulator value whose crossing ejected PILOT.

    ``done_target(preset)`` for a ``Done`` consumer; the comparison *threshold*
    for an ``Acc <cmp> threshold`` consumer.
    """
    if not match.via_done and threshold is not None:
        return threshold
    preset_value = _resolve_int(match.profile.preset, plc)
    if preset_value is None:
        return None
    return match.profile.done_target(preset_value)


def _crossed(snap: Any, profile: AccProfile, target: int) -> bool:
    acc = snap.get(profile.accumulator.name)
    if acc is None:
        return False
    return acc >= target if profile.direction > 0 else acc <= target


def scans_to_eject(
    match: AccumulatorMatch,
    plc: PLC,
    *,
    threshold: int | None = None,
    fork: PLC | None = None,
    budget: int = _MEASURE_BUDGET,
) -> int | None:
    """Held-advance scans until *match* crosses its ejecting threshold.

    Tier 1: analytic from the profile.  Tier 2: if analytic returns ``None`` and
    a held *fork* is supplied, measure empirically.  ``None`` when neither path
    can answer.
    """
    target = eject_target(match, plc, threshold=threshold)
    if target is None:
        return None
    acc_now = _resolve_int(match.profile.accumulator, plc) or 0
    dt = float(getattr(plc, "_dt", _DEFAULT_DT) or _DEFAULT_DT)
    analytic = match.profile.scans_until(target, acc_now=acc_now, dt=dt)
    if analytic is not None:
        return analytic
    if fork is not None:
        return measure_scans(match, target, fork, budget=budget)
    return None


def measure_scans(
    match: AccumulatorMatch,
    target: int,
    fork: PLC,
    *,
    budget: int = _MEASURE_BUDGET,
) -> int | None:
    """Tier 2 fallback: run a held *fork* until the accumulator crosses *target*.

    *fork* must already have the advance condition pinned (so the accumulator
    actually advances).  Returns the scans consumed, ``0`` if already crossed, or
    ``None`` if it never crosses within *budget*.
    """
    profile = match.profile
    if _crossed(fork.state.tags, profile, target):
        return 0
    start = fork.state.scan_id
    fork.run_until(
        lambda s: _crossed(s.tags, profile, target),
        max_cycles=budget,
        fold=True,
    )
    if not _crossed(fork.state.tags, profile, target):
        return None
    return fork.state.scan_id - start

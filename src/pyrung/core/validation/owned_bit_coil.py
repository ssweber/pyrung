"""Refuse coils that write a bit a timer or counter owns.

The Done bit of ``on_delay``, ``off_delay``, ``count_up`` and ``count_down`` is
rewritten by that instruction on every execution, so an ``out``, ``latch``,
``reset`` or ``toggle`` on it is a silent trap (and CLICK has no coil on a
timer or counter bit at all).  The instruction's own ``.reset()`` pin is the
way to clear it.  This runs when a ``Program`` scope closes.

Ranges resolved only at runtime (pointer-indexed block ranges) are not checked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.validation._common import walk_instructions

if TYPE_CHECKING:
    from pyrung.core.program import Program

OWNED_BIT_COIL = "OWNED_BIT_COIL"

_OWNERS = {
    "OnDelayInstruction": "on_delay",
    "OffDelayInstruction": "off_delay",
    "CountUpInstruction": "count_up",
    "CountDownInstruction": "count_down",
}
_COILS = {
    "OutInstruction": "out",
    "LatchInstruction": "latch",
    "ResetInstruction": "reset",
    "ToggleInstruction": "toggle",
    "DebounceInstruction": "debounce",
    "FlasherInstruction": "flasher",
    "TimedInstruction": "timed",
}


class OwnedBitCoilError(ValueError):
    """A coil writes a bit owned by a timer or counter instruction."""


def _coil_tag_names(instr: Any) -> list[str]:
    from pyrung.core.memory_block import BlockRange
    from pyrung.core.tag import ImmediateRef, Tag

    target = getattr(instr, "target", None)
    if isinstance(target, ImmediateRef):
        target = target.value
    if isinstance(target, Tag):
        return [target.name]
    if isinstance(target, BlockRange):
        return [t.name for t in target.tags()]
    return []


def _where(instr: Any) -> str:
    file = getattr(instr, "source_file", None)
    line = getattr(instr, "source_line", None)
    if file and line:
        return f" at {file}:{line}"
    return f" at line {line}" if line else ""


def check_owned_bit_coils(program: Program) -> None:
    """Raise OwnedBitCoilError if any coil targets a timer/counter Done bit."""
    instructions = list(walk_instructions(program))
    owners: dict[str, str] = {}
    for instr in instructions:
        label = _OWNERS.get(type(instr).__name__)
        done = getattr(instr, "done_bit", None)
        if label is not None and done is not None:
            owners.setdefault(done.name, label)
    if not owners:
        return
    for instr in instructions:
        coil = _COILS.get(type(instr).__name__)
        if coil is None:
            continue
        for name in _coil_tag_names(instr):
            owner = owners.get(name)
            if owner is not None:
                raise OwnedBitCoilError(
                    f"{OWNED_BIT_COIL}: {coil}() writes {name}, the Done bit of "
                    f"{owner}() (coil{_where(instr)}). The {owner}() rewrites it "
                    f"on every execution; use that instruction's .reset() pin instead "
                    f"of a coil."
                )

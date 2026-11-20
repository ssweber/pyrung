"""Click hardware capability profile for ladder-portability validation.

This module is table-driven and encodes the static compatibility rules used by
pyrung Click validation.
"""

from __future__ import annotations

from typing import Any, Literal

from pyclickplc.banks import BANKS

InstructionRole = Literal["timer_done_bit"]

CompareConstantKind = Literal["int1", "int2", "float", "hex", "text"]


INSTRUCTION_ROLE_COMPATIBILITY: dict[InstructionRole, frozenset[str]] = {
    "timer_done_bit": frozenset({"T"}),
}

_HEX_COMPARE_BANKS: frozenset[str] = frozenset({"XD", "YD", "DH"})
_NUMERIC_COMPARE_BANKS: frozenset[str] = frozenset({"TD", "CTD", "DS", "DD", "DF", "SD"})
_TEXT_COMPARE_BANKS: frozenset[str] = frozenset({"TXT"})

COMPARE_COMPATIBILITY: frozenset[tuple[str, str]] = frozenset(
    [(left, right) for left in _HEX_COMPARE_BANKS for right in _HEX_COMPARE_BANKS]
    + [(left, right) for left in _NUMERIC_COMPARE_BANKS for right in _NUMERIC_COMPARE_BANKS]
    + [("TXT", "TXT")]
)


def _constant_kinds(*kinds: CompareConstantKind) -> frozenset[CompareConstantKind]:
    return frozenset(kinds)


COMPARE_CONSTANT_COMPATIBILITY: dict[str, frozenset[CompareConstantKind]] = {
    "XD": _constant_kinds("hex"),
    "YD": _constant_kinds("hex"),
    "DH": _constant_kinds("hex"),
    "TD": _constant_kinds("int1", "int2", "float"),
    "CTD": _constant_kinds("int1", "int2", "float"),
    "DS": _constant_kinds("int1", "int2", "float"),
    "DD": _constant_kinds("int1", "int2", "float"),
    "DF": _constant_kinds("int1", "int2", "float"),
    "SD": _constant_kinds("int1", "int2", "float"),
    "TXT": _constant_kinds("text"),
}

# Banks whose constants CLICK writes in hex (``FFh``). The same applies to compares against
# those banks, and to copy/fill/search constants that land in them.
HEX_CONSTANT_BANKS: frozenset[str] = frozenset(
    bank for bank, kinds in COMPARE_CONSTANT_COMPATIBILITY.items() if kinds == {"hex"}
)


def single_register_fill_dest(dest: Any) -> Any | None:
    """Return the one tag that a ``fill()`` destination covers, or ``None``.

    CLICK's Fill dialog needs at least two registers, so a Fill of one register
    exports as a Copy. Only a range that is known when the program is written counts. A
    pointer range can be one register long at run time, and it stays a Fill.
    """
    from pyrung.core.memory_block import BlockRange

    if not isinstance(dest, BlockRange):
        return None
    tags = dest.tags()
    return tags[0] if len(tags) == 1 else None


class ClickHardwareProfile:
    """Capability lookup API for Click portability validation."""

    def valid_for_role(self, memory_type: str, role: InstructionRole) -> bool:
        if memory_type not in BANKS:
            raise KeyError(f"Unknown bank: {memory_type!r}")
        allowed = INSTRUCTION_ROLE_COMPATIBILITY.get(role)
        if allowed is None:
            raise KeyError(f"Unknown instruction role: {role!r}")
        return memory_type in allowed

    def compare_compatible(self, left_bank: str, right_bank: str) -> bool:
        if left_bank not in BANKS:
            raise KeyError(f"Unknown left bank: {left_bank!r}")
        if right_bank not in BANKS:
            raise KeyError(f"Unknown right bank: {right_bank!r}")
        return (left_bank, right_bank) in COMPARE_COMPATIBILITY

    def compare_constant_compatible(self, bank: str, const_kind: CompareConstantKind) -> bool:
        if bank not in BANKS:
            raise KeyError(f"Unknown bank: {bank!r}")
        compatibility = COMPARE_CONSTANT_COMPATIBILITY.get(bank)
        if compatibility is None:
            return False
        if const_kind not in {"int1", "int2", "float", "hex", "text"}:
            raise KeyError(f"Unknown constant kind: {const_kind!r}")
        return const_kind in compatibility


CLICK_HARDWARE_PROFILE = ClickHardwareProfile()

__all__ = [
    "InstructionRole",
    "CompareConstantKind",
    "INSTRUCTION_ROLE_COMPATIBILITY",
    "COMPARE_COMPATIBILITY",
    "COMPARE_CONSTANT_COMPATIBILITY",
    "HEX_CONSTANT_BANKS",
    "ClickHardwareProfile",
    "CLICK_HARDWARE_PROFILE",
]

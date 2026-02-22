"""Click hardware capability profile for ladder-portability validation.

This module is table-driven and encodes the static compatibility rules used by
pyrung Click validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pyclickplc.banks import BANKS

InstructionRole = Literal[
    "timer_done_bit",
    "timer_accumulator",
    "timer_preset",
    "counter_done_bit",
    "counter_accumulator",
    "counter_preset",
    "copy_pointer",
]

CopyOperation = Literal[
    "single",
    "block",
    "fill",
    "pack_bits",
    "pack_words",
    "unpack_bits",
    "unpack_words",
]

CompareConstantKind = Literal["int1", "int2", "float", "hex", "text"]


@dataclass(frozen=True)
class BankCapability:
    """Per-bank ladder validation write capability."""

    writable: bool
    writable_subset: frozenset[int] | None = None


# SC/SD writable subsets for ladder validation (different from Modbus writability).
LADDER_WRITABLE_SC: frozenset[int] = frozenset(
    {50, 51, 53, 55, 60, 61, 65, 66, 67, 75, 76, 120, 121}
)

LADDER_WRITABLE_SD: frozenset[int] = frozenset(
    {
        29,
        31,
        32,
        34,
        35,
        36,
        40,
        41,
        42,
        50,
        51,
        60,
        61,
        106,
        107,
        108,
        112,
        113,
        114,
        140,
        141,
        142,
        143,
        144,
        145,
        146,
        147,
        214,
        215,
    }
)

LADDER_BANK_CAPABILITIES: dict[str, BankCapability] = {
    "X": BankCapability(writable=False),
    "Y": BankCapability(writable=True),
    "C": BankCapability(writable=True),
    "T": BankCapability(writable=False),
    "CT": BankCapability(writable=False),
    "SC": BankCapability(writable=False, writable_subset=LADDER_WRITABLE_SC),
    "DS": BankCapability(writable=True),
    "DD": BankCapability(writable=True),
    "DH": BankCapability(writable=True),
    "DF": BankCapability(writable=True),
    "XD": BankCapability(writable=False),
    "YD": BankCapability(writable=False),
    "TD": BankCapability(writable=True),
    "CTD": BankCapability(writable=True),
    "SD": BankCapability(writable=False, writable_subset=LADDER_WRITABLE_SD),
    "TXT": BankCapability(writable=True),
}

INSTRUCTION_ROLE_COMPATIBILITY: dict[InstructionRole, frozenset[str]] = {
    "timer_done_bit": frozenset({"T"}),
    "timer_accumulator": frozenset({"TD"}),
    "timer_preset": frozenset({"DS"}),
    "counter_done_bit": frozenset({"CT"}),
    "counter_accumulator": frozenset({"CTD"}),
    "counter_preset": frozenset({"DS", "DD"}),
    "copy_pointer": frozenset({"DS"}),
}

_BIT_SOURCES: frozenset[str] = frozenset({"X", "Y", "C", "T", "CT", "SC"})
_BIT_DESTS: frozenset[str] = frozenset({"Y", "C"})

_REGISTER_SOURCES: frozenset[str] = frozenset(
    {"DS", "DD", "DH", "DF", "XD", "YD", "TD", "CTD", "SD", "TXT"}
)
_SINGLE_REGISTER_DESTS: frozenset[str] = frozenset(
    {"DS", "DD", "DH", "DF", "YD", "TD", "CTD", "SD", "TXT"}
)

_BLOCK_REGISTER_DESTS: frozenset[str] = frozenset({"DS", "DD", "DH", "DF", "YD", "TD", "CTD"})
_BLOCK_TXT_DESTS: frozenset[str] = frozenset({"DS", "DD", "DH", "DF"})

_FILL_REGISTER_DESTS: frozenset[str] = frozenset({"DS", "DD", "DH", "DF", "YD", "TD", "CTD", "SD"})

_PACK_BITS_16_SOURCES: frozenset[str] = frozenset({"X", "Y", "T", "CT", "SC"})
_PACK_BITS_16_DESTS: frozenset[str] = frozenset({"DS", "DH"})
_PACK_BITS_32_SOURCES: frozenset[str] = frozenset({"C"})
_PACK_BITS_32_DESTS: frozenset[str] = frozenset({"DD", "DF"})

PACK_WORDS_COMPATIBILITY: frozenset[tuple[str, str]] = frozenset(
    (source, dest) for source in ("DS", "DH") for dest in ("DD", "DF")
)

UNPACK_BITS_COMPATIBILITY: frozenset[tuple[str, str]] = frozenset(
    (source, dest) for source in ("DS", "DH", "DD", "DF") for dest in ("Y", "C")
)

UNPACK_WORDS_COMPATIBILITY: frozenset[tuple[str, str]] = frozenset(
    (source, dest) for source in ("DD", "DF") for dest in ("DS", "DH")
)

COPY_COMPATIBILITY: dict[CopyOperation, frozenset[tuple[str, str]]] = {
    "single": frozenset(
        [(source, dest) for source in _BIT_SOURCES for dest in _BIT_DESTS]
        + [(source, dest) for source in _REGISTER_SOURCES for dest in _SINGLE_REGISTER_DESTS]
    ),
    "block": frozenset(
        [(source, dest) for source in _BIT_SOURCES for dest in _BIT_DESTS]
        + [
            (source, dest)
            for source in ("DS", "DD", "DH", "DF", "SD")
            for dest in _BLOCK_REGISTER_DESTS
        ]
        + [("TXT", dest) for dest in _BLOCK_TXT_DESTS]
    ),
    "fill": frozenset(
        [
            (source, dest)
            for source in ("DS", "DD", "DH", "DF", "XD", "YD", "TD", "CTD", "SD")
            for dest in _FILL_REGISTER_DESTS
        ]
        + [("TXT", "TXT")]
    ),
    "pack_bits": frozenset(
        [(source, dest) for source in _PACK_BITS_16_SOURCES for dest in _PACK_BITS_16_DESTS]
        + [(source, dest) for source in _PACK_BITS_32_SOURCES for dest in _PACK_BITS_16_DESTS]
        + [(source, dest) for source in _PACK_BITS_32_SOURCES for dest in _PACK_BITS_32_DESTS]
    ),
    "pack_words": PACK_WORDS_COMPATIBILITY,
    "unpack_bits": UNPACK_BITS_COMPATIBILITY,
    "unpack_words": UNPACK_WORDS_COMPATIBILITY,
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


class ClickHardwareProfile:
    """Capability lookup API for Click portability validation."""

    def is_writable(self, memory_type: str, address: int | None = None) -> bool:
        if memory_type not in BANKS:
            raise KeyError(f"Unknown bank: {memory_type!r}")

        capability = LADDER_BANK_CAPABILITIES[memory_type]
        if capability.writable_subset is None:
            return capability.writable

        if address is None:
            return False
        return address in capability.writable_subset

    def valid_for_role(self, memory_type: str, role: InstructionRole) -> bool:
        if memory_type not in BANKS:
            raise KeyError(f"Unknown bank: {memory_type!r}")
        allowed = INSTRUCTION_ROLE_COMPATIBILITY.get(role)
        if allowed is None:
            raise KeyError(f"Unknown instruction role: {role!r}")
        return memory_type in allowed

    def copy_compatible(
        self,
        operation: CopyOperation,
        source_type: str,
        dest_type: str,
    ) -> bool:
        if source_type not in BANKS:
            raise KeyError(f"Unknown source bank: {source_type!r}")
        if dest_type not in BANKS:
            raise KeyError(f"Unknown destination bank: {dest_type!r}")
        compatibility = COPY_COMPATIBILITY.get(operation)
        if compatibility is None:
            raise KeyError(f"Unknown copy operation: {operation!r}")
        return (source_type, dest_type) in compatibility

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

assert set(LADDER_BANK_CAPABILITIES) == set(BANKS), (
    "LADDER_BANK_CAPABILITIES keys must match BANKS keys"
)

__all__ = [
    "InstructionRole",
    "CopyOperation",
    "CompareConstantKind",
    "BankCapability",
    "LADDER_WRITABLE_SC",
    "LADDER_WRITABLE_SD",
    "LADDER_BANK_CAPABILITIES",
    "INSTRUCTION_ROLE_COMPATIBILITY",
    "COPY_COMPATIBILITY",
    "COMPARE_COMPATIBILITY",
    "COMPARE_CONSTANT_COMPATIBILITY",
    "ClickHardwareProfile",
    "CLICK_HARDWARE_PROFILE",
]

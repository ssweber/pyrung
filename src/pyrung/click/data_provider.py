"""Soft PLC adapter between pyrung runtime state and pyclickplc DataProvider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from pyclickplc.addresses import format_address_display, parse_address
from pyclickplc.banks import BANKS
from pyclickplc.server import DataProvider, MemoryDataProvider, PlcValue
from pyclickplc.validation import assert_runtime_value

from pyrung.click.tag_map import TagMap
from pyrung.core.runner import PLCRunner


@dataclass(frozen=True)
class _MappedRuntimeSlot:
    logical_name: str
    default: object
    read_only: bool
    source: str


_xy_slot_ranges = BANKS["X"].valid_ranges
if _xy_slot_ranges is None:
    raise RuntimeError("X bank must define sparse valid ranges for XD/YD mirroring.")
if BANKS["Y"].valid_ranges != _xy_slot_ranges:
    raise RuntimeError("X and Y sparse ranges must match for XD/YD mirroring.")
_XY_SLOT_RANGES: tuple[tuple[int, int], ...] = _xy_slot_ranges
for _lo, _hi in _XY_SLOT_RANGES:
    if _hi - _lo + 1 != 16:
        raise RuntimeError("Each X/Y sparse slot must be 16 bits wide.")

_WORD_SIZE = 16
_MIRRORED_WORD_BANKS: dict[str, str] = {"XD": "X", "YD": "Y"}


class ClickDataProvider:
    """Bridges ``PLCRunner`` state to the ``pyclickplc`` ``DataProvider`` protocol.

    Implements the ``DataProvider`` interface so pyrung can act as a soft PLC
    accessible over Modbus TCP via ``pyclickplc.server.ClickServer``.

    - **Reads** return the current committed ``SystemState.tags`` value for the
      mapped logical tag.
    - **Writes** queue a ``runner.patch()`` so the new value takes effect at
      the start of the next scan.
    - Unmapped addresses fall through to an optional `fallback` provider.

    **XD / YD word-image mirroring:**

    - ``XD*`` reads are computed from the current X bit image (16 bits per slot).
    - ``YD*`` reads are computed from the current Y bit image.
    - ``YD*`` writes fan out to the corresponding Y bit tags via ``runner.patch()``.
    - ``XD*`` writes are rejected (read-only).

    Args:
        runner: The active ``PLCRunner`` whose state is served.
        tag_map: Mapping from logical tag names to Click hardware addresses.
        fallback: Optional provider for unmapped addresses.
            Defaults to an in-memory provider.

    Example:
        .. code-block:: python

            from pyrung.click import ClickDataProvider
            from pyclickplc.server import ClickServer

            provider = ClickDataProvider(runner, tag_map=mapping)
            server = ClickServer(provider, port=502)
    """

    def __init__(
        self,
        runner: PLCRunner,
        tag_map: TagMap,
        fallback: DataProvider | None = None,
    ) -> None:
        self._runner = runner
        self._fallback = fallback if fallback is not None else MemoryDataProvider()
        self._mapped_slots = self._build_reverse_index(tag_map)

    @staticmethod
    def _normalize(address: str) -> tuple[str, str, int]:
        bank, index = parse_address(address)
        normalized = format_address_display(bank, index)
        return normalized, bank, index

    @staticmethod
    def _build_reverse_index(tag_map: TagMap) -> dict[str, _MappedRuntimeSlot]:
        reverse: dict[str, _MappedRuntimeSlot] = {}
        for slot in tag_map.mapped_slots():
            # XD/YD are mirrored views over X/Y at runtime.
            if slot.memory_type in ("XD", "YD"):
                continue
            reverse[slot.hardware_address] = _MappedRuntimeSlot(
                logical_name=slot.logical_name,
                default=slot.default,
                read_only=slot.read_only,
                source=slot.source,
            )
        return reverse

    def read(self, address: str) -> PlcValue:
        normalized, bank, index = self._normalize(address)
        if bank in _MIRRORED_WORD_BANKS:
            return self._read_mirrored_word(bank, index)
        return self._read_mapped_or_fallback(normalized)

    def write(self, address: str, value: PlcValue) -> None:
        normalized, bank, index = self._normalize(address)
        if bank == "XD":
            assert_runtime_value(BANKS[bank].data_type, value, bank=bank, index=index)
            raise ValueError("XD addresses are read-only and cannot be written.")
        if bank == "YD":
            assert_runtime_value(BANKS[bank].data_type, value, bank=bank, index=index)
            self._write_mirrored_word(bank, index, cast(int, value))
            return
        self._write_mapped_or_fallback(normalized, value)

    def _read_mapped_or_fallback(self, normalized: str) -> PlcValue:
        mapped = self._mapped_slots.get(normalized)
        if mapped is None:
            return self._fallback.read(normalized)
        if mapped.source == "system":
            found, value = self._runner.system_runtime.resolve(
                mapped.logical_name, self._runner.current_state
            )
            if found:
                return value
        return self._runner.current_state.tags.get(mapped.logical_name, mapped.default)

    def _write_mapped_or_fallback(self, normalized: str, value: PlcValue) -> None:
        bank, index = parse_address(normalized)
        mapped = self._mapped_slots.get(normalized)
        if mapped is None:
            self._fallback.write(normalized, value)
            return

        assert_runtime_value(BANKS[bank].data_type, value, bank=bank, index=index)
        if mapped.source == "system" and mapped.read_only:
            raise ValueError(
                f"Tag '{mapped.logical_name}' is read-only system point and cannot be written"
            )
        self._runner.patch({mapped.logical_name: value})

    def _read_mirrored_word(self, word_bank: str, word_index: int) -> int:
        bit_bank, slot_index = self._resolve_xy_slot(word_bank, word_index)
        bit_addresses = self._slot_addresses(bit_bank, slot_index)
        bits = tuple(bool(self._read_mapped_or_fallback(addr)) for addr in bit_addresses)
        return self._pack_word(bits)

    def _write_mirrored_word(self, word_bank: str, word_index: int, value: int) -> None:
        bit_bank, slot_index = self._resolve_xy_slot(word_bank, word_index)
        bit_addresses = self._slot_addresses(bit_bank, slot_index)
        bits = self._unpack_word(value)
        for bit_address, bit_value in zip(bit_addresses, bits, strict=True):
            self._write_mapped_or_fallback(bit_address, bit_value)

    @staticmethod
    def _pack_word(bits: tuple[bool, ...]) -> int:
        word = 0
        for bit_index, bit_value in enumerate(bits):
            if bit_value:
                word |= 1 << bit_index
        return word

    @staticmethod
    def _unpack_word(value: int) -> tuple[bool, ...]:
        return tuple(bool((value >> bit_index) & 0x1) for bit_index in range(_WORD_SIZE))

    @staticmethod
    def _slot_addresses(bit_bank: str, slot_index: int) -> tuple[str, ...]:
        lo, hi = _XY_SLOT_RANGES[slot_index]
        return tuple(format_address_display(bit_bank, addr) for addr in range(lo, hi + 1))

    @staticmethod
    def _resolve_xy_slot(word_bank: str, word_index: int) -> tuple[str, int]:
        bit_bank = _MIRRORED_WORD_BANKS[word_bank]
        if word_index == 0:
            return bit_bank, 0
        if word_index == 1:
            return bit_bank, 1
        if word_index % 2 == 0 and word_index >= 2:
            slot_index = word_index // 2 + 1
            if slot_index < len(_XY_SLOT_RANGES):
                return bit_bank, slot_index
        raise ValueError(f"{word_bank}{word_index} does not map to a valid X/Y 16-bit slot.")

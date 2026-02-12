"""Soft PLC adapter between pyrung runtime state and pyclickplc DataProvider."""

from __future__ import annotations

from dataclasses import dataclass

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


class ClickDataProvider:
    """DataProvider bridge that serves mapped addresses from PLCRunner state."""

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
            # v1: XD/YD are always served by fallback.
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
        normalized, _bank, _index = self._normalize(address)
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

    def write(self, address: str, value: PlcValue) -> None:
        normalized, bank, index = self._normalize(address)
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

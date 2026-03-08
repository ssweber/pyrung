"""CircuitPython-native Modbus send/receive instructions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyclickplc.addresses import parse_address
from pyclickplc.banks import BANKS

from pyrung.core._source import _capture_source
from pyrung.core.instruction import Instruction
from pyrung.core.memory_block import BlockRange
from pyrung.core.program.context import _require_rung_context
from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


def _is_valid_index(bank: str, index: int) -> bool:
    cfg = BANKS[bank]
    if cfg.valid_ranges is None:
        return cfg.min_addr <= index <= cfg.max_addr
    return any(lo <= index <= hi for lo, hi in cfg.valid_ranges)


def _addresses_for_count(bank: str, start: int, count: int) -> tuple[int, ...]:
    if count <= 0:
        raise ValueError("count must be >= 1")
    if not _is_valid_index(bank, start):
        raise ValueError(f"{bank} address {start} is out of range")

    cfg = BANKS[bank]
    if cfg.valid_ranges is None:
        end = start + count - 1
        if end > cfg.max_addr:
            raise ValueError(
                f"{bank} range overflow: start {start}, count {count} exceeds {cfg.max_addr}"
            )
        return tuple(range(start, end + 1))

    addresses = [start]
    current = start
    while len(addresses) < count:
        current += 1
        while current <= cfg.max_addr and not _is_valid_index(bank, current):
            current += 1
        if current > cfg.max_addr:
            raise ValueError(
                f"{bank} range overflow: start {start}, count {count} exceeds valid addresses"
            )
        addresses.append(current)
    return tuple(addresses)


def _normalize_operand_count(operand: Tag | BlockRange, count: int | None) -> tuple[int, ...]:
    expected = 1 if isinstance(operand, Tag) else len(tuple(operand.addresses))
    effective = expected if count is None else count
    if effective != expected:
        raise ValueError(
            f"count mismatch: operand resolves to {expected} tag(s) but count={effective}"
        )
    return tuple(range(expected))


def _validate_status_tags(
    *,
    busy: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    busy_name: str,
) -> None:
    if busy.type != TagType.BOOL:
        raise TypeError(f"{busy_name} tag '{busy.name}' must be BOOL")
    if success.type != TagType.BOOL:
        raise TypeError(f"success tag '{success.name}' must be BOOL")
    if error.type != TagType.BOOL:
        raise TypeError(f"error tag '{error.name}' must be BOOL")
    if exception_response.type not in {TagType.INT, TagType.DINT}:
        raise TypeError(f"exception_response tag '{exception_response.name}' must be INT or DINT")


@dataclass
class CircuitPySendInstruction(Instruction):
    target: str
    bank: str
    start: int
    addresses: tuple[int, ...]
    source: Tag | BlockRange
    sending: Tag
    success: Tag
    error: Tag
    exception_response: Tag

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            ctx.set_tags(
                {
                    self.sending.name: False,
                    self.success.name: False,
                    self.error.name: False,
                    self.exception_response.name: 0,
                }
            )

    def is_inert_when_disabled(self) -> bool:
        return False


@dataclass
class CircuitPyReceiveInstruction(Instruction):
    target: str
    bank: str
    start: int
    addresses: tuple[int, ...]
    dest: Tag | BlockRange
    receiving: Tag
    success: Tag
    error: Tag
    exception_response: Tag

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            ctx.set_tags(
                {
                    self.receiving.name: False,
                    self.success.name: False,
                    self.error.name: False,
                    self.exception_response.name: 0,
                }
            )

    def is_inert_when_disabled(self) -> bool:
        return False


def send(
    *,
    target: str,
    remote_start: str,
    source: Tag | BlockRange,
    sending: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    count: int | None = None,
) -> None:
    _validate_status_tags(
        busy=sending,
        success=success,
        error=error,
        exception_response=exception_response,
        busy_name="sending",
    )
    if not isinstance(target, str) or not target:
        raise TypeError("target must be a non-empty string")
    if not isinstance(source, (Tag, BlockRange)):
        raise TypeError(f"source must be Tag or BlockRange, got {type(source).__name__}")
    bank, start = parse_address(remote_start)
    _normalize_operand_count(source, count)
    addresses = _addresses_for_count(bank, start, 1 if isinstance(source, Tag) else len(source.addresses))
    ctx = _require_rung_context("send")
    source_file, source_line = _capture_source(depth=2)
    instr = CircuitPySendInstruction(
        target=target,
        bank=bank,
        start=start,
        addresses=addresses,
        source=source,
        sending=sending,
        success=success,
        error=error,
        exception_response=exception_response,
    )
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def receive(
    *,
    target: str,
    remote_start: str,
    dest: Tag | BlockRange,
    receiving: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    count: int | None = None,
) -> None:
    _validate_status_tags(
        busy=receiving,
        success=success,
        error=error,
        exception_response=exception_response,
        busy_name="receiving",
    )
    if not isinstance(target, str) or not target:
        raise TypeError("target must be a non-empty string")
    if not isinstance(dest, (Tag, BlockRange)):
        raise TypeError(f"dest must be Tag or BlockRange, got {type(dest).__name__}")
    bank, start = parse_address(remote_start)
    _normalize_operand_count(dest, count)
    addresses = _addresses_for_count(bank, start, 1 if isinstance(dest, Tag) else len(dest.addresses))
    ctx = _require_rung_context("receive")
    source_file, source_line = _capture_source(depth=2)
    instr = CircuitPyReceiveInstruction(
        target=target,
        bank=bank,
        start=start,
        addresses=addresses,
        dest=dest,
        receiving=receiving,
        success=success,
        error=error,
        exception_response=exception_response,
    )
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


__all__ = [
    "CircuitPyReceiveInstruction",
    "CircuitPySendInstruction",
    "receive",
    "send",
]

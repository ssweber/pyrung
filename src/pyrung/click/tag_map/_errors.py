"""Structured Click nickname import failures."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SystemNicknameRepair:
    """One documented correction needed for a CLICK system nickname."""

    memory_type: str
    address: int
    current: str
    replacement: str

    @property
    def display_address(self) -> str:
        return f"{self.memory_type}{self.address}"


class SystemNicknameRepairRequired(ValueError):
    """Raised when vendor-owned system names need documented corrections."""

    def __init__(self, repairs: tuple[SystemNicknameRepair, ...]) -> None:
        self.repairs = repairs
        details = "; ".join(
            f"{repair.display_address}: {repair.current!r} -> {repair.replacement!r}"
            for repair in repairs
        )
        super().__init__(
            f"CLICK system nicknames need repair ({len(repairs)} known correction(s)): {details}"
        )

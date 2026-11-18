"""Structured failures and warnings from reading CLICK nickname files and PLC data files."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pyclickplc.addresses import format_address_display, parse_address


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


class OldCommentFormError(ValueError):
    """Raised when a nickname comment holds an annotation form that this pyrung no longer reads.

    An editor matches on ``code`` or the class, then offers "Upgrade Project".  Once a
    project load knows the offending row, ``row`` holds that row's nickname and address.
    """

    code = "OLD_COMMENT_FORM"

    def __init__(self, token: str, row: str | None = None) -> None:
        self.token = token
        self.row = row
        prefix = f"{row}: " if row else ""
        super().__init__(
            f"{prefix}{token!r} is an old annotation form, and this pyrung no longer reads it. "
            'Run "Upgrade Project" in clicknick to rewrite it, '
            "or pass the comment to pyrung.click.tag_map.upgrade_comment()."
        )


class SkippedAddressesWarning(UserWarning):
    """Issued when pyrung loads a PLC data file without some of its TXT addresses.

    CLICK writes a TXT comma without quotes, so a TXT row that holds a comma and an empty
    address can be read more than one way.
    ``addresses`` lists the TXT addresses that pyrung skipped, in file order.
    """

    def __init__(self, message: str, addresses: tuple[str, ...]) -> None:
        super().__init__(message)
        self.addresses = addresses

    def __reduce__(self) -> tuple[type[SkippedAddressesWarning], tuple[str, tuple[str, ...]]]:
        # The default reduce passes only the message to __init__, so pickle and copy would fail.
        return (type(self), (str(self), self.addresses))


def address_runs(addresses: Iterable[str]) -> str:
    """Return *addresses* as text, with each run of consecutive addresses as ``TXT13..TXT20``."""
    runs: list[tuple[str, int, int]] = []
    for address in addresses:
        bank, index = parse_address(address)
        if runs and runs[-1][0] == bank and runs[-1][2] + 1 == index:
            runs[-1] = (bank, runs[-1][1], index)
        else:
            runs.append((bank, index, index))
    return ", ".join(
        format_address_display(bank, first)
        if first == last
        else f"{format_address_display(bank, first)}..{format_address_display(bank, last)}"
        for bank, first, last in runs
    )


def skipped_addresses_text(addresses: Iterable[str], file_name: str) -> str:
    """Return the message that names the TXT addresses skipped in the PLC data file *file_name*."""
    return (
        f"pyrung skipped {address_runs(addresses)} in {file_name} and left them at their "
        "power-on values. CLICK writes a TXT comma without quotes, so their values in the "
        "file are unclear. The rest of the file loaded. Check these addresses in CLICK."
    )

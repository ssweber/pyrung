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


class OldCommentFormError(ValueError):
    """Raised when a nickname comment holds an annotation form this pyrung no longer reads.

    An editor matches on ``code`` or the class, then offers "Upgrade Project". ``row`` is the
    nickname and address of the offending row once a project load knows it.
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

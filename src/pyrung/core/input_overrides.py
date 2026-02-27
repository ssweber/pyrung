"""Patch/force override state for PLCRunner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from pyrung.core.context import ScanContext
    from pyrung.core.tag import Tag


class InputOverrideManager:
    """Encapsulates one-shot patches and persistent force overrides."""

    def __init__(self, *, is_read_only: Callable[[str], bool]) -> None:
        self._is_read_only = is_read_only
        self._pending_patches: dict[str, bool | int | float | str] = {}
        self._forces: dict[str, bool | int | float | str] = {}

    @property
    def pending_patches(self) -> dict[str, bool | int | float | str]:
        return self._pending_patches

    @property
    def forces_mutable(self) -> dict[str, bool | int | float | str]:
        return self._forces

    def _normalize_tag_name(self, tag: str | Tag, *, method: str) -> str:
        from pyrung.core.tag import Tag as TagClass

        if isinstance(tag, TagClass):
            return tag.name
        if isinstance(tag, str):
            return tag
        raise TypeError(f"{method}() keys must be str or Tag, got {type(tag).__name__}")

    def _normalize_tag_updates(
        self,
        tags: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
        *,
        method: str,
    ) -> dict[str, bool | int | float | str]:
        normalized: dict[str, bool | int | float | str] = {}
        for key, value in tags.items():
            name = self._normalize_tag_name(key, method=method)
            if self._is_read_only(name):
                raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
            normalized[name] = value
        return normalized

    def patch(
        self,
        tags: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> None:
        self._pending_patches.update(self._normalize_tag_updates(tags, method="patch"))

    def add_force(self, tag: str | Tag, value: bool | int | float | str) -> None:
        name = self._normalize_tag_name(tag, method="add_force")
        if self._is_read_only(name):
            raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
        self._forces[name] = value

    def remove_force(self, tag: str | Tag) -> None:
        name = self._normalize_tag_name(tag, method="remove_force")
        if name not in self._forces:
            raise KeyError(name)
        del self._forces[name]

    def clear_forces(self) -> None:
        self._forces.clear()

    @contextmanager
    def force(
        self,
        overrides: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> Iterator[None]:
        snapshot = self._forces.copy()
        try:
            for tag, value in overrides.items():
                self.add_force(tag, value)
            yield None
        finally:
            self._forces.clear()
            self._forces.update(snapshot)

    @property
    def forces(self) -> Mapping[str, bool | int | float | str]:
        return MappingProxyType(self._forces)

    def get_live_override(self, name: str) -> tuple[bool, Any]:
        if name in self._pending_patches:
            return True, self._pending_patches[name]
        if name in self._forces:
            return True, self._forces[name]
        return False, None

    def apply_pre_scan(self, ctx: ScanContext) -> None:
        if self._pending_patches:
            ctx.set_tags(self._pending_patches)
            self._pending_patches.clear()

        if self._forces:
            ctx.set_tags(self._forces)

    def apply_post_logic(self, ctx: ScanContext) -> None:
        if self._forces:
            ctx.set_tags(self._forces)

"""Patch/force override state for PLC."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import TYPE_CHECKING

from pyrung.core.storage import store_char

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from pyrung.core.context import ScanContext
    from pyrung.core.expression import RandIntExpr
    from pyrung.core.tag import Tag


class InputOverrideManager:
    """Encapsulates one-shot patches and persistent force overrides."""

    def __init__(
        self,
        *,
        is_read_only: Callable[[str], bool],
        known_tag: Callable[[str], Tag | None] | None = None,
    ) -> None:
        self._is_read_only = is_read_only
        self._known_tag = known_tag
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

    @staticmethod
    def _normalize_char_value(value: bool | int | float | str) -> bool | int | float | str:
        # Only a CHAR can be written "": patch/force of a blank stores NUL.
        return store_char(value)

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
            self._check_range(key, name, value, method=method)
            normalized[name] = self._normalize_char_value(value)
        return normalized

    def _check_range(
        self, key: str | Tag, name: str, value: bool | int | float | str, *, method: str
    ) -> None:
        """A REAL never holds a value outside CLICK's range: refuse one."""
        from pyrung.core.storage import check_real_value
        from pyrung.core.tag import Tag as TagClass
        from pyrung.core.tag import TagType

        tag = key if isinstance(key, TagClass) else None
        if tag is None and self._known_tag is not None:
            tag = self._known_tag(name)
        if tag is not None and tag.type is TagType.REAL:
            check_real_value(value, f"{method}() of {name}")

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
        self._check_range(tag, name, value, method="force")
        self._forces[name] = self._normalize_char_value(value)

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

    def apply_pre_scan(self, ctx: ScanContext) -> dict[str, bool | int | float | str]:
        """Drain patches and apply forces.  Returns a copy of the patches
        that were drained this scan (empty if none), for the recorder.
        """
        drained: dict[str, bool | int | float | str] = {}
        if self._pending_patches:
            drained = dict(self._pending_patches)
            ctx.set_tags(self._pending_patches)
            self._pending_patches.clear()

        if self._forces:
            self._apply_forces_if_changed(ctx)
        return drained

    def apply_randint_draws(
        self,
        ctx: ScanContext,
        sites: tuple[RandIntExpr, ...],
        drained: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Write this scan's ``randint`` draws after the drain.

        A site whose tag was patched this scan or is forced keeps that value
        (the test override).  Draws are a pure function of the scan id, so they
        are not recorded: replay recomputes them.  Returns the drawn names.
        """
        draws = {
            site.tag.name: site.draw(ctx)
            for site in sites
            if site.tag.name not in drained and site.tag.name not in self._forces
        }
        if draws:
            ctx.set_tags(draws)
        return tuple(draws)

    def apply_post_logic(self, ctx: ScanContext) -> None:
        if self._forces:
            self._apply_forces_if_changed(ctx)

    def _apply_forces_if_changed(self, ctx: ScanContext) -> None:
        """Write only forces whose value differs from current pending/state.

        Skipping already-matching writes keeps the ``tags`` PMap structurally
        shared when a force's value is stable.
        """
        diff = {name: value for name, value in self._forces.items() if ctx.get_tag(name) != value}
        if diff:
            ctx.set_tags(diff)

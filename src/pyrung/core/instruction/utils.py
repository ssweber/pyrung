"""Shared helpers for instruction execution and validation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.tag import Tag, TagType

F = TypeVar("F", bound=Callable[..., Any])


def guard_oneshot_execution(func: F) -> F:
    """Skip execute calls when one-shot state says not to run."""

    @wraps(func)
    def wrapper(self: Any, ctx: ScanContext, enabled: bool, *args: Any, **kwargs: Any) -> Any:
        should_execute = getattr(self, "should_execute", None)
        if callable(should_execute) and not should_execute(enabled):
            return None
        return func(self, ctx, enabled, *args, **kwargs)

    return cast(F, wrapper)


def to_condition(obj: Any) -> Any:
    """Normalize instruction condition input(s) to a single Condition."""
    from pyrung.core.condition import BitCondition, Condition, _normalize_and_condition
    from pyrung.core.tag import Tag as TagClass
    from pyrung.core.tag import TagType

    if obj is None:
        return None

    def _coerce(value: object) -> Condition:
        if isinstance(value, Condition):
            return value
        if isinstance(value, TagClass):
            if value.type == TagType.BOOL:
                return BitCondition(value)
            raise TypeError(
                f"Non-BOOL tag '{value.name}' cannot be used directly as condition. "
                "Use comparison operators: tag == value, tag > 0, etc."
            )
        raise TypeError(f"Expected Condition or Tag, got {type(value)}")

    return _normalize_and_condition(
        obj,
        coerce=_coerce,
        empty_error="condition requires at least one condition",
        group_empty_error="condition group cannot be empty",
    )


def resolve_preset_ctx(preset: Tag | int, ctx: ScanContext) -> int:
    """Resolve preset to int value (supports Tag or literal)."""
    from pyrung.core.tag import Tag as TagClass

    if isinstance(preset, TagClass):
        return ctx.get_tag(preset.name, preset.default)
    return preset


def _allowed_type_text(allowed_types: Iterable[TagType]) -> str:
    names = [tag_type.name for tag_type in allowed_types]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} or {names[1]}"
    return ", ".join(names[:-1]) + f", or {names[-1]}"


def assert_tag_type(
    tag: Tag,
    allowed_types: Iterable[TagType],
    *,
    label: str,
    include_tag_name: bool = False,
) -> None:
    """Assert a single tag is one of the allowed types."""
    allowed = tuple(allowed_types)
    if tag.type in allowed:
        return
    suffix = f" at {tag.name}" if include_tag_name else ""
    raise TypeError(f"{label} must be {_allowed_type_text(allowed)}; got {tag.type.name}{suffix}")

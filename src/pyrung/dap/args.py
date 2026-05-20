"""Dataclass-driven argument parsing helpers for DAP handlers."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast, get_args, get_origin, get_type_hints

T = TypeVar("T")


def parse_args(
    model: type[T],
    raw_args: object,
    *,
    error: Callable[[str], Exception] = ValueError,
    coercers: Mapping[str, Callable[[Any], Any]] | None = None,
) -> T:
    """Instantiate ``model`` from ``raw_args`` with field-level validation.

    - Missing required fields raise ``error(...)``.
    - Field values are type-checked against the dataclass annotation.
    - Optional coercers can be supplied per field name.
    """
    if not dataclasses.is_dataclass(model):
        raise TypeError("model must be a dataclass type")
    if not isinstance(raw_args, Mapping):
        raise error("Arguments must be an object")
    args_map = cast(Mapping[str, Any], raw_args)

    coercers = coercers or {}
    resolved_types = get_type_hints(model)
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(model):
        has_value = field.name in args_map
        if not has_value:
            if field.default is not dataclasses.MISSING:
                kwargs[field.name] = field.default
                continue
            if field.default_factory is not dataclasses.MISSING:
                kwargs[field.name] = field.default_factory()
                continue
            raise error(f"Missing required field: {field.name}")

        value = args_map[field.name]
        coercer = coercers.get(field.name)
        if coercer is not None:
            try:
                value = coercer(value)
            except Exception as exc:
                raise error(f"Invalid value for field: {field.name}") from exc

        field_type = resolved_types.get(field.name, field.type)
        if not _matches_type(value, field_type):
            raise error(f"Invalid type for field: {field.name}")
        kwargs[field.name] = value

    return model(**kwargs)


def parse_args_list(
    model: type[T],
    raw_items: list[Any],
    *,
    error: Callable[[str], Exception] = ValueError,
    coercers: Mapping[str, Callable[[Any], Any]] | None = None,
) -> list[T]:
    """Parse a list of argument objects into ``model`` instances."""
    parsed: list[T] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise error("Breakpoint entry must be an object")
        parsed.append(parse_args(model, item, error=error, coercers=coercers))
    return parsed


def coerce_int(value: Any) -> int:
    """Coerce using Python's built-in ``int`` semantics."""
    return int(value)


def _matches_type(value: Any, annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is None:
        if annotation is Any:
            return True
        if annotation is type(None):
            return value is None
        return isinstance(value, annotation)

    if origin in {list, tuple, set, frozenset, dict, Mapping}:
        return isinstance(value, origin)

    if origin is Callable:
        return callable(value)

    args = get_args(annotation)
    if origin is tuple:
        return isinstance(value, tuple)

    # PEP 604 unions and typing.Union resolve to UnionType/Union as origin.
    return any(_matches_type(value, member) for member in args)

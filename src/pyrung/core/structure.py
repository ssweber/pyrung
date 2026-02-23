"""Decorator-based structured logical tag factories.

`udt` creates mixed-type, field-grouped structures.
`named_array` creates single-type, instance-interleaved structures.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sized
from dataclasses import dataclass
from typing import Any, ClassVar, get_origin

from pyrung.core.memory_block import Block, BlockRange
from pyrung.core.tag import LiveTag, MappingEntry, TagType, _TagTypeBase

UNSET = object()
_NUMERIC_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.WORD})
_RESERVED_FIELD_NAMES = frozenset(
    {
        "clone",
        "count",
        "field_names",
        "fields",
        "map_to",
        "name",
        "stride",
        "type",
    }
)
_PRIMITIVE_TYPE_MAP = {
    bool: TagType.BOOL,
    int: TagType.INT,
    float: TagType.REAL,
    str: TagType.CHAR,
}
_STRING_TYPE_MAP = {
    "bool": TagType.BOOL,
    "bit": TagType.BOOL,
    "int": TagType.INT,
    "dint": TagType.DINT,
    "int2": TagType.DINT,
    "real": TagType.REAL,
    "float": TagType.REAL,
    "word": TagType.WORD,
    "hex": TagType.WORD,
    "char": TagType.CHAR,
    "str": TagType.CHAR,
    "txt": TagType.CHAR,
}


@dataclass(frozen=True)
class Field:
    """Field metadata used by `udt` and `named_array` declarations."""

    type: TagType | None = None
    default: Any = UNSET
    retentive: bool = False

    def __new__(
        cls,
        type: TagType | None = None,
        default: Any = UNSET,
        retentive: bool = False,
    ) -> Any:
        _ = (type, default, retentive)
        return super().__new__(cls)


@dataclass(frozen=True)
class AutoDefault:
    """Descriptor for per-instance numeric default sequences."""

    start: int = 1
    step: int = 1


@dataclass(frozen=True)
class _FieldSpec:
    name: str
    type: TagType
    default: object
    retentive: bool


def auto(*, start: int = 1, step: int = 1) -> Any:
    """Create a per-instance numeric default sequence descriptor."""
    return AutoDefault(start=start, step=step)


def resolve_default(spec: object, index: int) -> object:
    """Resolve a field default specification for a 1-based index."""
    if isinstance(spec, AutoDefault):
        return spec.start + (index - 1) * spec.step
    if spec is UNSET:
        return None
    return spec


class InstanceView:
    """1-based indexed view into one structure instance."""

    def __init__(self, owner: _StructRuntime, index: int):
        self._owner = owner
        self._index = index

    def __getattr__(self, field_name: str) -> LiveTag:
        block = self._owner._blocks.get(field_name)
        if block is None:
            raise AttributeError(f"{type(self._owner).__name__!s} has no field {field_name!r}.")
        return block[self._index]

    def __repr__(self) -> str:
        return f"InstanceView({self._owner.name}[{self._index}])"


class _StructRuntime:
    """Runtime object returned by `@udt`."""

    def __init__(self, name: str, count: int | None, field_specs: tuple[_FieldSpec, ...]):
        _validate_name(name)
        _validate_count(count)
        _validate_fields_present(field_specs)

        self._singleton = count is None
        resolved_count = 1 if count is None else count

        self.name = name
        self.count = resolved_count
        self._original_field_specs = field_specs
        self._field_specs: dict[str, Field] = {}
        self._field_order: tuple[str, ...] = tuple(spec.name for spec in field_specs)
        self._blocks: dict[str, Block] = {}

        for field_spec in field_specs:
            self._field_specs[field_spec.name] = Field(
                type=field_spec.type,
                default=field_spec.default,
                retentive=field_spec.retentive,
            )
            self._blocks[field_spec.name] = Block(
                name=f"{name}.{field_spec.name}",
                type=field_spec.type,
                start=1,
                end=resolved_count,
                retentive=field_spec.retentive,
                address_formatter=(
                    _make_singleton_formatter(name, field_spec.name)
                    if self._singleton
                    else _make_formatter(name, field_spec.name)
                ),
                default_factory=_make_default_factory(field_spec.default),
            )

    def clone(self, name: str) -> _StructRuntime:
        """Create a copy of this structure with a different base name."""
        return _StructRuntime(
            name=name,
            count=None if self._singleton else self.count,
            field_specs=self._original_field_specs,
        )

    @property
    def fields(self) -> dict[str, Field]:
        return dict(self._field_specs)

    @property
    def field_names(self) -> tuple[str, ...]:
        return self._field_order

    def __getitem__(self, index: int) -> InstanceView:
        if self._singleton:
            raise TypeError("singleton struct, no indexing")
        if not isinstance(index, int):
            raise TypeError(f"{type(self).__name__} index must be an int.")
        if index < 1 or index > self.count:
            raise IndexError(f"{type(self).__name__} index {index} out of range 1..{self.count}.")
        return InstanceView(self, index)

    def __getattr__(self, field_name: str) -> Block | LiveTag:
        block = self._blocks.get(field_name)
        if block is None:
            raise AttributeError(f"{type(self).__name__} has no field {field_name!r}.")
        if self._singleton:
            return block[1]
        return block

    def __repr__(self) -> str:
        rendered_count = None if self._singleton else self.count
        return (
            f"{type(self).__name__}({self.name!r}, count={rendered_count}, "
            f"fields={self._field_order!r})"
        )


class _NamedArrayRuntime(_StructRuntime):
    """Runtime object returned by `@named_array`."""

    def __init__(
        self,
        name: str,
        type: TagType,
        *,
        count: int | None,
        stride: int,
        field_specs: tuple[_FieldSpec, ...],
    ):
        _validate_stride(stride)
        if stride < len(field_specs):
            raise ValueError(
                f"stride must be >= declared field count ({len(field_specs)}), got {stride}."
            )

        self.type = type
        self.stride = stride
        super().__init__(name=name, count=count, field_specs=field_specs)

    def clone(self, name: str) -> _NamedArrayRuntime:
        """Create a copy of this named array with a different base name."""
        return _NamedArrayRuntime(
            name=name,
            type=self.type,
            count=None if self._singleton else self.count,
            stride=self.stride,
            field_specs=self._original_field_specs,
        )

    def map_to(self, target: BlockRange) -> list[MappingEntry]:
        """Map this named-array layout to a hardware range."""
        if self._singleton:
            raise TypeError("singleton named_array, no hardware mapping")
        if not isinstance(target, BlockRange):
            raise TypeError("named_array.map_to target must be a BlockRange.")

        hardware_addresses = tuple(target.addresses)
        expected = self.count * self.stride
        if len(hardware_addresses) != expected:
            raise ValueError(
                f"named_array {self.name!r} expects {expected} hardware slots, "
                f"received {len(hardware_addresses)}."
            )

        if len(self._field_order) == 1 and self.stride == 1:
            field_name = self._field_order[0]
            return [self._blocks[field_name].map_to(target)]

        entries: list[MappingEntry] = []
        for index in range(1, self.count + 1):
            base = (index - 1) * self.stride
            for offset, field_name in enumerate(self._field_order):
                logical = self._blocks[field_name][index]
                hardware = target.block[hardware_addresses[base + offset]]
                entries.append(logical.map_to(hardware))
        return entries

    def __repr__(self) -> str:
        rendered_count = None if self._singleton else self.count
        return (
            f"{type(self).__name__}({self.name!r}, {self.type}, count={rendered_count}, "
            f"stride={self.stride}, fields={self._field_order!r})"
        )


def udt(*, count: int | None = None) -> Callable[[type[Any]], _StructRuntime]:
    """Decorator that builds a mixed-type structured runtime from annotations."""
    _validate_count(count)

    def _decorator(cls: type[Any]) -> _StructRuntime:
        name = cls.__name__
        _validate_name(name)
        field_specs = _parse_udt_fields(cls)
        return _StructRuntime(name=name, count=count, field_specs=field_specs)

    return _decorator


def named_array(
    base_type: object, *, count: int | None = None, stride: int = 1
) -> Callable[[type[Any]], _NamedArrayRuntime]:
    """Decorator that builds a single-type, instance-interleaved structured runtime."""
    _validate_count(count)
    _validate_stride(stride)
    resolved_type = _resolve_annotation(base_type, "base_type")

    def _decorator(cls: type[Any]) -> _NamedArrayRuntime:
        name = cls.__name__
        _validate_name(name)
        field_specs = _parse_named_array_fields(cls, resolved_type)
        return _NamedArrayRuntime(
            name=name,
            type=resolved_type,
            count=count,
            stride=stride,
            field_specs=field_specs,
        )

    return _decorator


def _parse_udt_fields(cls: type[Any]) -> tuple[_FieldSpec, ...]:
    annotations = getattr(cls, "__annotations__", {})
    if not isinstance(annotations, dict):
        raise TypeError("UDT annotations must be a dict.")

    classvar_names = {
        field_name
        for field_name, annotation in annotations.items()
        if _is_classvar_annotation(annotation)
    }
    field_annotations = {
        field_name: annotation
        for field_name, annotation in annotations.items()
        if field_name not in classvar_names
    }

    _validate_fields_present(field_annotations)
    _validate_field_names(field_annotations.keys())

    parsed: list[_FieldSpec] = []
    for field_name, annotation in field_annotations.items():
        field_type = _resolve_annotation(annotation, field_name)
        raw_default = cls.__dict__.get(field_name, UNSET)
        parsed.append(_build_field_spec(field_name, field_type, raw_default, source="udt"))
    return tuple(parsed)


def _parse_named_array_fields(cls: type[Any], base_type: TagType) -> tuple[_FieldSpec, ...]:
    annotations = getattr(cls, "__annotations__", {})
    classvar_names: set[str] = set()
    if isinstance(annotations, dict):
        classvar_names = {
            field_name
            for field_name, annotation in annotations.items()
            if _is_classvar_annotation(annotation)
        }

    parsed: list[_FieldSpec] = []
    for field_name, value in cls.__dict__.items():
        if _should_skip_named_array_attr(field_name, value, classvar_names=classvar_names):
            continue
        parsed.append(_build_field_spec(field_name, base_type, value, source="named_array"))

    _validate_fields_present(parsed)
    _validate_field_names(spec.name for spec in parsed)
    return tuple(parsed)


def _is_classvar_annotation(annotation: object) -> bool:
    origin = get_origin(annotation)
    if origin is ClassVar or annotation is ClassVar:
        return True

    if isinstance(annotation, str):
        normalized = annotation.replace(" ", "")
        return (
            normalized == "ClassVar"
            or normalized.startswith("ClassVar[")
            or normalized == "typing.ClassVar"
            or normalized.startswith("typing.ClassVar[")
        )

    return False


def _should_skip_named_array_attr(name: str, value: object, *, classvar_names: set[str]) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return True
    if name in classvar_names:
        return True
    if isinstance(value, (classmethod, staticmethod, property)):
        return True
    return callable(value)


def _build_field_spec(
    field_name: str, type: TagType, raw_default: object, *, source: str
) -> _FieldSpec:
    retentive = False
    default_spec = raw_default

    if isinstance(raw_default, Field):
        if raw_default.type is not None and raw_default.type != type:
            if source == "named_array":
                raise ValueError(
                    f"named_array field {field_name!r} cannot declare type; "
                    "use the decorator base_type."
                )
            raise ValueError(
                f"udt field {field_name!r} type mismatch: annotation resolves to {type.name}, "
                f"but Field.type is {raw_default.type.name}."
            )
        retentive = raw_default.retentive
        default_spec = raw_default.default

    _validate_auto_default_allowed(field_name, default_spec, type)
    return _FieldSpec(name=field_name, type=type, default=default_spec, retentive=retentive)


def _resolve_annotation(annotation: object, field_name: str) -> TagType:
    """Resolve an annotation/base type to TagType."""
    if isinstance(annotation, TagType):
        return annotation

    if isinstance(annotation, str):
        token = annotation.strip().split(".")[-1].strip().strip("'\"")
        resolved = _STRING_TYPE_MAP.get(token.lower())
        if resolved is not None:
            return resolved
        raise TypeError(
            f"Field {field_name!r} annotation {annotation!r} is not supported. "
            "Use Bool/Int/Dint/Real/Word/Char, bool/int/float/str, or IEC names."
        )

    if isinstance(annotation, type) and issubclass(annotation, _TagTypeBase):
        return annotation._tag_type

    primitive = _PRIMITIVE_TYPE_MAP.get(annotation)
    if primitive is not None:
        return primitive

    raise TypeError(
        f"Field {field_name!r} annotation {annotation!r} is not supported. "
        "Use Bool/Int/Dint/Real/Word/Char, bool/int/float/str, or IEC names."
    )


def _make_default_factory(default_spec: object):
    def _factory(index: int) -> object:
        return resolve_default(default_spec, index)

    return _factory


def _make_formatter(struct_name: str, field_name: str):
    def _formatter(_: str, addr: int) -> str:
        return f"{struct_name}{addr}_{field_name}"

    return _formatter


def _make_singleton_formatter(struct_name: str, field_name: str):
    def _formatter(_: str, __: int) -> str:
        return f"{struct_name}_{field_name}"

    return _formatter


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or name.strip() == "":
        raise ValueError("Struct name must be a non-empty string.")


def _validate_count(count: int | None) -> None:
    if count is None:
        return
    if not isinstance(count, int) or count < 1:
        raise ValueError(f"count must be an int >= 1, got {count!r}.")


def _validate_stride(stride: int) -> None:
    if not isinstance(stride, int) or stride < 1:
        raise ValueError(f"stride must be an int >= 1, got {stride!r}.")


def _validate_fields_present(fields: Sized) -> None:
    if len(fields) == 0:
        raise ValueError("At least one field is required.")


def _validate_field_names(field_names: Iterable[str]) -> None:
    for field_name in field_names:
        if field_name in _RESERVED_FIELD_NAMES:
            raise ValueError(f"Field name {field_name!r} is reserved.")


def _validate_auto_default_allowed(field_name: str, default: object, type: TagType) -> None:
    if isinstance(default, AutoDefault) and type not in _NUMERIC_TYPES:
        raise ValueError(
            f"Field {field_name!r} uses auto() but type {type.name} is not numeric. "
            "Supported types: INT, DINT, WORD."
        )

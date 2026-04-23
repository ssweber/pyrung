from __future__ import annotations

from collections.abc import Callable
from enum import IntEnum
from typing import Any, Literal, Protocol

from pyrung.core.memory_block import Block, BlockRange
from pyrung.core.physical import Physical
from pyrung.core.tag import ChoiceMap, LiveTag, MappingEntry, Tag, TagType

class _FieldInfo:
    type: TagType | None
    default: Any
    retentive: bool | None
    choices: ChoiceMap | None
    readonly: bool | None
    external: bool | None
    final: bool | None
    public: bool | None
    physical: Physical | None
    link: str | None
    min: int | float | None
    max: int | float | None
    uom: str | None

def Field(
    type: TagType | None = None,
    default: Any = ...,
    retentive: bool | None = None,
    choices: type[IntEnum] | ChoiceMap | None = None,
    readonly: bool | None = None,
    external: bool | None = None,
    final: bool | None = None,
    public: bool | None = None,
    physical: Physical | None = None,
    link: str | None = None,
    min: int | float | None = None,
    max: int | float | None = None,
    uom: str | None = None,
) -> Any: ...

class AutoDefault:
    start: int
    step: int
    def __init__(self, start: int = 1, step: int = 1) -> None: ...

def auto(*, start: int = 1, step: int = 1) -> Any: ...

class DoneAccUDT(Protocol):
    @property
    def Done(self) -> Tag: ...
    @property
    def Acc(self) -> Tag: ...

class InstanceView:
    def __getattr__(self, field_name: str) -> Tag: ...

class _StructRuntime:
    name: str
    count: int
    always_number: bool
    readonly: bool
    external: bool
    final: bool
    public: bool
    _structure_kind: Literal["udt", "named_array"]
    def clone(
        self,
        name: str,
        *,
        count: int | None = None,
        readonly: bool | None = None,
        external: bool | None = None,
        final: bool | None = None,
        public: bool | None = None,
    ) -> _StructRuntime: ...
    @property
    def fields(self) -> dict[str, _FieldInfo]: ...
    @property
    def field_names(self) -> tuple[str, ...]: ...
    def __getitem__(self, index: int) -> InstanceView: ...
    def __getattr__(self, field_name: str) -> Block | LiveTag: ...

class _DoneAccRuntime(_StructRuntime):
    Done: LiveTag
    Acc: LiveTag
    def clone(
        self,
        name: str,
        *,
        count: int | None = None,
        readonly: bool | None = None,
        external: bool | None = None,
        final: bool | None = None,
        public: bool | None = None,
    ) -> _DoneAccRuntime: ...

class _NamedArrayRuntime(_StructRuntime):
    type: TagType
    stride: int
    def clone(
        self,
        name: str,
        *,
        count: int | None = None,
        stride: int | None = None,
        readonly: bool | None = None,
        external: bool | None = None,
        final: bool | None = None,
        public: bool | None = None,
    ) -> _NamedArrayRuntime: ...
    def hardware_span(self, hw_start: int) -> tuple[int, int]: ...
    def map_to(self, target: BlockRange) -> list[MappingEntry]: ...
    def instance(self, index: int) -> BlockRange: ...
    def instance_select(self, start: int, end: int) -> BlockRange: ...

def udt(
    *,
    count: int = 1,
    always_number: bool = False,
    readonly: bool = False,
    external: bool = False,
    final: bool = False,
    public: bool = False,
) -> Callable[[type[Any]], _StructRuntime]: ...
def named_array(
    base_type: object,
    *,
    count: int = 1,
    stride: int = 1,
    always_number: bool = False,
    readonly: bool = False,
    external: bool = False,
    final: bool = False,
    public: bool = False,
) -> Callable[[type[Any]], _NamedArrayRuntime]: ...

Timer: _DoneAccRuntime
Counter: _DoneAccRuntime

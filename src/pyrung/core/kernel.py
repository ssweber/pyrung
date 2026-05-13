"""Fast replay kernel for compiled PLC programs.

ReplayKernel is a mutable, plain-dict state bag that replaces
the immutable SystemState / ScanContext path for replay workloads.
CompiledKernel holds the compiled step function and metadata
produced by the codegen pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pyrung.core.tag import Tag, TagType

_TYPE_DEFAULTS: dict[TagType, bool | int | float | str] = {
    TagType.BOOL: False,
    TagType.INT: 0,
    TagType.DINT: 0,
    TagType.REAL: 0.0,
    TagType.WORD: 0,
    TagType.CHAR: "",
}

PROVE_EFFECTIVE_PRESET_PREFIX = "_prove:effective_preset:"


def prove_effective_preset_key(done_name: str) -> str:
    """Memory key for the preset value observed by a timer/counter instruction."""
    return f"{PROVE_EFFECTIVE_PRESET_PREFIX}{done_name}"


@dataclass(frozen=True)
class BlockSpec:
    """Metadata for a block-backed array in the kernel state."""

    symbol: str
    size: int
    default: bool | int | float | str
    tag_type: TagType
    tag_names: tuple[str, ...]
    tag_indices: tuple[int, ...] | None = None


class ReplayKernel:
    """Mutable state bag for compiled kernel execution.

    All values live in plain dicts/lists for zero-overhead access
    from the compiled step function.
    """

    __slots__ = ("tags", "blocks", "memory", "prev", "scan_id", "timestamp")

    def __init__(
        self,
        *,
        tag_template: dict[str, bool | int | float | str],
        blocks_template: dict[str, list[bool | int | float | str]],
        prev_template: dict[str, bool | int | float | str],
    ) -> None:
        self.tags: dict[str, bool | int | float | str] = dict(tag_template)
        self.blocks: dict[str, list[bool | int | float | str]] = {
            k: list(v) for k, v in blocks_template.items()
        }
        self.memory: dict[str, Any] = {}
        self.prev: dict[str, bool | int | float | str] = dict(prev_template)
        self.scan_id: int = 0
        self.timestamp: float = 0.0

    def snapshot_tags(self) -> dict[str, bool | int | float | str]:
        """Return a shallow copy of the merged tag dict (scalars + block elements)."""
        merged: dict[str, bool | int | float | str] = dict(self.tags)
        return merged

    def load_block_from_tags(self, spec: BlockSpec) -> None:
        """Populate a block array from the corresponding tag values."""
        arr = self.blocks[spec.symbol]
        indices = spec.tag_indices or range(len(spec.tag_names))
        for idx, name in zip(indices, spec.tag_names, strict=True):
            if name in self.tags:
                arr[idx] = self.tags[name]

    def flush_block_to_tags(self, spec: BlockSpec) -> None:
        """Write block array values back to the tag dict."""
        arr = self.blocks[spec.symbol]
        indices = spec.tag_indices or range(len(spec.tag_names))
        for idx, name in zip(indices, spec.tag_names, strict=True):
            self.tags[name] = arr[idx]

    def capture_prev(self, edge_tags: set[str]) -> None:
        """Snapshot current tag values into prev for edge detection."""
        for name in edge_tags:
            if name in self.tags:
                self.prev[name] = self.tags[name]

    def advance(self, dt: float) -> None:
        """Increment scan_id and accumulate timestamp."""
        self.scan_id += 1
        self.timestamp += dt


@dataclass(frozen=True)
class CompiledKernel:
    """Compiled artifact produced by the codegen pipeline.

    The step_fn signature is::

        step_fn(
            tags: dict[str, value],
            blocks: dict[str, list[value]],
            memory: dict[str, Any],
            prev: dict[str, value],
            dt: float,
        ) -> None

    It mutates the dicts in place — no return value.
    """

    step_fn: Callable[..., None]
    referenced_tags: dict[str, Tag] = field(default_factory=dict)
    block_specs: dict[str, BlockSpec] = field(default_factory=dict)
    edge_tags: set[str] = field(default_factory=set)
    source: str = ""
    blockless: bool = False
    has_io_gaps: bool = False
    indirect_block_info: dict[str, tuple[str, int, int, frozenset[int]]] = field(
        default_factory=dict
    )
    materialized_tag_names: frozenset[str] = field(default_factory=frozenset)
    _tag_template: dict[str, bool | int | float | str] = field(
        init=False, repr=False, default_factory=dict
    )
    _blocks_template: dict[str, list[bool | int | float | str]] = field(
        init=False, repr=False, default_factory=dict
    )
    _prev_template: dict[str, bool | int | float | str] = field(
        init=False, repr=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        tag_template: dict[str, bool | int | float | str] = {
            name: tag.default for name, tag in self.referenced_tags.items()
        }
        for spec in self.block_specs.values():
            for name in spec.tag_names:
                tag_template.setdefault(name, spec.default)
        object.__setattr__(self, "_tag_template", tag_template)

        if self.blockless:
            object.__setattr__(self, "_blocks_template", {})
        else:
            blocks_template: dict[str, list[bool | int | float | str]] = {
                spec.symbol: [spec.default] * spec.size for spec in self.block_specs.values()
            }
            object.__setattr__(self, "_blocks_template", blocks_template)

        prev_template: dict[str, bool | int | float | str] = {
            name: self.referenced_tags[name].default for name in self.edge_tags
        }
        object.__setattr__(self, "_prev_template", prev_template)

    def create_kernel(self) -> ReplayKernel:
        """Create a fresh ReplayKernel initialized from this compiled program."""
        return ReplayKernel(
            tag_template=self._tag_template,
            blocks_template=self._blocks_template,
            prev_template=self._prev_template,
        )

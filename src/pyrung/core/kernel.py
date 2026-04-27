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


@dataclass(frozen=True)
class BlockSpec:
    """Metadata for a block-backed array in the kernel state."""

    symbol: str
    size: int
    default: bool | int | float | str
    tag_type: TagType
    tag_names: tuple[str, ...]


class ReplayKernel:
    """Mutable state bag for compiled kernel execution.

    All values live in plain dicts/lists for zero-overhead access
    from the compiled step function.
    """

    __slots__ = ("tags", "blocks", "memory", "prev", "scan_id", "timestamp")

    def __init__(
        self,
        *,
        referenced_tags: dict[str, Tag],
        block_specs: dict[str, BlockSpec],
        edge_tags: set[str],
    ) -> None:
        self.tags: dict[str, bool | int | float | str] = {
            name: tag.default for name, tag in referenced_tags.items()
        }
        self.blocks: dict[str, list[bool | int | float | str]] = {
            spec.symbol: [spec.default] * spec.size for spec in block_specs.values()
        }
        self.memory: dict[str, Any] = {}
        self.prev: dict[str, bool | int | float | str] = {
            name: referenced_tags[name].default for name in edge_tags
        }
        self.scan_id: int = 0
        self.timestamp: float = 0.0

    def snapshot_tags(self) -> dict[str, bool | int | float | str]:
        """Return a shallow copy of the merged tag dict (scalars + block elements)."""
        merged: dict[str, bool | int | float | str] = dict(self.tags)
        return merged

    def load_block_from_tags(self, spec: BlockSpec) -> None:
        """Populate a block array from the corresponding tag values."""
        arr = self.blocks[spec.symbol]
        for i, name in enumerate(spec.tag_names):
            if name in self.tags:
                arr[i] = self.tags[name]

    def flush_block_to_tags(self, spec: BlockSpec) -> None:
        """Write block array values back to the tag dict."""
        arr = self.blocks[spec.symbol]
        for i, name in enumerate(spec.tag_names):
            self.tags[name] = arr[i]

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
    has_io_gaps: bool = False

    def create_kernel(self) -> ReplayKernel:
        """Create a fresh ReplayKernel initialized from this compiled program."""
        return ReplayKernel(
            referenced_tags=self.referenced_tags,
            block_specs=self.block_specs,
            edge_tags=self.edge_tags,
        )

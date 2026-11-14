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

from pyrung.core.storage import BLANK_CHAR
from pyrung.core.system_points import _DERIVED_TAG_NAMES, HOOK_BOUND_TAG_NAMES
from pyrung.core.tag import Tag, TagType

_TYPE_DEFAULTS: dict[TagType, bool | int | float | str] = {
    TagType.BOOL: False,
    TagType.INT: 0,
    TagType.DINT: 0,
    TagType.REAL: 0.0,
    TagType.WORD: 0,
    TagType.CHAR: BLANK_CHAR,
}

PROVE_EFFECTIVE_PRESET_PREFIX = "_prove:effective_preset:"


def prove_effective_preset_key(done_name: str) -> str:
    """Memory key for the preset value observed by a timer/counter instruction."""
    return f"{PROVE_EFFECTIVE_PRESET_PREFIX}{done_name}"


#: The most iterations the verifier's kernel runs one For for in one scan.  A
#: 16-bit count runs at most 65535, under it; a Dint count is read whole and
#: signed (``control.for_loop_iterations``), so a positive one can run up to
#: 2147483647 and the search would not return.
PROVE_FOR_ITERATION_CAP = 1_000_000


class ForIterationsBeyondCap(Exception):
    """A For in the verifier's kernel was about to run past ``PROVE_FOR_ITERATION_CAP``.

    Raised by the kernel ``compile_kernel(proof_metadata=True)`` builds, only
    for a For whose count is read 32-bit; the verifier answers ``Intractable``.
    """

    def __init__(self, where: str, count: str, tags: tuple[str, ...], iterations: int) -> None:
        super().__init__(f"forloop({count}) at {where} would run {iterations} times in one scan")
        self.where = where
        self.count = count
        self.iterations = iterations
        #: The tags the count reads: the count tag, or an indexed count's pointer.
        self.tags = tags


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

    def reset(
        self,
        tag_template: dict[str, bool | int | float | str],
        prev_template: dict[str, bool | int | float | str],
    ) -> None:
        """Re-initialize from templates, reusing existing dict objects."""
        tags = self.tags
        tags.clear()
        tags.update(tag_template)
        prev = self.prev
        prev.clear()
        prev.update(prev_template)
        self.memory.clear()
        self.scan_id = 0
        self.timestamp = 0.0


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
    # ``source`` with the text of each function it calls, which it holds by reference:
    # what a lock hashes, not what runs.
    lock_source: str = ""
    blockless: bool = False
    has_io_gaps: bool = False
    # Optional leading pass, run *before* the input drain so it reads the previous
    # commit (the synthesis ``plant`` / input-read phase).  ``None`` ⇒ a single
    # ``step_fn`` covers the whole scan (the common case; deploy/prove/bare replay).
    # Shares the runtime kernel's tags/blocks/memory with ``step_fn`` — same
    # signature — because both are compiled from one program (union templates).
    pre_step_fn: Callable[..., None] | None = None
    # Whether its passes can call a ``run_function`` body, which could ask for a scan.
    calls_functions: bool = True
    indirect_block_info: dict[str, tuple[str, int, int, frozenset[int]]] = field(
        default_factory=dict
    )
    materialized_tag_names: frozenset[str] = field(default_factory=frozenset)
    # Nanoseconds to a unit, per accumulator whose ``_frac:`` carry the kernel ticks.
    timer_unit_ns: dict[str, int] = field(default_factory=dict)
    _tag_template: dict[str, bool | int | float | str] = field(
        init=False, repr=False, default_factory=dict
    )
    _blocks_template: dict[str, list[bool | int | float | str]] = field(
        init=False, repr=False, default_factory=dict
    )
    _prev_template: dict[str, bool | int | float | str] = field(
        init=False, repr=False, default_factory=dict
    )
    # The scalars ``compiled_plc.absent_scalars`` may add, at the default the kernel holds.
    _scalar_defaults: tuple[tuple[str, Any], ...] = field(
        init=False, repr=False, compare=False, default=()
    )

    def __post_init__(self) -> None:
        tag_template: dict[str, bool | int | float | str] = {
            name: tag.default for name, tag in self.referenced_tags.items()
        }
        for spec in self.block_specs.values():
            for name in spec.tag_names:
                tag_template.setdefault(name, spec.default)
        object.__setattr__(self, "_tag_template", tag_template)
        cells = {name for spec in self.block_specs.values() for name in spec.tag_names}
        scalar_defaults = tuple(
            (name, default)
            for name, default in tag_template.items()
            if name not in cells
            and name not in _DERIVED_TAG_NAMES
            and name not in HOOK_BOUND_TAG_NAMES
        )
        object.__setattr__(self, "_scalar_defaults", scalar_defaults)

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

"""Static program dependence graph extraction for pyrung programs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from dataclasses import replace as _dc_replace
from enum import Enum
from types import BuiltinFunctionType, FunctionType, MethodType
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.condition import Condition
from pyrung.core.expression import Expression
from pyrung.core.instruction.base import Instruction
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.coils import OutInstruction
from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction
from pyrung.core.instruction.data_transfer import BlockCopyInstruction, CopyInstruction
from pyrung.core.instruction.packing import PackTextInstruction
from pyrung.core.memory_block import (
    Block,
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.tag import ImmediateRef, InputTag, OutputTag, Tag, TagType
from pyrung.core.validation.walker import _condition_children, _instruction_fields

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

GraphScope = Literal["main", "subroutine"]


class TagRole(Enum):
    """Structural role of a tag in the program graph."""

    INPUT = "input"
    PIVOT = "pivot"
    TERMINAL = "terminal"
    ISOLATED = "isolated"


@dataclass(frozen=True)
class RungNode:
    """Static summary of one rung or branch rung."""

    rung_index: int
    scope: GraphScope
    subroutine: str | None
    branch_path: tuple[int, ...]
    condition_reads: frozenset[str]
    data_reads: frozenset[str]
    exclusive_reads: frozenset[str]
    writes: frozenset[str]
    implicit_writes: frozenset[str]
    ote_writes: frozenset[str]
    calls: tuple[str, ...]
    source_file: str | None
    source_line: int | None
    guard_reads: frozenset[str] = frozenset()

    @property
    def all_writes(self) -> frozenset[str]:
        return self.writes | self.implicit_writes


@dataclass(frozen=True)
class TagVersion:
    """A single intra-scan version of a tag.

    ``defined_at`` and ``read_by`` use indexes into ``ProgramGraph.rung_nodes``.
    ``defined_at=None`` denotes the scan-entry value.
    """

    tag: str
    defined_at: int | None
    read_by: frozenset[int]


@dataclass
class ProgramGraph:
    """Static PDG-style summary for a Program."""

    rung_nodes: tuple[RungNode, ...]
    tag_roles: dict[str, TagRole]
    def_use_chains: dict[str, tuple[TagVersion, ...]]
    readers_of: dict[str, frozenset[int]]  # tag → node indices (rung_nodes position)
    all_readers_of: dict[str, frozenset[int]]  # includes exclusive_reads
    writers_of: dict[str, frozenset[int]]  # node indices — use timeline_writers_of() for timeline keys
    tags: dict[str, Tag]
    block_ranges: dict[str, list[str]]  # range label → member tag names
    pointer_tags: dict[str, tuple[str, int, int]]  # pointer name → (block, start, end)
    _main_node_index: dict[int, int] | None = field(default=None, init=False, repr=False)
    _call_site_cache: dict[str, frozenset[int]] | None = field(
        default=None, init=False, repr=False
    )

    @classmethod
    def from_program(cls, program: Program) -> ProgramGraph:
        return build_program_graph(program)

    def main_node_by_rung(self) -> dict[int, int]:
        """Map main-program rung_index → node index for top-level nodes."""
        if self._main_node_index is None:
            self._main_node_index = {
                node.rung_index: i
                for i, node in enumerate(self.rung_nodes)
                if node.scope == "main" and not node.branch_path
            }
        return self._main_node_index

    def _call_site_rung_indices(self) -> dict[str, frozenset[int]]:
        """Map subroutine name → main-rung indices of all call sites.

        Cached; used by :meth:`timeline_writers_of` to resolve subroutine
        writers to the main-rung indices the executor's ``capturing_rung``
        rolls them up under.  Includes branch call sites — branches
        execute under the same capturing scope as their parent rung.
        """
        if self._call_site_cache is not None:
            return self._call_site_cache
        sites: dict[str, set[int]] = {}
        for node in self.rung_nodes:
            if node.scope == "main" and node.calls:
                for sub_name in node.calls:
                    sites.setdefault(sub_name, set()).add(node.rung_index)
        self._call_site_cache = {name: frozenset(idxs) for name, idxs in sites.items()}
        return self._call_site_cache

    def timeline_writers_of(self, tag_name: str) -> frozenset[int]:
        """Main-rung indices whose ``capturing_rung`` scope captures writes to *tag_name*.

        For main-scope writers, returns ``node.rung_index`` directly.
        For subroutine writers, returns the call-site main-rung indices —
        the executor runs subroutines inside the caller's capture scope,
        so the timeline keys are call-site indices, not PDG node indices.
        """
        node_indices = self.writers_of.get(tag_name, frozenset())
        if not node_indices:
            return frozenset()
        main_indices: set[int] = set()
        call_sites = self._call_site_rung_indices()
        for ni in node_indices:
            node = self.rung_nodes[ni]
            if node.subroutine is None:
                main_indices.add(node.rung_index)
            else:
                main_indices.update(call_sites.get(node.subroutine, frozenset()))
        return frozenset(main_indices)

    def timeline_capture_indices_for_node(self, node_index: int) -> frozenset[int]:
        """Main-rung timeline indices that can capture writes from one PDG node."""
        node = self.rung_nodes[node_index]
        if node.subroutine is None:
            return frozenset({node.rung_index})
        return self._call_site_rung_indices().get(node.subroutine, frozenset())

    def is_physical_input(self, tag_name: str) -> bool:
        """Return whether ``tag_name`` resolves to a physical input tag."""
        return isinstance(self.tags.get(tag_name), InputTag)

    def is_physical_output(self, tag_name: str) -> bool:
        """Return whether ``tag_name`` resolves to a physical output tag."""
        return isinstance(self.tags.get(tag_name), OutputTag)

    def _collapse_map(self) -> dict[str, str]:
        """Build tag_name → range_label mapping for collapsible ranges."""
        collapse: dict[str, str] = {}
        for label, members in self.block_ranges.items():
            for name in members:
                collapse[name] = label
        return collapse

    def graph_edges(
        self,
        *,
        collapse: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Bipartite edges for visualization: tag→rung (reads) and rung→tag (writes).

        Returns list of ``{source, target, type}`` where *type* is
        ``"condition"`` | ``"data"`` | ``"write"``.  Sources and targets are
        tag names or ``"rung:<index>"`` identifiers.

        When *collapse* is provided, member tag names are replaced by their
        range label and duplicate edges are suppressed.
        """
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] | None = set() if collapse else None
        for idx, node in enumerate(self.rung_nodes):
            rung_id = f"rung:{idx}"
            for tag_name in sorted(node.condition_reads):
                src = collapse.get(tag_name, tag_name) if collapse else tag_name
                key = (src, rung_id, "condition")
                if seen is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                edges.append({"source": src, "target": rung_id, "type": "condition"})
            for tag_name in sorted(node.data_reads):
                src = collapse.get(tag_name, tag_name) if collapse else tag_name
                key = (src, rung_id, "data")
                if seen is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                edges.append({"source": src, "target": rung_id, "type": "data"})
            for tag_name in sorted(node.all_writes):
                tgt = collapse.get(tag_name, tag_name) if collapse else tag_name
                key = (rung_id, tgt, "write")
                if seen is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                edges.append({"source": rung_id, "target": tgt, "type": "write"})
        return edges

    def unconditional_write_before_read(self, tag_name: str) -> bool:
        """True when def-use chains prove unconditional write-before-read.

        The entry version must have no readers (no read precedes the first
        write in program order) *and* the first write must be unconditional.
        The unconditional requirement is essential: def-use chains are
        sequential SSA and optimistically assume every write fires, so a
        conditional first write could be skipped at runtime, leaving a later
        read to observe the entry version.

        A main-scope unconditional write always executes.  A subroutine-scope
        unconditional write is only guaranteed to precede every read when
        either (A) every reader is confined to the same subroutine — so no
        reader runs on a path where that subroutine did not — or (B) the
        subroutine is invoked unconditionally on every scan.

        Return-early guards are stripped from the condition check: a rung
        that is unconditional in its own right but guarded by a preceding
        ``return_early()`` is treated as unconditional, because all
        subsequent reads in the same subroutine share the same guard and
        guards grow monotonically down the subroutine.
        """
        chain = self.def_use_chains.get(tag_name)
        if not chain:
            return False
        entry_version = chain[0]
        if entry_version.defined_at is not None or entry_version.read_by:
            return False
        if len(chain) < 2:
            return False
        first_write = chain[1]
        if first_write.defined_at is None:
            return False
        node = self.rung_nodes[first_write.defined_at]
        # Strip return_early guards: both the write and all subsequent reads
        # share the same monotonically-accumulated guard, so if the write is
        # skipped (guard fired), every read is also skipped.
        own_conditions = node.condition_reads - node.guard_reads
        if own_conditions:
            return False
        if node.scope == "main":
            return True
        sub = node.subroutine
        if sub in _always_run_subroutines(self):
            return True
        return all(
            self.rung_nodes[ri].subroutine == sub
            for ri in self.all_readers_of.get(tag_name, frozenset())
        )

    def upstream_slice(self, tag_name: str) -> frozenset[str]:
        """Return all tags transitively upstream of *tag_name*."""
        visited_tags: set[str] = set()
        visited_rungs: set[int] = set()
        queue: list[str] = [tag_name]

        while queue:
            current = queue.pop()
            if current in visited_tags:
                continue
            visited_tags.add(current)
            for rung_idx in self.writers_of.get(current, frozenset()):
                if rung_idx in visited_rungs:
                    continue
                visited_rungs.add(rung_idx)
                node = self.rung_nodes[rung_idx]
                for read_tag in node.condition_reads | node.data_reads:
                    if read_tag not in visited_tags:
                        queue.append(read_tag)

        visited_tags.discard(tag_name)
        return frozenset(visited_tags)

    def upstream_slice_all(self, tag_name: str) -> frozenset[str]:
        """Like ``upstream_slice`` but also follows ``exclusive_reads``."""
        visited_tags: set[str] = set()
        visited_rungs: set[int] = set()
        queue: list[str] = [tag_name]

        while queue:
            current = queue.pop()
            if current in visited_tags:
                continue
            visited_tags.add(current)
            for rung_idx in self.writers_of.get(current, frozenset()):
                if rung_idx in visited_rungs:
                    continue
                visited_rungs.add(rung_idx)
                node = self.rung_nodes[rung_idx]
                for read_tag in node.condition_reads | node.data_reads | node.exclusive_reads:
                    if read_tag not in visited_tags:
                        queue.append(read_tag)

        visited_tags.discard(tag_name)
        return frozenset(visited_tags)

    def upstream_slice_with_calls(self, tag_name: str) -> frozenset[str]:
        """Like ``upstream_slice_all`` but also follows call-site conditions.

        When an upstream rung lives in a subroutine, the caller rung's
        condition_reads are included — the subroutine only executes when
        the caller fires.
        """
        visited_tags: set[str] = set()
        visited_rungs: set[int] = set()
        visited_subs: set[str] = set()
        queue: list[str] = [tag_name]

        while queue:
            current = queue.pop()
            if current in visited_tags:
                continue
            visited_tags.add(current)
            for rung_idx in self.writers_of.get(current, frozenset()):
                if rung_idx in visited_rungs:
                    continue
                visited_rungs.add(rung_idx)
                node = self.rung_nodes[rung_idx]
                for read_tag in node.condition_reads | node.data_reads | node.exclusive_reads:
                    if read_tag not in visited_tags:
                        queue.append(read_tag)
                if node.subroutine is not None and node.subroutine not in visited_subs:
                    visited_subs.add(node.subroutine)
                    for caller in self.rung_nodes:
                        if node.subroutine in caller.calls:
                            for read_tag in caller.condition_reads:
                                if read_tag not in visited_tags:
                                    queue.append(read_tag)

        visited_tags.discard(tag_name)
        return frozenset(visited_tags)

    def downstream_slice(self, tag_name: str) -> frozenset[str]:
        """Return all tags transitively downstream of *tag_name*."""
        visited_tags: set[str] = set()
        visited_rungs: set[int] = set()
        queue: list[str] = [tag_name]

        while queue:
            current = queue.pop()
            if current in visited_tags:
                continue
            visited_tags.add(current)
            for rung_idx in self.readers_of.get(current, frozenset()):
                if rung_idx in visited_rungs:
                    continue
                visited_rungs.add(rung_idx)
                node = self.rung_nodes[rung_idx]
                for written_tag in node.writes:
                    if written_tag not in visited_tags:
                        queue.append(written_tag)

        visited_tags.discard(tag_name)
        return frozenset(visited_tags)

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize the graph for DAP/webview consumption.

        Block ranges with 3+ members are collapsed into single nodes.
        """
        collapse = self._collapse_map()
        collapsed_members = set(collapse.keys())

        # Collapsed tag roles: keep non-collapsed tags, add range labels
        tag_roles: dict[str, str] = {}
        for name, role in sorted(self.tag_roles.items()):
            if name not in collapsed_members:
                tag_roles[name] = role.value
        for label in sorted(self.block_ranges):
            tag_roles[label] = "pivot"  # ranges are typically intermediate data

        # Collapsed tag list
        tags = sorted(set(self.tags.keys()) - collapsed_members | set(self.block_ranges.keys()))

        # Collapsed readers/writers: remap member indices to range label
        readers_of: dict[str, list[int]] = {}
        writers_of: dict[str, list[int]] = {}
        for name, indices in sorted(self.readers_of.items()):
            key = collapse.get(name, name)
            merged = readers_of.get(key, [])
            merged.extend(indices)
            readers_of[key] = merged
        for name, indices in sorted(self.writers_of.items()):
            key = collapse.get(name, name)
            merged = writers_of.get(key, [])
            merged.extend(indices)
            writers_of[key] = merged
        # Deduplicate and sort
        readers_of = {k: sorted(set(v)) for k, v in readers_of.items()}
        writers_of = {k: sorted(set(v)) for k, v in writers_of.items()}

        return {
            "rungNodes": [
                {
                    "rungIndex": node.rung_index,
                    "scope": node.scope,
                    "subroutine": node.subroutine,
                    "branchPath": list(node.branch_path),
                    "conditionReads": sorted(node.condition_reads),
                    "dataReads": sorted(node.data_reads),
                    "writes": sorted(node.all_writes),
                    "calls": list(node.calls),
                    "sourceFile": node.source_file,
                    "sourceLine": node.source_line,
                }
                for node in self.rung_nodes
            ],
            "tagRoles": tag_roles,
            "tags": tags,
            "readersOf": readers_of,
            "writersOf": writers_of,
            "graphEdges": self.graph_edges(collapse=collapse),
            "blockRanges": {label: members for label, members in sorted(self.block_ranges.items())},
        }


@dataclass(frozen=True)
class _AccessEvent:
    node_index: int
    condition_reads: frozenset[str] = frozenset()
    data_reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()


def _register_tag(tag: Tag, tag_refs: dict[str, Tag], found: set[str]) -> None:
    tag_refs.setdefault(tag.name, tag)
    found.add(tag.name)


def _block_tags(block_range: BlockRange | IndirectBlockRange) -> list[Tag]:
    if isinstance(block_range, BlockRange):
        return block_range.tags()

    block = block_range.block
    return [block._get_tag(addr) for addr in block._window_addresses(block.start, block.end)]


_INDIRECT_BLOCK_CAP = 1000


def _full_block_tags(block: Block) -> list[Tag]:
    """Return all tags in a block's range, materializing as needed."""
    size = block.end - block.start + 1
    if size > _INDIRECT_BLOCK_CAP:
        return []
    return [block._get_tag(addr) for addr in range(block.start, block.end + 1)]


def _indirect_ref_tags(block: Block, pointer: Tag) -> list[Tag] | None:
    """Narrow an indirect block access using pointer tag metadata.

    Returns the bounded tag list when choices or min/max constrain the
    pointer, or ``None`` when the address is statically unbounded.
    """
    if pointer.choices is not None:
        addrs = sorted(int(k) for k in pointer.choices if block.start <= int(k) <= block.end)
        return [block._get_tag(a) for a in addrs]
    if pointer.min is not None and pointer.max is not None:
        lo = max(int(pointer.min), block.start)
        hi = min(int(pointer.max), block.end)
        if lo <= hi:
            return _block_tags(block.select(lo, hi))
    return None


def _indirect_expr_base_tag(expr: Any) -> Tag | None:
    """Walk an Expression tree to find the underlying Tag, if any."""
    from pyrung.core.expression import Expression

    if isinstance(expr, Tag):
        return expr
    if isinstance(expr, Expression):
        for key in vars(expr):
            if key.startswith("_"):
                continue
            child = getattr(expr, key)
            result = _indirect_expr_base_tag(child)
            if result is not None:
                return result
    return None


_RANGE_COLLAPSE_THRESHOLD = 3


def _record_range(
    ranges: dict[str, list[str]],
    block_name: str,
    tags: list[Tag],
) -> None:
    """Merge *tags* into a single per-block range entry in *ranges*."""
    existing = ranges.get(block_name)
    if existing is not None:
        seen = set(existing)
        for t in tags:
            if t.name not in seen:
                existing.append(t.name)
                seen.add(t.name)
    else:
        ranges[block_name] = [t.name for t in tags]


def _extract_tag_names(
    value: Any,
    tag_refs: dict[str, Tag],
    ranges: dict[str, list[str]] | None = None,
) -> set[str]:
    """Extract statically-known tag names from values, expressions, and refs.

    If *ranges* is provided, static ``BlockRange`` accesses with
    ``_RANGE_COLLAPSE_THRESHOLD`` or more elements are recorded as
    ``{group_label: [member_tag_name, ...]}`` entries.
    """
    found: set[str] = set()
    seen: set[int] = set()

    def walk(current: Any) -> None:
        if current is None:
            return
        if isinstance(current, (bool, int, float, str, bytes, bytearray, Enum)):
            return

        current_id = id(current)
        if current_id in seen:
            return
        seen.add(current_id)

        if isinstance(current, ImmediateRef):
            walk(current.value)
            return

        if isinstance(current, Tag):
            _register_tag(current, tag_refs, found)
            return

        if isinstance(current, BlockRange | IndirectBlockRange):
            tags = _block_tags(current)
            for tag in tags:
                _register_tag(tag, tag_refs, found)
            if (
                ranges is not None
                and isinstance(current, BlockRange)
                and len(tags) >= _RANGE_COLLAPSE_THRESHOLD
            ):
                _record_range(ranges, current.block.name, tags)
            if isinstance(current, IndirectBlockRange):
                walk(current.start_expr)
                walk(current.end_expr)
            return

        if isinstance(current, IndirectRef):
            walk(current.pointer)
            tags = _indirect_ref_tags(current.block, current.pointer)
            if tags is not None:
                for tag in tags:
                    _register_tag(tag, tag_refs, found)
                if ranges is not None and len(tags) >= _RANGE_COLLAPSE_THRESHOLD:
                    _record_range(ranges, current.block.name, tags)
            return

        if isinstance(current, IndirectExprRef):
            walk(current.expr)
            base = _indirect_expr_base_tag(current.expr)
            tags = _indirect_ref_tags(current.block, base) if base is not None else None
            if tags is not None:
                for tag in tags:
                    _register_tag(tag, tag_refs, found)
                if ranges is not None and len(tags) >= _RANGE_COLLAPSE_THRESHOLD:
                    _record_range(ranges, current.block.name, tags)
            return

        if isinstance(current, Condition):
            for _, child in _condition_children(current):
                walk(child)
            return

        if isinstance(current, Expression):
            for key in sorted(vars(current)):
                if key.startswith("_"):
                    continue
                walk(getattr(current, key))
            return

        if isinstance(current, dict):
            for key in sorted(current, key=repr):
                walk(current[key])
            return

        if isinstance(current, (list, tuple)):
            for item in current:
                walk(item)
            return

        if isinstance(current, (set, frozenset)):
            for item in sorted(current, key=repr):
                walk(item)
            return

        if hasattr(current, "__dict__"):
            for key in sorted(vars(current)):
                if key.startswith("_"):
                    continue
                walk(getattr(current, key))

    walk(value)
    return found


def _extract_write_targets(
    value: Any,
    tag_refs: dict[str, Tag],
    ranges: dict[str, list[str]] | None = None,
) -> tuple[set[str], set[str]]:
    """Extract written tags plus any address-resolution reads for a target."""
    writes: set[str] = set()
    reads: set[str] = set()
    seen: set[int] = set()

    def walk_target(current: Any) -> None:
        if current is None:
            return
        if isinstance(current, (bool, int, float, str, bytes, bytearray, Enum)):
            return

        current_id = id(current)
        if current_id in seen:
            return
        seen.add(current_id)

        if isinstance(current, ImmediateRef):
            walk_target(current.value)
            return

        if isinstance(current, Tag):
            _register_tag(current, tag_refs, writes)
            return

        if isinstance(current, BlockRange | IndirectBlockRange):
            tags = _block_tags(current)
            for tag in tags:
                _register_tag(tag, tag_refs, writes)
            if (
                ranges is not None
                and isinstance(current, BlockRange)
                and len(tags) >= _RANGE_COLLAPSE_THRESHOLD
            ):
                _record_range(ranges, current.block.name, tags)
            if isinstance(current, IndirectBlockRange):
                reads.update(_extract_tag_names(current.start_expr, tag_refs, ranges=ranges))
                reads.update(_extract_tag_names(current.end_expr, tag_refs, ranges=ranges))
            return

        if isinstance(current, IndirectRef):
            reads.update(_extract_tag_names(current.pointer, tag_refs, ranges=ranges))
            tags = _indirect_ref_tags(current.block, current.pointer)
            if tags is None:
                tags = _full_block_tags(current.block)
            for tag in tags:
                _register_tag(tag, tag_refs, writes)
            if ranges is not None and len(tags) >= _RANGE_COLLAPSE_THRESHOLD:
                _record_range(ranges, current.block.name, tags)
            return

        if isinstance(current, IndirectExprRef):
            reads.update(_extract_tag_names(current.expr, tag_refs, ranges=ranges))
            base = _indirect_expr_base_tag(current.expr)
            tags = _indirect_ref_tags(current.block, base) if base is not None else None
            if tags is None:
                tags = _full_block_tags(current.block)
            for tag in tags:
                _register_tag(tag, tag_refs, writes)
            if ranges is not None and len(tags) >= _RANGE_COLLAPSE_THRESHOLD:
                _record_range(ranges, current.block.name, tags)
            return

        if isinstance(current, dict):
            for key in sorted(current, key=repr):
                walk_target(current[key])
            return

        if isinstance(current, (list, tuple)):
            for item in current:
                walk_target(item)
            return

        if isinstance(current, (set, frozenset)):
            for item in sorted(current, key=repr):
                walk_target(item)
            return

    walk_target(value)
    return writes, reads


def _extract_reads_from_condition(
    condition: Condition | None,
    tag_refs: dict[str, Tag],
) -> set[str]:
    """Extract read tag names from a condition tree."""
    if condition is None:
        return set()
    return _extract_tag_names(condition, tag_refs)


def _extract_rung_node(
    rung: Rung,
    *,
    rung_index: int,
    scope: GraphScope,
    subroutine: str | None,
    branch_path: tuple[int, ...],
    tag_refs: dict[str, Tag],
    range_acc: dict[str, list[str]] | None = None,
) -> RungNode:
    """Extract one rung/branch rung into a static node summary."""
    condition_reads: set[str] = set()
    data_reads: set[str] = set()
    exclusive_reads: set[str] = set()
    writes: set[str] = set()
    implicit_writes: set[str] = set()
    ote_writes: set[str] = set()
    calls: list[str] = []

    for condition in rung._conditions:
        condition_reads.update(_extract_reads_from_condition(condition, tag_refs))

    def walk_instruction(instr: Any) -> None:
        if isinstance(instr, CallInstruction):
            calls.append(instr.subroutine_name)

        if _instruction_fields(instr) is None:
            return

        cls = type(instr)
        for field_name in getattr(cls, "_reads", ()):
            data_reads.update(
                _extract_tag_names(getattr(instr, field_name), tag_refs, ranges=range_acc)
            )

        instruction_writes, target_reads, instr_implicit_writes = _extract_instruction_writes(
            instr,
            tag_refs,
            ranges=range_acc,
        )
        writes.update(instruction_writes)
        implicit_writes.update(instr_implicit_writes)
        data_reads.update(target_reads)
        if isinstance(instr, OutInstruction):
            ote_writes.update(instruction_writes)

        for field_name in getattr(cls, "_exclusive_fields", ()):
            exclusive_reads.update(
                _extract_tag_names(getattr(instr, field_name), tag_refs, ranges=range_acc)
            )

        for field_name in getattr(cls, "_conditions", ()):
            condition_reads.update(_extract_tag_names(getattr(instr, field_name), tag_refs))

        if isinstance(instr, ForLoopInstruction):
            for child_instr in instr.instructions:
                walk_instruction(child_instr)

    for instruction in rung._instructions:
        walk_instruction(instruction)

    return RungNode(
        rung_index=rung_index,
        scope=scope,
        subroutine=subroutine,
        branch_path=branch_path,
        condition_reads=frozenset(condition_reads),
        data_reads=frozenset(data_reads),
        exclusive_reads=frozenset(exclusive_reads),
        writes=frozenset(writes),
        implicit_writes=frozenset(implicit_writes),
        ote_writes=frozenset(ote_writes),
        calls=tuple(calls),
        source_file=getattr(rung, "source_file", None),
        source_line=getattr(rung, "source_line", None),
    )


def _extract_instruction_event(
    instr: Any, node_index: int, tag_refs: dict[str, Tag]
) -> _AccessEvent:
    """Extract one instruction's ordered reads/writes."""
    condition_reads: set[str] = set()
    data_reads: set[str] = set()
    writes: set[str] = set()

    cls = type(instr)
    for field_name in getattr(cls, "_reads", ()):
        data_reads.update(_extract_tag_names(getattr(instr, field_name), tag_refs))

    instruction_writes, target_reads, _implicit_writes = _extract_instruction_writes(
        instr, tag_refs
    )
    writes.update(instruction_writes)
    data_reads.update(target_reads)

    for field_name in getattr(cls, "_conditions", ()):
        condition_reads.update(_extract_tag_names(getattr(instr, field_name), tag_refs))

    return _AccessEvent(
        node_index=node_index,
        condition_reads=frozenset(condition_reads),
        data_reads=frozenset(data_reads),
        writes=frozenset(writes),
    )


def _extract_instruction_writes(
    instr: Any,
    tag_refs: dict[str, Tag],
    ranges: dict[str, list[str]] | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Extract write targets for one instruction plus target-address reads.

    Returns ``(writes, reads, implicit_writes)`` — fault writes are tracked
    separately so they don't pollute ``writers_of`` or cone computations.
    """
    writes: set[str] = set()
    reads: set[str] = set()

    cls = type(instr)
    for field_name in getattr(cls, "_writes", ()):
        target_writes, target_reads = _extract_write_targets(
            getattr(instr, field_name),
            tag_refs,
            ranges=ranges,
        )
        writes.update(target_writes)
        reads.update(target_reads)

    writes.update(_static_copy_fanout_writes(instr, tag_refs))
    implicit_writes = _implicit_fault_writes(instr, tag_refs)
    return writes, reads, implicit_writes


def _static_copy_fanout_writes(instr: Any, tag_refs: dict[str, Tag]) -> set[str]:
    """Return sequential writes that a statically-known copy fan-out performs."""
    if not isinstance(instr, CopyInstruction):
        return set()

    dest = instr.dest
    if isinstance(dest, ImmediateRef):
        dest = dest.value
    if not isinstance(dest, Tag):
        return set()

    source = instr.source
    converter = instr.convert
    count = 0

    if converter is None and isinstance(source, str) and len(source) > 1:
        if dest.type == TagType.CHAR:
            count = len(source)
    elif converter is not None and getattr(converter, "mode", None) in {"value", "ascii"}:
        if isinstance(source, str):
            count = len(source)

    if count <= 1:
        return set()

    from pyrung.core.instruction.resolvers import _sequential_tags

    try:
        tags = _sequential_tags(dest, count)
    except ValueError:
        return set()

    writes: set[str] = set()
    for tag in tags:
        _register_tag(tag, tag_refs, writes)
    return writes


def _implicit_fault_writes(instr: Any, tag_refs: dict[str, Tag]) -> set[str]:
    """Return implicit system fault tags written by *instr*."""
    from pyrung.core.system_points import system

    writes: set[str] = set()
    if isinstance(instr, CalcInstruction):
        _register_tag(system.fault.division_error, tag_refs, writes)
        _register_tag(system.fault.out_of_range, tag_refs, writes)
        return writes

    if isinstance(instr, CopyInstruction):
        _register_tag(system.fault.address_error, tag_refs, writes)
        _register_tag(system.fault.out_of_range, tag_refs, writes)
        return writes

    if isinstance(instr, BlockCopyInstruction | PackTextInstruction):
        _register_tag(system.fault.out_of_range, tag_refs, writes)
        return writes

    return writes


def _rung_condition_reads(
    rung: Rung,
    tag_refs: dict[str, Tag],
    *,
    local_only: bool = False,
) -> frozenset[str]:
    """Extract rung condition reads.

    Branch rungs store inherited parent conditions first; ``local_only=True``
    returns just the branch-local slice used during the branch prepass.
    """
    conditions = (
        rung._conditions[rung._branch_condition_start :] if local_only else rung._conditions
    )
    reads: set[str] = set()
    for condition in conditions:
        reads.update(_extract_reads_from_condition(condition, tag_refs))
    return frozenset(reads)


def _build_access_sequence(
    program: Program,
    node_index_by_rung: dict[int, int],
    tag_refs: dict[str, Tag],
) -> tuple[_AccessEvent, ...]:
    """Build execution-ordered access events following runner semantics.

    Subroutines are inlined at their call sites so that def-use chains
    correctly reflect cross-subroutine ordering (e.g. a main-program rung
    after a ``call()`` reads the version written by the subroutine, not the
    scan-entry version).
    """
    from pyrung.core.rung import Rung as RungClass

    events: list[_AccessEvent] = []
    active_calls: set[str] = set()

    def emit_condition_prepass(rung: Rung, *, emit_own_conditions: bool) -> None:
        node_index = node_index_by_rung[id(rung)]

        if emit_own_conditions:
            own_condition_reads = _rung_condition_reads(rung, tag_refs)
            if own_condition_reads:
                events.append(
                    _AccessEvent(node_index=node_index, condition_reads=own_condition_reads)
                )

        # All branch local conditions conceptually read the same rung-entry
        # snapshot, so we emit the whole branch tree's condition prepass before
        # any instruction events in this rung execute.
        for item in rung._execution_items:
            if not isinstance(item, RungClass):
                continue
            local_condition_reads = _rung_condition_reads(item, tag_refs, local_only=True)
            if local_condition_reads:
                events.append(
                    _AccessEvent(
                        node_index=node_index_by_rung[id(item)],
                        condition_reads=local_condition_reads,
                    )
                )
            emit_condition_prepass(item, emit_own_conditions=False)

    def inline_subroutine(name: str) -> None:
        if name in active_calls:
            return
        sub_rungs = program.subroutines.get(name)
        if sub_rungs is None:
            return
        active_calls.add(name)
        for sub_rung in sub_rungs:
            emit_condition_prepass(sub_rung, emit_own_conditions=True)
            walk_execution(sub_rung)
        active_calls.discard(name)

    def walk_execution(rung: Rung) -> None:
        node_index = node_index_by_rung[id(rung)]
        for item in rung._execution_items:
            if isinstance(item, RungClass):
                walk_execution(item)
                continue

            instruction_event = _extract_instruction_event(item, node_index, tag_refs)
            if (
                instruction_event.condition_reads
                or instruction_event.data_reads
                or instruction_event.writes
            ):
                events.append(instruction_event)

            if isinstance(item, ForLoopInstruction):
                for child_instr in item.instructions:
                    child_event = _extract_instruction_event(child_instr, node_index, tag_refs)
                    if child_event.condition_reads or child_event.data_reads or child_event.writes:
                        events.append(child_event)

            if isinstance(item, CallInstruction):
                inline_subroutine(item.subroutine_name)

    for rung in program.rungs:
        emit_condition_prepass(rung, emit_own_conditions=True)
        walk_execution(rung)

    return tuple(events)


def _build_def_use_chains(
    access_events: tuple[_AccessEvent, ...],
) -> dict[str, tuple[TagVersion, ...]]:
    """Build ordered def-use chains keyed by tag name."""
    all_tags = sorted(
        {
            tag_name
            for event in access_events
            for tag_name in (event.condition_reads | event.data_reads | event.writes)
        }
    )
    chains: dict[str, tuple[TagVersion, ...]] = {}

    for tag_name in all_tags:
        versions: list[dict[str, Any]] = [{"defined_at": None, "read_by": set()}]
        current_index = 0

        for event in access_events:
            if tag_name in event.condition_reads or tag_name in event.data_reads:
                versions[current_index]["read_by"].add(event.node_index)

            if tag_name in event.writes:
                versions.append({"defined_at": event.node_index, "read_by": set()})
                current_index = len(versions) - 1

        chains[tag_name] = tuple(
            TagVersion(
                tag=tag_name,
                defined_at=version["defined_at"],
                read_by=frozenset(version["read_by"]),
            )
            for version in versions
        )

    return chains


def resolve_rung(program: Program, node: RungNode) -> Rung | None:
    """Resolve a PDG node to its ``Rung`` object (main, subroutine, or branch)."""
    if node.subroutine is not None:
        rungs = program.subroutines.get(node.subroutine)
        if rungs is None or node.rung_index >= len(rungs):
            return None
        rung = rungs[node.rung_index]
    else:
        if node.rung_index >= len(program.rungs):
            return None
        rung = program.rungs[node.rung_index]
    for bi in node.branch_path:
        if bi >= len(rung._branches):
            return None
        rung = rung._branches[bi]
    return rung


def classify_tags(graph: ProgramGraph) -> dict[str, TagRole]:
    """Classify tags by coarse graph role."""
    condition_readers_of: dict[str, frozenset[int]] = {
        tag_name: frozenset(
            node_index
            for node_index, node in enumerate(graph.rung_nodes)
            if tag_name in node.condition_reads
        )
        for tag_name in (set(graph.readers_of) | set(graph.writers_of))
    }

    roles: dict[str, TagRole] = {}
    for tag_name in sorted(set(graph.readers_of) | set(graph.writers_of)):
        readers = graph.readers_of.get(tag_name, frozenset())
        writers = graph.writers_of.get(tag_name, frozenset())
        condition_readers = condition_readers_of.get(tag_name, frozenset())
        touching_nodes = readers | writers

        if readers and writers and len(touching_nodes) == 1:
            roles[tag_name] = TagRole.ISOLATED
            continue

        if readers and not writers:
            roles[tag_name] = TagRole.INPUT
            continue

        # PIVOT: written by some rung(s) AND condition-read by a *different* rung.
        # Same-rung-only cycles already matched ISOLATED above.
        if writers and condition_readers and len(writers | condition_readers) > 1:
            roles[tag_name] = TagRole.PIVOT
            continue

        if writers:
            roles[tag_name] = TagRole.TERMINAL

    return roles


def _augment_return_early_guards(
    nodes: list[RungNode],
    program: Program,
    tag_refs: dict[str, Tag],
) -> None:
    """Propagate return_early guard condition_reads to subsequent write nodes.

    When a subroutine rung fires return_early(), all subsequent rungs only
    execute if the guard condition was False.  Their writes therefore depend
    on the guard's condition_reads — make that visible in the graph so
    upstream_slice discovers the dependency.
    """
    from pyrung.core.instruction.control import ReturnInstruction

    for sub_name in sorted(program.subroutines):
        sub_rungs = program.subroutines[sub_name]
        sub_indices = [
            i for i, n in enumerate(nodes) if n.subroutine == sub_name and n.branch_path == ()
        ]
        if len(sub_indices) != len(sub_rungs):
            continue

        guard_reads: frozenset[str] = frozenset()
        for node_idx, rung in zip(sub_indices, sub_rungs, strict=True):
            node = nodes[node_idx]
            if guard_reads and node.writes:
                nodes[node_idx] = _dc_replace(
                    node,
                    condition_reads=node.condition_reads | guard_reads,
                    guard_reads=guard_reads,
                )
            if any(isinstance(instr, ReturnInstruction) for instr in rung._instructions):
                guard_reads = guard_reads | _rung_condition_reads(rung, tag_refs)


def _always_run_subroutines(graph: ProgramGraph) -> frozenset[str]:
    """Subroutines invoked unconditionally on every scan.

    A subroutine qualifies when it has at least one call site and every call
    site is an unconditional rung that is itself unconditionally reached — a
    main rung with no condition, or a rung inside another always-run
    subroutine.  Computed as a monotone fixpoint.
    """
    call_sites: dict[str, list[RungNode]] = defaultdict(list)
    for node in graph.rung_nodes:
        for sub_name in node.calls:
            call_sites[sub_name].append(node)

    always: set[str] = set()
    changed = True
    while changed:
        changed = False
        for sub_name, nodes in call_sites.items():
            if sub_name in always or not nodes:
                continue
            if all(
                not node.condition_reads and (node.subroutine is None or node.subroutine in always)
                for node in nodes
            ):
                always.add(sub_name)
                changed = True
    return frozenset(always)


def build_program_graph(program: Program) -> ProgramGraph:
    """Build the static PDG summary for a Program."""
    cached_graph = getattr(program, "_cached_graph", None)
    if cached_graph is not None:
        return cached_graph

    tag_refs: dict[str, Tag] = {}
    rung_nodes: list[RungNode] = []
    node_index_by_rung: dict[int, int] = {}
    range_acc: dict[str, list[str]] = {}

    def walk_rung(
        rung: Rung,
        *,
        scope: GraphScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        node = _extract_rung_node(
            rung,
            rung_index=rung_index,
            scope=scope,
            subroutine=subroutine,
            branch_path=branch_path,
            tag_refs=tag_refs,
            range_acc=range_acc,
        )
        node_index_by_rung[id(rung)] = len(rung_nodes)
        rung_nodes.append(node)
        for branch_index, branch_rung in enumerate(rung._branches):
            walk_rung(
                branch_rung,
                scope=scope,
                subroutine=subroutine,
                rung_index=rung_index,
                branch_path=branch_path + (branch_index,),
            )

    for rung_index, rung in enumerate(program.rungs):
        walk_rung(rung, scope="main", subroutine=None, rung_index=rung_index, branch_path=())

    for subroutine_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[subroutine_name]):
            walk_rung(
                rung,
                scope="subroutine",
                subroutine=subroutine_name,
                rung_index=rung_index,
                branch_path=(),
            )

    readers_of_mut: dict[str, set[int]] = defaultdict(set)
    all_readers_of_mut: dict[str, set[int]] = defaultdict(set)
    writers_of_mut: dict[str, set[int]] = defaultdict(set)
    access_events = _build_access_sequence(program, node_index_by_rung, tag_refs)

    for node_index, node in enumerate(rung_nodes):
        for tag_name in node.condition_reads | node.data_reads:
            readers_of_mut[tag_name].add(node_index)
        for tag_name in node.condition_reads | node.data_reads | node.exclusive_reads:
            all_readers_of_mut[tag_name].add(node_index)
        for tag_name in node.writes:
            writers_of_mut[tag_name].add(node_index)

    _augment_return_early_guards(rung_nodes, program, tag_refs)
    frozen_nodes = tuple(rung_nodes)

    graph = ProgramGraph(
        rung_nodes=frozen_nodes,
        tag_roles={},
        def_use_chains=_build_def_use_chains(access_events),
        readers_of={name: frozenset(indices) for name, indices in readers_of_mut.items()},
        all_readers_of={name: frozenset(indices) for name, indices in all_readers_of_mut.items()},
        writers_of={name: frozenset(indices) for name, indices in writers_of_mut.items()},
        tags=dict(sorted(tag_refs.items())),
        block_ranges=range_acc,
        pointer_tags=_collect_pointer_tags(program),
    )
    graph.tag_roles = classify_tags(graph)
    program._cached_graph = graph
    return graph


def _collect_pointer_tags(program: Program) -> dict[str, tuple[str, int, int]]:
    """Find tags used as pointers in IndirectRef/IndirectExprRef accesses."""
    from pyrung.core.validation._common import walk_instructions

    pointers: dict[str, tuple[str, int, int]] = {}

    def _scan(obj: Any, seen: set[int]) -> None:
        if obj is None or isinstance(
            obj,
            (bool, int, float, str, bytes, FunctionType, BuiltinFunctionType, MethodType),
        ):
            return
        obj_id = id(obj)
        if obj_id in seen:
            return
        seen.add(obj_id)
        if isinstance(obj, IndirectRef):
            pointers.setdefault(obj.pointer.name, (obj.block.name, obj.block.start, obj.block.end))
            return
        if isinstance(obj, IndirectExprRef):
            base = _indirect_expr_base_tag(obj.expr)
            if base is not None:
                pointers.setdefault(base.name, (obj.block.name, obj.block.start, obj.block.end))
            return
        if isinstance(obj, (list, tuple, set, frozenset)):
            for item in obj:
                _scan(item, seen)
            return
        if isinstance(obj, dict):
            for v in obj.values():
                _scan(v, seen)
            return
        if isinstance(obj, Instruction):
            fields = _instruction_fields(obj)
            if fields is not None:
                for field_name in fields:
                    _scan(getattr(obj, field_name, None), seen)
                return
        if isinstance(obj, Condition):
            for _child_name, child_value in _condition_children(obj):
                _scan(child_value, seen)
            return
        if hasattr(obj, "__dict__"):
            for v in vars(obj).values():
                _scan(v, seen)

    def _scan_rung_conditions(rung: Rung) -> None:
        for cond in rung._conditions:
            _scan(cond, seen)
        for branch in rung._branches:
            _scan_rung_conditions(branch)

    seen: set[int] = set()
    for instr in walk_instructions(program):
        _scan(instr, seen)
    for rung in program.rungs:
        _scan_rung_conditions(rung)
    for subroutine_rungs in program.subroutines.values():
        for rung in subroutine_rungs:
            _scan_rung_conditions(rung)

    return pointers


__all__ = [
    "ProgramGraph",
    "RungNode",
    "TagRole",
    "TagVersion",
    "build_program_graph",
    "classify_tags",
    "resolve_rung",
]

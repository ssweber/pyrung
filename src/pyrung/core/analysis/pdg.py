"""Static program dependence graph extraction for pyrung programs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from dataclasses import replace as _dc_replace
from enum import Enum
from types import BuiltinFunctionType, FunctionType, MethodType
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.condition import Condition
from pyrung.core.expression import Expression
from pyrung.core.instruction.advanced import SearchInstruction
from pyrung.core.instruction.base import Instruction
from pyrung.core.instruction.coils import OutInstruction
from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction
from pyrung.core.memory_block import (
    UNSET,
    Block,
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
    block_positions,
)
from pyrung.core.tag import ImmediateRef, InputTag, OutputTag, Tag, tag_written_from_outside
from pyrung.core.validation._common import walk_instructions
from pyrung.core.validation.walker import _condition_children, _instruction_fields

from .affine import extract_affine_expression
from .write_sites import (
    instruction_fault_writes,
    instruction_partial_write_targets,
    instruction_write_targets,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

GraphScope = Literal["main", "subroutine"]


@dataclass(frozen=True)
class IndirectWriteRef:
    """Descriptor for an indirect write whose block exceeded the enumeration cap.

    When a block is too large for ``_full_block_tags`` to enumerate statically,
    the individual writes are dropped from ``writers_of``.  This descriptor
    preserves the instruction-level metadata so that ``cause()`` can resolve the
    actual target at runtime by reading the pointer value from recorded state.
    """

    node_index: int
    pointer_tag: str
    source_tags: frozenset[str]
    block: Block


@dataclass(frozen=True)
class PointerUse:
    """One indexed access ``block[scale * pointer + offset]``.

    ``DS[Ptr]`` is ``scale=1, offset=0``; ``DS[Ptr + 2]`` and ``DS[4 - Ptr]``
    are the affine forms ``_indirect_affine`` accepts.
    """

    pointer: str
    block: str
    start: int
    end: int
    scale: int = 1
    offset: int = 0

    def address(self, value: Any) -> int:
        """Return the block address that the access resolves to when the pointer is *value*."""
        return int(self.scale * value + self.offset)

    def pointer_span(self) -> tuple[int, int]:
        """Return the pointer values whose address lands in ``start..end``."""
        if self.scale == 1:
            return self.start - self.offset, self.end - self.offset
        return self.offset - self.end, self.offset - self.start


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
    # The node's own condition reads, before ``_augment_return_early_guards``
    # folds return-guard reads into ``condition_reads``.  It is not
    # ``condition_reads - guard_reads``: a tag the node's own contacts read can
    # be a guard read too.
    own_condition_reads: frozenset[str]
    guard_reads: frozenset[str] = frozenset()
    # The writes the node may leave alone when it runs, from :func:`_instruction_may_writes`.
    may_writes: frozenset[str] = frozenset()

    @property
    def all_writes(self) -> frozenset[str]:
        return self.writes | self.implicit_writes

    @property
    def sure_writes(self) -> frozenset[str]:
        """Return the tags that the node writes every time it runs.

        An analysis that asks whether a rung always writes a tag reads this.  The result
        leaves out the writes in ``may_writes`` and the fault flags.  Most instructions
        set a fault flag and never clear it.  So the node does not write the flag every
        time it runs.
        """
        return self.writes - self.may_writes


@dataclass(frozen=True)
class CallSite:
    """One call edge anchored to the PDG rung node that contains it."""

    node_index: int
    call_index: int
    caller: str | None
    callee: str


@dataclass(frozen=True)
class TagVersion:
    """A single intra-scan version of a tag.

    ``defined_at`` and ``read_by`` use indexes into ``ProgramGraph.rung_nodes``.
    ``defined_at=None`` denotes the scan-entry value.
    """

    tag: str
    defined_at: int | None
    read_by: frozenset[int]


@dataclass(frozen=True)
class InfluenceCone:
    """Static rungs and tags influenced by one seed tag within a scan."""

    rung_indices: frozenset[int]
    read_tags: frozenset[str]
    write_tags: frozenset[str]


@dataclass
class ProgramGraph:
    """Static PDG-style summary for a Program."""

    rung_nodes: tuple[RungNode, ...]
    tag_roles: dict[str, TagRole]
    def_use_chains: dict[str, tuple[TagVersion, ...]]
    readers_of: dict[str, frozenset[int]]  # tag → node indices (rung_nodes position)
    all_readers_of: dict[str, frozenset[int]]  # includes exclusive_reads
    writers_of: dict[
        str, frozenset[int]
    ]  # node indices — use timeline_writers_of() for timeline keys
    tags: dict[str, Tag]
    block_ranges: dict[str, list[str]]  # range label → member tag names
    pointer_tags: dict[str, tuple[str, int, int]]  # pointer name → (block, start, end)
    indirect_writes: tuple[IndirectWriteRef, ...] = ()
    # The writes through a pointer that list no targets, by block.  Each is a writer node
    # and the addresses the program's own pointer values reach (None for every element).
    # See ``unlisted_writers_of`` and ``pointer_writers_of``.
    unlisted_writes: dict[Block, tuple[tuple[int, frozenset[int] | None], ...]] = field(
        default_factory=dict
    )
    # The named slots of a block with no formatter (name -> addresses), and every address with
    # a name.  Keyed by the block and its count of slot-name changes.
    _slot_named: dict[tuple[Block, int], tuple[dict[str, set[int]], set[int]]] = field(
        default_factory=dict, repr=False, compare=False
    )
    # Each slot name of a block with a custom formatter, from one walk, keyed the same way.
    _slot_index: dict[tuple[Block, int], dict[str, frozenset[int]]] = field(
        default_factory=dict, repr=False, compare=False
    )
    # Every indexed access with a single affine pointer, and the blocks indexed
    # by any other expression (``DS[Ptr * 2]``), whose elements no pointer
    # range bounds.
    pointer_uses: tuple[PointerUse, ...] = ()
    opaque_index_blocks: frozenset[str] = frozenset()
    # Every tag an index reads: pointers, the tags of index expressions, and
    # ``IndirectBlockRange`` bounds.
    index_tags: frozenset[str] = frozenset()
    # The fault flags some rung reads.  Their implicit writes
    # (``RungNode.implicit_writes``) count as writes here.  A fault write nothing
    # reads is dead, so it stays out of ``writers_of`` and the cones.
    fault_writes: frozenset[str] = frozenset()
    unread_fault_writes: frozenset[str] = frozenset()
    _main_node_index: dict[int, int] | None = field(default=None, init=False, repr=False)
    _call_site_cache: dict[str, frozenset[int]] | None = field(default=None, init=False, repr=False)
    _call_sites_cache: tuple[CallSite, ...] | None = field(default=None, init=False, repr=False)
    _rung_node_location_cache: (
        dict[tuple[GraphScope, str | None, int, tuple[int, ...]], RungNode] | None
    ) = field(default=None, init=False, repr=False)
    _subroutine_member_cache: dict[str, tuple[int, ...]] | None = field(
        default=None, init=False, repr=False
    )
    _subroutine_caller_cache: dict[str, tuple[int, ...]] | None = field(
        default=None, init=False, repr=False
    )
    # Slice methods are pure functions of the (immutable) graph but are called
    # with repeating arguments across pilot iterations — memoize them.
    _influenced_cone_cache: dict[tuple[str, bool, frozenset[str]], InfluenceCone] | None = field(
        default=None, init=False, repr=False
    )
    _downstream_slice_cache: dict[tuple[str, bool | str], frozenset[str]] | None = field(
        default=None, init=False, repr=False
    )
    # The reverse of the reads that ``upstream_slice`` follows: tag -> nodes that read it.
    _slice_readers_cache: dict[str, frozenset[int]] | None = field(
        default=None, init=False, repr=False
    )
    _upstream_slice_cache: dict[tuple[str, bool | str], frozenset[str]] | None = field(
        default=None, init=False, repr=False
    )

    @classmethod
    def from_program(cls, program: Program) -> ProgramGraph:
        return build_program_graph(program)

    def node_writes(self, node: RungNode) -> frozenset[str]:
        """Return the destinations of *node* and the fault flags that count as its writes."""
        if node.implicit_writes and self.fault_writes:
            return node.writes | (node.implicit_writes & self.fault_writes)
        return node.writes

    def main_node_by_rung(self) -> dict[int, int]:
        """Map main-program rung_index → node index for top-level nodes."""
        if self._main_node_index is None:
            self._main_node_index = {
                node.rung_index: i
                for i, node in enumerate(self.rung_nodes)
                if node.scope == "main" and not node.branch_path
            }
        return self._main_node_index

    def call_site_rung_indices(self) -> dict[str, frozenset[int]]:
        """Map subroutine name → main-rung indices of all call sites.

        Cached; used by the recorded causal walk to surface the caller
        gate as a lever on a subroutine writer (reversing the caller
        disables the whole subtree), and by :meth:`timeline_writers_of`
        to resolve subroutine writers to the main-rung indices the
        executor's ``capturing_rung`` rolls them up under.  Includes
        branch call sites — branches execute under the same capturing
        scope as their parent rung.
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

    def call_sites(self) -> tuple[CallSite, ...]:
        """Return every structural call edge with its containing rung node.

        Calls nested in loop instructions are already represented by
        :attr:`RungNode.calls`; this method preserves node and per-node call order.
        """
        if self._call_sites_cache is None:
            self._call_sites_cache = tuple(
                CallSite(
                    node_index=node_index,
                    call_index=call_index,
                    caller=node.subroutine,
                    callee=callee,
                )
                for node_index, node in enumerate(self.rung_nodes)
                for call_index, callee in enumerate(node.calls)
            )
        return self._call_sites_cache

    def rung_node(
        self,
        *,
        scope: GraphScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...] = (),
    ) -> RungNode | None:
        """Resolve a structural rung location to its PDG node."""
        if self._rung_node_location_cache is None:
            self._rung_node_location_cache = {
                (node.scope, node.subroutine, node.rung_index, node.branch_path): node
                for node in self.rung_nodes
            }
        return self._rung_node_location_cache.get((scope, subroutine, rung_index, branch_path))

    def _subroutine_member_indices(self) -> dict[str, tuple[int, ...]]:
        """Map subroutine name to PDG node indices inside that subroutine."""
        if self._subroutine_member_cache is None:
            members: dict[str, list[int]] = defaultdict(list)
            for idx, node in enumerate(self.rung_nodes):
                if node.subroutine is not None:
                    members[node.subroutine].append(idx)
            self._subroutine_member_cache = {
                name: tuple(indices) for name, indices in members.items()
            }
        return self._subroutine_member_cache

    def _subroutine_caller_indices(self) -> dict[str, tuple[int, ...]]:
        """Map subroutine name to PDG node indices that call it."""
        if self._subroutine_caller_cache is None:
            callers: dict[str, list[int]] = defaultdict(list)
            for idx, node in enumerate(self.rung_nodes):
                for sub_name in node.calls:
                    callers[sub_name].append(idx)
            self._subroutine_caller_cache = {
                name: tuple(indices) for name, indices in callers.items()
            }
        return self._subroutine_caller_cache

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
        call_sites = self.call_site_rung_indices()
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
        return self.call_site_rung_indices().get(node.subroutine, frozenset())

    def is_physical_input(self, tag_name: str) -> bool:
        """Return whether ``tag_name`` resolves to a physical input tag."""
        return isinstance(self.tags.get(tag_name), InputTag)

    def written_from_outside(self, tag_name: str) -> bool:
        """Return whether something outside the ladder can write ``tag_name``.

        See :func:`tag_written_from_outside`.
        """
        tag = self.tags.get(tag_name)
        return tag is not None and tag_written_from_outside(tag)

    def unlisted_writers_of(self, tag_name: str) -> frozenset[int]:
        """Return the nodes whose write through a pointer may store into ``tag_name`` unlisted.

        A write through a pointer with no declared bounds, into a block of more than 1000
        elements, names none of its targets.  ``writers_of`` and the facts' writer lists
        leave it out, so a check that reads "no writer" as "constant" or "input" asks this
        too.  It answers "can the program's own values aim the pointer at this tag?".  So
        each write counts only for the addresses the program can store in its pointer.
        """
        if not self.unlisted_writes:
            return frozenset()
        nodes: frozenset[int] = frozenset()
        for block, addr in self.tag_positions(tag_name, self.unlisted_writes):
            nodes |= self.unlisted_writers_at(block, addr)
        return nodes

    def pointer_writers_of(
        self, tag_name: str, blocks: Collection[Block] | None = None
    ) -> frozenset[int]:
        """Return the nodes whose unlisted write through a pointer can store into ``tag_name``.

        It answers "could this write have stored into the tag, whatever its pointer
        held?".  A patch or a force can put any value in a pointer, so every write into a
        block counts for every element of it.  A walk over a recorded run asks this, and
        keeps a write only when the run shows it stored into the tag.  *blocks* keeps only
        the writes into those blocks.
        """
        if not self.unlisted_writes:
            return frozenset()
        written = (
            self.unlisted_writes
            if blocks is None
            else [block for block in blocks if block in self.unlisted_writes]
        )
        nodes: set[int] = set()
        for block, _addr in self.tag_positions(tag_name, written):
            nodes.update(node for node, _reach in self.unlisted_writes[block])
        return frozenset(nodes)

    def tag_positions(
        self, tag_name: str, blocks: Collection[Block]
    ) -> frozenset[tuple[Block, int]]:
        """Return each ``(block, address)`` in *blocks* where a store writes ``tag_name``.

        These are the elements that the tag occupies (``block_positions``) and the addresses
        whose slot name is the tag's name.  The runtime stores by the name that the bank
        gives the address.  So a store also reaches a tag mapped onto a second slot, or a plain tag
        named like a slot.  A block with a custom ``address_formatter`` and more than
        ``_MAX_BLOCK_SCAN`` elements is not scanned.  There every tag counts as reached,
        at the block's first address and at each address an unlisted write reaches.
        """
        found = {
            (block, addr)
            for block, addr in block_positions(self.tags.get(tag_name))
            if block in blocks
        }
        for block in blocks:
            if self._is_unscannable(block):
                found.update(self._unscanned_positions(block))
            else:
                found.update((block, addr) for addr in self._slot_addresses(block, tag_name))
        return frozenset(found)

    @staticmethod
    def _is_unscannable(block: Block) -> bool:
        from .value_sets import _MAX_BLOCK_SCAN

        return block.address_formatter is not None and block.end - block.start + 1 > _MAX_BLOCK_SCAN

    def _unscanned_positions(self, block: Block) -> set[tuple[Block, int]]:
        """Return the positions in a block too large to scan where a store may reach any tag."""
        found: set[tuple[Block, int]] = {(block, block.start)}
        for _node, reach in self.unlisted_writes.get(block, ()):
            if reach is not None:
                found.update((block, addr) for addr in reach)
        return found

    def _slot_addresses(self, block: Block, name: str) -> frozenset[int]:
        """Return the addresses of *block* whose slot name is *name*, as the bank resolves them now.

        A slot's name is its override, else its mapped tag's name, else the default
        ``<block><address>``.  The first two are read from the few slots that have them.
        The default is parsed from *name*, so no block is walked.  A block with a custom
        ``address_formatter`` is walked once, and the walk is kept.
        """
        if block.address_formatter is not None:
            key = (block, block._slot_names_changed)
            index = self._slot_index.get(key)
            if index is None:
                built: dict[str, set[int]] = {}
                for addr in range(block.start, block.end + 1):
                    built.setdefault(block._effective_slot_name(addr), set()).add(addr)
                index = self._slot_index[key] = {n: frozenset(a) for n, a in built.items()}
            return index.get(name, frozenset())
        key = (block, block._slot_names_changed)
        cached = self._slot_named.get(key)
        if cached is None:
            by_name: dict[str, set[int]] = {}
            named: set[int] = set()
            for addr, entry in block._slot_config.items():
                if entry.name is not UNSET:
                    named.add(addr)
                    by_name.setdefault(entry.name, set()).add(addr)
            for addr, mapped in block._mapped_tags.items():
                if addr not in named:
                    named.add(addr)
                    by_name.setdefault(mapped.name, set()).add(addr)
            cached = self._slot_named[key] = (by_name, named)
        by_name, named = cached
        found = set(by_name.get(name, ()))
        digits = name[len(block.name) :]
        if name.startswith(block.name) and digits.isascii() and digits.isdecimal():
            addr = int(digits)
            if str(addr) == digits and block.start <= addr <= block.end and addr not in named:
                found.add(addr)
        return frozenset(found)

    def unlisted_writers_at(self, block: Block, addr: int) -> frozenset[int]:
        """Return the nodes whose pointer write may store into ``block[addr]`` unlisted."""
        return frozenset(
            node
            for node, reach in self.unlisted_writes.get(block, ())
            if reach is None or addr in reach
        )

    def unlisted_written(self) -> frozenset[str]:
        """Return every tag of the graph that :meth:`unlisted_writers_of` names a writer for."""
        if not self.unlisted_writes:
            return frozenset()
        return frozenset(name for name in self.tags if self.unlisted_writers_of(name))

    def all_writers_of(self, tag_name: str) -> frozenset[int]:
        """Return the nodes that may write ``tag_name``.  These are ``writers_of`` plus
        :meth:`unlisted_writers_of`.

        It answers "which rungs can write the tag, from the program alone?".  A static
        answer asks this.
        """
        return self.writers_of.get(tag_name, frozenset()) | self.unlisted_writers_of(tag_name)

    def writers_in_any_run_of(self, tag_name: str) -> frozenset[int]:
        """Return the nodes that may write ``tag_name``.  These are ``writers_of`` plus
        :meth:`pointer_writers_of`.

        It answers "which rungs could have written the tag in a run, whatever a patch or a
        force put in a pointer?".  A walk over a recorded run asks this, and keeps a rung
        only when the run shows it wrote the tag.
        """
        return self.writers_of.get(tag_name, frozenset()) | self.pointer_writers_of(tag_name)

    def may_be_written(self, tag_name: str) -> bool:
        """Return whether a rung may write ``tag_name``, whether ``writers_of`` lists it or not."""
        return bool(self.writers_of.get(tag_name)) or bool(self.unlisted_writers_of(tag_name))

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
        """Return True when def-use chains prove unconditional write-before-read.

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
        guards grow monotonically down the subroutine.  Only (A) survives a
        guard: a reader outside the subroutine runs after the return and
        reads the value the skipped write left standing, however the
        subroutine is called.

        A fault flag is never written before it is read.  The chains count each
        implicit fault write (``_fault_writes``) as a full write, but most of
        those instructions clear the flag only when they run, or only ever set
        it, and the flag carries from scan to scan.
        """
        chain = self.def_use_chains.get(tag_name)
        if not chain:
            return False
        if any(tag_name in node.implicit_writes for node in self.rung_nodes):
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
        # in the subroutine share the same monotonically-accumulated guard, so
        # if the write is skipped (guard fired), every such read is skipped.
        if node.own_condition_reads:
            return False
        if node.scope == "main":
            return True
        sub = node.subroutine
        if not node.guard_reads and sub in _always_run_subroutines(self):
            return True
        return all(
            self.rung_nodes[ri].subroutine == sub
            for ri in self.all_readers_of.get(tag_name, frozenset())
        )

    def upstream_slice(
        self,
        tag_name: str,
        *,
        follow_calls: bool | Literal["call_site"] = True,
    ) -> frozenset[str]:
        """Return all tags transitively upstream of *tag_name*.

        Always follows ``condition_reads``, ``data_reads``, and
        ``exclusive_reads``.  *follow_calls* says what a writer rung inside a
        subroutine adds:

        - ``True`` (the default): the condition tags of every rung that calls
          the subroutine, up the call chain, and everything upstream of them.
          Soundness-critical analysis needs this full closure.
        - ``"call_site"``: the same call-condition tags, but none of their own
          upstream. The call condition is the subroutine's enable, so the cone
          stops there. A call-condition tag that the cone also reads in the
          ordinary way still expands.
        - ``False``: nothing.
        """
        if self._upstream_slice_cache is None:
            self._upstream_slice_cache = {}
        cache_key = (tag_name, follow_calls)
        cached = self._upstream_slice_cache.get(cache_key)
        if cached is not None:
            return cached

        visited_tags: set[str] = set()
        visited_rungs: set[int] = set()
        visited_subs: set[str] = set()
        call_conditions: set[str] = set()
        queue: list[str] = [tag_name]
        expand_call_conditions = follow_calls is True

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
                if not follow_calls or node.subroutine is None:
                    continue
                # The call guards on every path into the subroutine: a caller
                # rung in another subroutine runs only when that one is called.
                pending_subs = [node.subroutine]
                while pending_subs:
                    sub_name = pending_subs.pop()
                    if sub_name in visited_subs:
                        continue
                    visited_subs.add(sub_name)
                    for caller_idx in self._subroutine_caller_indices().get(sub_name, ()):
                        caller = self.rung_nodes[caller_idx]
                        if expand_call_conditions:
                            for read_tag in caller.condition_reads:
                                if read_tag not in visited_tags:
                                    queue.append(read_tag)
                        else:
                            call_conditions.update(caller.condition_reads)
                        if caller.subroutine is not None:
                            pending_subs.append(caller.subroutine)

        visited_tags.update(call_conditions)
        visited_tags.discard(tag_name)
        result = frozenset(visited_tags)
        self._upstream_slice_cache[cache_key] = result
        return result

    def influenced_cone(
        self,
        tag_name: str,
        *,
        follow_calls: bool = True,
        barrier_tags: frozenset[str] = frozenset(),
    ) -> InfluenceCone:
        """Return rungs, reads, and writes influenced by *tag_name*.

        The traversal starts at rungs that read *tag_name*, follows write→read
        edges transitively, and optionally crosses subroutine boundaries.  When
        *follow_calls* is true, a cone rung that calls a subroutine pulls in the
        called subroutine body, and a cone rung inside a subroutine pulls in its
        callers.  *barrier_tags* are recorded as writes but not followed through
        to their readers.
        """
        if self._influenced_cone_cache is None:
            self._influenced_cone_cache = {}
        cache_key = (tag_name, follow_calls, barrier_tags)
        cached = self._influenced_cone_cache.get(cache_key)
        if cached is not None:
            return cached

        sub_members = self._subroutine_member_indices()
        callers = self._subroutine_caller_indices()

        visited_tags: set[str] = set()
        visited_rungs: set[int] = set()
        read_tags: set[str] = set()
        write_tags: set[str] = set()
        tag_queue: list[str] = [tag_name]
        rung_queue: list[int] = []

        def _visit_rung(rung_idx: int) -> None:
            if rung_idx in visited_rungs:
                return
            visited_rungs.add(rung_idx)
            node = self.rung_nodes[rung_idx]
            read_tags.update(node.condition_reads)
            read_tags.update(node.data_reads)
            for written_tag in self.node_writes(node):
                write_tags.add(written_tag)
                if written_tag not in visited_tags and written_tag not in barrier_tags:
                    tag_queue.append(written_tag)
            if not follow_calls:
                return
            for sub_name in node.calls:
                rung_queue.extend(sub_members.get(sub_name, ()))
            if node.subroutine is not None:
                rung_queue.extend(callers.get(node.subroutine, ()))

        while tag_queue or rung_queue:
            while tag_queue:
                current = tag_queue.pop()
                if current in visited_tags:
                    continue
                visited_tags.add(current)
                rung_queue.extend(self.readers_of.get(current, frozenset()))
            while rung_queue:
                _visit_rung(rung_queue.pop())

        read_tags.discard(tag_name)
        write_tags.discard(tag_name)
        result = InfluenceCone(
            rung_indices=frozenset(visited_rungs),
            read_tags=frozenset(read_tags),
            write_tags=frozenset(write_tags),
        )
        self._influenced_cone_cache[cache_key] = result
        return result

    def downstream_slice(
        self,
        tag_name: str,
        *,
        follow_calls: bool | Literal["call_site"] = False,
    ) -> frozenset[str]:
        """Return all tags transitively downstream of *tag_name*.

        *follow_calls* says how the slice crosses subroutine calls:

        - ``False`` (the default): it does not. This is the direct PDG slice.
        - ``True``: it includes subroutine bodies reached by call instructions
          and caller sites reached from subroutine rungs.
        - ``"call_site"``: the reverse of ``upstream_slice(follow_calls="call_site")``.
          If a rung that calls a subroutine reads *tag_name* in its condition, the
          slice includes every tag that the subroutine, or a subroutine it calls,
          writes. Every later step follows ordinary reads, so a tag reached on the
          way does not open a subroutine. *X* is in the call-site upstream slice of
          *Y* exactly when *Y* is in the call-site downstream slice of *X*.
        """
        if self._downstream_slice_cache is None:
            self._downstream_slice_cache = {}
        cache_key = (tag_name, follow_calls)
        cached = self._downstream_slice_cache.get(cache_key)
        if cached is not None:
            return cached

        if follow_calls == "call_site":
            result = self._call_site_downstream(tag_name)
            self._downstream_slice_cache[cache_key] = result
            return result

        if follow_calls:
            # influenced_cone has its own cache; this just keys the write_tags.
            result = self.influenced_cone(tag_name, follow_calls=True).write_tags
            self._downstream_slice_cache[cache_key] = result
            return result

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
                for written_tag in self.node_writes(node):
                    if written_tag not in visited_tags:
                        queue.append(written_tag)

        visited_tags.discard(tag_name)
        result = frozenset(visited_tags)
        self._downstream_slice_cache[cache_key] = result
        return result

    def _slice_readers(self) -> dict[str, frozenset[int]]:
        """Map each tag to the nodes whose reads ``upstream_slice`` follows back to it."""
        if self._slice_readers_cache is None:
            readers: dict[str, set[int]] = defaultdict(set)
            for idx, node in enumerate(self.rung_nodes):
                for read_tag in node.condition_reads | node.data_reads | node.exclusive_reads:
                    readers[read_tag].add(idx)
            self._slice_readers_cache = {name: frozenset(ids) for name, ids in readers.items()}
        return self._slice_readers_cache

    def _call_site_downstream(self, tag_name: str) -> frozenset[str]:
        # The edges are those that upstream_slice walks backward, so the two slices
        # stay exact reverses of each other.
        readers = self._slice_readers()
        members = self._subroutine_member_indices()
        callers = self._subroutine_caller_indices()

        # The first step may cross a call: the subroutines whose call rungs read
        # tag_name, and every subroutine those call in turn.
        pending_subs = [
            sub_name
            for sub_name, caller_indices in callers.items()
            if any(tag_name in self.rung_nodes[idx].condition_reads for idx in caller_indices)
        ]
        opened: set[str] = set()
        while pending_subs:
            sub_name = pending_subs.pop()
            if sub_name in opened:
                continue
            opened.add(sub_name)
            for idx in members.get(sub_name, ()):
                pending_subs.extend(self.rung_nodes[idx].calls)

        queue: list[str] = [tag_name]
        for sub_name in opened:
            for idx in members.get(sub_name, ()):
                queue.extend(self.node_writes(self.rung_nodes[idx]))

        visited_tags: set[str] = set()
        visited_rungs: set[int] = set()
        while queue:
            current = queue.pop()
            if current in visited_tags:
                continue
            visited_tags.add(current)
            for rung_idx in readers.get(current, frozenset()):
                if rung_idx in visited_rungs:
                    continue
                visited_rungs.add(rung_idx)
                queue.extend(self.node_writes(self.rung_nodes[rung_idx]))

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
    # The writes the instruction may not make, so the value before it can last past it.
    may_writes: frozenset[str] = frozenset()


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


def _indirect_ref_tags(
    block: Block, pointer: Tag, scale: int = 1, offset: int = 0
) -> list[Tag] | None:
    """Narrow an indirect block access using pointer tag metadata.

    The access is ``block[scale * pointer + offset]``.  Returns the bounded tag
    list when choices or min/max constrain the pointer, or ``None`` when the
    address is statically unbounded.
    """
    if pointer.choices is not None:
        addrs = sorted(
            {
                addr
                for addr in (int(scale * float(k) + offset) for k in pointer.choices)
                if block.start <= addr <= block.end
            }
        )
        return [block._get_tag(a) for a in addrs]
    if pointer.min is not None and pointer.max is not None:
        # int() truncates monotonically, so the end points bound every address.
        lo_v, hi_v = sorted((scale * pointer.min + offset, scale * pointer.max + offset))
        lo = max(int(lo_v), block.start)
        hi = min(int(hi_v), block.end)
        if lo <= hi:
            return _block_tags(block.select(lo, hi))
    return None


def _indirect_affine(expr: Any) -> tuple[Tag, int, int] | None:
    """Return ``(pointer, scale, offset)`` when *expr* is ``scale * pointer + offset``.

    It accepts a bare tag and the single-tag forms of ``extract_affine_expression``
    with an integer offset.  It returns ``None`` for anything else (``Ptr * 2``,
    ``Ptr + Other``), because no pointer range bounds those addresses.
    """
    from pyrung.core.expression import TagExpr

    if isinstance(expr, Tag):
        return expr, 1, 0
    if isinstance(expr, TagExpr):
        return expr.tag, 1, 0
    affine = extract_affine_expression(expr)
    if affine is None:
        return None
    name, scale, offset = affine
    if isinstance(offset, bool) or not isinstance(offset, int):
        return None
    pointer = _indirect_expr_base_tag(expr)
    if pointer is None or pointer.name != name:
        return None
    return pointer, scale, offset


def _indirect_expr_tags(block: Block, expr: Any) -> list[Tag] | None:
    """Return the bounded tags ``block[expr]`` can touch, or ``None`` when unbounded."""
    affine = _indirect_affine(expr)
    if affine is None:
        return None
    pointer, scale, offset = affine
    return _indirect_ref_tags(block, pointer, scale, offset)


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
            tags = _indirect_expr_tags(current.block, current.expr)
            if tags is None and _indirect_affine(current.expr) is None:
                # No pointer range bounds a non-affine index: it may read any element.
                tags = _full_block_tags(current.block)
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
            tags = _indirect_expr_tags(current.block, current.expr)
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
    may_writes: set[str] = set()
    sure_writes: set[str] = set()
    calls: list[str] = []

    for condition in rung._conditions:
        condition_reads.update(_extract_reads_from_condition(condition, tag_refs))

    def walk_instruction(instr: Any, *, in_loop: bool = False) -> None:
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
        instr_may = _instruction_may_writes(instr, tag_refs)
        may_writes.update(instr_may)
        sure_writes.update(instruction_writes - instr_may)
        # An out() in a forloop body is not a level follower of this node's
        # guard: a false For rung skips the body (the coil holds), a count of 0
        # runs no iteration, and in a body rung it follows that rung's contacts
        # on the last iteration.
        if isinstance(instr, OutInstruction) and not in_loop:
            ote_writes.update(instruction_writes)

        for field_name in getattr(cls, "_exclusive_fields", ()):
            exclusive_reads.update(
                _extract_tag_names(getattr(instr, field_name), tag_refs, ranges=range_acc)
            )

        for field_name in getattr(cls, "_conditions", ()):
            condition_reads.update(_extract_tag_names(getattr(instr, field_name), tag_refs))

        if isinstance(instr, ForLoopInstruction):
            # Body-rung contacts gate their writes and, through break/continue,
            # every later body instruction.  The node has one guard, so they
            # all join it (over-approximate, the safe direction).
            for body_rung in instr.body_rungs:
                condition_reads.update(_rung_condition_reads(body_rung, tag_refs, local_only=True))
            # A count read from a tag gates the whole body too: one that
            # resolves to 0 runs no iteration.
            condition_reads.update(_extract_tag_names(instr.count, tag_refs))
            for child_instr, _local in instr.iter_body():
                walk_instruction(child_instr, in_loop=True)

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
        own_condition_reads=frozenset(condition_reads),
        may_writes=frozenset(may_writes - sure_writes),
    )


def _extract_instruction_event(
    instr: Any,
    node_index: int,
    tag_refs: dict[str, Tag],
    fault_writes: frozenset[str] = frozenset(),
) -> _AccessEvent:
    """Extract one instruction's ordered reads/writes."""
    condition_reads: set[str] = set()
    data_reads: set[str] = set()
    writes: set[str] = set()

    cls = type(instr)
    for field_name in getattr(cls, "_reads", ()):
        data_reads.update(_extract_tag_names(getattr(instr, field_name), tag_refs))

    instruction_writes, target_reads, implicit_writes = _extract_instruction_writes(instr, tag_refs)
    writes.update(instruction_writes)
    writes.update(implicit_writes & fault_writes)
    data_reads.update(target_reads)

    for field_name in getattr(cls, "_conditions", ()):
        condition_reads.update(_extract_tag_names(getattr(instr, field_name), tag_refs))

    return _AccessEvent(
        node_index=node_index,
        condition_reads=frozenset(condition_reads),
        data_reads=frozenset(data_reads),
        writes=frozenset(writes),
        may_writes=frozenset(_instruction_may_writes(instr, tag_refs) & writes),
    )


def _instruction_may_writes(instr: Any, tag_refs: dict[str, Tag]) -> set[str]:
    """Return the tags that *instr* may leave alone when it runs, though it can write them.

    Two kinds of write reach a tag only for some values.  A pointer, or a range
    with a pointer at an end, writes one of the elements it can reach.  A
    ``to_text()`` copy of a tag writes as many characters as its value has
    (``instruction_partial_write_targets``).  A tag that another target of
    *instr* always writes is not in the set.
    """
    partial = instruction_partial_write_targets(instr)
    sure: set[str] = set()
    may: set[str] = set()
    for target in instruction_write_targets(instr):
        names = _extract_write_targets(target, tag_refs)[0]
        leaf = target.value if isinstance(target, ImmediateRef) else target
        dynamic = isinstance(leaf, IndirectRef | IndirectExprRef | IndirectBlockRange)
        if any(target is p for p in partial) or (dynamic and len(names) > 1):
            may |= names
        else:
            sure |= names
    return may - sure


def _extract_instruction_writes(
    instr: Any,
    tag_refs: dict[str, Tag],
    ranges: dict[str, list[str]] | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Extract shared write targets for one instruction plus target-address reads.

    Declared writes, declared status fields, and static sequential-copy fan-out
    come from :mod:`write_sites`.  This PDG-specific layer retains indirect
    address reads and conservative regions.  It returns
    ``(writes, reads, implicit_writes)``.  The implicit writes are the fault
    flags the instruction can set (``_fault_writes``).  They stay out of the
    node's ``writes`` (an instruction's declared destinations).  They count in
    ``writers_of``, the def-use chains and the cones for the flags a rung reads
    (``ProgramGraph.fault_writes``).  So ``Rung(fault.division_error)`` depends
    on the divisor.
    """
    writes: set[str] = set()
    reads: set[str] = set()

    for target in instruction_write_targets(instr):
        target_writes, target_reads = _extract_write_targets(
            target,
            tag_refs,
            ranges=ranges,
        )
        writes.update(target_writes)
        reads.update(target_reads)

    implicit_writes = _implicit_fault_writes(instr, tag_refs)
    return writes, reads, implicit_writes


def _implicit_fault_writes(instr: Any, tag_refs: dict[str, Tag]) -> set[str]:
    """Return implicit system fault tags written by *instr*."""
    from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME

    writes: set[str] = set()
    for name in instruction_fault_writes(instr):
        _register_tag(SYSTEM_TAGS_BY_NAME[name], tag_refs, writes)
    return writes


def _rung_condition_reads(
    rung: Rung,
    tag_refs: dict[str, Tag],
    *,
    local_only: bool = False,
) -> frozenset[str]:
    """Extract rung condition reads.

    Branch rungs store their inherited parent conditions first.  ``local_only=True``
    returns just the branch-local slice, which the solve reads when it reaches the branch.
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
    fault_writes: frozenset[str] = frozenset(),
) -> tuple[_AccessEvent, ...]:
    """Build execution-ordered access events following runner semantics.

    Subroutines are inlined at their call sites so that def-use chains
    correctly reflect cross-subroutine ordering (e.g. a main-program rung
    after a ``call()`` reads the version written by the subroutine, not the
    scan-entry version).  Implicit fault writes count for *fault_writes* only.
    """
    from pyrung.core.rung import Rung as RungClass

    events: list[_AccessEvent] = []
    active_calls: set[str] = set()

    def emit_rung_conditions(rung: Rung) -> None:
        # A top-level rung's trunk is read once, as the rung starts.
        own_condition_reads = _rung_condition_reads(rung, tag_refs)
        if own_condition_reads:
            events.append(
                _AccessEvent(
                    node_index=node_index_by_rung[id(rung)], condition_reads=own_condition_reads
                )
            )

    def inline_subroutine(name: str) -> None:
        if name in active_calls:
            return
        sub_rungs = program.subroutines.get(name)
        if sub_rungs is None:
            return
        active_calls.add(name)
        for sub_rung in sub_rungs:
            emit_rung_conditions(sub_rung)
            walk_execution(sub_rung)
        active_calls.discard(name)

    def walk_instruction(item: Any, node_index: int) -> None:
        instruction_event = _extract_instruction_event(item, node_index, tag_refs, fault_writes)
        if (
            instruction_event.condition_reads
            or instruction_event.data_reads
            or instruction_event.writes
        ):
            events.append(instruction_event)

        if isinstance(item, ForLoopInstruction):
            for child in item.body:
                if isinstance(child, RungClass):
                    # A body rung reads its contacts when it runs, in body order.
                    local_reads = _rung_condition_reads(child, tag_refs, local_only=True)
                    if local_reads:
                        events.append(
                            _AccessEvent(node_index=node_index, condition_reads=local_reads)
                        )
                    for body_instr in child._instructions:
                        walk_instruction(body_instr, node_index)
                else:
                    walk_instruction(child, node_index)

        if isinstance(item, CallInstruction):
            inline_subroutine(item.subroutine_name)

    def walk_execution(rung: Rung) -> None:
        node_index = node_index_by_rung[id(rung)]

        def emit_branch_conditions(branch: Rung) -> None:
            local_condition_reads = _rung_condition_reads(branch, tag_refs, local_only=True)
            if local_condition_reads:
                events.append(
                    _AccessEvent(
                        node_index=node_index_by_rung[id(branch)],
                        condition_reads=local_condition_reads,
                    )
                )

        for item in rung._execution_items:
            if isinstance(item, RungClass):
                # A branch reads its own contacts when the solve reaches it,
                # after the instructions above it have run.
                emit_branch_conditions(item)
                walk_execution(item)
                continue
            walk_instruction(item, node_index)

    for rung in program.rungs:
        emit_rung_conditions(rung)
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

    # One pass over the events, touching only the tags each one actually names.
    # Sweeping every tag across every event instead asks "does this event mention
    # me?" for each pair — quadratic, and almost always a miss, since a tag appears
    # in a handful of events out of thousands.  The event already knows its tags.
    versions_by_tag: dict[str, list[dict[str, Any]]] = {
        tag_name: [{"defined_at": None, "read_by": set()}] for tag_name in all_tags
    }

    for event in access_events:
        # Reads land on the version in force *before* this event's write, so a
        # rung that reads and writes the same tag reads the old value — the scan's
        # own semantics, and the reason reads are applied first.
        for tag_name in event.condition_reads:
            versions_by_tag[tag_name][-1]["read_by"].add(event.node_index)
        for tag_name in event.data_reads:
            versions_by_tag[tag_name][-1]["read_by"].add(event.node_index)
        # A write the instruction may not make carries the version before it, so
        # that version counts as read there.
        for tag_name in event.may_writes:
            versions_by_tag[tag_name][-1]["read_by"].add(event.node_index)
        for tag_name in event.writes:
            versions_by_tag[tag_name].append({"defined_at": event.node_index, "read_by": set()})

    return {
        tag_name: tuple(
            TagVersion(
                tag=tag_name,
                defined_at=version["defined_at"],
                read_by=frozenset(version["read_by"]),
            )
            for version in versions_by_tag[tag_name]
        )
        for tag_name in all_tags
    }


def continuous_search_results(program: Program) -> frozenset[str]:
    """Return the result tag of each continuous search.  The search reads it to resume.

    The def-use chains leave that read out, so a reader that trusts
    ``ProgramGraph.unconditional_write_before_read`` excludes these tags itself.
    """
    return frozenset(
        instr.result.name
        for instr in walk_instructions(program)
        if isinstance(instr, SearchInstruction) and instr.continuous
    )


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
    # One pass over the nodes, same reason as _build_def_use_chains: sweeping every
    # tag across every node asks "does this node read me?" per pair and answers no
    # almost every time.  Tags with no condition reads simply get no entry — every
    # read below goes through .get(..., frozenset()).
    _condition_readers: dict[str, set[int]] = {}
    for node_index, node in enumerate(graph.rung_nodes):
        for tag_name in node.condition_reads:
            _condition_readers.setdefault(tag_name, set()).add(node_index)
    condition_readers_of: dict[str, frozenset[int]] = {
        tag_name: frozenset(indices) for tag_name, indices in _condition_readers.items()
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
    node_index_by_rung: dict[int, int],
) -> None:
    """Propagate return_early guard condition_reads to the nodes it can skip.

    When a subroutine rung fires return_early(), the rest of the subroutine
    is skipped: the later rungs, and the items of the same rung that follow
    the return in execution order, branches included (a return in a branch
    leaves the subroutine, not just the branch).  Their writes, and the
    subroutines they call, therefore depend on the guard's condition_reads,
    and an ``out()`` among them holds its value instead of following its
    contacts — make that visible in the graph (``guard_reads``) so
    upstream_slice discovers the dependency.  The node's own reads stay in
    ``own_condition_reads``.
    """
    for sub_name in sorted(program.subroutines):
        guard_reads: frozenset[str] = frozenset()
        for rung in program.subroutines[sub_name]:
            return_reads = _rung_return_condition_reads(rung, tag_refs)
            same_rung = (
                _nodes_after_first_return(rung, node_index_by_rung)
                if return_reads is not None
                else frozenset()
            )
            for node_idx in _rung_tree_node_indices(rung, node_index_by_rung):
                node = nodes[node_idx]
                if not (node.all_writes or node.calls):
                    continue
                reads = guard_reads
                if node_idx in same_rung:
                    assert return_reads is not None
                    reads = reads | return_reads
                if reads:
                    nodes[node_idx] = _dc_replace(
                        node,
                        condition_reads=node.condition_reads | reads,
                        guard_reads=node.guard_reads | reads,
                    )
            if return_reads is not None:
                guard_reads = guard_reads | return_reads


def _rung_tree_node_indices(rung: Rung, node_index_by_rung: dict[int, int]) -> list[int]:
    """Return the PDG node indices of a top-level rung and its branches, at any depth."""
    out = [node_index_by_rung[id(rung)]]
    for branch in rung._branches:
        out.extend(_rung_tree_node_indices(branch, node_index_by_rung))
    return out


def _nodes_after_first_return(rung: Rung, node_index_by_rung: dict[int, int]) -> frozenset[int]:
    """Return the nodes of *rung*'s tree with an item that a return earlier in the rung can skip.

    The rung's items run in source order.  A branch runs in place, and its contacts
    were read up front, with the rung's.  When a return fires, it skips everything after
    the first item that holds a return.  A loop that holds a return can also skip the
    later iterations of its own body, so the loop counts as after it.
    """
    from pyrung.core.rung import Rung as RungClass

    order: list[tuple[int, Any]] = []

    def walk(current: Rung) -> None:
        for item in current._execution_items:
            if isinstance(item, RungClass):
                walk(item)
            else:
                order.append((node_index_by_rung[id(current)], item))

    walk(rung)
    skipped: set[int] = set()
    seen_return = False
    for node_idx, item in order:
        holds = _items_hold_return((item,))
        if seen_return or (holds and isinstance(item, ForLoopInstruction)):
            skipped.add(node_idx)
        seen_return = seen_return or holds
    return frozenset(skipped)


def _items_hold_return(items: Any) -> bool:
    """Return whether a return sits among *items*, in a rung among them or in a loop body."""
    from pyrung.core.instruction.control import ReturnInstruction
    from pyrung.core.rung import Rung as RungClass

    for item in items:
        if isinstance(item, ReturnInstruction):
            return True
        if isinstance(item, RungClass):
            if _items_hold_return(item._execution_items):
                return True
        elif isinstance(item, ForLoopInstruction) and _items_hold_return(item.body):
            return True
    return False


def _rung_has_return(rung: Rung) -> bool:
    """Return whether ``return_early()`` sits anywhere in the rung: its own items,
    its branches (at any depth), or a loop body in either."""
    return _rung_return_condition_reads(rung, {}) is not None


def _rung_return_condition_reads(rung: Rung, tag_refs: dict[str, Tag]) -> frozenset[str] | None:
    """Return the reads that decide whether a ``return_early()`` in this rung fires.

    Return ``None`` when the rung holds no return.  A return can sit in the rung's
    own items, in a branch at any depth, or in a loop body (a bare item or a
    body rung) in either.  The reads are the conditions on the path to each
    one.  A branch's conditions hold its parents' first, so its full reads
    are its path.  A loop holding a return adds its count (a count of 0 runs
    no iteration) and every body rung's contacts (a ``break_loop()`` or
    ``continue_loop()`` in one can skip the return).
    """
    from pyrung.core.instruction.control import ReturnInstruction
    from pyrung.core.rung import Rung as RungClass

    reads: set[str] = set()

    def visit_items(items: Any) -> bool:
        found = False
        for item in items:
            if isinstance(item, ReturnInstruction):
                found = True
            elif isinstance(item, ForLoopInstruction):
                found = visit_loop(item) or found
        return found

    def visit_loop(loop: ForLoopInstruction) -> bool:
        found = False
        for item in loop.body:
            if isinstance(item, RungClass):
                found = visit_rung(item) or found
            else:
                found = visit_items((item,)) or found
        if found:
            for body_rung in loop.body_rungs:
                reads.update(_rung_condition_reads(body_rung, tag_refs))
            reads.update(_extract_tag_names(loop.count, tag_refs))
        return found

    def visit_rung(current: Rung) -> bool:
        found = visit_items(current._instructions)
        for branch in current._branches:
            found = visit_rung(branch) or found
        if found:
            reads.update(_rung_condition_reads(current, tag_refs))
        return found

    if not visit_rung(rung):
        return None
    return frozenset(reads)


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


def collect_program_tags(program: Program) -> tuple[Tag, ...]:
    """Collect program tag identities without constructing a dependency graph.

    The collector shares the graph builder's rung extraction so conditions,
    expressions, ranges, nested instructions, branches, subroutines, and
    implicit status tags retain the same identity semantics.  When a graph is
    already cached, its tag collection is reused directly.
    """
    cached_graph = getattr(program, "_cached_graph", None)
    if cached_graph is not None:
        return tuple(cached_graph.tags.values())

    tag_refs: dict[str, Tag] = {}

    def walk_rung(
        rung: Rung,
        *,
        scope: GraphScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        _extract_rung_node(
            rung,
            rung_index=rung_index,
            scope=scope,
            subroutine=subroutine,
            branch_path=branch_path,
            tag_refs=tag_refs,
        )
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

    return tuple(tag_refs[name] for name in sorted(tag_refs))


def build_program_graph(program: Program) -> ProgramGraph:
    """Build the static PDG summary for a Program, cached on the program.

    An instruction's implicit fault writes (``_fault_writes``) count as writes
    for the fault flags some rung reads.  A read through an unbounded index
    (``DS[Ptr]`` where ``Ptr`` has neither ``choices=`` nor both ``min=`` and
    ``max=``) adds no element of ``DS`` to the reads.
    """
    cached_graph = getattr(program, "_cached_graph", None)
    if cached_graph is None:
        cached_graph = _build_program_graph(program)
        program._cached_graph = cached_graph
    return cached_graph


def _build_program_graph(program: Program) -> ProgramGraph:
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

    # Indirect-destination writer attribution (the region crossing): resolve the
    # slots an over-cap ``copy(src, block[ptr])`` can statically reach and fold
    # them into the writer node's ``writes`` BEFORE writers_of is built, so a
    # program-authored status band stops masquerading as never-written free words.
    writer_instrs = _build_writer_instrs(program)
    indirect_writes, indirect_attribution, unlisted_writes = _collect_indirect_writes(
        program, rung_nodes, tag_refs, writer_instrs
    )
    for node_index, slots in indirect_attribution.items():
        node = rung_nodes[node_index]
        rung_nodes[node_index] = _dc_replace(node, writes=node.writes | slots)

    readers_of_mut: dict[str, set[int]] = defaultdict(set)
    all_readers_of_mut: dict[str, set[int]] = defaultdict(set)
    writers_of_mut: dict[str, set[int]] = defaultdict(set)

    implicit: set[str] = set()
    for node_index, node in enumerate(rung_nodes):
        for tag_name in node.condition_reads | node.data_reads:
            readers_of_mut[tag_name].add(node_index)
        for tag_name in node.condition_reads | node.data_reads | node.exclusive_reads:
            all_readers_of_mut[tag_name].add(node_index)
        for tag_name in node.writes:
            writers_of_mut[tag_name].add(node_index)
        implicit.update(node.implicit_writes)
    fault_writes = frozenset(name for name in implicit if name in all_readers_of_mut)
    if fault_writes:
        for node_index, node in enumerate(rung_nodes):
            for tag_name in node.implicit_writes & fault_writes:
                writers_of_mut[tag_name].add(node_index)
    access_events = _build_access_sequence(program, node_index_by_rung, tag_refs, fault_writes)

    _augment_return_early_guards(rung_nodes, program, tag_refs, node_index_by_rung)
    frozen_nodes = tuple(rung_nodes)
    pointer_tags, pointer_uses, opaque_index_blocks, index_tags = _collect_pointer_uses(program)

    graph = ProgramGraph(
        rung_nodes=frozen_nodes,
        tag_roles={},
        def_use_chains=_build_def_use_chains(access_events),
        readers_of={name: frozenset(indices) for name, indices in readers_of_mut.items()},
        all_readers_of={name: frozenset(indices) for name, indices in all_readers_of_mut.items()},
        writers_of={name: frozenset(indices) for name, indices in writers_of_mut.items()},
        tags=dict(sorted(tag_refs.items())),
        block_ranges=range_acc,
        pointer_tags=pointer_tags,
        indirect_writes=indirect_writes,
        pointer_uses=pointer_uses,
        opaque_index_blocks=opaque_index_blocks,
        index_tags=index_tags,
        fault_writes=fault_writes,
        unread_fault_writes=frozenset(implicit) - fault_writes,
    )
    if unlisted_writes:
        graph.unlisted_writes = _UnlistedReach(program, graph).narrow(unlisted_writes)
    graph.tag_roles = classify_tags(graph)
    return graph


def _visit_indirect_refs(
    obj: Any,
    visit: Callable[[Any], None],
    seen: set[int],
    visit_range: Callable[[IndirectBlockRange], None] | None = None,
) -> None:
    """Call *visit* on every IndirectRef/IndirectExprRef reachable from *obj*.

    *visit_range*, when given, is also called on every ``IndirectBlockRange``.
    """
    if obj is None or isinstance(
        obj,
        (bool, int, float, str, bytes, FunctionType, BuiltinFunctionType, MethodType, Tag, Block),
    ):
        return  # a tag or block holds no index, only every other tag of its block
    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)
    if isinstance(obj, (IndirectRef, IndirectExprRef)):
        visit(obj)
        return
    if visit_range is not None and isinstance(obj, IndirectBlockRange):
        visit_range(obj)
    if isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            _visit_indirect_refs(item, visit, seen, visit_range)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _visit_indirect_refs(v, visit, seen, visit_range)
        return
    if isinstance(obj, Instruction):
        fields = _instruction_fields(obj)
        if fields is not None:
            for field_name in fields:
                _visit_indirect_refs(getattr(obj, field_name, None), visit, seen, visit_range)
            return
    if isinstance(obj, Condition):
        for _child_name, child_value in _condition_children(obj):
            _visit_indirect_refs(child_value, visit, seen, visit_range)
        return
    if hasattr(obj, "__dict__"):
        for v in vars(obj).values():
            _visit_indirect_refs(v, visit, seen, visit_range)


def _collect_pointer_uses(
    program: Program,
) -> tuple[dict[str, tuple[str, int, int]], tuple[PointerUse, ...], frozenset[str], frozenset[str]]:
    """Find the indexed accesses in a program.

    Returns ``pointer_tags`` (each pointer's first block span), every affine
    ``PointerUse``, the blocks indexed by a non-affine expression, and every tag
    an index reads.
    """
    from pyrung.core.validation._common import walk_instructions

    pointers: dict[str, tuple[str, int, int]] = {}
    uses: dict[PointerUse, None] = {}
    opaque: set[str] = set()
    index_tags: set[str] = set()
    scratch_refs: dict[str, Tag] = {}

    def _visit(ref: Any) -> None:
        block = ref.block
        if isinstance(ref, IndirectRef):
            index_tags.update(_extract_tag_names(ref.pointer, scratch_refs))
            affine: tuple[Tag, int, int] | None = (ref.pointer, 1, 0)
        else:
            index_tags.update(_extract_tag_names(ref.expr, scratch_refs))
            affine = _indirect_affine(ref.expr)
        if affine is None:
            opaque.add(block.name)
            return
        pointer, scale, offset = affine
        pointers.setdefault(pointer.name, (block.name, block.start, block.end))
        uses.setdefault(PointerUse(pointer.name, block.name, block.start, block.end, scale, offset))

    def _visit_range(ref: IndirectBlockRange) -> None:
        index_tags.update(_extract_tag_names(ref.start_expr, scratch_refs))
        index_tags.update(_extract_tag_names(ref.end_expr, scratch_refs))

    def _scan_rung_conditions(rung: Rung) -> None:
        for cond in rung._conditions:
            _visit_indirect_refs(cond, _visit, seen, _visit_range)
        for branch in rung._branches:
            _scan_rung_conditions(branch)

    seen: set[int] = set()
    for instr in walk_instructions(program):
        _visit_indirect_refs(instr, _visit, seen, _visit_range)
    for rung in program.rungs:
        _scan_rung_conditions(rung)
    for subroutine_rungs in program.subroutines.values():
        for rung in subroutine_rungs:
            _scan_rung_conditions(rung)

    return pointers, tuple(uses), frozenset(opaque), frozenset(index_tags)


def _named_write_dests(instr: Any) -> list[str]:
    """Names of exact single-``Tag`` destinations *instr* writes.

    The affine-pointer hop and the root's literal-write domain
    (:mod:`pyrung.core.analysis.crossings.indirect_dest`) both follow these named,
    non-indirect writes.  Indirect, range, and expression destinations are
    skipped because they resolve to no single root register.
    """
    out: list[str] = []
    for dest in instruction_write_targets(instr):
        if isinstance(dest, ImmediateRef):
            dest = dest.value
        if isinstance(dest, Tag):
            out.append(dest.name)
    return out


def _build_writer_instrs(program: Program) -> dict[str, list[Any]]:
    """Map each named-``Tag`` destination to the instructions that write it.

    The indirect-destination region crossing reads this map to hop an affine pointer
    (``math(root ± k)``) back to its root and read that root's literal-write
    domain.  Whole-program (main + branches + subroutines + ForLoop bodies)."""
    from pyrung.core.validation._common import walk_instructions

    writer_instrs: dict[str, list[Any]] = {}
    for instr in walk_instructions(program):
        for name in _named_write_dests(instr):
            writer_instrs.setdefault(name, []).append(instr)
    return writer_instrs


def _collect_indirect_writes(
    program: Program,
    rung_nodes: list[RungNode],
    tag_refs: dict[str, Tag],
    writer_instrs: dict[str, list[Any]],
) -> tuple[tuple[IndirectWriteRef, ...], dict[int, frozenset[str]], list[_UnlistedWrite]]:
    """Collect descriptors for over-cap indirect writes, plus static attribution.

    Returns ``(descriptors, attribution, unlisted)`` where *descriptors* preserve the
    runtime-resolution metadata (unchanged — ``cause()`` reads it) and
    *attribution* maps a writer node index to the concrete destination slots the
    indirect write can *statically* reach, via the indirect-destination region
    crossing (:func:`crossings.indirect_dest.writable_slots`).  Attribution fires
    only where the ordinary write-target extraction dropped the write (over-cap
    block, pointer with no declared domain) — precisely the masquerade gap: an
    indirectly-written status band otherwise looks never-program-written.  The
    region is bounded by the pointer's derivable domain (a sound over-approximation
    — a superset of write targets only ever *removes* a tag from the operator-lever
    set); a non-derivable pointer contributes no attribution (today's behavior).

    *unlisted* lists the writes into a block of more than ``_INDIRECT_BLOCK_CAP``
    elements that the facts' writer lists cannot follow
    (``ProgramGraph.unlisted_writers_of``).  Those lists name no element of such a
    block, unless the pointer's declared bounds give 1000 addresses or fewer.  So
    *unlisted* holds every write a descriptor holds, attributed or not, and also a window
    (``block.select(Ptr, Ptr + 1)``) and an index expression (``block[Ptr + 1]``).
    ``_UnlistedReach`` narrows each to the addresses its pointer can hold, once the
    graph is built.
    """
    from pyrung.core.analysis.crossings.indirect_dest import writable_slots

    refs: list[IndirectWriteRef] = []
    attribution: dict[int, set[str]] = {}
    unlisted: list[_UnlistedWrite] = []

    def _check_instruction(instr: Any, node_index: int) -> None:
        cls = type(instr)
        for dest in instruction_write_targets(instr):
            if isinstance(dest, ImmediateRef):
                dest = dest.value
            if not isinstance(dest, (IndirectRef, IndirectExprRef, IndirectBlockRange)):
                continue
            block = dest.block
            if block.end - block.start + 1 <= _INDIRECT_BLOCK_CAP:
                continue
            if not _listed_by_declared_bounds(dest):
                unlisted.append((node_index, instr, dest))
            if isinstance(dest, IndirectBlockRange):
                continue
            if isinstance(dest, IndirectRef):
                if _indirect_ref_tags(block, dest.pointer) is not None:
                    continue
                pointer_name = dest.pointer.name
            else:
                affine = _indirect_affine(dest.expr)
                if affine is None:
                    continue
                if _indirect_ref_tags(block, *affine) is not None:
                    continue
                pointer_name = affine[0].name
            source_tags: set[str] = set()
            for read_field in getattr(cls, "_reads", ()):
                source_tags.update(_extract_tag_names(getattr(instr, read_field), tag_refs))
            refs.append(
                IndirectWriteRef(
                    node_index=node_index,
                    pointer_tag=pointer_name,
                    source_tags=frozenset(source_tags),
                    block=block,
                )
            )
            slots = writable_slots(dest, block=block, writer_instrs=writer_instrs, tags=tag_refs)
            if slots:
                attribution.setdefault(node_index, set()).update(slots)

    for node_index, node in enumerate(rung_nodes):
        rung = resolve_rung(program, node)
        if rung is None:
            continue
        pending = list(rung._instructions)
        while pending:
            instr = pending.pop(0)
            _check_instruction(instr, node_index)
            if isinstance(instr, ForLoopInstruction):
                pending[:0] = instr.instructions

    return (
        tuple(refs),
        {ni: frozenset(slots) for ni, slots in attribution.items()},
        unlisted,
    )


#: A write that lists no targets, as its node, its instruction and its destination.
_UnlistedWrite = tuple[int, Any, Any]


class _UnlistedReach:
    """The addresses a write through a pointer can reach, from what the program stores in it.

    A pointer holds its power-on value and what its writers store.  A writer stores under
    its own rung's comparisons, so ``math(Cmd * 10 + 2999, Idx)`` under ``Cmd >= 1, Cmd <=
    100`` gives 3009..3999 (``value_sets.rung_guards``).  The write's own rung narrows the
    pointer too.  A pointer the outside or a ``receive()`` sets is not narrowed.  An input
    with declared ``choices`` or ``min``/``max`` holds those, unless the ladder writes it
    too.  A ladder write past a declared bound stores, as on a CLICK.  A tag written
    before every read of it in the scan (``unconditional_write_before_read``) never shows
    its power-on value.  An address outside the block writes nothing, as the runtime
    drops it.
    """

    def __init__(self, program: Program, graph: ProgramGraph) -> None:
        self._program = program
        self._graph = graph
        self._sets: Any = None
        self._outside: frozenset[str] = frozenset()

    def narrow(
        self, writes: list[_UnlistedWrite]
    ) -> dict[Block, tuple[tuple[int, frozenset[int] | None], ...]]:
        """Return *writes* grouped by block, each with the addresses it can reach (None means all).

        A write that reaches no address stays, with an empty reach.  A patch or a force on
        its pointer can still aim it into the block.
        """
        by_block: dict[Block, dict[int, set[int] | None]] = {}
        for node, instr, dest in writes:
            addresses = self.addresses(instr, dest)
            by_node = by_block.setdefault(dest.block, {})
            known = by_node.get(node, set())
            by_node[node] = None if addresses is None or known is None else known | addresses
        return {
            block: tuple(
                (node, None if addrs is None else frozenset(addrs))
                for node, addrs in sorted(by_node.items())
            )
            for block, by_node in by_block.items()
        }

    def _build(self) -> Any:
        from pyrung.core.instruction.send_receive._core import ModbusReceiveInstruction
        from pyrung.core.validation._common import walk_instructions

        from .value_sets import _ValueSets, rung_guards

        instructions = list(walk_instructions(self._program))
        received: set[str] = set()
        for instr in instructions:
            if isinstance(instr, ModbusReceiveInstruction):
                dest = instr.dest
                if isinstance(dest, Tag):
                    received.add(dest.name)
                elif hasattr(dest, "tags"):
                    received.update(tag.name for tag in dest.tags())
        graph = self._graph
        self._outside = frozenset(
            received | {name for name, tag in graph.tags.items() if tag_written_from_outside(tag)}
        )
        return _ValueSets(
            instructions,
            graph.tags,
            frozenset(received),
            None,
            declared_inputs=True,
            written_first=frozenset(
                name for name in graph.writers_of if graph.unconditional_write_before_read(name)
            ),
            guards=rung_guards(self._program),
            declared_if_unwritten=True,
            listed_reach={
                id(ref): _declared_reach(ref)
                for instr in instructions
                for ref in instruction_write_targets(instr)
                if _listed_by_declared_bounds(ref)
            },
        )

    def addresses(self, instr: Any, dest: Any) -> frozenset[int] | None:
        """Return the in-block addresses *instr* can write through *dest*, or None for all."""
        if self._sets is None:
            self._sets = self._build()
        if isinstance(dest, IndirectRef) and dest.pointer.name in self._outside:
            return None
        found = self._sets.write_addresses(instr, dest)
        if found is None:
            return None
        block = dest.block
        return frozenset(
            addr
            for addr in found
            if block.start <= addr <= block.end and block._is_sparse_address_valid(addr)
        )


def _declared_reach(dest: IndirectRef) -> frozenset[int]:
    """Return the addresses of ``block[Ptr]`` that the pointer's declared bounds allow.

    These are the elements ``_indirect_ref_tags`` lists.  A ladder write past the bounds
    would reach others (roadmap 1b).
    """
    block, pointer = dest.block, dest.pointer
    if pointer.choices is not None:
        found = {int(float(k)) for k in pointer.choices}
    else:
        lo, hi = pointer.min, pointer.max
        assert lo is not None and hi is not None  # _listed_by_declared_bounds holds
        found = set(range(int(lo), int(hi) + 1))
    return frozenset(
        addr
        for addr in found
        if block.start <= addr <= block.end and block._is_sparse_address_valid(addr)
    )


def _listed_by_declared_bounds(dest: Any) -> bool:
    """Return whether the facts' writer lists name what *dest* writes in a block past the cap.

    They follow a plain pointer's ``choices=``, or a ``min=``/``max=`` span of 1000
    addresses or fewer (``prove.classify._expand_indirect_tag_names``), and
    nothing else past the cap.
    """
    if not isinstance(dest, IndirectRef):
        return False
    pointer = dest.pointer
    if pointer.choices is not None:
        return True
    if pointer.min is None or pointer.max is None:
        return False
    return int(pointer.max) - int(pointer.min) + 1 <= _INDIRECT_BLOCK_CAP


__all__ = [
    "CallSite",
    "IndirectWriteRef",
    "ProgramGraph",
    "RungNode",
    "TagRole",
    "TagVersion",
    "build_program_graph",
    "classify_tags",
    "resolve_rung",
]

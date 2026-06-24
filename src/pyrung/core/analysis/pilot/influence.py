"""Layer 6: Don't Rediscover — opaque pipeline detection and influence mapping.

Detects ``copy(block[ptr], tag)`` patterns statically, identifies the
steerable inputs (free arguments) that feed the pipeline, and builds a
per-tag transition table from fork-probe observations.  BFS over the
table finds the shortest input sequence to reach a target value.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineSlice:
    """Learned function signature for an opaque pipeline.

    Just the free args — steerable inputs that enter the pipeline through
    a convergence point (e.g. command buttons writing ``C_CtrlCmd``).
    PILOT decides which tags to probe; the slice just says what to try.
    """

    free_args: frozenset[str]


class InfluenceMap:
    """Per-tag transition table built from fork-probe observations.

    Seeded at startup with statically-detected opaque pipelines so PILOT
    can go straight to systematic exploration without a first observation.
    """

    def __init__(self, slices: list[PipelineSlice] | None = None) -> None:
        self._slices: list[PipelineSlice] = list(slices or [])
        self._all_free_args: frozenset[str] = (
            frozenset().union(*(s.free_args for s in self._slices)) if self._slices else frozenset()
        )
        self._transitions: dict[str, dict[tuple[Any, str], Any]] = {}
        self._probed: dict[str, set[tuple[Any, str]]] = {}

    @property
    def free_args(self) -> frozenset[str]:
        return self._all_free_args

    def has_transitions(self, tag: str) -> bool:
        return tag in self._transitions

    def record(self, tag: str, input_tag: str, from_val: Any, to_val: Any) -> None:
        table = self._transitions.setdefault(tag, {})
        table[(from_val, input_tag)] = to_val
        self._probed.setdefault(tag, set()).add((from_val, input_tag))

    def record_no_change(self, tag: str, input_tag: str, from_val: Any) -> None:
        self._probed.setdefault(tag, set()).add((from_val, input_tag))

    def find_path(self, tag: str, from_val: Any, to_val: Any) -> list[str] | None:
        """BFS shortest input sequence through the transition table."""
        from pyrung.core.analysis.sp_values import _values_match

        table = self._transitions.get(tag)
        if not table:
            return None
        if _values_match(from_val, to_val):
            return []

        queue: deque[tuple[Any, list[str]]] = deque([(from_val, [])])
        visited: set[Any] = {from_val}

        while queue:
            state, path = queue.popleft()
            for (s, inp), dest in table.items():
                if not _values_match(s, state):
                    continue
                if dest in visited:
                    continue
                new_path = [*path, inp]
                if _values_match(dest, to_val):
                    return new_path
                visited.add(dest)
                queue.append((dest, new_path))

        return None

    def unprobed_inputs(self, tag: str, from_val: Any) -> list[str]:
        """Free args not yet tried from *from_val* for *tag*."""
        if not self._all_free_args:
            return []
        return sorted(self._all_free_args - self.probed_inputs(tag, from_val))

    def probed_inputs(self, tag: str, from_val: Any) -> set[str]:
        """Input tags already probed from *from_val* for *tag*."""
        return {inp for (fv, inp) in self._probed.get(tag, set()) if fv == from_val}

    def harmful_inputs(self, tag: str, from_val: Any, to_val: Any) -> set[str]:
        """Inputs known to move *tag* away from the BFS path toward *to_val*.

        Once we know the shortest path, any input from the current state
        that goes to a state NOT on that path (or with no path to the
        target) is harmful and should be excluded from candidates.
        """
        from pyrung.core.analysis.sp_values import _values_match

        path = self.find_path(tag, from_val, to_val)
        if not path:
            return set()
        good_input = path[0]
        table = self._transitions.get(tag, {})

        # Compute states on the BFS path
        on_path: set[Any] = {from_val}
        state = from_val
        for inp in path:
            dest = table.get((state, inp))
            if dest is not None:
                on_path.add(dest)
                state = dest

        harmful: set[str] = set()
        for (fv, inp), dest in table.items():
            if not _values_match(fv, from_val):
                continue
            if inp == good_input:
                continue
            if dest not in on_path:
                harmful.add(inp)
        return harmful


def _find_convergent_steers(
    opaque_tag: str,
    pdg: ProgramGraph,
    steerable: frozenset[str],
    *,
    max_hops: int = 8,
    min_writers: int = 2,
) -> frozenset[str]:
    """Bounded upstream BFS to find convergence-point steerable inputs.

    A convergence point is an intermediate tag written by multiple rungs
    where each writer is conditioned on a different steerable input
    (e.g. ``C_CtrlCmd`` written by 10 rungs, each gated by a different
    command button).  Returns the union of those steerable condition reads.

    Falls back to the full ``upstream_slice & steerable`` if no
    convergence point is found within *max_hops*.
    """
    visited_tags: set[str] = set()
    visited_rungs: set[int] = set()
    queue: list[tuple[str, int]] = [(opaque_tag, 0)]
    convergent: set[str] = set()

    while queue:
        tag, depth = queue.pop(0)
        if tag in visited_tags or depth > max_hops:
            continue
        visited_tags.add(tag)
        tag_steer_conds: set[str] = set()
        for ri in pdg.writers_of.get(tag, frozenset()):
            if ri in visited_rungs:
                continue
            visited_rungs.add(ri)
            node = pdg.rung_nodes[ri]
            tag_steer_conds |= node.condition_reads & steerable
            for rt in node.condition_reads | node.data_reads:
                if rt not in visited_tags:
                    queue.append((rt, depth + 1))
        if len(tag_steer_conds) >= min_writers:
            convergent |= tag_steer_conds

    if convergent:
        return frozenset(convergent)
    return pdg.upstream_slice(opaque_tag) & steerable


def _scan_indirect_copy_targets(program: Any) -> set[str]:
    """Destination tag names of ``copy(block[ptr], tag)`` indirect copies."""
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    targets: set[str] = set()

    def _scan(rungs: Any) -> None:
        for r in rungs:
            for instr in getattr(r, "_instructions", ()):
                if isinstance(instr, CopyInstruction) and isinstance(
                    instr.source, (IndirectRef, IndirectExprRef)
                ):
                    dest_name = getattr(instr.dest, "name", None)
                    if dest_name:
                        targets.add(dest_name)
            _scan(getattr(r, "_branches", ()))

    _scan(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan(sub_rungs)
    return targets


def detect_opaque_loop(
    pdg: ProgramGraph,
    program: Any,
    *,
    max_hops: int = 3,
) -> frozenset[str]:
    """Tags in a feedback loop through an opaque (indirect-copy) pipeline.

    These are the jump-table state-machine registers (``S_StateCurrent``,
    ``isStateEnbl_Yes``, ``S_StateRequested``, the ``S_<state>`` flags,
    ``C_CtrlCmd`` …) that mutually drive each other through the indirect-copy
    machinery.  ``trace_back`` must not invert them as a finite prerequisite
    chain — it walks the entire state-transition graph backward (e.g.
    ``StateCurrent=6 → enable → StateRequested=2 → Stopping → StateCurrent=7
    → …``), scrambling depth and inflating the unsatisfied count.  They are
    Layer 6 territory: learned by observation, not static inversion.

    A tag qualifies when it is BOTH within *max_hops* downstream of an
    indirect-copy target AND upstream of one — i.e. it participates in the
    loop.  Simple state machines built from direct copies have no
    indirect-copy targets, so this returns empty and ``trace_back`` is
    unaffected.
    """
    targets = _scan_indirect_copy_targets(program)
    if not targets:
        return frozenset()

    # Bounded downstream BFS: tag -> rungs reading it -> their written tags.
    seen: set[str] = set(targets)
    frontier: set[str] = set(targets)
    for _ in range(max_hops):
        nxt: set[str] = set()
        for tag in frontier:
            for ri in pdg.readers_of.get(tag, frozenset()):
                for w in pdg.rung_nodes[ri].all_writes:
                    if w not in seen:
                        seen.add(w)
                        nxt.add(w)
        frontier = nxt

    upstream: set[str] = set()
    for t in targets:
        upstream |= pdg.upstream_slice(t)

    return frozenset(seen & upstream)


def detect_opaque_pipelines(
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
) -> list[PipelineSlice]:
    """Find indirect-copy write targets and their steerable upstream inputs.

    Scans the program for ``CopyInstruction`` with ``IndirectRef`` or
    ``IndirectExprRef`` sources (the ``copy(block[ptr], tag)`` pattern).
    For each, follows downstream via the PDG to find affected output tags,
    and uses convergence-point detection to find the steerable inputs that
    actually enter the pipeline (not the full upstream cone).

    Deduplicates slices that share the same free args (e.g. multiple
    indirect copies in the same subroutine).
    """
    opaque_targets = _scan_indirect_copy_targets(program)
    if not opaque_targets:
        return []

    # Deduplicate: multiple opaque targets may share convergent steers.
    seen_args: set[frozenset[str]] = set()
    slices: list[PipelineSlice] = []
    for opaque_tag in sorted(opaque_targets):
        free_args = _find_convergent_steers(opaque_tag, pdg, steerable)
        if not free_args or free_args in seen_args:
            continue
        seen_args.add(free_args)
        slices.append(PipelineSlice(free_args=frozenset(free_args)))
        logger.info(
            "pilot: opaque pipeline (%s) -> %d free args: %s",
            opaque_tag,
            len(free_args),
            sorted(free_args),
        )

    return slices
